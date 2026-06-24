"""
system.crashes — recent Windows Event Log errors and critical events.

Pulls from the System and Application logs. Filters out noise (DCOM 10016,
driver verifier chatter, etc.) to surface actual problems.

Platform: Windows only (uses PowerShell / Windows Event Log).
"""
import subprocess
import json

from kai.tools.registry import registry
from kai.system.platform import IS_WINDOWS as _IS_WINDOWS

# Event sources that are usually noise — skip them
_NOISE_SOURCES = {
    "DCOM", "DistributedCOM", "Microsoft-Windows-TPM-WMI",
    "Microsoft-Windows-WMI", "Microsoft-Windows-Hyper-V-Hypervisor",
    "Microsoft-Windows-Hyper-V-Worker",
}

# Event IDs that are usually noise
_NOISE_IDS = {10016, 10010, 7031}


@registry.tool(
    name="system.crashes",
    description=(
        "Check system logs for recent errors, warnings, and crashes. "
        "On Windows: searches Event Log (System + Application) for the past 7 days. "
        "On Linux: searches the systemd journal for errors and critical events since last boot. "
        "Filters common noise to surface real problems. "
        "After retrieving results: for each error, service failure, or event found — "
        "(1) explain in plain language what the error code or source means, "
        "(2) describe the most common reasons it occurs, "
        "(3) if unsure about any error code or service name, call search.web to look it up "
        "before responding. Never guess at error code meanings."
    ),
)
def get_crash_logs() -> str:
    if not _IS_WINDOWS:
        return _journalctl_errors(priority="err", since="7 days ago")
    events = _fetch_events(days=7, max_events=20)
    events = _filter_noise(events)

    if not events:
        return "No significant errors or crashes in the past 7 days."

    lines = [f"Recent system events ({len(events)} found, past 7 days):\n"]
    for e in events[:10]:  # show top 10 after filtering
        time_str = e.get("time", "?")[:19].replace("T", " ")
        level    = e.get("level", "?")
        source   = e.get("source", "?")
        msg      = e.get("message", "")[:120].replace("\n", " ").strip()
        lines.append(f"[{time_str}] {level.upper()} — {source}\n  {msg}")

    return "\n\n".join(lines)


def _fetch_events(days: int, max_events: int) -> list[dict]:
    ps = f"""
$start = (Get-Date).AddDays(-{days})
$events = Get-WinEvent -FilterHashtable @{{
    LogName=@('System','Application')
    Level=@(1,2,3)
    StartTime=$start
}} -MaxEvents {max_events} -ErrorAction SilentlyContinue 2>$null

if ($events) {{
    $events | ForEach-Object {{
        [PSCustomObject]@{{
            time    = $_.TimeCreated.ToString('o')
            level   = switch($_.Level) {{ 1 {{'critical'}} 2 {{'error'}} 3 {{'warning'}} default {{'info'}} }}
            source  = $_.ProviderName
            id      = $_.Id
            message = $_.Message
        }}
    }} | ConvertTo-Json -Compress
}}
"""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=15,
            encoding="utf-8", errors="replace",
        )
        if not result.stdout.strip():
            return []
        raw = result.stdout.strip()
        data = json.loads(raw)
        if isinstance(data, dict):
            data = [data]
        return data
    except Exception:
        return []


def _filter_noise(events: list[dict]) -> list[dict]:
    clean = []
    for e in events:
        source = e.get("source", "")
        event_id = e.get("id", 0)
        if source in _NOISE_SOURCES:
            continue
        if event_id in _NOISE_IDS:
            continue
        clean.append(e)
    return clean


# ── GPU crash tool ─────────────────────────────────────────────────────────────

