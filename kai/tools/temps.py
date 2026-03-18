"""
system.temps — CPU/GPU temperatures, load, clock speeds, fan, power.

Sources (no kernel driver required — WinRing0/hwmon.exe removed):
  - GPU: nvidia-smi (NVIDIA only; AMD/Intel fall back to WMI with no temps)
  - CPU temp: WMI MSAcpi_ThermalZoneTemperature (best-effort, hardware-dependent)
  - CPU load/clock: Win32_Processor via WMI
"""
import json
import subprocess
import threading
import time

from kai.tools.registry import registry

_cache_lock      = threading.Lock()
_cache_result:   str | None = None
_cache_time:     float = 0.0
_cache_ttl       = 60.0   # seconds before refresh
_refresh_running = False


def _ps(cmd: str, timeout: int = 10) -> str:
    """Run PowerShell, return stdout or empty string."""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
        return r.stdout.strip()
    except Exception:
        return ""


def _maybe_refresh() -> None:
    global _refresh_running
    with _cache_lock:
        now = time.monotonic()
        if _refresh_running or (now - _cache_time) < _cache_ttl:
            return
        _refresh_running = True

    def _run():
        global _cache_result, _cache_time, _refresh_running
        result = _gather_temps()
        with _cache_lock:
            _cache_result = result
            _cache_time = time.monotonic()
            _refresh_running = False

    threading.Thread(target=_run, daemon=True).start()


@registry.tool(
    name="system.temps",
    description=(
        "Get CPU and GPU temperatures, load, clock speeds, fan speed, and power draw. "
        "After retrieving: flag anything concerning. "
        "CPU: above 80°C under load is warm, above 90°C is hot and needs attention. "
        "GPU core (NVIDIA): above 85°C is warm, above 95°C is hot. "
        "GPU junction (AMD): above 90°C is warm, above 105°C is hot. "
        "If temps look normal for the current load, say so briefly. "
        "If fan speed is 0% with high temps, flag it — that's a problem. "
        "If temperature reads n/a: the hardware sensor wasn't accessible — "
        "report the load and clock data that IS available, then suggest HWiNFO64 "
        "as a free tool for detailed sensor readings. Do NOT say the tool failed."
    ),
)
def get_temps() -> str:
    global _cache_result, _cache_time
    _maybe_refresh()
    with _cache_lock:
        cached = _cache_result
    if cached:
        return cached
    # First call — gather synchronously (no cache yet)
    result = _gather_temps()
    with _cache_lock:
        _cache_result = result
        _cache_time = time.monotonic()
    return result


def _gather_temps() -> str:
    lines = []

    cpu = _cpu_info()
    if cpu:
        lines.append(cpu)

    # Detect vendor first, then use the right tool — no blind trial-and-error
    vendor = _gpu_vendor()
    if vendor == "amd":
        # pyadl: ADL (AMD Display Library) — same SDK Adrenalin uses, no admin needed
        # amd-smi: ships with ROCm/enterprise cards, rare on consumer Windows
        gpu = _pyadl() or _amd_smi() or _rocm_smi() or _gpu_wmi_fallback()
    elif vendor == "nvidia":
        gpu = _nvidia_smi() or _gpu_wmi_fallback()
    else:
        gpu = _nvidia_smi() or _pyadl() or _amd_smi() or _rocm_smi() or _gpu_wmi_fallback()
    if gpu:
        lines.append(gpu)

    if not lines:
        return "Hardware info unavailable — WMI did not return CPU or GPU data."

    # Check if all temp fields are n/a and add a helper note for the model
    combined = "\n".join(lines)
    if "Temp: n/a" in combined and "°C" not in combined:
        combined += (
            "\n\nNote: Temperature sensors are unavailable on this hardware via WMI. "
            "The load, clock, and VRAM data above IS valid — report those. "
            "Suggest HWiNFO64 (free) for detailed temperature monitoring."
        )
    return combined


