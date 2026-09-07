"""
Shared subprocess helpers for tool modules.

The PowerShell invocation (flags, UTF-8 decoding, timeout handling) used to be
copy-pasted as a private `_ps()` in four tool files — each with a slightly
different return shape; likewise a plain-argv `_run()` in three. The actual calls
live here now (`run_powershell`, `run_argv`); each tool keeps a thin adapter that
maps this structured result onto whatever shape its call sites expect.
"""

import subprocess
from dataclasses import dataclass

# Returned by file_tools._ps on timeout so callers can give honest feedback.
TIMEOUT_SENTINEL = "__TIMEOUT__"


@dataclass
class ShellResult:
    out: str  # stripped stdout ("" if none / on failure)
    err: str  # stripped stderr; "Timed out" or the exception text on failure
    timed_out: bool  # True if the process exceeded its timeout
    ok: bool  # True if the process actually ran (False on timeout/exception)


def run_powershell(cmd: str, timeout: int = 20) -> ShellResult:
    """Run a PowerShell command and return a structured result.

    Never raises — failures come back as a ShellResult with ok=False.
    """
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        return ShellResult(r.stdout.strip(), r.stderr.strip(), False, True)
    except subprocess.TimeoutExpired:
        return ShellResult("", "Timed out", True, False)
    except Exception as exc:
        return ShellResult("", str(exc), False, False)


def run_argv(args: list[str], timeout: int = 20) -> ShellResult:
    """Run a command from an argv list (no shell) and return a structured result.

    The argv companion to run_powershell — no shell means no quoting or injection
    concerns. Each tool file keeps a thin adapter that maps this onto whatever
    shape its call sites expect (str, str|None + TIMEOUT_SENTINEL, …), exactly as
    they do for run_powershell. Never raises — failures come back as ok=False.
    """
    try:
        r = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        return ShellResult(r.stdout.strip(), r.stderr.strip(), False, True)
    except subprocess.TimeoutExpired:
        return ShellResult("", "Timed out", True, False)
    except Exception as exc:
        return ShellResult("", str(exc), False, False)