@registry.tool(
    name="system.gpu_crashes",
    description=(
        "Find GPU crash and driver timeout events (TDR) from the past N days. "
        "Use when the user reports screen going black, GPU driver crash, display driver "
        "stopped responding, or random restarts during gaming. "
        "Checks Event Viewer for TDR events (ID 4101), GPU hardware errors (ID 141), "
        "and scans for recent minidump files in C:\\Windows\\Minidump\\."
    ),
    parameters={
        "days": {
            "type": "integer",
            "description": "How many days back to search (default 30)",
        },
    },
)
def get_gpu_crashes(days: int = 30) -> str:
    if not _IS_WINDOWS:
        return _linux_gpu_crashes(days)
    ps = f"""
$start = (Get-Date).AddDays(-{int(days)})
$results = @()

# TDR events: display driver timeout/recovery (ID 4101) and GPU hardware errors (ID 141)
$tdrs = Get-WinEvent -FilterHashtable @{{
    LogName = 'System'
    Id      = @(4101, 141)
    StartTime = $start
}} -ErrorAction SilentlyContinue 2>$null

if ($tdrs) {{
    $results += $tdrs | ForEach-Object {{
        [PSCustomObject]@{{
            time    = $_.TimeCreated.ToString('yyyy-MM-dd HH:mm')
            id      = $_.Id
            source  = $_.ProviderName
            message = $_.Message -replace '\\s+', ' '
        }}
    }}
}}

# AMD/display driver errors from System log
$amd = Get-WinEvent -FilterHashtable @{{
    LogName   = 'System'
    Level     = @(1, 2)
    StartTime = $start
}} -MaxEvents 200 -ErrorAction SilentlyContinue 2>$null |
Where-Object {{ $_.ProviderName -match 'amd|display|video|gpu|dxgkrnl|atikmpag|amdkmdag' }}

if ($amd) {{
    $results += $amd | ForEach-Object {{
        [PSCustomObject]@{{
            time    = $_.TimeCreated.ToString('yyyy-MM-dd HH:mm')
            id      = $_.Id
            source  = $_.ProviderName
            message = ($_.Message -replace '\\s+', ' ').Substring(0, [Math]::Min(200, $_.Message.Length))
        }}
    }}
}}

# Minidumps
$dumps = Get-ChildItem -Path 'C:\\Windows\\Minidump' -Filter '*.dmp' -ErrorAction SilentlyContinue |
    Where-Object {{ $_.LastWriteTime -gt $start }} |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 10 Name, @{{N='Date';E={{$_.LastWriteTime.ToString('yyyy-MM-dd HH:mm')}}}}, @{{N='SizeMB';E={{[math]::Round($_.Length/1MB,1)}}}}

$output = [PSCustomObject]@{{
    events = $results | Sort-Object time -Descending | Select-Object -First 20
    minidumps = $dumps
}}
$output | ConvertTo-Json -Depth 4 -Compress
"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=20,
            encoding="utf-8", errors="replace",
        )
        raw = r.stdout.strip()
        if not raw:
            return f"No GPU crash events found in the past {days} days."
        data = json.loads(raw)

        lines = []

        events = data.get("events") or []
        if isinstance(events, dict):
            events = [events]
        if events:
            lines.append(f"GPU/driver events (past {days} days):")
            for e in events:
                id_label = {4101: "TDR — driver timeout", 141: "GPU hardware error"}.get(e.get("id"), f"ID {e.get('id')}")
                msg = e.get("message", "")[:180].replace("\n", " ")
                lines.append(f"  [{e.get('time')}] {id_label} ({e.get('source')})\n    {msg}")
        else:
            lines.append(f"No GPU TDR or hardware error events in the past {days} days.")

        dumps = data.get("minidumps") or []
        if isinstance(dumps, dict):
            dumps = [dumps]
        if dumps:
            lines.append(f"\nRecent minidumps (C:\\Windows\\Minidump):")
            for d in dumps:
                lines.append(f"  {d.get('Date')}  {d.get('Name')}  ({d.get('SizeMB')} MB)")
        else:
            lines.append("\nNo recent minidumps found.")

        return "\n".join(lines)
    except Exception as e:
        return f"Error checking GPU crashes: {e}"


# ── Game crash tool ────────────────────────────────────────────────────────────

# Known non-game Windows processes to exclude from Event ID 1000 results
_SYSTEM_PROCS = {
    "svchost.exe", "explorer.exe", "taskhostw.exe", "runtimebroker.exe",
    "searchhost.exe", "sihost.exe", "ctfmon.exe", "dwm.exe", "werfault.exe",
    "microsoftedge.exe", "msedge.exe", "backgroundtaskhost.exe",
}


@registry.tool(
    name="system.game_crashes",
    description=(
        "Find application and game crashes without needing to know where the logs are. "
        "Use when a game or program crashes to desktop, freezes, or throws an error code. "
        "Searches three sources automatically: "
        "(1) Windows Application Event Log ID 1000 — catches every crash on the system with exe name, "
        "faulting module, and exception code; "
        "(2) Windows Error Reporting (WER) report files; "
        "(3) Unity/Unreal game log files in LocalLow and Documents. "
        "No folder paths needed — pass only days and optionally a game name filter."
    ),
    parameters={
        "days": {
            "type": "integer",
            "description": "How many days back to search (default 7)",
        },
        "game_name": {
            "type": "string",
            "description": "Optional: filter to a specific app or game name (partial match, e.g. 'elden' or 'cyberpunk')",
        },
    },
)
def get_game_crashes(days: int = 7, game_name: str = "") -> str:
    if not _IS_WINDOWS:
        return _linux_game_crashes(days, game_name)
    game_filter = game_name.strip().lower()
    ps = f"""