# ── CPU ────────────────────────────────────────────────────────────────────────

def _cpu_info() -> str | None:
    try:
        ps = (
            "Get-WmiObject Win32_Processor | "
            "Select-Object Name,LoadPercentage,CurrentClockSpeed,NumberOfCores,NumberOfLogicalProcessors | "
            "ConvertTo-Json"
        )
        out = _ps(ps, timeout=8)
        if not out:
            return None
        d = json.loads(out)
        if isinstance(d, list):
            d = d[0]
        name    = d.get("Name", "Unknown").strip()
        load    = d.get("LoadPercentage", "?")
        clock   = d.get("CurrentClockSpeed", "?")
        cores   = d.get("NumberOfCores", "?")
        threads = d.get("NumberOfLogicalProcessors", "?")
        temp    = _cpu_temp_wmi()
        return (
            f"CPU: {name}\n"
            f"  Load: {load}%  Clock: {clock} MHz  "
            f"Cores: {cores}C/{threads}T  Temp: {temp}"
        )
    except Exception:
        return None


def _cpu_temp_wmi() -> str:
    """
    WMI thermal zones (tenths of Kelvin → Celsius).
    Many boards don't expose this — returns 'n/a' if unavailable.
    """
    try:
        ps = (
            "Get-WmiObject MSAcpi_ThermalZoneTemperature -Namespace 'root/wmi' "
            "-ErrorAction SilentlyContinue | "
            "Select-Object -ExpandProperty CurrentTemperature"
        )
        out = _ps(ps, timeout=6)
        if not out:
            return "n/a"
        temps_c = [(int(v) - 2732) / 10 for v in out.splitlines() if v.strip().isdigit()]
        if not temps_c:
            return "n/a"
        return f"{max(temps_c):.0f}°C"
    except Exception:
        return "n/a"


# ── GPU vendor detection ──────────────────────────────────────────────────────

_gpu_vendor_cache: str | None = None   # "amd" | "nvidia" | "intel" | "unknown"


def _gpu_vendor() -> str:
    """Detect installed GPU vendor from WMI. Cached after first call."""
    global _gpu_vendor_cache
    if _gpu_vendor_cache:
        return _gpu_vendor_cache
    try:
        ps = (
            "Get-WmiObject Win32_VideoController | "
            "Where-Object {$_.Name -notmatch 'Virtual|Microsoft|Parsec|Basic'} | "
            "Select-Object -ExpandProperty Name -First 1"
        )
        out = _ps(ps, timeout=6).lower()
        if "amd" in out or "radeon" in out:
            _gpu_vendor_cache = "amd"
        elif "nvidia" in out or "geforce" in out or "quadro" in out:
            _gpu_vendor_cache = "nvidia"
        elif "intel" in out:
            _gpu_vendor_cache = "intel"
        else:
            _gpu_vendor_cache = "unknown"
    except Exception:
        _gpu_vendor_cache = "unknown"
    return _gpu_vendor_cache


# ── AMD via pyadl (AMD Display Library — same SDK Adrenalin uses) ─────────────

