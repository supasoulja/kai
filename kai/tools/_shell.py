"""
Shared subprocess helpers for tool modules.

The PowerShell invocation (flags, UTF-8 decoding, timeout handling) used to be
copy-pasted as a private `_ps()` in four tool files — each with a slightly
different return shape. The actual call lives here now; each tool keeps a thin
`_ps()` adapter that maps this structured result onto whatever shape its call
sites expect.
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


def run_shell(cmd: str, timeout: int = 20) -> ShellResult:
    """POSIX companion to run_powershell — run a command through /bin/sh and
    return the same structured ShellResult.

    Never raises — failures come back as a ShellResult with ok=False. Note that
    a command which runs but exits non-zero still has ok=True (it *ran*); inspect
    `err` or do your own returncode handling for command-level failure.
    """
    try:
        r = subprocess.run(
            ["/bin/sh", "-c", cmd],
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