$start = (Get-Date).AddDays(-{int(days)})
$results = @()

# ── Source 1: Event ID 1000 (Application Error) ──────────────────────────────
# Windows logs every application crash here automatically — no folder knowledge needed.
$appErrors = Get-WinEvent -FilterHashtable @{{
    LogName   = 'Application'
    Id        = 1000
    StartTime = $start
}} -MaxEvents 100 -ErrorAction SilentlyContinue 2>$null

if ($appErrors) {{
    $appErrors | ForEach-Object {{
        $msg = $_.Message
        # Extract fields from the structured message
        $appName      = if ($msg -match 'Faulting application name: ([^,\\r\\n]+)') {{ $Matches[1].Trim() }} else {{ '?' }}
        $faultMod     = if ($msg -match 'Faulting module name: ([^,\\r\\n]+)') {{ $Matches[1].Trim() }} else {{ '?' }}
        $exCode       = if ($msg -match 'Exception code: (0x[0-9a-fA-F]+)') {{ $Matches[1] }} else {{ '?' }}
        $results += [PSCustomObject]@{{
            source  = 'EventLog'
            date    = $_.TimeCreated.ToString('yyyy-MM-dd HH:mm')
            app     = $appName
            module  = $faultMod
            code    = $exCode
            detail  = ''
        }}
    }}
}}

# ── Source 2: WER report files ────────────────────────────────────────────────
$werBase = @(
    "$env:LOCALAPPDATA\\Microsoft\\Windows\\WER\\ReportArchive",
    "$env:ProgramData\\Microsoft\\Windows\\WER\\ReportArchive"
)
foreach ($base in $werBase) {{
    if (Test-Path $base) {{
        Get-ChildItem -Path $base -Directory -ErrorAction SilentlyContinue |
        Where-Object {{ $_.LastWriteTime -gt $start }} |
        ForEach-Object {{
            $wer = Join-Path $_.FullName 'Report.wer'
            if (Test-Path $wer) {{
                $lines = Get-Content $wer -ErrorAction SilentlyContinue
                $appName  = ($lines | Select-String 'AppName=(.+)' | Select-Object -First 1) -replace '.*AppName=',''
                $faultMod = ($lines | Select-String 'ModuleName=(.+)' | Select-Object -First 1) -replace '.*ModuleName=',''
                $exCode   = ($lines | Select-String 'ExceptionCode=(.+)' | Select-Object -First 1) -replace '.*ExceptionCode=',''
                $results += [PSCustomObject]@{{
                    source = 'WER'
                    date   = $_.LastWriteTime.ToString('yyyy-MM-dd HH:mm')
                    app    = $appName.Trim()
                    module = $faultMod.Trim()
                    code   = $exCode.Trim()
                    detail = $_.FullName
                }}
            }}
        }}
    }}
}}