def _pyadl() -> str | None:
    """
    Query AMD GPU via pyadl — a Python wrapper for AMD's Display Library (ADL).
    ADL is the same low-level SDK used by Adrenalin internally, so this works on
    any consumer AMD GPU with Adrenalin drivers installed. No admin rights needed.
    Install: pip install pyadl

    NOTE: ADL (v1) does NOT support RDNA3+ GPUs (RX 7000 series and newer).
    Those cards need ADLX (AMD Display Library eXtreme) which pyadl doesn't wrap.
    When all sensor calls fail, we return None so the fallback chain can try
    WMI or amd-smi instead.
    """
    try:
        from pyadl import ADLManager
        devices = ADLManager.getInstance().getDevices()
        if not devices:
            return None

        def _safe(fn):
            try: return fn()
            except Exception: return None

        lines = []
        any_useful = False  # Track whether we got ANY real sensor data
        for device in devices:
            raw_name = getattr(device, "adapterName", f"AMD GPU {device.adapterIndex}")
            # adapterName can return bytes on some systems — normalise to str
            name = raw_name.decode("utf-8", errors="replace") if isinstance(raw_name, bytes) else str(raw_name)
            name = name.strip()

            temp = _safe(device.getCurrentTemperature)  # °C
            core = _safe(device.getCurrentEngineClock)  # MHz
            mem  = _safe(device.getCurrentMemoryClock)  # MHz
            # getCurrentFanSpeed requires speedType arg on newer pyadl
            fan  = _safe(lambda: device.getCurrentFanSpeed(1))  # 1 = percentage
            if fan is None:
                fan = _safe(device.getCurrentFanSpeed)  # try legacy no-arg form
            load = _safe(device.getCurrentUsage)        # %

            # If every sensor returned None, ADL can't talk to this GPU (e.g. RDNA3).
            # Return None so the fallback chain continues to amd-smi / WMI.
            if any(v is not None for v in (temp, core, mem, fan, load)):
                any_useful = True

            temp_str = f"{temp}°C"    if temp is not None else "n/a"
            core_str = f"{core}MHz"   if core is not None else "n/a"
            mem_str  = f"{mem}MHz"    if mem  is not None else "n/a"
            fan_str  = f"{fan}%"      if fan  is not None else "n/a"
            load_str = f"{load}%"     if load is not None else "n/a"

            lines.append(
                f"GPU: {name}\n"
                f"  Temp: {temp_str}  Load: {load_str}\n"
                f"  Core: {core_str}  Mem clock: {mem_str}  Fan: {fan_str}"
            )

        if not any_useful:
            return None  # ADL returned nothing useful — let fallback chain continue

        return "\n".join(lines) if lines else None
    except ImportError:
        return None  # pyadl not installed — try next method
    except Exception:
        return None


# ── AMD via amd-smi ───────────────────────────────────────────────────────────

_amd_smi_path_cache: str | None = None
_amd_smi_searched  = False


def _find_amd_smi() -> str | None:
    """
    Find amd-smi dynamically — checks PATH, common fixed locations,
    then broad search of AMD install directories. Result is cached.
    amd-smi ships with Radeon Software 23.5+ (Adrenalin Edition).
    """
    global _amd_smi_path_cache, _amd_smi_searched
    if _amd_smi_searched:
        return _amd_smi_path_cache

    _amd_smi_searched = True
    import shutil, os

    # 1. Check PATH first (fastest)
    for name in ("amd-smi", "amd-smi.exe"):
        if shutil.which(name):
            _amd_smi_path_cache = name
            return _amd_smi_path_cache

    # 2. Common fixed locations (Radeon Software / ROCm installs)
    candidates = [
        r"C:\Windows\System32\amd-smi.exe",
        r"C:\Program Files\AMD\ROCm\bin\amd-smi.exe",
        r"C:\Program Files\AMD Software\amd-smi.exe",
        r"C:\Program Files (x86)\AMD Software\amd-smi.exe",
    ]
    for path in candidates:
        if os.path.isfile(path):
            _amd_smi_path_cache = path
            return _amd_smi_path_cache

    # 3. Broad recursive search in all AMD dirs (handles any version path)
    ps = (
        "Get-ChildItem -Path 'C:\\Program Files\\AMD','C:\\Program Files (x86)\\AMD' "
        "-Recurse -Filter 'amd-smi.exe' -ErrorAction SilentlyContinue "
        "| Select-Object -ExpandProperty FullName -First 1"
    )
    found = _ps(ps, timeout=10).strip()
    if found and os.path.isfile(found):
        _amd_smi_path_cache = found
        return _amd_smi_path_cache

    return None


_rocm_smi_path_cache: str | None = None
_rocm_smi_searched  = False


