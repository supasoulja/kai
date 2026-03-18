"""
system.* operation tools — actions that modify system state.

These tools change the PC. Each one is labeled clearly and creates a
restore point first where appropriate. Always tell the user what you
are about to do before calling any tool in this file.
"""
import subprocess
import datetime
from kai.tools.registry import registry


def _ps(cmd: str, timeout: int = 30) -> tuple[str, str]:
    """Run PowerShell, return (stdout, stderr)."""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
        return r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return "", "Timed out"
    except Exception as exc:
        return "", str(exc)


@registry.tool(
    name="system.create_restore_point",
    description=(
        "Create a Windows System Restore Point so the user can undo any changes "
        "if something goes wrong. Always create a restore point before making any "
        "system changes like cleaning disk, modifying startup, or changing settings."
    ),
    parameters={
        "description": {
            "type": "string",
            "description": "Label for the restore point (e.g. 'Before Kai PC cleanup')",
        },
    },
)
def create_restore_point(description: str = "Kai checkpoint") -> str:
    label = description.replace("'", "").replace('"', "")[:80]
    cmd = (
        f"Checkpoint-Computer -Description '{label}' "
        f"-RestorePointType 'MODIFY_SETTINGS' -ErrorAction Stop"
    )
    stdout, stderr = _ps(cmd, timeout=60)
    if stderr and "error" in stderr.lower():
        return (
            f"Could not create restore point: {stderr[:200]}\n"
            "Note: System Protection must be enabled on C:\\ — "
            "check Control Panel → System → System Protection."
        )
    ts = datetime.datetime.now().strftime("%b %d %H:%M")
    return f"Restore point created: '{label}' at {ts}. Safe to proceed."


@registry.tool(
    name="system.clear_temp_files",
    description=(
        "Delete temporary files from the Windows Temp folder and user Temp folder "
        "to free up disk space. Safe to run — temp files are regenerated automatically. "
        "Create a restore point first before calling this."
    ),
)
def clear_temp_files() -> str:
    cmd = r"""
$paths = @($env:TEMP, $env:WINDIR + '\Temp')
$freed = 0
$errors = 0
foreach ($path in $paths) {
    if (Test-Path $path) {
        $files = Get-ChildItem -Path $path -Recurse -Force -ErrorAction SilentlyContinue |
                 Where-Object { -not $_.PSIsContainer }
        foreach ($f in $files) {
            try {
                $freed += $f.Length
                Remove-Item -Path $f.FullName -Force -ErrorAction Stop
            } catch { $errors++ }
        }
    }
}
$mb = [math]::Round($freed / 1MB, 1)
"Freed ${mb} MB. Could not delete $errors file(s) (in use or locked)."
"""
    stdout, stderr = _ps(cmd, timeout=60)
    if not stdout:
        return "Temp file cleanup ran but returned no output."
    return stdout.strip()


@registry.tool(
    name="system.disable_startup_program",
    description=(
        "Disable a specific program from running at Windows startup. "
        "This does NOT uninstall the program — it just stops it from auto-starting. "
        "Always confirm with the user before calling this."
    ),
    parameters={
        "program_name": {
            "type": "string",
            "description": "Exact name of the startup entry to disable (get this from pc.startup_programs)",
        },
    },
)
def disable_startup_program(program_name: str) -> str:
    # Use WMIC or Registry to disable — safest is to use the registry key approach
    name = program_name.replace("'", "").replace('"', "")
    cmd = (
        f"$regPaths = @("
        f"  'HKCU:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Run',"
        f"  'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Run'"
        f"); "
        f"$found = $false; "
        f"foreach ($p in $regPaths) {{ "
        f"  if ((Get-ItemProperty -Path $p -ErrorAction SilentlyContinue).'{name}') {{ "
        f"    Remove-ItemProperty -Path $p -Name '{name}' -ErrorAction Stop; "
        f"    $found = $true; "
        f"    Write-Output \"Disabled '$name' from startup ($p).\" "
        f"  }} "
        f"}}; "
        f"if (-not $found) {{ Write-Output \"Startup entry '$name' not found in registry. "
        f"It may be a shortcut-based entry — check Task Manager > Startup tab.\" }}"
    )
    stdout, stderr = _ps(cmd, timeout=15)
    if stderr and "error" in stderr.lower():
        return f"Error: {stderr[:200]}"
    return stdout or f"Startup entry '{name}' not found."


@registry.tool(
    name="system.run_disk_cleanup",
    description=(
        "Run Windows built-in Disk Cleanup (cleanmgr) to remove system junk files. "
        "This is the safe, official Windows cleanup tool. "
        "Tell the user this will open a cleanup process in the background."
    ),
)
def run_disk_cleanup() -> str:
    cmd = "Start-Process cleanmgr.exe -ArgumentList '/sagerun:1' -NoNewWindow"
    stdout, stderr = _ps(cmd, timeout=15)
    if stderr and "error" in stderr.lower():
        return f"Could not launch Disk Cleanup: {stderr[:200]}"
    return (
        "Windows Disk Cleanup has been started in the background. "
        "It will remove temporary files, thumbnails, and other system junk. "
        "This usually takes 1-5 minutes."
    )


@registry.tool(
    name="system.repair_files",
    description=(
        "Run Windows System File Checker (sfc /scannow) to scan for and repair "
        "corrupted or missing system files. Takes 5-15 minutes. "
        "Use this when crashes point to DLL faults, 0xe0434352 (.NET runtime errors), "
        "or other system-level failures. "
        "Report the exact outcome: whether corruption was found, repaired, or nothing was wrong."
    ),
)
def repair_files() -> str:
    stdout, stderr = _ps("sfc /scannow", timeout=900)
    if stderr and "error" in stderr.lower() and not stdout:
        return f"sfc /scannow failed to run: {stderr[:200]}"
    # sfc outputs to stdout; summarize the key line
    if stdout:
        lines = [l.strip() for l in stdout.splitlines() if l.strip()]
        # Find the result line (contains "found" or "did not find" or "repaired")
        for line in lines:
            low = line.lower()
            if any(k in low for k in ("found", "repaired", "could not", "did not find", "no integrity")):
                return line
        return lines[-1] if lines else "sfc /scannow completed (no output captured)."
    return "sfc /scannow ran but produced no output. Try running as administrator."


@registry.tool(
    name="system.kill_process",
    description=(
        "Kill a running process by name. Use this when a crash loop or stuck process "
        "needs to be cleared — e.g. tbs_browser.exe after a game crash, or a hung updater. "
        "Only call this after confirming the process is safe to terminate. "
        "Report whether the process was found and killed."
    ),
    parameters={
        "process_name": {
            "type": "string",
            "description": "Process name to kill, e.g. 'tbs_browser.exe'. Include .exe extension.",
        },
    },
)
def kill_process(process_name: str) -> str:
    name = process_name.strip().replace('"', '').replace("'", "")
    # Check if it's running first
    check_cmd = f"Get-Process -Name '{name.replace('.exe','')}' -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id"
    pids, _ = _ps(check_cmd, timeout=8)
    if not pids.strip():
        return f"Process '{name}' is not currently running."
    kill_cmd = f"Stop-Process -Name '{name.replace('.exe','')}' -Force -ErrorAction Stop"
    stdout, stderr = _ps(kill_cmd, timeout=10)
    if stderr and "error" in stderr.lower():
        return f"Could not kill '{name}': {stderr[:200]}"
    return f"Killed '{name}' (PID(s): {pids.strip()})."
