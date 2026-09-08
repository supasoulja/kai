"""
system.drives — every mounted drive, with capacity and free space.

Kai could not answer "what drives do I have" at all. system.info reports
psutil.disk_usage("/") — one mount, the root filesystem — and files.disk_usage
returns folder sizes despite its name. Asked to look for other drives, the model
was handed folder sizes and invented /dev/sda1, /dev/sdb1 and /dev/sdc1 with
capacities that appeared nowhere in the tool output.

The data already existed: kai.system.devstats.collect_disk() has enumerated
every partition since the dashboard needed it. It was simply never registered as
a tool. This wraps it rather than reimplementing, so the dashboard gauges and
what Kai says can never disagree.
"""

from kai.system.devstats import collect_disk
from kai.tools.registry import registry


@registry.tool(
    name="system.drives",
    description=(
        "List every mounted drive and partition with its total size, used space, "
        "free space and filesystem type. Use this for any question about drives, "
        "disks, partitions, mount points, or how much space is left — including "
        "'what drives do I have', 'is there another drive', and 'how full is X'. "
        "system.info reports only the root filesystem; this reports all of them. "
        "After retrieving: name the mounts by their actual mount point, never a "
        "device path like /dev/sda1 — this tool does not report device names and "
        "you must not invent one. Flag anything above 90% full."
    ),
    category="disk_analysis",
    label="Checking drives",
    risk="safe",
)
def get_drives() -> str:
    parts = collect_disk()
    if not parts:
        return "No mounted drives could be read."
    lines = [f"{len(parts)} mounted drive(s):"]
    for p in parts:
        lines.append(
            f"  {p['mount']}  ({p['fstype']})  "
            f"{p['total_gb']:.1f} GB total, {p['used_gb']:.1f} used, "
            f"{p['free_gb']:.1f} free — {p['percent']:.1f}% used"
        )
    return "\n".join(lines)
