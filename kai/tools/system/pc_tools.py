"""
pc.* tools — startup programs, event logs, network info, Windows updates,
and deep system scan.
All read-only. No system changes made.

Platform: Windows only (uses PowerShell / WMI).  On Linux these tools return
a helpful message instead of crashing.
"""
import json
import subprocess
import threading
from kai.tools.registry import registry
from kai.tools._shell import run_powershell
from kai.system.platform import IS_WINDOWS as _IS_WINDOWS


def _run(cmd: list[str], timeout: int = 10) -> str:
    """Run a command, return stdout or empty string."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


def _ps(cmd: str, timeout: int = 20) -> str | None:
    """Run a PowerShell command, return stdout or None on error/timeout/empty."""
    return run_powershell(cmd, timeout).out or None


@registry.tool(
    name="pc.startup_programs",
    description=(
        "List all programs configured to run at Windows startup. "
        "After retrieving: identify entries that are likely unnecessary or bloatware — "
        "gaming launchers (Epic, Steam, GOG, Ubisoft), chat apps, cloud sync, "
        "manufacturer update helpers, or anything the user clearly doesn't need at boot. "
        "Flag which ones are safe to disable and which should stay "
        "(security software, hardware drivers, core system entries). "
        "Never suggest disabling antivirus, Windows Defender, or GPU drivers."
    ),
)
def get_startup_programs() -> str:
    if not _IS_WINDOWS:
        return _linux_startup_programs()
    cmd = (
        "Get-CimInstance Win32_StartupCommand | "
        "Select-Object Name, Command, Location, User | "
        "ConvertTo-Json -Depth 2"
    )
    out = _ps(cmd)
    if not out:
        return "Could not retrieve startup programs."
    try:
        data = json.loads(out)
        if isinstance(data, dict):
            data = [data]
        lines = []
        for item in data:
            name = item.get("Name", "Unknown")
            cmd_str = item.get("Command", "")
            loc = item.get("Location", "")
            user = item.get("User", "")
            lines.append(f"• {name}\n  {cmd_str}\n  [{loc}]  User: {user}")
        return "\n".join(lines) if lines else "No startup programs found."
    except Exception:
        return out[:2000]


@registry.tool(
    name="pc.event_logs",
    description=(
        "Get recent Windows Event Log errors or warnings. "
        "Useful for diagnosing crashes, failures, and system issues. "
        "After retrieving: for each significant event, explain what the error source "
        "and message means in plain language and the most likely cause. "
        "Skip entries that are clearly noise. "
        "If an error code or source name is unfamiliar, call search.web to look it up "
        "before explaining it — never guess."
    ),
    parameters={
        "hours": {
            "type": "integer",
            "description": "How many hours back to look (default 24)",
        },
        "level": {
            "type": "string",
            "description": "Log level: 'Error', 'Warning', or 'Both' (default 'Error')",
        },
    },
)
def get_event_logs(hours: int = 24, level: str = "Error") -> str:
    if not _IS_WINDOWS:
        return _linux_event_logs(hours, level)
    level_map = {"warning": "3", "warn": "3", "both": "2,3", "all": "2,3"}
    levels = level_map.get(level.strip().lower(), "2")  # default Error

    cmd = (
        f"$since = (Get-Date).AddHours(-{int(hours)}); "
        f"Get-WinEvent -FilterHashtable @{{LogName='System','Application'; Level={levels}; StartTime=$since}} "
        f"-MaxEvents 25 -ErrorAction SilentlyContinue | "
        f"Select-Object TimeCreated, Id, ProviderName, "
        f"@{{N='Msg';E={{$_.Message.Substring(0,[Math]::Min(150,$_.Message.Length))}}}} | "
        f"ConvertTo-Json -Depth 2"
    )
    out = _ps(cmd, timeout=20)
    if not out:
        return f"No {level} events found in the last {hours} hours."
    try:
        data = json.loads(out)
        if isinstance(data, dict):
            data = [data]
        lines = []
        for item in data[:20]:
            # TimeCreated comes out as {"value": "...", "DisplayHint": 2}
            tc = item.get("TimeCreated", "?")
            ts = tc.get("value", "?")[:19] if isinstance(tc, dict) else str(tc)[:19]
            src = item.get("ProviderName", "?")
            msg = item.get("Msg", "").strip()
            lines.append(f"[{ts}] {src}\n  {msg}")
        return "\n\n".join(lines) if lines else "No events found."
    except Exception:
        return out[:3000]


@registry.tool(
    name="pc.network_info",
    description="Get active network adapter info: IP address, gateway, DNS, and link speed.",
)
def get_network_info() -> str:
    if not _IS_WINDOWS:
        return _linux_network_info()
    cmd = (
        "Get-NetAdapter | Where-Object {$_.Status -eq 'Up'} | ForEach-Object { "
        "  $idx = $_.InterfaceIndex; "
        "  $ip = (Get-NetIPAddress -InterfaceIndex $idx -AddressFamily IPv4 -ErrorAction SilentlyContinue | Select-Object -First 1); "
        "  $dns = (Get-DnsClientServerAddress -InterfaceIndex $idx -AddressFamily IPv4 -ErrorAction SilentlyContinue); "
        "  $gw = (Get-NetRoute -InterfaceIndex $idx -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue | Select-Object -ExpandProperty NextHop -First 1); "
        "  [PSCustomObject]@{ "
        "    Adapter=$_.Name; Speed=$_.LinkSpeed; "
        "    IP=$ip.IPAddress; PrefixLength=$ip.PrefixLength; "
        "    Gateway=$gw; DNS=($dns.ServerAddresses -join ', ') "
        "  } "
        "} | ConvertTo-Json -Depth 2"
    )
    out = _ps(cmd, timeout=15)
    if not out:
        return "Could not retrieve network info."
    try:
        data = json.loads(out)
        if isinstance(data, dict):
            data = [data]
        lines = []
        for item in data:
            lines.append(
                f"Adapter: {item.get('Adapter','?')}  ({item.get('Speed','?')})\n"
                f"  IP:      {item.get('IP','?')}/{item.get('PrefixLength','?')}\n"
                f"  Gateway: {item.get('Gateway','?')}\n"
                f"  DNS:     {item.get('DNS','?')}"
            )
        return "\n\n".join(lines) if lines else "No active network adapters found."
    except Exception:
        return out[:2000]


@registry.tool(
    name="pc.windows_updates",
    description=(
        "Check for pending Windows updates and show recently installed updates. "
        "After retrieving: if there are 0 pending updates, confirm the system is up to date. "
        "If updates are pending, recommend installing them — flag security or cumulative "
        "updates as higher priority. "
        "If the count is large (10+), note that running Windows Update would be a good idea."
    ),
)
def get_windows_updates() -> str:
    if not _IS_WINDOWS:
        return _linux_updates()
    cmd = (
        "try { "
        "  $s = New-Object -ComObject Microsoft.Update.Session; "
        "  $searcher = $s.CreateUpdateSearcher(); "
        "  $pending = ($searcher.Search('IsInstalled=0 and Type=Software')).Updates.Count; "
        "  $count = $searcher.GetTotalHistoryCount(); "
        "  $hist = if ($count -gt 0) { "
        "    $searcher.QueryHistory(0, [Math]::Min(5,$count)) | "
        "    Where-Object {$_.ResultCode -eq 2} | "
        "    Select-Object -First 3 Title, @{N='Date';E={$_.Date.ToString('yyyy-MM-dd')}} "
        "  } else { @() }; "
        "  [PSCustomObject]@{Pending=$pending; Recent=$hist} | ConvertTo-Json -Depth 3 "
        "} catch { Write-Output ('ERROR: ' + $_.Exception.Message) }"
    )
    out = _ps(cmd, timeout=30)
    if not out:
        return "Could not check Windows Update (may need admin)."
    if out.startswith("ERROR:"):
        return f"Windows Update check failed: {out[7:200]}"
    try:
        data = json.loads(out)
        pending = data.get("Pending", "?")
        hist = data.get("Recent") or []
        if isinstance(hist, dict):
            hist = [hist]
        result = f"Pending updates: {pending}\n"
        if hist:
            result += "Recently installed:\n"
            for h in hist:
                title = h.get("Title", "?")[:80]
                date = h.get("Date", "?")
                result += f"  • [{date}] {title}\n"
        return result.strip()
    except Exception:
        return out[:2000]


@registry.tool(
    name="pc.deep_scan",
    description=(
        "Run a full system health scan covering CPU/RAM/disk usage, temperatures, "
        "recent crashes and errors, startup programs, and disk space. "
        "Use this whenever someone asks you to speed up their PC, do a health check, "
        "diagnose slowness, or figure out what's wrong with their computer. "
        "After the scan: present findings as a prioritized issue list, worst first. "
        "Be specific: '92% disk full' not 'disk space is low'. "
        "Only report sections that have actual problems — skip clean ones. "
        "End with a short recommended action list."
    ),
)
def deep_scan() -> str:
    """
    Runs all major diagnostics concurrently and returns a consolidated health report.
    Calls the individual tool functions directly in parallel threads to save time.
    On Linux, Windows-only sections (crashes, startup, event logs) are skipped.
    """
    # Lazy imports to avoid circular deps at module load time
    from kai.tools.system.system_info import get_system_info
    from kai.tools.system.temps import get_temps
    from kai.tools.files.file_tools import get_disk_usage

    results: dict[str, str] = {}

    def run(key: str, fn, *args, **kwargs) -> None:
        try:
            results[key] = fn(*args, **kwargs)
        except Exception as exc:
            results[key] = f"(error: {exc})"

    # Cross-platform tasks always run
    disk_root = "C:\\" if _IS_WINDOWS else "/"
    tasks = [
        ("resources", get_system_info, [], {}),
        ("temps",     get_temps,       [], {}),
        ("disk",      get_disk_usage,  [], {"path": disk_root, "top_n": 8}),
    ]

    # Platform-specific tasks (both Windows and Linux now have real implementations)
    from kai.tools.system.crash_logs import get_crash_logs
    tasks.extend([
        ("crashes", get_crash_logs,       [], {}),
        ("startup", get_startup_programs, [], {}),
        ("errors",  get_event_logs,       [], {"hours": 48, "level": "Error"}),
    ])
    if not _IS_WINDOWS:
        tasks.append(("updates", _linux_updates, [], {}))

    threads = [
        threading.Thread(target=run, args=(key, fn, *args), kwargs=kwargs, daemon=True)
        for key, fn, args, kwargs in tasks
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    sections = ["=== SYSTEM HEALTH SCAN ==="]

    if "resources" in results:
        sections.append("-- Resources (CPU / RAM / Disk) --")
        sections.append(results["resources"])

    if "temps" in results:
        sections.append("-- Temperatures --")
        sections.append(results["temps"])

    if "crashes" in results:
        sections.append("-- Recent Crashes & Critical Events (7 days) --")
        sections.append(results["crashes"])

    if "errors" in results:
        sections.append("-- System Errors (last 48 hours) --")
        sections.append(results["errors"])

    if "startup" in results:
        sections.append("-- Startup Programs --")
        sections.append(results["startup"])

    if "updates" in results:
        sections.append("-- Pending Updates --")
        sections.append(results["updates"])

    if "disk" in results:
        sections.append(f"-- Disk Usage ({disk_root}) --")
        sections.append(results["disk"])

    return "\n\n".join(sections)


# ── Linux helpers ──────────────────────────────────────────────────────────────

def _linux_event_logs(hours: int = 24, level: str = "Error") -> str:
    level_map = {"error": "err", "warning": "warning", "warn": "warning", "both": "warning", "all": "info"}
    jlevel = level_map.get(level.strip().lower(), "err")
    try:
        r = subprocess.run(
            ["journalctl", f"-p{jlevel}", f"--since={hours} hours ago",
             "--no-pager", "-n25", "-o", "short-iso"],
            capture_output=True, text=True, timeout=10,
        )
        out = r.stdout.strip()
        if not out or out == "-- No entries --":
            return f"No {level} events in the last {hours} hours."
        return out
    except FileNotFoundError:
        return "journalctl not available — not a systemd system."
    except Exception as exc:
        return f"journalctl error: {exc}"


def _linux_startup_programs() -> str:
    """List systemd services enabled at boot."""
    try:
        # User services
        user_out = _run(["systemctl", "list-unit-files", "--user",
                         "--state=enabled", "--no-pager", "--no-legend"])
        # System services
        sys_out = _run(["systemctl", "list-unit-files",
                        "--state=enabled", "--no-pager", "--no-legend"])

        lines = []
        if sys_out:
            lines.append("System services (enabled at boot):")
            for line in sys_out.splitlines():
                parts = line.split()
                if parts:
                    lines.append(f"  • {parts[0]}")

        if user_out:
            lines.append("\nUser services (enabled at boot):")
            for line in user_out.splitlines():
                parts = line.split()
                if parts:
                    lines.append(f"  • {parts[0]}")

        # XDG autostart entries
        import os, glob
        autostart_dirs = [
            os.path.expanduser("~/.config/autostart"),
            "/etc/xdg/autostart",
        ]
        desktop_entries = []
        for d in autostart_dirs:
            for f in glob.glob(f"{d}/*.desktop"):
                try:
                    with open(f) as fh:
                        content = fh.read()
                    name_line = next((line for line in content.splitlines() if line.startswith("Name=")), "")
                    name = name_line.split("=", 1)[1] if "=" in name_line else os.path.basename(f)
                    desktop_entries.append(f"  • {name}  ({os.path.basename(f)})")
                except Exception:
                    pass
        if desktop_entries:
            lines.append("\nXDG Autostart (.desktop entries):")
            lines.extend(desktop_entries)

        return "\n".join(lines) if lines else "No enabled startup services found."
    except Exception as exc:
        return f"Error listing startup programs: {exc}"


def _linux_updates() -> str:
    """Check for pending apt/dnf/pacman updates."""
    import shutil

    if shutil.which("apt"):
        try:
            # apt-get -s upgrade simulates an upgrade — counts pending packages
            r = subprocess.run(
                ["apt-get", "-s", "upgrade"],
                capture_output=True, text=True, timeout=30,
            )
            lines = r.stdout.splitlines()
            upgraded = next((line for line in lines if "upgraded," in line), "")
            if upgraded:
                return f"apt: {upgraded.strip()}"
            # Fallback: apt list --upgradable
            r2 = subprocess.run(
                ["apt", "list", "--upgradable"],
                capture_output=True, text=True, timeout=15,
                env={**__import__("os").environ, "LANG": "C"},
            )
            pkgs = [line for line in r2.stdout.splitlines() if "/" in line]
            if pkgs:
                return f"{len(pkgs)} package(s) can be upgraded:\n" + "\n".join(f"  • {p.split('/')[0]}" for p in pkgs[:20])
            return "System is up to date (apt)."
        except Exception as exc:
            return f"apt check failed: {exc}"

    if shutil.which("dnf"):
        out = _run(["dnf", "check-update", "--quiet"], timeout=30)
        pkgs = [line for line in out.splitlines() if line and not line.startswith("Last")]
        return f"{len(pkgs)} DNF update(s) available." if pkgs else "System is up to date (dnf)."

    if shutil.which("pacman"):
        out = _run(["pacman", "-Qu"], timeout=30)
        pkgs = out.splitlines()
        return f"{len(pkgs)} pacman update(s) available." if pkgs else "System is up to date (pacman)."

    return "No supported package manager found (apt/dnf/pacman)."


def _linux_network_info() -> str:
    """Get network adapter info via `ip` command."""
    import json as _json
    try:
        addr_out = _run(["ip", "-j", "addr"])
        route_out = _run(["ip", "-j", "route"])
        dns_out = _run(["cat", "/etc/resolv.conf"])

        addrs = _json.loads(addr_out) if addr_out else []
        routes = _json.loads(route_out) if route_out else []

        # Default gateway
        gw = next((r.get("gateway", "") for r in routes if r.get("dst") == "default"), "")

        # DNS servers from resolv.conf
        dns = [line.split()[1] for line in dns_out.splitlines()
               if line.startswith("nameserver") and len(line.split()) >= 2]

        lines = []
        for iface in addrs:
            name = iface.get("ifname", "?")
            if name == "lo":
                continue
            state = iface.get("operstate", "")
            if state not in ("UP", "UNKNOWN"):
                continue
            # IPv4 addresses
            ipv4 = [f"{a['local']}/{a['prefixlen']}" for a in iface.get("addr_info", [])
                    if a.get("family") == "inet"]
            if not ipv4:
                continue
            lines.append(
                f"Interface: {name}  ({state})\n"
                f"  IP:      {', '.join(ipv4)}\n"
                f"  Gateway: {gw or 'n/a'}\n"
                f"  DNS:     {', '.join(dns[:2]) or 'n/a'}"
            )
        return "\n\n".join(lines) if lines else "No active network interfaces found."
    except Exception as exc:
        return f"Network info error: {exc}"
