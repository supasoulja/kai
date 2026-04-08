"""
files.* tools — find large/old files, check folder sizes, list recent files,
read file contents, list directory contents.
All read-only. No files are moved, deleted, or modified.
"""
import json
import subprocess
from pathlib import Path

from kai.tools.registry import registry


_TIMEOUT_SENTINEL = "__TIMEOUT__"

def _ps(cmd: str, timeout: int = 45) -> str | None:
    """
    Run a PowerShell command, return stdout or None on empty result.
    Returns _TIMEOUT_SENTINEL string on timeout so callers can give honest feedback.
    """
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
        out = r.stdout.strip()
        return out if out else None
    except subprocess.TimeoutExpired:
        return _TIMEOUT_SENTINEL
    except Exception:
        return None


def _safe_path(path: str) -> str:
    """Escape a path for safe interpolation inside a PowerShell single-quoted string.
    Single quotes are doubled ('') — the only escape needed in PS literal strings.
    Backticks are removed (PS escape/expansion character)."""
    p = path.strip().strip('"')   # remove surrounding double-quotes only
    p = p.replace("`", "")        # remove PS backtick (escape/expansion char)
    p = p.replace("'", "''")      # PS literal single-quote escape
    return p


def _default_home() -> str:
    return str(Path.home())


@registry.tool(
    name="files.disk_usage",
    description=(
        "Show how much disk space each top-level folder uses. "
        "Good for finding what's eating up space on a drive or directory. "
        "Use the drive path the user mentions (e.g. 'C:\\', 'D:\\', 'E:\\'). "
        "If the user says 'everywhere', 'all drives', or 'my whole PC', call this tool "
        "once per drive that likely has user data — check C:\\ first, then any others (D:\\, E:\\, etc.). "
        "Before calling: tell the user which drive(s) you're scanning — this can take 30-60 seconds per drive. "
        "After retrieving: flag the top 2-3 largest folders and whether they're expected "
        "(e.g. 'Games at 400 GB is normal if you have a lot installed') or worth investigating."
    ),
    parameters={
        "path": {
            "type": "string",
            "description": "Directory to analyze top-level subfolders of — use 'C:\\' for full drive (default: user home folder)",
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
    cmd = (
        f"Get-ChildItem -Path '{path}' -Directory -ErrorAction SilentlyContinue | "
        f"ForEach-Object {{ "
        f"  $sz = (Get-ChildItem -Path $_.FullName -Recurse -File -ErrorAction SilentlyContinue | "
        f"    Measure-Object -Property Length -Sum).Sum; "
        f"  [PSCustomObject]@{{Path=$_.FullName; SizeMB=[math]::Round(($sz ?? 0)/1MB,0)}} "
        f"}} | Sort-Object SizeMB -Descending | Select-Object -First {int(top_n)} | ConvertTo-Json"
    )
    out = _ps(cmd, timeout=90)
    if out == _TIMEOUT_SENTINEL:
        return f"Scan of '{path}' timed out. Try a more specific subfolder."
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


@registry.tool(
    name="files.find_large",
    description=(
        "Find the largest files in a directory, sorted by size descending. "
        "IMPORTANT: use whatever drive path the user mentions — 'C:\\', 'D:\\', 'E:\\', etc. "
        "To find the single biggest file on a drive, pass that drive path, min_size_mb=0, top_n=1. "
        "If the user says 'everywhere' or 'all drives', call this tool once per drive. "
        "Only set min_size_mb>0 if the user explicitly wants files over a certain size. "
        "Skips Windows system folders automatically when scanning a drive root. "
        "Before calling: tell the user which drive you're scanning — drive scans can take up to 2 minutes. "
        "After retrieving: point out obvious space hogs — game installs, video files, "
        "old installer archives (.iso, .exe, .msi in Downloads), large zip/rar files. "
        "Note which look safe to delete (old installers, duplicate downloads) vs. keep (active game installs)."
    ),
    parameters={
        "path": {
            "type": "string",
            "description": "Directory to search — e.g. 'C:\\', 'C:\\Users\\YourName', 'D:\\Games' (default: user home folder)",
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

    # When scanning a drive root, exclude slow/protected system directories
    p_lower = path.lower().rstrip("\\")
    is_drive_root = len(p_lower) <= 3 and p_lower.endswith(":")  # e.g. "c:" or "c:\"
    if is_drive_root:
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
        "PATH RULES: use whichever drive the user mentions ('C:\\', 'D:\\', 'E:\\', etc.). "
        "If they say 'on my PC', 'everywhere', or 'all drives', call this tool once per drive. "
        "Only use the home folder default if the user is asking about their personal files specifically. "
        "SIZE RULES: if the user says 'oldest files' without specifying a size, use min_size_mb=0 "
        "to find files of any size, not just files over 50 MB. "
        "Before calling: tell the user what you're about to scan and that it may take up to a minute. "
        "After retrieving: note which files look like safe cleanup candidates — "
        "old installers, archived downloads, unused media, old backups. "
        "Flag anything that might be important (old documents, project files) as 'check before deleting'. "
        "Always let the user confirm before suggesting any deletion."
    ),
    parameters={
        "path": {
            "type": "string",
            "description": "Directory to search — use 'C:\\' for the full drive, or default (home folder) for personal files",
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
