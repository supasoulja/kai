#!/usr/bin/env python3
"""
watchdog/agent.py — bidirectional Kai agent for remote machines.

Pairs this machine with Kai and then runs a polling loop that:
  1. Fetches pending commands from Kai every 15 seconds
  2. Executes them (read-only diagnostics only — cpu, ram, disk, temps, procs, network)
  3. Posts results back to Kai so cluster tools can surface them in chat

First run (pairing):
    python agent.py --server https://kai-host:7860 --join-code <code> --label "my-vm"

Already paired:
    python agent.py

Self-contained: stdlib + requests + (optional) psutil. No kai.* imports.
"""

import argparse
import json
import os
import platform
import socket
import subprocess
import sys
import time
from pathlib import Path

import requests

try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False

from common import DEFAULT_CONFIG_PATH, default_pc_label, load_config, save_config

POLL_INTERVAL = 15  # seconds between polls


# ── Diagnostic handlers (all read-only) ──────────────────────────────────────

def _run(cmd: list[str], timeout: int = 10) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace")
        return r.stdout.strip()
    except Exception as e:
        return f"[error: {e}]"


def _cmd_system_info(_args: dict) -> dict:
    info = {
        "hostname": socket.gethostname(),
        "os": platform.system(),
        "os_version": platform.version(),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "uptime_seconds": None,
        "ram_total_gb": None,
    }
    if _HAS_PSUTIL:
        info["uptime_seconds"] = int(time.time() - psutil.boot_time())
        mem = psutil.virtual_memory()
        info["ram_total_gb"] = round(mem.total / 1e9, 1)
    else:
        try:
            info["uptime_seconds"] = int(float(Path("/proc/uptime").read_text().split()[0]))
        except Exception:
            pass
    return info


def _cmd_cpu_load(_args: dict) -> dict:
    if _HAS_PSUTIL:
        load_avg = list(os.getloadavg()) if hasattr(os, "getloadavg") else None
        return {
            "percent_1s": psutil.cpu_percent(interval=1),
            "load_avg_1_5_15": load_avg,
            "cpu_count": psutil.cpu_count(),
        }
    load_avg = None
    try:
        load_avg = list(os.getloadavg())
    except Exception:
        pass
    raw = Path("/proc/loadavg").read_text() if Path("/proc/loadavg").exists() else ""
    return {"load_avg_1_5_15": load_avg, "proc_loadavg": raw.split()[:3] if raw else None}


def _cmd_ram_usage(_args: dict) -> dict:
    if _HAS_PSUTIL:
        m = psutil.virtual_memory()
        return {
            "total_gb": round(m.total / 1e9, 1),
            "used_gb": round(m.used / 1e9, 1),
            "available_gb": round(m.available / 1e9, 1),
            "percent": m.percent,
        }
    return {"raw": _run(["free", "-m"])}


def _cmd_disk_usage(_args: dict) -> dict:
    if _HAS_PSUTIL:
        partitions = []
        for p in psutil.disk_partitions():
            try:
                u = psutil.disk_usage(p.mountpoint)
                partitions.append({
                    "mount": p.mountpoint,
                    "total_gb": round(u.total / 1e9, 1),
                    "used_gb": round(u.used / 1e9, 1),
                    "free_gb": round(u.free / 1e9, 1),
                    "percent": u.percent,
                })
            except Exception:
                pass
        return {"partitions": partitions}
    return {"raw": _run(["df", "-h"])}


def _cmd_top_procs(_args: dict) -> dict:
    if _HAS_PSUTIL:
        procs = []
        for p in sorted(
            psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]),
            key=lambda x: x.info.get("cpu_percent") or 0,
            reverse=True,
        )[:15]:
            procs.append(p.info)
        return {"processes": procs}
    lines = _run(["ps", "aux", "--sort=-%cpu"]).splitlines()[:16]
    return {"raw": "\n".join(lines)}


def _cmd_network_info(_args: dict) -> dict:
    if _HAS_PSUTIL:
        addrs: dict = {}
        for iface, snics in psutil.net_if_addrs().items():
            addrs[iface] = [{"family": str(s.family), "address": s.address} for s in snics]
        return {"interfaces": addrs}
    return {"raw": _run(["ip", "addr"])}


def _cmd_temps(_args: dict) -> dict:
    if _HAS_PSUTIL and hasattr(psutil, "sensors_temperatures"):
        try:
            raw = psutil.sensors_temperatures()
            result: dict = {}
            for name, entries in raw.items():
                result[name] = [
                    {"label": e.label, "current": e.current, "high": e.high, "critical": e.critical}
                    for e in entries
                ]
            return {"sensors": result}
        except Exception as e:
            return {"error": str(e)}
    # /sys/class/thermal fallback (Linux)
    zones: dict = {}
    thermal_base = Path("/sys/class/thermal")
    if thermal_base.exists():
        for zone in sorted(thermal_base.iterdir()):
            if zone.name.startswith("thermal_zone"):
                try:
                    temp_c = int((zone / "temp").read_text()) / 1000
                    zone_type = (zone / "type").read_text().strip()
                    zones[zone.name] = {"type": zone_type, "temp_c": temp_c}
                except Exception:
                    pass
    return {"thermal_zones": zones} if zones else {"error": "no temperature data available"}


