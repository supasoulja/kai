"""
cluster.* tools — monitor, scan, and broadcast tasks across registered nodes.

All read-only. Commands are queued in SQLite and picked up by watchdog/agent.py
running on each remote machine. Results are polled with a timeout so the Brain
gets a real answer, not a fire-and-forget ack.
"""

import time

from kai.tools.registry import registry


_SCAN_COMMANDS = ["system_info", "cpu_load", "ram_usage", "disk_usage", "temps"]
_POLL_INTERVAL = 2    # seconds between result polls
_NODE_TIMEOUT  = 60   # seconds to wait for a single node scan
_BROAD_TIMEOUT = 90   # seconds to wait for a broadcast scan


def _queue():
    from kai import watchdog_queue
    return watchdog_queue


def _wait_results(command_ids: list[str], timeout: float) -> dict[str, dict | None]:
    """Poll until all command_ids have results or timeout expires."""
    wq = _queue()
    deadline = time.time() + timeout
    pending = list(command_ids)
    results: dict[str, dict | None] = {cid: None for cid in command_ids}
    while pending and time.time() < deadline:
        batch = wq.get_command_results(pending)
        for cid, result in batch.items():
            if result is not None:
                results[cid] = result
                pending.remove(cid)
        if pending:
            time.sleep(_POLL_INTERVAL)
    return results


def _format_node_report(label: str, results: dict[str, dict | None]) -> str:
    lines = [f"=== {label} ==="]
    for cmd, result in results.items():
        if result is None:
            lines.append(f"  {cmd}: [timed out]")
            continue
        status = result.pop("_status", "done")
        tag = " [ERROR]" if status == "error" else ""
        lines.append(f"  {cmd}{tag}: {result}")
    return "\n".join(lines)


# ── Tools ─────────────────────────────────────────────────────────────────────

@registry.tool(
    name="cluster.list_nodes",
    description=(
        "List all machines registered on Kai's watchdog network — their label, "
        "last-seen time, and whether they are currently online (checked in within 60s). "
        "Use this to see what nodes are available before running diagnostics."
    ),
)
def list_nodes() -> str:
    devices = _queue().get_all_devices()
    if not devices:
        return "No nodes registered. Use the watchdog join flow to pair a machine."
    now = time.time()
    lines = []
    for d in devices:
        last = d["last_seen"]
        if last is None:
            age = "never seen"
        else:
            secs = int(now - last)
            age = f"{secs}s ago"
        online = "ONLINE" if last and (now - last) < 60 else "offline"
        lines.append(f"  [{online}] {d['label']}  (id: {d['device_id']})  last seen: {age}")
    return "Registered nodes:\n" + "\n".join(lines)


@registry.tool(
    name="cluster.node_status",
    description=(
        "Quick health check of a single node: hostname, OS, uptime, CPU load, and RAM. "
        "Faster than a full scan — use this for a one-liner status on a specific machine."
    ),
    parameters={
        "device_id": {
            "type": "string",
            "description": "The device_id of the node to check (from cluster.list_nodes)",
        },
    },
)
def node_status(device_id: str) -> str:
    wq = _queue()
    cmd_ids = [
        wq.queue_command(device_id, "system_info"),
        wq.queue_command(device_id, "cpu_load"),
        wq.queue_command(device_id, "ram_usage"),
    ]
    results = _wait_results(cmd_ids, timeout=30)
    if all(v is None for v in results.values()):
        return f"No response from {device_id} within 30s — node may be offline or agent not running."
    lines = [f"Status: {device_id}"]
    for cid, result in zip(cmd_ids, [results[c] for c in cmd_ids]):
        if result:
            result.pop("_status", None)
            lines.append(f"  {result}")
    return "\n".join(lines)


@registry.tool(
    name="cluster.node_scan",
    description=(
        "Run a full diagnostic scan on a single remote node: system info, CPU load, RAM, "
        "disk usage, and temperatures. Waits up to 60 seconds for results."
    ),
    parameters={
        "device_id": {
            "type": "string",
            "description": "The device_id of the node to scan (from cluster.list_nodes)",
        },
    },
)
def node_scan(device_id: str) -> str:
    wq = _queue()
    devices = {d["device_id"]: d["label"] for d in wq.get_all_devices()}
    label = devices.get(device_id, device_id)

    cmd_map = {cmd: wq.queue_command(device_id, cmd) for cmd in _SCAN_COMMANDS}
    results_by_id = _wait_results(list(cmd_map.values()), timeout=_NODE_TIMEOUT)
    results_by_cmd = {cmd: results_by_id[cid] for cmd, cid in cmd_map.items()}

    return _format_node_report(label, results_by_cmd)


@registry.tool(
    name="cluster.broadcast_scan",
    description=(
        "Run a full diagnostic scan on ALL active nodes simultaneously and return an "
        "aggregated health report. Waits up to 90 seconds for all nodes to respond. "
        "Use this when asked 'how are all my machines doing?' or 'scan everything'."
    ),
)
def broadcast_scan() -> str:
    wq = _queue()
    devices = wq.get_all_devices()
    active = [d for d in devices if d["status"] == "active"]
    if not active:
        return "No active nodes registered."

    # Queue all commands for all nodes
    node_cmd_maps: dict[str, dict[str, str]] = {}
    all_cmd_ids: list[str] = []
    for d in active:
        did = d["device_id"]
        node_cmd_maps[did] = {cmd: wq.queue_command(did, cmd) for cmd in _SCAN_COMMANDS}
        all_cmd_ids.extend(node_cmd_maps[did].values())

    results_by_id = _wait_results(all_cmd_ids, timeout=_BROAD_TIMEOUT)

    sections = []
    for d in active:
        did = d["device_id"]
        results_by_cmd = {cmd: results_by_id[cid] for cmd, cid in node_cmd_maps[did].items()}
        sections.append(_format_node_report(d["label"], results_by_cmd))

    return "\n\n".join(sections)


@registry.tool(
    name="cluster.get_result",
    description=(
        "Check whether a previously queued command has completed and retrieve its result. "
        "Use this to follow up on a command you sent earlier."
    ),
    parameters={
        "command_id": {
            "type": "string",
            "description": "The command_id returned when the command was queued",
        },
    },
)
def get_result(command_id: str) -> str:
    results = _queue().get_command_results([command_id])
    result = results.get(command_id)
    if result is None:
        return f"Command {command_id} is still pending or does not exist."
    status = result.pop("_status", "done")
    return f"[{status}] {result}"
