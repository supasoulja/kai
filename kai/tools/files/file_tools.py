"""
files.* tools — find large/old files, check folder sizes, list recent files,
read file contents, list directory contents.
All read-only. No files are moved, deleted, or modified.

Platform: Cross-platform.  PowerShell is used on Windows for disk scanning;
pure Python / shell commands are used on Linux.
"""
import json
import subprocess
from pathlib import Path

from kai.tools.registry import registry
from kai.tools._shell import run_powershell, TIMEOUT_SENTINEL as _TIMEOUT_SENTINEL
from kai.system.platform import IS_WINDOWS as _IS_WINDOWS


# Platform-aware path guidance baked into the tool descriptions. On Linux the
# model must NOT be told to probe 'C:\' — that hardcoded Windows guidance wasted
# the opening turn of a real session ("those paths don't exist"). The execution
# layer already handles each OS; this just keeps the *guidance* honest.
if _IS_WINDOWS:
    _PATH_EXAMPLES = "'C:\\', 'D:\\', 'E:\\'"
    _ALL_DRIVES_HINT = "call this tool once per drive — check C:\\ first, then any others (D:\\, E:\\, etc.)"
    _ROOT_PARAM_HINT = "use 'C:\\' for the full drive"
else:
    _PATH_EXAMPLES = "'/', '/home', '/mnt'"
    _ALL_DRIVES_HINT = "call this tool on '/' for the whole filesystem, or '/home' for user data"
    _ROOT_PARAM_HINT = "use '/' for the whole filesystem"


def _ps(cmd: str, timeout: int = 45) -> str | None:
    """
    Run a PowerShell command, return stdout or None on empty result.
    Returns _TIMEOUT_SENTINEL string on timeout so callers can give honest feedback.
    Only works on Windows.
    """
    res = run_powershell(cmd, timeout)
    if res.timed_out:
        return _TIMEOUT_SENTINEL
    return res.out or None


