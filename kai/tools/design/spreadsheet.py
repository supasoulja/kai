"""
design.spreadsheet — turns CSV text into a styled Excel workbook (.xlsx).

The model writes CSV because a small model writes it reliably; this makes it a real
spreadsheet. Numbers, money and percentages become typed cells with formats, formulas stay
live, the header is styled and frozen, and a Total row is bolded. Needs openpyxl (the
documents extra).
"""

import csv as csvlib
import io
import re

from kai.tools.design._common import (
    CATEGORY,
    CATEGORY_DESCRIPTION,
    THEMES,
    file_entry,
    output_dir,
    stem,
    theme_name,
)
from kai.tools.registry import ToolUnavailable, registry

_MAX_CSV = 500_000
FMT_MONEY = '"$"#,##0.00'
FMT_PERCENT = "0.0%"
_MONEY = re.compile(r"^-?\$-?[\d,]*\.?\d+$")
_PERCENT = re.compile(r"^-?[\d,]*\.?\d+%$")
_NUMBER = re.compile(r"^-?[\d,]*\.?\d+$")
_FORMULA = re.compile(r"^=[A-Za-z(+\-$\d]")
_FORMULA_REF = re.compile(r"\$?([A-Z]{1,3})\$?\d+")
# Functions that reach outside the workbook, and DDE's "cmd|". The CSV can carry words from
# a web page, and a sheet must not phone home or run anything when it's opened.
_UNSAFE_FORMULA = re.compile(
    r"\b(?:WEBSERVICE|HYPERLINK|FILTERXML|IMPORT\w*|CALL|REGISTER|RTD|DDE\w*|EXEC)\s*\(|\|",
    re.I,
)
# An unquoted "$1,200.00" reaches us as "$1" and "200.00".
_SPLIT_HEAD = re.compile(r"^-?\$?\d{1,3}(?:,\d{3})*$")
_SPLIT_TAIL = re.compile(r"^\d{3}(?:\.\d+)?$")


def _rows(text: str) -> list[list[str]]:
    rows = [[cell.strip() for cell in row] for row in csvlib.reader(io.StringIO(text.strip()))]
    return [row for row in rows if any(row)]


def _rejoin_thousands(row: list[str], width: int) -> list[str]:
    """Join numbers the CSV split at a thousands comma, only while the row is wider than the
    header, so real side-by-side numbers stay apart."""
    row = list(row)
    i = 0
    while len(row) > width and i < len(row) - 1:
        if _SPLIT_HEAD.match(row[i]) and _SPLIT_TAIL.match(row[i + 1]):
            row[i : i + 2] = [f"{row[i]},{row[i + 1]}"]
        else:
            i += 1
    return row


def _column_number(letters: str) -> int:
    """A1-style column letters to a number, without openpyxl's range check: a formula can
    mention names like LOG10 that only look like cell references."""
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n


def _typed(raw: str):
    """(value, number format) for one data cell."""
    if _MONEY.match(raw):
        return float(raw.replace("$", "").replace(",", "")), FMT_MONEY
    if _PERCENT.match(raw):
        return float(raw[:-1].replace(",", "")) / 100, FMT_PERCENT
    if _NUMBER.match(raw):
        whole = "." not in raw
        n = float(raw.replace(",", ""))
        value = int(n) if whole else n
        return value, ("#,##0" if whole else "#,##0.00") if "," in raw else None
    return raw, None