def _cmd_ping(args: dict) -> dict:
    host = args.get("host", "8.8.8.8")
    count = min(int(args.get("count", 4)), 10)
    cmd = (["ping", "-n", str(count), host] if platform.system() == "Windows"
           else ["ping", "-c", str(count), host])
    return {"host": host, "output": _run(cmd, timeout=20)}


_COMMANDS = {
    "system_info": _cmd_system_info,
    "cpu_load": _cmd_cpu_load,
    "ram_usage": _cmd_ram_usage,
    "disk_usage": _cmd_disk_usage,
    "top_procs": _cmd_top_procs,
    "network_info": _cmd_network_info,
    "temps": _cmd_temps,
    "ping": _cmd_ping,
}


# ── API helpers ───────────────────────────────────────────────────────────────

def _make_session() -> requests.Session:
    s = requests.Session()
    s.verify = False  # allow Kai's self-signed cert from --lan mode
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        pass
    return s


def _fetch_commands(config: dict, session: requests.Session) -> list[dict]:
    try:
        resp = session.get(
            f"{config['server_url']}/api/node/{config['device_id']}/commands",
            headers={"X-Device-Key": config["device_key"]},
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json().get("commands", [])
        print(f"[agent] poll returned {resp.status_code}")
    except Exception as e:
        print(f"[agent] poll error: {e}")
    return []


def _post_result(config: dict, session: requests.Session, command_id: str, result: dict, error: bool = False):
    try:
        session.post(
            f"{config['server_url']}/api/node/{config['device_id']}/result/{command_id}",
            headers={"X-Device-Key": config["device_key"]},
            json={"result": result, "error": error},
            timeout=10,
        )
    except Exception as e:
        print(f"[agent] result post error: {e}")


# ── Main loop ─────────────────────────────────────────────────────────────────

def run_agent(config: dict):
    session = _make_session()
    label = config.get("pc_label", config["device_id"])
    print(f"[agent] online — {socket.gethostname()} ({label})")
    print(f"[agent] polling {config['server_url']} every {POLL_INTERVAL}s")
    print(f"[agent] supported commands: {', '.join(_COMMANDS)}")

    while True:
        commands = _fetch_commands(config, session)
        for cmd in commands:
            command_id = cmd["id"]
            command_name = cmd["command"]
            args = cmd.get("args") or {}
            handler = _COMMANDS.get(command_name)
            if handler is None:
                _post_result(config, session, command_id,
                             {"error": f"unknown command: {command_name}"}, error=True)
                continue
            print(f"[agent] → {command_name}")
            try:
                result = handler(args)
                _post_result(config, session, command_id, result)
            except Exception as e:
                _post_result(config, session, command_id, {"error": str(e)}, error=True)
        time.sleep(POLL_INTERVAL)


def main():
    parser = argparse.ArgumentParser(description="Kai bidirectional agent — pairs this machine and runs a command loop")
    parser.add_argument("--server", default=None,
                        help="Kai base URL, e.g. https://192.168.1.10:7860 (only needed to pair)")
    parser.add_argument("--join-code", default=None, dest="join_code",
                        help="single-use code minted by a logged-in Kai session")
    parser.add_argument("--label", default=None,
                        help="friendly name for this machine (default: hostname)")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH,
                        help=f"path to paired-identity config (default: {DEFAULT_CONFIG_PATH})")
    args = parser.parse_args()

    config = load_config(args.config)

    if args.join_code:
        if not args.server:
            print("[agent] --server is required when pairing with --join-code")
            sys.exit(1)
        label = args.label or default_pc_label()
        server_url = args.server.rstrip("/")
        session = _make_session()
        resp = session.post(
            f"{server_url}/api/watchdog/register",
            json={"join_code": args.join_code, "label": label},
            timeout=15,
        )
        if resp.status_code != 200:
            print(f"[agent] pairing failed ({resp.status_code}): {resp.text}")
            sys.exit(1)
        data = resp.json()
        config = {
            "server_url": server_url,
            "device_id": data["device_id"],
            "device_key": data["device_key"],
            "pc_label": label,
        }
        save_config(config, args.config)
        print(f"[agent] paired as '{label}' (device_id={data['device_id']})")

    if config is None:
        print("[agent] not paired yet — run with --server <url> --join-code <code>")
        sys.exit(1)

    run_agent(config)


if __name__ == "__main__":
    main()