def _run_args(args: list[str], timeout: int = 45) -> str | None:
    """Run a command via a list of arguments (no shell). Returns stdout or None on error."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        out = r.stdout.strip()
        return out if out else None
    except subprocess.TimeoutExpired:
        return _TIMEOUT_SENTINEL
    except Exception:
        return None


def _safe_path(path: str) -> str:
    r"""Escape a path for safe interpolation inside a PowerShell single-quoted string.
    Single quotes are doubled ('') — the only escape needed in PS literal strings.
    Backticks are removed (PS escape/expansion character).
    All backslashes normalized to forward slashes (PS handles both, avoids
    trailing backslash escaping the closing quote)."""
    p = path.strip().strip('"')   # remove surrounding double-quotes only
    if _IS_WINDOWS:
        p = p.replace("`", "")        # remove PS backtick (escape/expansion char)
        p = p.replace("'", "''")      # PS literal single-quote escape
        p = p.replace("\\", "/")      # normalize to forward slash
    return p


def _is_drive_root(path: str) -> bool:
    """Check if a path is a drive root like C:/ or D:/ (Windows only)."""
    p = path.replace("\\", "/").rstrip("/")
    return len(p) == 2 and p[1] == ":" and p[0].isalpha()


def _is_fs_root(path: str) -> bool:
    """Check if a path is the filesystem root (/ on Linux, C:/ etc. on Windows)."""
    if _IS_WINDOWS:
        return _is_drive_root(path)
    return path.rstrip("/") == "" or path == "/"


def _default_home() -> str:
    return str(Path.home())


@registry.tool(
    name="files.disk_usage",
    description=(
        "Show how much disk space each top-level folder uses. "
        "Good for finding what's eating up space on a drive or directory. "
        f"Use the path the user mentions (e.g. {_PATH_EXAMPLES}). "
        f"If the user says 'everywhere', 'all drives', or 'my whole PC', {_ALL_DRIVES_HINT}. "
        "After retrieving: flag the top 2-3 largest folders and whether they're expected "
        "(e.g. 'Games at 400 GB is normal if you have a lot installed') or worth investigating."
    ),
    parameters={
        "path": {
            "type": "string",
            "description": f"Directory to analyze top-level subfolders of — {_ROOT_PARAM_HINT} (default: user home folder)",
        },
        "top_n": {
            "type": "integer",
            "description": "Number of largest folders to show (default 12)",
        },
    },
)
def get_disk_usage(path: str = "", top_n: int = 12) -> str:
    top_n = max(1, min(int(top_n), 100))
    path = _safe_path(path) or _default_home()

    # On filesystem roots, scan folders one at a time to avoid timeouts
    if _is_fs_root(path):
        if _IS_WINDOWS:
            return _disk_usage_chunked_win(path, top_n)
        else:
            return _disk_usage_linux(path, top_n)

    if _IS_WINDOWS:
        cmd = (
            f"Get-ChildItem -Path '{path}' -Directory -Force -ErrorAction SilentlyContinue | "
            f"ForEach-Object {{ "
            f"  $sz = (Get-ChildItem -Path $_.FullName -Recurse -File -ErrorAction SilentlyContinue | "
            f"    Measure-Object -Property Length -Sum).Sum; "
            f"  if ($null -eq $sz) {{ $sz = 0 }}; "
            f"  [PSCustomObject]@{{Path=$_.FullName; SizeMB=[math]::Round($sz/1MB,0)}} "
            f"}} | Sort-Object SizeMB -Descending | Select-Object -First {int(top_n)} | ConvertTo-Json"
        )
        out = _ps(cmd, timeout=120)
        if out == _TIMEOUT_SENTINEL:
            return f"Scan of '{path}' timed out (120s). Try a more specific subfolder."
        if not out:
            return f"Could not analyze '{path}'. Check the path exists and is accessible."
        try:
            data = json.loads(out)
            if isinstance(data, dict):
                data = [data]
            lines = [f"Folder sizes in {path}:"]
            for item in data:
                mb = item.get("SizeMB") or 0
                name = item.get("Path", "?")
                size_str = f"{mb/1024:.2f} GB" if mb >= 1024 else f"{mb} MB"
                lines.append(f"  {size_str:>10}  {name}")
            return "\n".join(lines)
        except Exception:
            return out[:2000]
    else:
        return _disk_usage_linux(path, top_n)


# ── Linux disk usage helpers ──────────────────────────────────────────

_SKIP_DIRS_LINUX = {
    "proc", "sys", "dev", "run", "snap", "lost+found", "mnt", "media",
}

def _disk_usage_linux(path: str, top_n: int = 12) -> str:
    """Scan folder sizes using du on Linux."""
    p = Path(path)
    if not p.exists():
        return f"Could not analyze '{path}'. Path does not exist."

    # du -s --block-size=1M on each top-level dir
    try:
        entries = sorted(p.iterdir())
    except PermissionError:
        return f"Permission denied: {path}"

    results: list[tuple[str, int]] = []
    skipped: list[str] = []

    for entry in entries:
        if not entry.is_dir():
            continue
        name_lower = entry.name.lower()
        if name_lower in _SKIP_DIRS_LINUX or name_lower.startswith("."):
            skipped.append(str(entry))
            continue
        out = _run_args(["du", "-s", "--block-size=1M", str(entry)], timeout=15)
        if out == _TIMEOUT_SENTINEL:
            skipped.append(f"{entry} (scan timed out)")
            continue
        if out:
            try:
                mb = int(out.split()[0])
                results.append((str(entry), mb))
            except (ValueError, IndexError):
                pass

    results.sort(key=lambda x: x[1], reverse=True)
    results = results[:top_n]

    lines = [f"Folder sizes in {path}:"]
    for folder, mb in results:
        size_str = f"{mb/1024:.2f} GB" if mb >= 1024 else f"{mb} MB"
        lines.append(f"  {size_str:>10}  {folder}")
    if skipped:
        lines.append(f"\nSkipped {len(skipped)} system/hidden folder(s)")
    return "\n".join(lines)


# ── Windows disk usage helpers ────────────────────────────────────────

_SKIP_FOLDERS_WIN = {
    "windows", "$recycle.bin", "system volume information",
    "recovery", "$winreagent", "$sysreset", "documents and settings",
}


def _disk_usage_chunked_win(drive: str, top_n: int = 12) -> str:
    """Scan a drive root one folder at a time. Skips system dirs, skips
    any folder that takes longer than 15s to enumerate."""
    list_cmd = (
        f"Get-ChildItem -Path '{drive}' -Directory -Force -ErrorAction SilentlyContinue | "
        f"Select-Object -ExpandProperty FullName"
    )
    out = _ps(list_cmd, timeout=10)
    if not out:
        return f"Could not list folders in '{drive}'."

    folders = [f.strip() for f in out.splitlines() if f.strip()]
    results: list[tuple[str, int]] = []
    skipped: list[str] = []

    for folder in folders:
        name = Path(folder).name.lower()
        if name in _SKIP_FOLDERS_WIN:
            skipped.append(folder)
            continue

        safe_folder = folder.replace("'", "''").replace("\\", "/")
        cmd = (
            f"$sz = (Get-ChildItem -Path '{safe_folder}' -Recurse -File "
            f"-ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum).Sum; "
            f"if ($null -eq $sz) {{ $sz = 0 }}; "
            f"[math]::Round($sz/1MB,0)"
        )
        size_out = _ps(cmd, timeout=15)
        if size_out == _TIMEOUT_SENTINEL:
            skipped.append(f"{folder} (scan timed out)")
            continue
        try:
            mb = int(size_out.strip()) if size_out and size_out.strip().lstrip("-").isdigit() else 0
        except (ValueError, AttributeError):
            mb = 0
        results.append((folder, mb))

    results.sort(key=lambda x: x[1], reverse=True)
    results = results[:top_n]

    lines = [f"Folder sizes in {drive}:"]
    for folder, mb in results:
        size_str = f"{mb/1024:.2f} GB" if mb >= 1024 else f"{mb} MB"
        lines.append(f"  {size_str:>10}  {folder}")
    if skipped:
        lines.append(f"\nSkipped {len(skipped)} system/slow folder(s)")
    return "\n".join(lines)


@registry.tool(
    name="files.find_large",
    description=(
        "Find the largest files in a directory, sorted by size descending. "
        f"IMPORTANT: use whatever path the user mentions — e.g. {_PATH_EXAMPLES}. "
        "To find the single biggest file, pass that path, min_size_mb=0, top_n=1. "
        f"If the user says 'everywhere' or 'all drives', {_ALL_DRIVES_HINT}. "
        "Only set min_size_mb>0 if the user explicitly wants files over a certain size. "
        "Skips system folders automatically when scanning a filesystem/drive root. "
        "After retrieving: point out obvious space hogs — game installs, video files, "
        "old installer archives (.iso, .exe, .msi in Downloads), large zip/rar files. "
        "Note which look safe to delete (old installers, duplicate downloads) vs. keep (active game installs)."
    ),
    parameters={
        "path": {
            "type": "string",
            "description": f"Directory to search — {_ROOT_PARAM_HINT}, or a subfolder (default: user home folder)",
        },
        "min_size_mb": {
            "type": "integer",
            "description": "Only show files larger than this many MB. Use 0 for no size filter (returns any size). Default 0.",
        },
        "top_n": {
            "type": "integer",
            "description": "Max number of results (default 10). Use 1 to find the single biggest file.",
        },
    },
)
def find_large_files(path: str = "", min_size_mb: int = 0, top_n: int = 10) -> str:
    top_n = max(1, min(int(top_n), 100))
    min_size_mb = max(0, min(int(min_size_mb), 1_000_000))
    path = _safe_path(path) or _default_home()
    min_bytes = min_size_mb * 1024 * 1024

    if _IS_WINDOWS:
        return _find_large_win(path, min_bytes, min_size_mb, top_n)
    else:
        return _find_large_linux(path, min_bytes, min_size_mb, top_n)


def _find_large_win(path: str, min_bytes: int, min_size_mb: int, top_n: int) -> str:
    # When scanning a drive root, exclude slow/protected system directories
    if _is_drive_root(path):
        exclude_filter = (
            "Where-Object { "
            "$_.FullName -notlike '*\\Windows\\*' -and "
            "$_.FullName -notlike '*\\System Volume Information\\*' -and "
            "$_.FullName -notlike '*\\$Recycle.Bin\\*' -and "
            "$_.FullName -notlike '*\\Recovery\\*' "
            "} | "
        )
    else:
        exclude_filter = ""

    size_filter = f"Where-Object {{$_.Length -gt {min_bytes}}} | " if min_bytes > 0 else ""

    cmd = (
        f"Get-ChildItem -Path '{path}' -Recurse -File -ErrorAction SilentlyContinue | "
        f"{exclude_filter}"
        f"{size_filter}"
        f"Sort-Object Length -Descending | "
        f"Select-Object -First {int(top_n)} FullName, "
        f"@{{N='SizeMB';E={{[math]::Round($_.Length/1MB,1)}}}} | "
        f"ConvertTo-Json"
    )
    out = _ps(cmd, timeout=120)
    if out == _TIMEOUT_SENTINEL:
        return (
            f"Scan of '{path}' timed out (120s). "
            "Try a more specific folder like 'C:\\Users\\YourName' or 'C:\\Games', "
            "or use files.disk_usage to find which top-level folder is biggest first."
        )
    if not out:
        return f"No files found in '{path}'."
    return _format_large_results(out, path, min_size_mb)


def _find_large_linux(path: str, min_bytes: int, min_size_mb: int, top_n: int) -> str:
    find_args = ["find", path]
    if _is_fs_root(path):
        for d in ("proc", "sys", "dev", "run"):
            find_args += ["-path", f"/{d}", "-prune", "-o"]
    find_args += ["-type", "f"]
    if min_bytes > 0:
        find_args += ["-size", f"+{min_size_mb}M"]
    find_args += ["-printf", "%s %p\n"]

    raw = _run_args(find_args, timeout=120)
    if raw == _TIMEOUT_SENTINEL:
        return f"Scan of '{path}' timed out (120s). Try a more specific subfolder."
    if not raw:
        return f"No files found in '{path}'."

    records: list[tuple[int, str]] = []
    for line in raw.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            try:
                records.append((int(parts[0]), parts[1]))
            except ValueError:
                pass
    records.sort(reverse=True)
    records = records[:top_n]

    header = (
        f"Largest files (>{min_size_mb} MB) in {path}:"
        if min_size_mb > 0
        else f"Largest files in {path}:"
    )
    lines = [header]
    for size, name in records:
        mb = round(size / 1_048_576, 1)
        lines.append(f"  {mb:>8.1f} MB  {name}")
    return "\n".join(lines)


def _format_large_results(out: str, path: str, min_size_mb: int) -> str:
    try:
        data = json.loads(out)
        if isinstance(data, dict):
            data = [data]
        header = (
            f"Largest files (>{min_size_mb} MB) in {path}:"
            if min_size_mb > 0
            else f"Largest files in {path}:"
        )
        lines = [header]
        for item in data:
            mb = item.get("SizeMB", 0)
            name = item.get("FullName", "?")
            lines.append(f"  {mb:>8.1f} MB  {name}")
        return "\n".join(lines)
    except Exception:
        return out[:2000]


@registry.tool(
    name="files.find_old",
    description=(
        "Find files that haven't been modified in a long time. "
        "Useful for identifying unused files to clean up. "
        f"PATH RULES: use whichever path the user mentions (e.g. {_PATH_EXAMPLES}). "
        f"If they say 'on my PC', 'everywhere', or 'all drives', {_ALL_DRIVES_HINT}. "
        "Only use the home folder default if the user is asking about their personal files specifically. "
        "SIZE RULES: if the user says 'oldest files' without specifying a size, use min_size_mb=0 "
        "to find files of any size, not just files over 50 MB. "
        "After retrieving: note which files look like safe cleanup candidates — "
        "old installers, archived downloads, unused media, old backups. "
        "Flag anything that might be important (old documents, project files) as 'check before deleting'. "
        "Always let the user confirm before suggesting any deletion."
    ),
    parameters={
        "path": {
            "type": "string",
            "description": f"Directory to search — {_ROOT_PARAM_HINT}, or default (home folder) for personal files",
        },
        "days": {
            "type": "integer",
            "description": "Find files not modified in this many days (default 365)",
        },
        "min_size_mb": {
            "type": "integer",
            "description": "Only show files larger than this many MB (default 50)",
        },
        "top_n": {
            "type": "integer",
            "description": "Max number of results (default 20)",
        },
    },
)
def find_old_files(
    path: str = "", days: int = 365, min_size_mb: int = 50, top_n: int = 20
) -> str:
    days = max(1, min(int(days), 3650))
    min_size_mb = max(0, min(int(min_size_mb), 1_000_000))
    top_n = max(1, min(int(top_n), 100))
    path = _safe_path(path) or _default_home()
    min_bytes = min_size_mb * 1024 * 1024

    if _IS_WINDOWS:
        cmd = (
            f"$cutoff = (Get-Date).AddDays(-{int(days)}); "
            f"Get-ChildItem -Path '{path}' -Recurse -File -ErrorAction SilentlyContinue | "
            f"Where-Object {{$_.LastWriteTime -lt $cutoff -and $_.Length -gt {min_bytes}}} | "
            f"Sort-Object LastWriteTime | "
            f"Select-Object -First {int(top_n)} FullName, "
            f"@{{N='LastModified';E={{$_.LastWriteTime.ToString('yyyy-MM-dd')}}}}, "
            f"@{{N='SizeMB';E={{[math]::Round($_.Length/1MB,1)}}}} | "
            f"ConvertTo-Json"
        )
        out = _ps(cmd, timeout=90)
        if out == _TIMEOUT_SENTINEL:
            return f"Scan of '{path}' timed out. Try a more specific subfolder."
        if not out:
            return f"No files older than {days} days (>{min_size_mb} MB) found in '{path}'."
        try:
            data = json.loads(out)
            if isinstance(data, dict):
                data = [data]
            lines = [f"Files not modified in {days}+ days (>{min_size_mb} MB) in {path}:"]
            for item in data:
                mb = item.get("SizeMB", 0)
                name = item.get("FullName", "?")
                last = item.get("LastModified", "?")
                lines.append(f"  {last}  {mb:>8.1f} MB  {name}")
            return "\n".join(lines)
        except Exception:
            return out[:2000]
    else:
        find_args = ["find", path, "-type", "f", "-mtime", f"+{days}"]
        if min_bytes > 0:
            find_args += ["-size", f"+{min_size_mb}M"]
        find_args += ["-printf", "%T+ %s %p\n"]
        raw = _run_args(find_args, timeout=90)
        if raw == _TIMEOUT_SENTINEL:
            return f"Scan of '{path}' timed out. Try a more specific subfolder."
        if not raw:
            return f"No files older than {days} days (>{min_size_mb} MB) found in '{path}'."
        records: list[tuple[str, str, float, str]] = []
        for line in raw.splitlines():
            parts = line.split(None, 2)
            if len(parts) >= 3:
                try:
                    date_str = parts[0][:10]
                    mb = round(int(parts[1]) / 1_048_576, 1)
                    records.append((parts[0], date_str, mb, parts[2]))
                except (ValueError, IndexError):
                    pass
        records.sort()
        records = records[:top_n]
        lines = [f"Files not modified in {days}+ days (>{min_size_mb} MB) in {path}:"]
        for _, date_str, mb, name in records:
            lines.append(f"  {date_str}  {mb:>8.1f} MB  {name}")
        return "\n".join(lines)


@registry.tool(
    name="files.recent",
    description="List recently modified files in a directory.",
    parameters={
        "path": {
            "type": "string",
            "description": "Directory to search (default: user home folder)",
        },
        "count": {
            "type": "integer",
            "description": "Number of recent files to show (default 15)",
        },
    },
)
def get_recent_files(path: str = "", count: int = 15) -> str:
    count = max(1, min(int(count), 100))
    path = _safe_path(path) or _default_home()

    if _IS_WINDOWS:
        cmd = (
            f"Get-ChildItem -Path '{path}' -Recurse -File -ErrorAction SilentlyContinue | "
            f"Sort-Object LastWriteTime -Descending | "
            f"Select-Object -First {int(count)} FullName, "
            f"@{{N='Modified';E={{$_.LastWriteTime.ToString('yyyy-MM-dd HH:mm')}}}}, "
            f"@{{N='SizeMB';E={{[math]::Round($_.Length/1MB,2)}}}} | "
            f"ConvertTo-Json"
        )
        out = _ps(cmd, timeout=60)
        if not out:
            return f"No files found in '{path}'."
        try:
            data = json.loads(out)
            if isinstance(data, dict):
                data = [data]
            lines = [f"Recently modified files in {path}:"]
            for item in data:
                mb = item.get("SizeMB", 0)
                name = item.get("FullName", "?")
                mod = item.get("Modified", "?")
                lines.append(f"  {mod}  {mb:>8.2f} MB  {name}")
            return "\n".join(lines)
        except Exception:
            return out[:2000]
    else:
        find_args = ["find", path, "-maxdepth", "3", "-type", "f",
                     "-printf", "%T+ %s %p\n"]
        raw = _run_args(find_args, timeout=60)
        if not raw or raw == _TIMEOUT_SENTINEL:
            return f"No files found in '{path}'."
        records: list[tuple[str, str, float, str]] = []
        for line in raw.splitlines():
            parts = line.split(None, 2)
            if len(parts) >= 3:
                try:
                    date_str = parts[0][:16].replace("+", " ")
                    mb = round(int(parts[1]) / 1_048_576, 2)
                    records.append((parts[0], date_str, mb, parts[2]))
                except (ValueError, IndexError):
                    pass
        records.sort(reverse=True)
        records = records[:count]
        lines = [f"Recently modified files in {path}:"]
        for _, date_str, mb, name in records:
            lines.append(f"  {date_str}  {mb:>8.2f} MB  {name}")
        return "\n".join(lines)


_MAX_LINES  = 400   # max lines returned per read
_MAX_CHARS  = 8000  # hard cap on total output chars

# Filenames that should never be read — even if they have a text extension.
# Prevents the model from being tricked into exfiltrating secrets.
_BLOCKED_FILENAMES = {
    ".env", ".env.local", ".env.production", ".env.development",
    "credentials.json", "service_account.json",
    "id_rsa", "id_ed25519", "id_ecdsa",
    ".netrc", ".npmrc",
}

# Directories that should never be read from (resolved, lowercased on Windows).
_BLOCKED_DIR_PARTS = {
    ".ssh", ".gnupg", ".aws", ".azure", ".config/gcloud",
}

_TEXT_EXTENSIONS = {
    ".py", ".pyw", ".txt", ".md", ".rst", ".json", ".jsonl", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".env", ".log", ".csv", ".tsv",
    ".js", ".ts", ".jsx", ".tsx", ".html", ".htm", ".css", ".scss",
    ".sh", ".bash", ".zsh", ".fish", ".bat", ".ps1", ".cmd",
    ".c", ".cpp", ".h", ".hpp", ".cs", ".java", ".go", ".rs",
    ".sql", ".xml", ".svg", ".tf", ".conf", ".config", ".gitignore",
}


@registry.tool(
    name="files.read",
    description=(
        "Read the contents of a text file. Use when the user asks to open, read, "
        "show, view, or check what's in a specific file — including source code, "
        "configs, logs, scripts, and notes. Returns up to 400 lines."
    ),
    parameters={
        "path": {
            "type": "string",
            "description": "Path to the file (absolute, or relative to user home). Required.",
        },
        "start_line": {
            "type": "integer",
            "description": "First line to return, 1-indexed (default: 1).",
        },
        "end_line": {
            "type": "integer",
            "description": "Last line to return, inclusive (default: start_line + 399).",
        },
    },
)
def read_file(path: str, start_line: int = 1, end_line: int = 0) -> str:
    path = path.strip().strip("'\"")
    p = Path(path).expanduser().resolve()

    if not p.exists():
        return f"File not found: {p}"
    if not p.is_file():
        return f"Path is not a file: {p}"
    if p.suffix.lower() not in _TEXT_EXTENSIONS:
        return (
            f"'{p.name}' doesn't look like a text file (extension: '{p.suffix}'). "
            "Only text/code/config files are supported."
        )
    # Block sensitive files by name
    if p.name.lower() in _BLOCKED_FILENAMES:
        return f"Blocked: '{p.name}' may contain secrets and cannot be read."
    # Block sensitive directories
    path_lower = str(p).lower().replace("\\", "/")
    if any(f"/{part}/" in path_lower or path_lower.endswith(f"/{part}") for part in _BLOCKED_DIR_PARTS):
        return f"Blocked: files inside '{p.parent.name}' cannot be read for security reasons."

    start_line = max(1, int(start_line))
    end_line   = int(end_line)
    if end_line <= 0:
        end_line = start_line + _MAX_LINES - 1

    try:
        raw_lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as e:
        return f"Could not read file: {e}"

    total = len(raw_lines)
    selected = raw_lines[start_line - 1 : end_line]

    header = f"File: {p}\nLines {start_line}–{min(end_line, total)} of {total}\n"
    body   = "\n".join(
        f"{start_line + i:>5}  {line}" for i, line in enumerate(selected)
    )

    result = header + "─" * 60 + "\n" + body
    if len(result) > _MAX_CHARS:
        result = result[:_MAX_CHARS] + f"\n… (truncated at {_MAX_CHARS} chars)"
    return result


@registry.tool(
    name="files.list",
    description=(
        "List the files and folders inside a directory. Use when the user wants to "
        "explore a folder, browse project structure, or find a file by looking around. "
        "Also use this to list installed Steam games by reading "
        "C:\\Program Files (x86)\\Steam\\steamapps\\common\\ or "
        "C:\\Program Files\\Steam\\steamapps\\common\\"
    ),
    parameters={
        "path": {
            "type": "string",
            "description": "Directory path to list (default: user home folder).",
        },
        "show_hidden": {
            "type": "boolean",
            "description": "Include hidden files/folders (names starting with .) — default false.",
        },
    },
)
def list_directory(path: str = "", show_hidden: bool = False) -> str:
    raw = path.strip().strip("'\"") if path.strip() else "~"
    p   = Path(raw).expanduser().resolve()

    if not p.exists():
        return f"Directory not found: {p}"
    if not p.is_dir():
        return f"Path is not a directory: {p}"

    try:
        entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
    except PermissionError:
        return f"Permission denied: {p}"

    lines = [f"Contents of {p}:", ""]
    dirs, files = [], []
    for e in entries:
        if not show_hidden and e.name.startswith("."):
            continue
        if e.is_dir():
            dirs.append(f"  📁  {e.name}/")
        else:
            try:
                size = e.stat().st_size
                sz = f"{size/1_048_576:.1f} MB" if size >= 1_048_576 else f"{size/1024:.1f} KB"
            except Exception:
                sz = "?"
            files.append(f"  📄  {e.name:<40} {sz}")

    lines += dirs + files
    if not dirs and not files:
        lines.append("  (empty)")
    lines.append(f"\n{len(dirs)} folder(s), {len(files)} file(s)")
    return "\n".join(lines)