def _build(rows: list[list[str]], path, theme: str) -> dict:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    width = len(rows[0])
    notes: list[str] = []
    wb = Workbook()
    ws = wb.active
    ws.title = path.stem.replace("-", " ").capitalize()[:31]
    col_fmt: dict[int, str] = {}
    formulas = 0

    for r, raw_row in enumerate(rows, 1):
        row = raw_row if r == 1 else _rejoin_thousands(raw_row, width)
        if r > 1 and len(row) != width:
            notes.append(f"Row {r} has {len(row)} cells; the header has {width}.")
        for c, raw in enumerate(row, 1):
            value, fmt = (raw, None) if r == 1 else _typed(raw)
            if isinstance(value, str) and value.startswith("="):
                if r > 1 and _FORMULA.match(value) and not _UNSAFE_FORMULA.search(value):
                    formulas += 1
                else:
                    if r > 1 and _UNSAFE_FORMULA.search(value):
                        notes.append(
                            f"{get_column_letter(c)}{r} was kept as text: that formula reaches "
                            "outside the workbook."
                        )
                    value = value.lstrip("=")
            cell = ws.cell(row=r, column=c, value=value)
            if fmt:
                cell.number_format = fmt
                col_fmt.setdefault(c, fmt)

    # A formula takes the format of the columns it reads: =C2*D2 with D in dollars is dollars.
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            if cell.data_type != "f":
                continue
            cols = {_column_number(col) for col in _FORMULA_REF.findall(str(cell.value))}
            fmts = [col_fmt.get(col) for col in cols] + [col_fmt.get(cell.column)]
            fmt = next((f for f in fmts if f in (FMT_MONEY, FMT_PERCENT)), None)
            if fmt:
                cell.number_format = fmt
                col_fmt.setdefault(cell.column, fmt)

    accent = THEMES[theme][0].lstrip("#").upper()
    for cell in ws[1]:
        cell.font = Font(name="Calibri", bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=accent)
        cell.alignment = Alignment(vertical="center")
    for row in ws.iter_rows(min_row=2):
        if str(row[0].value or "").strip().lower().startswith("total"):
            for cell in row:
                cell.font = Font(name="Calibri", bold=True)
    for c in range(1, ws.max_column + 1):
        longest = max(
            len(str(ws.cell(row=r, column=c).value or "")) for r in range(1, ws.max_row + 1)
        )
        ws.column_dimensions[get_column_letter(c)].width = min(max(longest + 3, 10), 50)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    wb.save(path)

    result = {
        "files": [file_entry(path)],
        "sheet": ws.title,
        "rows": ws.max_row - 1,
        "columns": ws.max_column,
        "formulas": formulas,
    }
    if notes:
        result["notes"] = notes[:10]
    return result


@registry.tool(
    name="design.spreadsheet",
    description=(
        "Create an Excel spreadsheet (.xlsx) from CSV text: budgets, trackers, schedules, lists, "
        "tables of numbers. The first row is the header. Write money as $1200.00 and "
        "percentages as 15%, without thousands commas; they become real numbers. Cells starting "
        "with = stay live Excel formulas, e.g. =SUM(B2:B7) or =B2*C2. A last row named Total "
        "is bolded."
    ),
    parameters={
        "filename": {
            "type": "string",
            "description": "Short name without extension, e.g. 'monthly-budget'.",
            "required": True,
        },
        "csv": {
            "type": "string",
            "description": "The CSV text, header row first.",
            "required": True,
        },
        "theme": {
            "type": "string",
            "enum": list(THEMES),
            "description": "Header color.",
        },
    },
    category=CATEGORY,
    category_description=CATEGORY_DESCRIPTION,
    label="Building the spreadsheet",
    risk="caution",
)
def design_spreadsheet(filename: str, csv: str, theme: str = "") -> dict:
    try:
        import openpyxl  # noqa: F401
    except ImportError as e:
        raise ToolUnavailable(
            "design.spreadsheet needs openpyxl. Run: pip install -r requirements-documents.txt"
        ) from e
    if not (csv or "").strip():
        raise ValueError("csv is empty. Pass the table as CSV text, header row first.")
    if len(csv) > _MAX_CSV:
        raise ValueError(f"csv is {len(csv):,} characters; the limit is {_MAX_CSV:,}.")
    rows = _rows(csv)
    path = output_dir() / f"{stem(filename)}.xlsx"
    return _build(rows, path, theme_name(theme))