# ── Source 3: Game engine log files (Unity = LocalLow, Unreal = LocalAppData) ─
$gameLogs = @()
$localLow = "$env:USERPROFILE\\AppData\\LocalLow"
if (Test-Path $localLow) {{
    $gameLogs += Get-ChildItem -Path $localLow -Recurse -File -ErrorAction SilentlyContinue `
        -Include 'output_log.txt','*crash*','Player.log' |
        Where-Object {{ $_.LastWriteTime -gt $start -and $_.Length -gt 100 }}
}}
$myGames = "$env:USERPROFILE\\Documents\\My Games"
if (Test-Path $myGames) {{
    $gameLogs += Get-ChildItem -Path $myGames -Recurse -File -ErrorAction SilentlyContinue `
        -Include '*crash*','*.log' |
        Where-Object {{ $_.LastWriteTime -gt $start -and $_.Length -gt 100 }}
}}
$gameLogs | Sort-Object LastWriteTime -Descending | Select-Object -First 8 | ForEach-Object {{
    $results += [PSCustomObject]@{{
        source = 'LogFile'
        date   = $_.LastWriteTime.ToString('yyyy-MM-dd HH:mm')
        app    = $_.Directory.Parent.Name
        module = ''
        code   = ''
        detail = $_.FullName
    }}
}}

$results | Sort-Object date -Descending | ConvertTo-Json -Compress
"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace",
        )
        raw = r.stdout.strip()
        if not raw:
            return f"No crash reports found in the past {days} days."

        data = json.loads(raw)
        if isinstance(data, dict):
            data = [data]

        # Filter out known Windows system processes from Event Log results
        data = [
            d for d in data
            if not (d.get("source") == "EventLog" and d.get("app", "").lower() in _SYSTEM_PROCS)
        ]

        # Apply optional game name filter
        if game_filter:
            data = [
                d for d in data
                if game_filter in d.get("app", "").lower()
                or game_filter in d.get("detail", "").lower()
            ]

        if not data:
            suffix = f" matching '{game_name}'" if game_filter else ""
            return f"No crash reports found in the past {days} days{suffix}."

        lines = [f"Crash reports (past {days} days, {len(data)} found):"]
        for item in data:
            src    = item.get("source", "?")
            date   = item.get("date", "?")
            app    = item.get("app", "?")
            module = item.get("module", "")
            code   = item.get("code", "")
            detail = item.get("detail", "")[:120]

            summary = f"  [{date}] {src} — {app}"
            if module:
                summary += f"\n    Faulting module: {module}"
            if code:
                summary += f"  |  Exception: {code}"
            if detail and src in ("WER", "LogFile"):
                summary += f"\n    Path: {detail}"
            lines.append(summary)

        if any(d.get("code") for d in data):
            lines.append(
                "\nTo investigate: search.web with the exception code + game name "
                "(e.g. '0xc0000005 Elden Ring fix')."
            )
        return "\n".join(lines)
    except Exception as e:
        return f"Error checking game crashes: {e}"


# ── Linux helpers ──────────────────────────────────────────────────────────────

def _journalctl_errors(priority: str = "err", since: str = "24 hours ago", max_lines: int = 25) -> str:
    """
    Pull errors from the systemd journal.
    priority: journalctl -p level — "err", "crit", "warning", etc.
    since: passed to --since (e.g. "7 days ago", "boot")
    """
    try:
        since_arg = ["-b"] if since == "boot" else [f"--since={since}"]
        r = subprocess.run(
            ["journalctl", f"-p{priority}", *since_arg, "--no-pager",
             f"-n{max_lines}", "-o", "short-iso"],
            capture_output=True, text=True, timeout=10,
        )
        out = r.stdout.strip()
        if not out or out == "-- No entries --":
            return f"No {priority}+ events found since {since}."
        lines = out.splitlines()
        # Filter kernel spam that's usually harmless
        _noise = {"microcode", "EDAC", "ACPI Warning", "pcie_aspm"}
        lines = [l for l in lines if not any(n in l for n in _noise)]
        return "\n".join(lines[:max_lines]) if lines else f"No significant {priority}+ events."
    except FileNotFoundError:
        return "journalctl not found — not a systemd system?"
    except Exception as exc:
        return f"journalctl error: {exc}"


