"""
system.info — CPU, RAM, disk, and GPU usage.
Uses psutil for system stats. GPU via pynvml (NVIDIA) or direct ADL
query isn't available on Windows AMD without extra drivers, so we
report what we can and skip what we can't.
"""
import json
import psutil
from kai.tools.registry import registry


@registry.tool(
    name="system.info",
    description=(
        "Get current system resource usage: CPU load percentage, RAM usage, "
        "disk space, and top CPU-consuming processes. "
        "This tool does NOT provide temperatures, GPU stats, fan speed, or clock speeds — "
        "use system.temps for those. "
        "After retrieving: flag anything concerning — CPU above 80% sustained is high, "
        "RAM above 85% used means memory pressure, disk above 90% full needs attention. "
        "Name the specific top process if it's eating unusual CPU. "
        "If everything looks healthy, say so briefly."
    ),
)
def get_system_info() -> str:
    cpu_pct  = psutil.cpu_percent(interval=0.5)
    ram      = psutil.virtual_memory()
    disk     = psutil.disk_usage("/")

    # Top 5 processes by CPU
    procs = []
    for p in sorted(
        psutil.process_iter(["name", "cpu_percent", "memory_percent"]),
        key=lambda p: p.info["cpu_percent"] or 0,
        reverse=True,
    )[:5]:
        procs.append(f"{p.info['name']} ({p.info['cpu_percent']:.1f}% cpu)")

    result = {
        "cpu":  f"{cpu_pct:.1f}%",
        "ram":  f"{ram.percent:.1f}% used ({_gb(ram.used)}/{_gb(ram.total)} GB)",
        "disk": f"{disk.percent:.1f}% used ({_gb(disk.used)}/{_gb(disk.total)} GB)",
        "top_processes": procs,
        "note": "Ollama (AI inference) runs on the GPU — not visible in CPU process list.",
    }
    return json.dumps(result, indent=2)


def _gb(bytes_: int) -> str:
    return f"{bytes_ / 1_073_741_824:.1f}"
