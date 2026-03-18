"""
network.* tools — ping, traceroute, and full connectivity diagnostics.
All read-only. No configuration changes made.
"""
import re
import subprocess

from kai.tools.registry import registry


def _run(args: list[str], timeout: int = 60) -> str:
    """Run a command, return stdout. Empty string on error/timeout."""
    try:
        r = subprocess.run(
            args,
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
        return (r.stdout or "").strip()
    except subprocess.TimeoutExpired:
        return ""
    except Exception:
        return ""


def _safe_host(host: str) -> str:
    """Strip everything except valid hostname/IP characters."""
    return re.sub(r"[^a-zA-Z0-9.\-]", "", host.strip())


def _parse_ping(output: str, host: str, count: int) -> str:
    """Parse Windows ping.exe output into a clean summary."""
    if not output:
        return f"Ping to {host} timed out (no response)."

    lower = output.lower()
    if "could not find host" in lower or "request could not find" in lower:
        return f"Could not resolve host: {host}. DNS may not be working."
    if "destination host unreachable" in lower or "request timed out" in lower:
        # Count how many timed out
        timeouts = lower.count("request timed out")
        loss_pct = int(timeouts / count * 100)
        return f"Ping {host}: {loss_pct}% packet loss (host may be down or blocking ICMP)."

    # Parse "Lost = X (Y% loss)"
    loss_m = re.search(r"Lost\s*=\s*\d+\s*\(\s*(\d+)%\s*loss\)", output)
    # Parse "Minimum = Xms, Maximum = Xms, Average = Xms"
    rtt_m = re.search(
        r"Minimum\s*=\s*(\d+)ms.*?Maximum\s*=\s*(\d+)ms.*?Average\s*=\s*(\d+)ms",
        output, re.DOTALL | re.IGNORECASE,
    )

    if not loss_m and not rtt_m:
        return output[:600]

    loss = loss_m.group(1) if loss_m else "?"
    if rtt_m:
        mn, mx, avg = rtt_m.group(1), rtt_m.group(2), rtt_m.group(3)
        quality = ""
        avg_ms = int(avg)
        if avg_ms < 30:
            quality = " — excellent"
        elif avg_ms < 80:
            quality = " — good"
        elif avg_ms < 150:
            quality = " — noticeable lag"
        else:
            quality = " — high latency, expect issues"
        return (
            f"Ping {host}  ({count} packets)\n"
            f"  Packet loss: {loss}%\n"
            f"  Latency:     min {mn} ms  avg {avg} ms  max {mx} ms{quality}"
        )
    return f"Ping {host}: {loss}% packet loss"


@registry.tool(
    name="network.ping",
    description=(
        "Ping a host to measure latency and packet loss. "
        "Use this to test internet connectivity or diagnose lag in games."
    ),
    parameters={
        "host": {
            "type": "string",
            "description": "Hostname or IP to ping (e.g. '8.8.8.8', 'google.com', '1.1.1.1')",
        },
        "count": {
            "type": "integer",
            "description": "Number of pings to send (default 10)",
        },
    },
)
def ping_host(host: str, count: int = 10) -> str:
    host = _safe_host(host)
    if not host:
        return "Invalid host."
    count = max(1, min(int(count), 50))
    out = _run(["ping", "-n", str(count), host], timeout=count * 3 + 10)
    return _parse_ping(out, host, count)


@registry.tool(
    name="network.traceroute",
    description=(
        "Trace the network route to a host, showing each hop and latency. "
        "Useful for finding where lag or packet loss occurs between you and a server. "
        "After retrieving: identify where the latency first jumps or where * (timeout) appears. "
        "A spike at hop 1-2 = local router or WiFi issue. "
        "A spike at hop 3-5 = ISP handoff issue. "
        "A spike near the end = destination server or its hosting network. "
        "Give a plain-language verdict on where the problem is."
    ),
    parameters={
        "host": {
            "type": "string",
            "description": "Hostname or IP to trace (e.g. '8.8.8.8', 'google.com')",
        },
    },
)
def traceroute(host: str) -> str:
    host = _safe_host(host)
    if not host:
        return "Invalid host."
    # -d = no DNS lookup (faster), -w 1000 = 1s timeout per hop, -h 20 = max 20 hops
    out = _run(["tracert", "-d", "-w", "1000", "-h", "20", host], timeout=90)
    if not out:
        return f"Traceroute to {host} timed out."
    # Trim the header/footer and limit output
    lines = [l.rstrip() for l in out.splitlines() if l.strip()]
    return "\n".join(lines[:35])


@registry.tool(
    name="network.full_diagnostic",
    description=(
        "Run a full network diagnostic: ping the local gateway, 8.8.8.8, and 1.1.1.1. "
        "Returns latency and packet loss for each. "
        "Use this when someone reports lag, slow internet, or connection problems. "
        "After retrieving: interpret the pattern — "
        "gateway bad = local router or WiFi issue (not the ISP); "
        "gateway fine but 8.8.8.8/1.1.1.1 bad = ISP problem; "
        "all fine = the problem is with the specific destination, not the connection. "
        "Give a plain-language verdict and a suggested next step."
    ),
)
def full_diagnostic() -> str:
    import subprocess as _sp

    # Get default gateway from routing table
    gateway = ""
    try:
        r = _sp.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "(Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue | "
             "Sort-Object RouteMetric | Select-Object -First 1).NextHop"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
        )
        gateway = r.stdout.strip()
    except Exception:
        pass

    results = []

    # 1. Gateway ping
    if gateway and gateway not in ("", "{}"):
        out = _run(["ping", "-n", "5", gateway], timeout=25)
        results.append(_parse_ping(out, f"Gateway ({gateway})", 5))
    else:
        results.append("Gateway: could not determine default gateway.")

    # 2. Google DNS
    out = _run(["ping", "-n", "10", "8.8.8.8"], timeout=40)
    results.append(_parse_ping(out, "8.8.8.8 (Google DNS)", 10))

    # 3. Cloudflare DNS
    out = _run(["ping", "-n", "10", "1.1.1.1"], timeout=40)
    results.append(_parse_ping(out, "1.1.1.1 (Cloudflare)", 10))

    return "\n\n".join(results)