def _linux_gpu_crashes(days: int = 30) -> str:
    """Check dmesg and journal for GPU hangs, resets, and driver errors on Linux."""
    results = []

    # dmesg GPU errors
    try:
        r = subprocess.run(
            ["dmesg", "--level=err,crit", "--notime", "-T"],
            capture_output=True, text=True, timeout=10,
        )
        gpu_lines = [
            l for l in r.stdout.splitlines()
            if any(k in l.lower() for k in ("gpu", "amdgpu", "radeon", "nvidia", "drm", "reset", "hang", "timeout"))
        ]
        if gpu_lines:
            results.append("dmesg GPU errors:")
            results.extend(gpu_lines[-20:])
    except Exception:
        pass

    # Journal GPU errors
    try:
        r = subprocess.run(
            ["journalctl", "-b", "--no-pager", "-n50", "-o", "short-iso",
             "-p", "err", "--grep", "amdgpu|radeon|nvidia|drm|gpu"],
            capture_output=True, text=True, timeout=10,
        )
        out = r.stdout.strip()
        if out and out != "-- No entries --":
            results.append("\nJournal GPU errors (this boot):")
            results.append(out)
    except Exception:
        pass

    if not results:
        return f"No GPU crash events found in the past {days} days."
    return "\n".join(results)


def _linux_game_crashes(days: int = 7, game_name: str = "") -> str:
    """Check ~/.steam logs and journal for game crashes on Linux."""
    import os, glob as _glob
    results = []

    # Steam crash logs
    steam_logs = os.path.expanduser("~/.steam/steam/logs")
    if os.path.isdir(steam_logs):
        for log_file in _glob.glob(f"{steam_logs}/*.log"):
            try:
                with open(log_file) as f:
                    content = f.read()
                if "crash" in content.lower() or "exception" in content.lower():
                    fname = os.path.basename(log_file)
                    if not game_name or game_name.lower() in content.lower():
                        results.append(f"Steam log: {fname}")
                        # Show last few crash lines
                        crash_lines = [l for l in content.splitlines() if "crash" in l.lower() or "exception" in l.lower()]
                        results.extend(crash_lines[-5:])
            except Exception:
                pass

    # ~/.local/share/Steam game crash reports
    crash_dir = os.path.expanduser("~/.local/share/Steam/ubuntu12_64/dumps")
    if os.path.isdir(crash_dir):
        dumps = sorted(_glob.glob(f"{crash_dir}/*.dmp"), key=os.path.getmtime, reverse=True)[:5]
        if dumps:
            results.append(f"\nRecent crash dumps ({crash_dir}):")
            for d in dumps:
                mtime = subprocess.run(["date", "-r", d, "+%Y-%m-%d %H:%M"], capture_output=True, text=True).stdout.strip()
                results.append(f"  {mtime}  {os.path.basename(d)}")

    # Journal: segfaults and killed processes
    try:
        r = subprocess.run(
            ["journalctl", f"--since={days} days ago", "--no-pager", "-n30",
             "-o", "short-iso", "--grep", "segfault|killed process|core dumped|Out of memory"],
            capture_output=True, text=True, timeout=10,
        )
        out = r.stdout.strip()
        if out and out != "-- No entries --":
            lines = out.splitlines()
            if game_name:
                lines = [l for l in lines if game_name.lower() in l.lower()]
            if lines:
                results.append(f"\nJournal crashes (past {days} days):")
                results.extend(lines[:15])
    except Exception:
        pass

    if not results:
        suffix = f" for '{game_name}'" if game_name else ""
        return f"No game crash reports found in the past {days} days{suffix}."
    return "\n".join(results)
