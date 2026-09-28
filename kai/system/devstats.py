"""
Structured system-stat collectors for Developer Mode (dashboard Phase B).

The chat diagnostic tools (system.temps, pc.network_info, files.disk_usage)
return human-readable text and shell out — fine for a conversation, too slow to
poll. These return plain dicts/numbers straight from psutil so the dashboard can
render gauges and refresh on a live loop. Each collector is defensive: a missing
sensor or unreadable mount yields None / is skipped, never an exception.

The /api/dev/stats route can expose these for live polling; the collectors
themselves carry no auth or web concerns.
"""

from __future__ import annotations

import glob
import re
import shutil
import socket
import subprocess
from pathlib import Path

import psutil


def collect_disk() -> list[dict]:
    """Per-partition usage:
    [{mount, fstype, total_gb, used_gb, free_gb, percent}], largest first."""
    out: list[dict] = []
    for part in psutil.disk_partitions(all=False):
        try:
            u = psutil.disk_usage(part.mountpoint)
        except (PermissionError, OSError):
            continue  # e.g. an empty optical drive or a permission-locked mount
        out.append(
            {
                "mount": part.mountpoint,
                "fstype": part.fstype,
                "total_gb": round(u.total / 1e9, 1),
                "used_gb": round(u.used / 1e9, 1),
                "free_gb": round(u.free / 1e9, 1),
                "percent": u.percent,
            }
        )
    out.sort(key=lambda d: d["total_gb"], reverse=True)
    return out


def _gpu_name(device_dir: Path) -> str | None:
    """Marketing name from lspci for a DRM device: the bracketed part of a
    "Chip [Card Name]" device string. None when lspci is missing or says nothing."""
    try:
        uevent = (device_dir / "uevent").read_text()
    except OSError:
        return None
    slot = next(
        (ln.split("=", 1)[1] for ln in uevent.splitlines() if ln.startswith("PCI_SLOT_NAME=")),
        None,
    )
    if not slot or not shutil.which("lspci"):
        return None
    try:
        out = subprocess.run(
            ["lspci", "-vmm", "-s", slot], capture_output=True, text=True, timeout=3
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    device = next(
        (ln.split(":", 1)[1].strip() for ln in out.splitlines() if ln.startswith("Device:")), ""
    )
    bracketed = re.findall(r"\[([^\]]+)\]", device)
    return (bracketed[-1] if bracketed else device) or None


def collect_gpus() -> list[dict]:
    """GPUs with readable VRAM, in card order: [{index, name, total_bytes, used_bytes}].
    AMD sysfs first, nvidia-smi if there is none; [] on a machine with neither."""
    out: list[dict] = []
    cards = glob.glob("/sys/class/drm/card*/device/mem_info_vram_total")
    for path in sorted(cards, key=lambda p: int(re.search(r"card(\d+)", p).group(1))):
        device_dir = Path(path).parent
        try:
            total = int((device_dir / "mem_info_vram_total").read_text())
            used = int((device_dir / "mem_info_vram_used").read_text())
        except (OSError, ValueError):
            continue
        out.append({"name": _gpu_name(device_dir), "total_bytes": total, "used_bytes": used})
    if not out and shutil.which("nvidia-smi"):
        try:
            res = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=name,memory.total,memory.used",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            for line in res.stdout.strip().splitlines():
                name, total, used = (s.strip() for s in line.rsplit(",", 2))
                out.append(
                    {"name": name, "total_bytes": int(total) << 20, "used_bytes": int(used) << 20}
                )
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    for i, gpu in enumerate(out, 1):
        gpu["index"] = i
    return out


def collect_ram() -> dict:
    """{total_bytes, used_bytes}, where used is what isn't available to new programs."""
    vm = psutil.virtual_memory()
    return {"total_bytes": vm.total, "used_bytes": vm.total - vm.available}


def collect_network() -> list[dict]:
    """Per-interface IPv4 + link state:
    [{name, up, speed_mbps, addresses: [ip, ...]}]. Loopback excluded."""
    stats = psutil.net_if_stats()
    out: list[dict] = []
    for name, addr_list in psutil.net_if_addrs().items():
        if name == "lo" or name.startswith("lo"):
            continue
        ips = [a.address for a in addr_list if a.family == socket.AF_INET]
        st = stats.get(name)
        out.append(
            {
                "name": name,
                "up": bool(st.isup) if st else False,
                "speed_mbps": st.speed if st else 0,
                "addresses": ips,
            }
        )
    return out


def collect_temps() -> dict:
    """CPU load/clock/temp + best-effort GPU temp. Missing values are None:
    {cpu: {load_pct, clock_mhz, temp_c}, gpu: {temp_c}}."""
    cpu = {"load_pct": None, "clock_mhz": None, "temp_c": _cpu_temp()}
    try:
        cpu["load_pct"] = psutil.cpu_percent(interval=0.1)
    except Exception:
        pass
    try:
        freq = psutil.cpu_freq()
        cpu["clock_mhz"] = round(freq.current) if freq and freq.current else None
    except Exception:
        pass
    return {"cpu": cpu, "gpu": {"temp_c": _gpu_temp()}}


def _cpu_temp() -> float | None:
    """Highest plausible CPU-package temperature from psutil (Linux hwmon).
    psutil has no sensors_temperatures on Windows — returns None there."""
    try:
        temps = psutil.sensors_temperatures()  # not present on Windows
    except Exception:
        return None
    for name, entries in (temps or {}).items():
        for e in entries:
            label = (e.label or name).lower()
            if any(
                k in label
                for k in ("core", "tctl", "tdie", "package", "cpu", "k10temp", "coretemp")
            ):
                if e.current and e.current > 10:
                    return round(e.current, 1)
    return None


def _gpu_temp() -> float | None:
    """GPU temp via nvidia-smi — the one cheap structured numeric source. Returns
    None when nvidia-smi is absent (AMD/Intel/headless) or errors."""
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if r.returncode == 0 and r.stdout.strip():
            return float(r.stdout.strip().splitlines()[0])
    except Exception:
        pass
    return None
