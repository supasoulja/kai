"""Shared by the design.* tools: where output goes, how it is named, the color themes, and
the file entries the UI turns into previews."""

import re
from pathlib import Path

import kai.config as cfg
from kai.core._app_state import get_current_user_id

# name -> (accent, second accent, soft tint)
THEMES: dict[str, tuple[str, str, str]] = {
    "indigo": ("#4f46e5", "#06b6d4", "#eef2ff"),
    "teal": ("#0d9488", "#f59e0b", "#f0fdfa"),
    "coral": ("#e11d48", "#f97316", "#fff1f2"),
    "forest": ("#15803d", "#84cc16", "#f0fdf4"),
    "mono": ("#0f172a", "#64748b", "#f1f5f9"),
}
DEFAULT_THEME = "indigo"

CATEGORY = "design_and_documents"
CATEGORY_DESCRIPTION = (
    "Make things: design a flyer or poster, build a slide deck or presentation, write up a "
    "summary or report as a PDF, or create an Excel spreadsheet for a budget, tracker or list. "
    "Make me a flyer, turn this into slides, make a spreadsheet, put this in a PDF."
)


def theme_name(theme: str | None) -> str:
    """The theme to use: the one asked for when it exists, otherwise the default."""
    name = (theme or "").strip().lower()
    return name if name in THEMES else DEFAULT_THEME


def output_dir() -> Path:
    """This account's design folder."""
    folder = Path(cfg.DESIGNS_DIR) / str(get_current_user_id())
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def stem(filename: str) -> str:
    """A safe file stem from whatever name the model gave: extension dropped, slugged, so it
    can never carry a path."""
    name = re.sub(r"\.(pdf|png|xlsx|csv|md|html?)$", "", (filename or "").strip(), flags=re.I)
    # Underscores survive: the model repeats the name it passed, so the file should carry it.
    slug = re.sub(r"[^a-z0-9_]+", "-", name.lower()).strip("-_")[:60].rstrip("-_")
    return slug or "design"


def file_entry(path: Path, *, preview: bool = False) -> dict:
    """A made file, as the model and the UI see it. The version query keeps a browser from
    showing a cached copy after the same name is rendered again."""
    stat = path.stat()
    entry = {
        "name": path.name,
        "path": str(path),
        "url": f"/api/designs/{path.name}?v={stat.st_mtime_ns}",
        "bytes": stat.st_size,
    }
    if preview:
        entry["preview"] = True
    return entry