def _find_rocm_smi() -> str | None:
    """Find rocm-smi — an older AMD GPU monitoring tool, fallback when amd-smi not available."""
    global _rocm_smi_path_cache, _rocm_smi_searched
    if _rocm_smi_searched:
        return _rocm_smi_path_cache
    _rocm_smi_searched = True
    import shutil, os

    for name in ("rocm-smi", "rocm-smi.exe"):
        if shutil.which(name):
            _rocm_smi_path_cache = name
            return _rocm_smi_path_cache

    candidates = [
        r"C:\Program Files\AMD\ROCm\bin\rocm-smi.exe",
        r"C:\Program Files\ROCm\bin\rocm-smi.exe",
    ]
    for path in candidates:
        if os.path.isfile(path):
            _rocm_smi_path_cache = path
            return _rocm_smi_path_cache

    return None


def _rocm_smi() -> str | None:
    """Query AMD GPU via rocm-smi (fallback when amd-smi unavailable)."""
    try:
        exe = _find_rocm_smi()
        if not exe:
            return None
        r = subprocess.run(
            [exe, "--showtemp", "--showuse", "--showclocks", "--showfan", "--json"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
        )
        if r.returncode != 0 or not r.stdout.strip():
            return None
        data = json.loads(r.stdout)
        lines = []
        for gpu_id, metrics in data.items():
            if not isinstance(metrics, dict):
                continue
            temp  = metrics.get("Temperature (Sensor edge) (C)", metrics.get("Temperature (C)", "n/a"))
            load  = metrics.get("GPU use (%)", "n/a")
            clk   = metrics.get("sclk clock speed:", metrics.get("Current GFX Clock (MHz)", "n/a"))
            fan   = metrics.get("Fan speed (%)", "n/a")
            lines.append(
                f"GPU {gpu_id}:\n"
                f"  Temp: {temp}°C  Load: {load}%  Clock: {clk}  Fan: {fan}%"
            )
        return "\n".join(lines) if lines else None
    except Exception:
        return None


def _amd_smi() -> str | None:
    """
    Query AMD GPU via amd-smi (included with Radeon Software 23.5+).
    No kernel driver beyond the standard AMD display driver required.
    Returns None if amd-smi is not installed or GPU is not AMD.
    """
    try:
        exe = _find_amd_smi()
        if not exe:
            return None
        # Probe: if this fails, amd-smi isn't available
        r = subprocess.run(
            [exe, "metric", "--json"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
        )
        if r.returncode != 0 or not r.stdout.strip():
            return None

        data = json.loads(r.stdout)
        # amd-smi returns a list of GPU objects
        if isinstance(data, dict):
            data = [data]

        # Get GPU names from static info
        names: dict[int, str] = {}
        try:
            rs = subprocess.run(
                [exe, "static", "--json"],
                capture_output=True, text=True, timeout=8,
                encoding="utf-8", errors="replace",
            )
            if rs.returncode == 0 and rs.stdout.strip():
                static = json.loads(rs.stdout)
                if isinstance(static, dict):
                    static = [static]
                for i, g in enumerate(static):
                    asic = g.get("asic", g.get("gpu", {}))
                    name = (asic.get("market_name") or asic.get("asic_serial")
                            or g.get("gpu", {}).get("model", f"AMD GPU {i}"))
                    names[i] = name
        except Exception:
            pass

        lines = []
        for i, gpu in enumerate(data):
            name = names.get(i, f"AMD GPU {i}")

            # Temperature — field names vary by amd-smi version
            temp_block = gpu.get("temperature", gpu.get("Temperature", {}))
            edge   = temp_block.get("edge", temp_block.get("Edge", temp_block.get("hotspot")))
            junct  = temp_block.get("junction", temp_block.get("Junction", temp_block.get("Junction Temperature (C)")))
            mem_t  = temp_block.get("memory", temp_block.get("Memory"))

            temp_parts = []
            if edge   is not None: temp_parts.append(f"Edge {edge}°C")
            if junct  is not None: temp_parts.append(f"Junction {junct}°C")
            if mem_t  is not None: temp_parts.append(f"Mem {mem_t}°C")

            # Usage / load
            use_block = gpu.get("usage", gpu.get("Usage", {}))
            gfx_load  = use_block.get("gfx_activity", use_block.get("GFX Activity",
                         use_block.get("GFX Usage (%)")))

            # Clocks
            clk_block  = gpu.get("clock", gpu.get("Clock", {}))
            gfx_clk    = clk_block.get("gfx_0", clk_block.get("GFX Clock",
                          clk_block.get("gfx")))

            # Fan
            fan_block = gpu.get("fan", gpu.get("Fan", {}))
            fan_spd   = fan_block.get("speed", fan_block.get("Fan Speed (%)",
                         fan_block.get("fan_speed_percentage")))

            # Power
            pwr_block = gpu.get("power", gpu.get("Power", {}))
            pwr_val   = pwr_block.get("average_socket_power", pwr_block.get(
                         "Average Socket Power (W)", pwr_block.get("current_socket_power")))

            def fs(v, unit=""):
                return f"{v}{unit}" if v is not None else "n/a"

            lines.append(
                f"GPU: {name}\n"
                f"  Temps: {', '.join(temp_parts) or 'n/a'}\n"
                f"  Load: {fs(gfx_load, '%')}  Clock: {fs(gfx_clk, ' MHz')}  "
                f"Fan: {fs(fan_spd, '%')}  Power: {fs(pwr_val, 'W')}"
            )

        return "\n".join(lines) if lines else None
    except FileNotFoundError:
        return None  # amd-smi not on PATH
    except Exception:
        return None


# ── NVIDIA via nvidia-smi ──────────────────────────────────────────────────────

_nvidia_smi_path_cache: str | None = None
_nvidia_smi_searched  = False


def _find_nvidia_smi() -> str | None:
    """
    Find nvidia-smi dynamically — checks PATH first, then searches
    common NVIDIA driver install locations. Result is cached.
    """
    global _nvidia_smi_path_cache, _nvidia_smi_searched
    if _nvidia_smi_searched:
        return _nvidia_smi_path_cache

    _nvidia_smi_searched = True
    import shutil, os

    # 1. Check PATH first
    if shutil.which("nvidia-smi"):
        _nvidia_smi_path_cache = "nvidia-smi"
        return _nvidia_smi_path_cache

    # 2. Common fixed locations
    candidates = [
        r"C:\Windows\System32\nvidia-smi.exe",
        r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
    ]
    for path in candidates:
        if os.path.isfile(path):
            _nvidia_smi_path_cache = path
            return _nvidia_smi_path_cache

    # 3. Search Program Files broadly (handles non-standard installs)
    ps = (
        "Get-ChildItem -Path 'C:\\Program Files\\NVIDIA Corporation' "
        "-Recurse -Filter 'nvidia-smi.exe' -ErrorAction SilentlyContinue "
        "| Select-Object -ExpandProperty FullName -First 1"
    )
    found = _ps(ps, timeout=8).strip()
    if found and os.path.isfile(found):
        _nvidia_smi_path_cache = found
        return _nvidia_smi_path_cache

    return None


def _nvidia_smi() -> str | None:
    """
    Query NVIDIA GPU via nvidia-smi. Uses the standard NVIDIA display driver —
    no WinRing0 or any other kernel driver required.
    """
    try:
        exe = _find_nvidia_smi()
        if not exe:
            return None
        query = (
            "name,temperature.gpu,utilization.gpu,utilization.memory,"
            "clocks.current.graphics,clocks.current.memory,"
            "fan.speed,power.draw,memory.used,memory.total"
        )
        r = subprocess.run(
            [exe, f"--query-gpu={query}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8,
            encoding="utf-8", errors="replace",
        )
        if r.returncode != 0 or not r.stdout.strip():
            return None

        lines = []
        for row in r.stdout.strip().splitlines():
            parts = [p.strip() for p in row.split(",")]
            if len(parts) < 10:
                continue
            name, temp, gpu_util, mem_util, core_clk, mem_clk, fan, power, vram_used, vram_total = parts[:10]

            def fmt(v: str, unit: str = "") -> str:
                return f"{v}{unit}" if v not in ("", "[N/A]", "N/A") else "n/a"

            lines.append(
                f"GPU: {name}\n"
                f"  GPU Core: {fmt(temp, '°C')}  Load: {fmt(gpu_util, '%')}  "
                f"Fan: {fmt(fan, '%')}  Power: {fmt(power, 'W')}\n"
                f"  Core Clock: {fmt(core_clk, ' MHz')}  Mem Clock: {fmt(mem_clk, ' MHz')}\n"
                f"  VRAM: {fmt(vram_used)} / {fmt(vram_total)} MB"
            )

        return "\n".join(lines) if lines else None
    except FileNotFoundError:
        return None  # nvidia-smi not on PATH — not NVIDIA or driver not installed
    except Exception:
        return None


# ── GPU WMI fallback (AMD / Intel — no temps without amd-smi/nvidia-smi) ──────

def _gpu_wmi_fallback() -> str | None:
    try:
        ps = (
            "Get-WmiObject Win32_VideoController | "
            "Where-Object {$_.Name -notmatch 'Virtual|Microsoft|Parsec'} | "
            "Select-Object Name,AdapterRAM,DriverVersion | ConvertTo-Json"
        )
        out = _ps(ps, timeout=8)
        if not out:
            return None
        d = json.loads(out)
        if isinstance(d, dict):
            d = [d]

        # Also grab 3D utilization from WDDM performance counters
        util = _gpu_util_wmi()
        util_str = f"{util}%" if util is not None else "n/a"

        vendor = _gpu_vendor()
        if vendor == "amd":
            # Check if pyadl is installed but ADL failed (RDNA3+ GPUs)
            try:
                import pyadl as _pyadl_check  # noqa: F811
                amd_smi_note = "  (ADL does not support this GPU — temp unavailable via WMI)"
            except ImportError:
                amd_smi_note = "  (temp unavailable — run: pip install pyadl)"
        elif vendor == "nvidia":
            amd_smi_note = "  (nvidia-smi not found in System32 or NVIDIA Corporation folder — temp unavailable)"
        else:
            amd_smi_note = "  (temp unavailable)"
        lines = []
        for g in d:
            name    = g.get("Name", "Unknown")
            raw_vram = g.get("AdapterRAM") or 0
            # WMI AdapterRAM is uint32 — maxes out at ~4.29 GB.
            # If the value is exactly 4 GB (or suspiciously close to the uint32 ceiling),
            # the real VRAM is likely higher. Don't report a known-bad number.
            vram_gb = round(raw_vram / 1_073_741_824, 1)
            if vram_gb >= 4.0 and raw_vram >= 0xF0000000:  # near uint32 max
                vram_str = "n/a (WMI limit)"
            else:
                vram_str = f"{vram_gb} GB"
            lines.append(
                f"GPU: {name}\n"
                f"  3D Load: {util_str}  VRAM: {vram_str}  Temp: n/a{amd_smi_note}"
            )
        return "\n".join(lines) if lines else None
    except Exception:
        return None


def _gpu_util_wmi() -> int | None:
    try:
        ps = (
            "Get-WmiObject -Namespace 'root\\cimv2' "
            "-Class 'Win32_PerfFormattedData_GPUPerformanceCounters_GPUEngine' "
            "-ErrorAction Stop | "
            "Where-Object { $_.Name -like '*engtype_3D' } | "
            "Measure-Object -Property UtilizationPercentage -Maximum | "
            "Select-Object -ExpandProperty Maximum"
        )
        out = _ps(ps, timeout=8)
        if not out or not out.strip().isdigit():
            return None
        return int(out.strip())
    except Exception:
        return None


# Pre-warm cache on module load
_maybe_refresh()
