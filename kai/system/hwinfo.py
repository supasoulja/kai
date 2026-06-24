"""
hwinfo.py — Read sensor data from HWiNFO64's Shared Memory interface.

HWiNFO64 exposes all sensor data via a Windows shared memory segment
named "Global\\HWiNFO_SENS_SM2". No pip install needed — just ctypes + mmap.

Requirements:
  1. HWiNFO64 must be running
  2. Settings → Main Settings → Shared Memory Support → ON

The shared memory layout is documented at:
  https://www.hwinfo.com/sdk/HWiNFO_ShM.h
"""
import ctypes
import struct

# Win32 API constants
FILE_MAP_READ = 0x0004

_kernel32 = ctypes.windll.kernel32

# Set proper return types for 64-bit pointers (default c_int truncates on x64)
_kernel32.OpenFileMappingW.restype = ctypes.c_void_p
_kernel32.MapViewOfFile.restype = ctypes.c_void_p
_kernel32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
_kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

# Shared memory header (matches HWiNFO_SENS_SM2 v2 layout)
_SHM_NAME = "Global\\HWiNFO_SENS_SM2"
_HEADER_SIZE = 32  # dwSignature(4) + dwVersion(4) + dwRevision(4) + pollTime(8) +
                    # dwOffsetOfSensorSection(4) + dwSizeOfSensorElement(4) +
                    # dwNumSensorElements(4)
                    # ... followed by reading section info


class HWiNFOReading:
    __slots__ = ("sensor", "label", "value", "unit")
    def __init__(self, sensor: str, label: str, value: float, unit: str):
        self.sensor = sensor
        self.label = label
        self.value = value
        self.unit = unit

    def __repr__(self):
        return f"{self.sensor} / {self.label}: {self.value} {self.unit}"


def is_available() -> bool:
    """Check if HWiNFO64 shared memory is accessible (i.e. HWiNFO64 is running with SHM enabled)."""
    handle = _kernel32.OpenFileMappingW(FILE_MAP_READ, False, _SHM_NAME)
    if handle:
        _kernel32.CloseHandle(handle)
        return True
    return False


def read_all() -> list[HWiNFOReading]:
    """Read all sensor readings from HWiNFO64 shared memory. Returns empty list if unavailable."""
    handle = _kernel32.OpenFileMappingW(FILE_MAP_READ, False, _SHM_NAME)
    if not handle:
        return []

    try:
        view = _kernel32.MapViewOfFile(handle, FILE_MAP_READ, 0, 0, 0)
        if not view:
            return []

        try:
            return _parse_shm(view)
        finally:
            _kernel32.UnmapViewOfFile(view)
    finally:
        _kernel32.CloseHandle(handle)


def _read_bytes(base: int, offset: int, size: int) -> bytes:
    buf = (ctypes.c_char * size)()
    ctypes.memmove(buf, base + offset, size)
    return bytes(buf)


def _read_str(base: int, offset: int, max_len: int = 128) -> str:
    raw = _read_bytes(base, offset, max_len)
    # Null-terminated ASCII/UTF-8 string
    end = raw.find(b'\x00')
    if end >= 0:
        raw = raw[:end]
    return raw.decode("utf-8", errors="replace").strip()


def _parse_shm(view: int) -> list[HWiNFOReading]:
    # Read header
    hdr = _read_bytes(view, 0, 64)

    signature = struct.unpack_from("<I", hdr, 0)[0]
    if signature != 0x53695748:  # "HWiS" in little-endian
        return []

    version = struct.unpack_from("<I", hdr, 4)[0]
    revision = struct.unpack_from("<I", hdr, 8)[0]
    # offset 12: pollTime (8 bytes, __int64)

    # Sensor section (offsets shifted by 8-byte pollTime)
    sensor_offset = struct.unpack_from("<I", hdr, 20)[0]
    sensor_elem_size = struct.unpack_from("<I", hdr, 24)[0]
    num_sensors = struct.unpack_from("<I", hdr, 28)[0]

    # Reading section
    reading_offset = struct.unpack_from("<I", hdr, 32)[0]
    reading_elem_size = struct.unpack_from("<I", hdr, 36)[0]
    num_readings = struct.unpack_from("<I", hdr, 40)[0]

    # Parse sensor names (use index as key, not sensor ID)
    sensor_names: dict[int, str] = {}
    for i in range(num_sensors):
        base = view + sensor_offset + (i * sensor_elem_size)
        # Sensor ID at offset 0 (4 bytes), instance at offset 4
        # Original name at offset 8, user name at offset 136
        name = _read_str(base, 8, 128)
        user_name = _read_str(base, 136, 128)
        sensor_names[i] = user_name or name

    # Parse readings
    results = []
    for i in range(num_readings):
        base = view + reading_offset + (i * reading_elem_size)

        # Reading type at offset 0
        reading_type = struct.unpack("<I", _read_bytes(base, 0, 4))[0]
        # Sensor index at offset 4 (index into sensor array)
        sensor_idx = struct.unpack("<I", _read_bytes(base, 4, 4))[0]
        # Reading ID at offset 8
        # Original label at offset 12 (128 bytes), user label at offset 140 (128 bytes)
        label_orig = _read_str(base, 12, 128)
        label_user = _read_str(base, 140, 128)
        label = label_user or label_orig
        # Unit at offset 268 (16 bytes)
        unit = _read_str(base, 268, 16)
        # Value (double) at offset 284
        value = struct.unpack("<d", _read_bytes(base, 284, 8))[0]

        sensor_name = sensor_names.get(sensor_idx, f"Sensor {sensor_idx}")
        results.append(HWiNFOReading(sensor_name, label, value, unit))

    return results


def get_temps() -> dict[str, float]:
    """Convenience: return a dict of temperature readings {label: celsius}."""
    readings = read_all()
    temps = {}
    for r in readings:
        if r.unit == "°C" or "temp" in r.label.lower() or "temperature" in r.label.lower():
            key = f"{r.sensor} / {r.label}"
            temps[key] = r.value
    return temps


def get_gpu_summary() -> str | None:
    """Get a formatted GPU summary from HWiNFO64. Returns None if unavailable."""
    readings = read_all()
    if not readings:
        return None

    # Find GPU-related readings — match dGPU sensor entries, not CPU/mobo
    gpu_data: dict[str, dict[str, str]] = {}
    for r in readings:
        sensor_lower = r.sensor.lower()
        if any(k in sensor_lower for k in ("dgpu", "radeon", "geforce", "nvidia")) and "cpu" not in sensor_lower:
            if r.sensor not in gpu_data:
                gpu_data[r.sensor] = {}
            label = r.label.lower()
            val = f"{r.value:.1f}" if r.value != int(r.value) else f"{int(r.value)}"
            gpu_data[r.sensor][label] = f"{val}{r.unit}"

    if not gpu_data:
        return None

    lines = []
    for name, metrics in gpu_data.items():
        temp = metrics.get("gpu temperature", "n/a")
        junction = metrics.get("gpu hot spot temperature", "")
        load = metrics.get("gpu utilization", metrics.get("gpu d3d usage", "n/a"))
        clock = metrics.get("gpu shader clock", metrics.get("gpu front end clock", "n/a"))
        fan = metrics.get("gpu fan", "n/a")
        power = metrics.get("total board power (tbp)", metrics.get("total graphics power (tgp)", "n/a"))
        vram_used = metrics.get("gpu d3d memory dedicated", metrics.get("gpu memory usage", ""))
        vram_clock = metrics.get("gpu memory clock", "")

        line = f"GPU: {name}\n  Temp: {temp}"
        if junction:
            line += f"  Junction: {junction}"
        line += f"  Load: {load}  Clock: {clock}  Fan: {fan}  Power: {power}"
        if vram_used:
            line += f"\n  VRAM Used: {vram_used}"
        if vram_clock:
            line += f"  Mem Clock: {vram_clock}"
        lines.append(line)

    return "\n".join(lines)


def get_cpu_temp() -> str:
    """Get CPU temperature from HWiNFO64. Returns temp string or 'n/a'."""
    readings = read_all()
    for r in readings:
        sensor_lower = r.sensor.lower()
        label_lower = r.label.lower()
        if ("cpu" in sensor_lower and "gpu" not in sensor_lower and
                ("tctl" in label_lower or "tdie" in label_lower or
                 "cpu temp" in label_lower or "cpu package" in label_lower)):
            return f"{r.value:.0f}°C"
    return "n/a"
