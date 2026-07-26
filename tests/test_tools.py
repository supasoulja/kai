"""
Unit tests for all tools — no Ollama, no network.
Each tool is tested for its core logic, not model behavior.
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("KAI_TEST_MODE", "1")

# Redirect DB to a temp file so tool tests don't touch real data
import kai.config as cfg

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
cfg.DB_PATH = Path(_tmp.name)

from kai.store.db import _reset_for_tests

_reset_for_tests()

from kai.tools.knowledge.notes import list_notes, save_note, search_notes
from kai.tools.registry import ToolRegistry
from kai.tools.system.system_info import get_system_info
from kai.tools.system.time_tool import get_time
from kai.tools.web.search import _ddg_search, _strip_tags, web_search

# ── Registry ─────────────────────────────────────────────────────────────────


def test_registry_registers_and_lists():
    reg = ToolRegistry()

    @reg.tool(name="test.ping", description="Ping tool.")
    def ping():
        return "pong"

    assert "test.ping" in reg.list_tools()


def test_tool_inline_metadata_populates_central_tables():
    """A tool can declare its own category/label/risk inline; the registry
    reflects them into the central tables so the confirm gate, semantic
    selection, and the metadata audit all work without editing core dicts.
    This is the pluggability seam for marketplace packs."""
    from kai.tools.registry import _TOOL_CATEGORIES, _TOOL_RISK, TOOL_LABELS, ToolRegistry

    reg = ToolRegistry()

    @reg.tool(
        name="packtest.do_thing",
        description="Pluggable tool declared inline.",
        category="packtest_domain",
        category_description="Test domain for a pluggable pack tool.",
        label="Doing the thing",
        risk="caution",
    )
    def do_thing():
        return "done"

    try:
        assert reg.label_for("packtest.do_thing") == "Doing the thing"
        assert reg.risk_for("packtest.do_thing") == "caution"
        assert "packtest.do_thing" in _TOOL_CATEGORIES["packtest_domain"]["tools"]
        # New category carries a description for the semantic-selection embedding.
        assert _TOOL_CATEGORIES["packtest_domain"]["description"]
        # An unknown risk tier is rejected loudly, not silently accepted.
        with pytest.raises(ValueError):

            @reg.tool(name="packtest.bad", description="x", risk="nope")
            def bad():
                return "x"
    finally:
        # Leave the shared module-level tables clean for the other tests.
        TOOL_LABELS.pop("packtest.do_thing", None)
        _TOOL_RISK.pop("packtest.do_thing", None)
        _TOOL_CATEGORIES.pop("packtest_domain", None)


def test_every_tool_has_label_and_category():
    """Each registered tool must have a UI label and belong to a category.

    Guards the single-source metadata in kai/tools/registry.py: adding a tool
    without a label/category should fail here, not silently degrade at runtime.
    """
    import kai.tools  # noqa: F401 — fires every @registry.tool decorator
    from kai.tools.registry import registry

    audit = registry.audit_metadata()
    assert not audit["missing_label"], f"tools missing a label: {audit['missing_label']}"
    assert not audit["uncategorized"], f"tools missing a category: {audit['uncategorized']}"


def test_self_list_tools_reflects_live_registry():
    """self.list_tools enumerates the real registry (so Kai stops hallucinating
    tools). Its output must name actual registered tools, including itself."""
    import kai.tools  # noqa: F401 — register every tool
    from kai.tools.registry import registry
    from kai.tools.system.self_inspect import list_tools

    out = list_tools()
    assert "self.list_tools" in out  # it lists itself
    # Every namespace in the registry shows up in the rendered inventory.
    namespaces = {n.split(".")[0] for n in registry.list_tools()}
    for ns in namespaces:
        assert f"{ns}.*" in out

    # Namespace filter narrows the output and rejects unknowns gracefully.
    only_self = list_tools(namespace="self")
    assert "self.list_tools" in only_self and "system.*" not in only_self
    assert "No tools in namespace" in list_tools(namespace="does_not_exist")


def test_risk_tiers_are_consistent():
    """Risk tiers are the single source of truth for the confirm gate.

    Every risk-table entry must name a real tool (no drift), destructive tools
    must include the genuinely irreversible ones, and read-only tools must stay
    'safe' so Kai keeps running them without asking.
    """
    import kai.tools  # noqa: F401 — registers every tool
    from kai.tools.registry import confirm_tool_names, registry

    assert registry.audit_metadata()["stale_risk"] == [], "risk table names unknown tools"

    confirm = confirm_tool_names()
    # self.apply_persona_update is destructive: self-modification must be gated so
    # Kai can never rewrite her own persona without explicit user approval.
    for t in (
        "lxc.delete",
        "system.kill_process",
        "docs.delete",
        "system.repair_files",
        "self.apply_persona_update",
    ):
        assert registry.risk_for(t) == "destructive"
        assert t in confirm
    # Read-only tools stay safe → they auto-run, never gated.
    for t in ("weather.current", "system.temps", "lxc.list", "search.web"):
        assert registry.risk_for(t) == "safe"
        assert t not in confirm
    # Reversible writes are 'caution', not gated.
    assert registry.risk_for("lxc.create") == "caution"
    assert "lxc.create" not in confirm


def test_brain_confirm_gate_matches_registry():
    """brain._CONFIRM_TOOLS must equal the registry's destructive set — no drift
    between the gate and the single source of truth."""
    import kai.tools  # noqa: F401
    from kai.core.engine import _CONFIRM_TOOLS
    from kai.tools.registry import confirm_tool_names

    assert _CONFIRM_TOOLS == confirm_tool_names()


def test_registry_execute_known_tool():
    reg = ToolRegistry()

    @reg.tool(
        name="test.double",
        description="Double a number.",
        parameters={"n": {"type": "integer", "description": "number"}},
    )
    def double(n: int):
        return n * 2

    assert reg.execute("test.double", {"n": 5}) == 10


def test_registry_execute_unknown_tool_raises():
    reg = ToolRegistry()
    with pytest.raises(KeyError, match="Unknown tool"):
        reg.execute("does.not.exist", {})


def test_registry_schema_format():
    reg = ToolRegistry()

    @reg.tool(
        name="test.greet",
        description="Says hello.",
        parameters={"name": {"type": "string", "description": "The name."}},
    )
    def greet(name: str):
        return f"Hello {name}"

    schema = reg.get_schema()
    assert len(schema) == 1
    fn = schema[0]["function"]
    assert fn["name"] == "test.greet"
    assert "name" in fn["parameters"]["properties"]


def test_alias_redirects_when_args_fit():
    reg = ToolRegistry()

    @reg.tool(name="pc.startup_programs", description="List startup programs.")
    def startup():
        return "ok"

    assert reg.learn_alias("pc.startups") == "pc.startup_programs"


def test_alias_rejects_incompatible_args():
    """A hallucinated name must not redirect to a tool that can't take its
    args — that's a different intent, not a misspelling (the
    system.execute_command → system.temps incident)."""
    reg = ToolRegistry()

    @reg.tool(name="system.temps", description="Read temperatures.")
    def temps():
        return "ok"

    assert reg.learn_alias("system.tempss", args={"command": "dir"}) is None
    # Without conflicting args the same name redirects fine
    assert reg.learn_alias("system.tempss") == "system.temps"


def test_schema_exclude_hides_tool_and_its_alias():
    """A turned-off tool — and any alias pointing at it — must not appear in the
    schema handed to the model."""
    reg = ToolRegistry()

    @reg.tool(name="demo.keep", description="keep me")
    def keep():
        return "k"

    @reg.tool(name="demo.drop", description="drop me")
    def drop():
        return "d"

    reg.learn_alias("demo.dropp")  # register an alias → demo.drop
    names = {s["function"]["name"] for s in reg.get_schema(exclude={"demo.drop"})}
    assert "demo.keep" in names
    assert "demo.drop" not in names
    assert "demo.dropp" not in names  # the alias is hidden too


def test_resolve_name_follows_alias():
    reg = ToolRegistry()

    @reg.tool(name="pc.startup_programs", description="list startup programs")
    def startup():
        return "ok"

    assert reg.resolve_name("pc.startup_programs") == "pc.startup_programs"
    reg.learn_alias("pc.startups")
    assert reg.resolve_name("pc.startups") == "pc.startup_programs"
    assert reg.resolve_name("totally.unknown") == "totally.unknown"


def test_disabled_tool_is_blocked_at_dispatch():
    """The authoritative enablement gate: a turned-off tool must not execute even
    if named directly or via an alias — belt-and-suspenders behind the schema
    filter, covering the crew and hallucination paths too."""
    from types import SimpleNamespace

    from kai.core.engine import TurnEngine

    reg = ToolRegistry()

    @reg.tool(name="demo.run", description="runs a thing")
    def run():
        return "ran"

    host = SimpleNamespace(
        skill_registry=None,
        tool_registry=reg,
        user_id=0,
        session_id=None,
        disabled_tools={"demo.run"},
    )
    eng = TurnEngine(host)

    blocked = eng._execute_tool("demo.run", {}, "t")
    assert blocked["success"] is False and "turned off" in blocked["error"]

    # An alias to a disabled tool is blocked as well (learn_alias would else
    # resurrect it under the hallucinated name).
    aliased = eng._execute_tool("demo.runn", {}, "t")
    assert aliased["success"] is False and "turned off" in aliased["error"]

    # Re-enabling it lets it run normally.
    host.disabled_tools = set()
    ok = eng._execute_tool("demo.run", {}, "t")
    assert ok["success"] is True and ok["output"] == "ran"


def test_describe_catalog_groups_registered_tools_with_metadata():
    """The Settings → Tools inventory groups registered tools by category and
    carries each tool's label + risk for the toggle UI."""
    import kai.tools  # noqa: F401 — register every tool
    from kai.tools.registry import registry

    groups = registry.describe_catalog()
    assert groups, "catalog should not be empty"
    flat = {}
    for g in groups:
        assert g["category"] and isinstance(g["tools"], list)
        for t in g["tools"]:
            assert t["name"] in registry.list_tools()
            assert t["label"] and t["risk"] in {"safe", "caution", "destructive"}
            flat[t["name"]] = t
    # Metadata is accurate: a known destructive tool reads as destructive.
    assert flat["system.kill_process"]["risk"] == "destructive"
    assert flat["weather.current"]["risk"] == "safe"


# ── Memory tree ──────────────────────────────────────────────────────────────


def test_tree_seed_is_idempotent(tmp_path, monkeypatch):
    from kai.memory import tree as mtree

    monkeypatch.setattr(mtree, "_TREE_DIR", tmp_path)
    created = mtree.seed_skeleton("0")
    assert created == len(mtree.SKELETON)
    assert mtree.seed_skeleton("0") == 0  # second run touches nothing


def test_tree_tools_save_browse_read(tmp_path, monkeypatch):
    from kai.core._app_state import set_current_user_id
    from kai.memory import tree as mtree
    from kai.tools import memory_tools as mt

    monkeypatch.setattr(mtree, "_TREE_DIR", tmp_path)
    mt._TREE_SEEDED.clear()
    set_current_user_id(0)

    # Paths without the user/ root get rooted automatically
    out = mt.tree_save("identity/profession", "stuntman")
    assert "user/identity/profession" in out

    listing = mt.tree_browse("")
    assert "identity/" in listing

    branch = mt.tree_read("user/identity")
    assert "stuntman" in branch

    # Saving over a seeded folder node replaces the index with the fact
    node = mtree.read("0", "user/identity/profession")
    assert node.value == "stuntman"
    assert node.source == "stated"


def test_seed_nodes_never_surface_in_scoring(tmp_path, monkeypatch):
    """Folder scaffolding must stay invisible to retrieval — several seeded
    folders sit on hardcoded paths and would otherwise appear in EVERY turn."""
    from kai.memory import scorer
    from kai.memory import tree as mtree

    monkeypatch.setattr(mtree, "_TREE_DIR", tmp_path)
    mtree.seed_skeleton("7")
    assert mtree.count_facts("7") == 0
    assert scorer.select_for_context("7", None) == []

    mtree.write(
        "7",
        mtree.Node(
            path="user/identity/profession",
            value="stuntman",
            source="stated",
            importance=0.6,
            specificity=0.6,
        ),
    )
    assert mtree.count_facts("7") == 1
    surfaced = scorer.select_for_context("7", None)
    assert [n.path for n, _s in surfaced] == ["user/identity/profession"]


# ── Time tool ────────────────────────────────────────────────────────────────


def test_time_now_returns_string():
    result = get_time()
    assert isinstance(result, str)
    assert len(result) > 0


def test_time_now_contains_day():
    result = get_time()
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    assert any(day in result for day in days)


def test_time_now_contains_year():
    result = get_time()
    assert "2026" in result or "2025" in result  # reasonable range


# ── System info ──────────────────────────────────────────────────────────────


def test_system_info_returns_valid_json():
    result = get_system_info()
    data = json.loads(result)
    assert "cpu" in data
    assert "ram" in data
    assert "disk" in data
    assert "top_processes" in data


def test_system_info_cpu_is_percentage():
    result = json.loads(get_system_info())
    assert result["cpu"].endswith("%")


def test_system_info_top_processes_is_list():
    result = json.loads(get_system_info())
    assert isinstance(result["top_processes"], list)


# ── Notes ────────────────────────────────────────────────────────────────────


def test_notes_save_returns_confirmation():
    result = save_note("Remember to buy milk", title="grocery")
    assert "grocery" in result or "milk" in result


def test_notes_search_finds_saved_note():
    save_note("The launch is on Friday", title="schedule")
    result = search_notes("Friday")
    assert "Friday" in result


def test_notes_search_no_match():
    result = search_notes("xyzzy_nonexistent_query_12345")
    assert "No notes found" in result


def test_notes_list_returns_recent():
    save_note("List test note")
    result = list_notes()
    assert "List test note" in result or len(result) > 0


def test_notes_list_empty_when_no_notes():
    # Use a user_id that has no notes to get the empty-state response
    from kai.core._app_state import set_current_user_id

    set_current_user_id(99999)  # unused user — guaranteed no notes
    try:
        result = list_notes()
    finally:
        set_current_user_id(0)
    assert result == "No notes saved yet."


# ── Search (HTML parsing, no network) ────────────────────────────────────────


def test_strip_tags_removes_html():
    assert _strip_tags("<b>Hello</b> world") == "Hello world"


def test_strip_tags_decodes_entities():
    assert _strip_tags("a &amp; b") == "a & b"
    assert _strip_tags("it&#x27;s") == "it's"


def test_ddg_search_returns_empty_on_bad_html():
    results = _ddg_search.__wrapped__("anything") if hasattr(_ddg_search, "__wrapped__") else []
    # Just testing the parser handles garbage HTML gracefully
    from kai.tools.web.search import _parse_results

    results = _parse_results("<html><body>no results here</body></html>", 5)
    assert results == []


def test_web_search_returns_no_results_message_on_empty():
    with patch("kai.tools.web.search._ddg_search", return_value=[]):
        result = web_search("something impossible xyzzy12345")
    assert "No results found" in result


def test_web_search_formats_results():
    fake = [
        {"title": "Python Docs", "snippet": "The official Python docs.", "url": "python.org"},
    ]
    with patch("kai.tools.web.search._ddg_search", return_value=fake):
        result = web_search("python")
    assert "Python Docs" in result
    assert "python.org" in result


# ── Weather (forecast formatting, no network) ────────────────────────────────


def test_weather_forecast_block_surfaces_hi_lo_and_rain():
    from kai.tools.web.weather import _format_forecast

    days = [
        {
            "date": "2026-06-27",
            "maxtempF": "89",
            "mintempF": "69",
            "maxtempC": "31",
            "mintempC": "20",
            "hourly": [
                {"weatherDesc": [{"value": "Sunny"}], "chanceofrain": "10"},
                {"weatherDesc": [{"value": "Partly cloudy"}], "chanceofrain": "40"},
            ],
        }
    ]
    out = _format_forecast(days)
    assert "Today" in out
    assert "69-89°F" in out
    assert "40% rain" in out  # takes the max chance across the day


def test_weather_forecast_empty_when_no_days():
    from kai.tools.web.weather import _format_forecast

    assert _format_forecast([]) == ""


# ── Search (recency + max_results + lite fallback, no network) ────────────────


def test_web_search_passes_recency_and_clamps_max_results():
    from kai.tools.web import search as S

    captured = {}

    def fake(query, max_results=5, recency=""):
        captured["max_results"] = max_results
        captured["recency"] = recency
        return [{"title": "t", "snippet": "s", "url": "u"}]

    with patch.object(S, "_ddg_search", side_effect=fake):
        S.web_search("q", recency="week", max_results=99)
    assert captured["recency"] == "week"
    assert captured["max_results"] == 10  # clamped to the 1-10 ceiling


def test_recency_maps_to_ddg_df_codes():
    from kai.tools.web.search import _RECENCY_CODES

    assert _RECENCY_CODES == {"day": "d", "week": "w", "month": "m", "year": "y"}


def test_lite_parser_extracts_results_and_unwraps_redirect():
    from kai.tools.web.search import _parse_lite_results

    html = (
        '<a class="result-link" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fpython.org">'
        'Python</a><td class="result-snippet">Official site</td>'
    )
    results = _parse_lite_results(html, 5)
    assert results == [{"title": "Python", "snippet": "Official site", "url": "https://python.org"}]


def test_ddg_search_falls_back_to_lite_when_html_parses_nothing():
    from kai.tools.web import search as S

    lite_html = (
        '<a class="result-link" href="https://example.com">Example</a>'
        '<td class="result-snippet">A site</td>'
    )
    # First _fetch (html endpoint) returns unparseable markup; second (lite) parses.
    with patch.object(S, "_fetch", side_effect=["<html>nothing useful</html>", lite_html]):
        results = S._ddg_search("anything")
    assert results and results[0]["title"] == "Example"


# ── Researcher (excerpt + PDF + library, no network) ─────────────────────────


def test_fetch_url_returns_excerpt_with_footer():
    from kai.config import WEB_EXCERPT_CHARS
    from kai.tools.web import researcher as R

    with patch.object(
        R, "_extract_url_text", return_value=(200, "X" * (WEB_EXCERPT_CHARS + 500), None)
    ):
        out = R.fetch_url("http://example.com")
    assert "Excerpt" in out
    assert len(out) < WEB_EXCERPT_CHARS + 400  # body trimmed, not the full page


def test_fetch_url_surfaces_extractor_error():
    from kai.tools.web import researcher as R

    with patch.object(R, "_extract_url_text", return_value=(None, "", "boom")):
        assert R.fetch_url("http://x") == "boom"


def test_add_to_library_full_read_then_searchable():
    from kai.core._app_state import set_current_user_id
    from kai.tools.knowledge.rag import docs_search
    from kai.tools.web import researcher as R

    set_current_user_id(0)
    page = "The aurora borealis is caused by solar wind hitting the magnetosphere. " * 50
    with patch.object(R, "_extract_url_text", return_value=(200, page, None)):
        out = R.add_to_library("https://example.com/aurora")
    assert "Saved to library" in out
    assert "example.com/aurora" in out
    # The full text is now retrievable via docs.search (text search is fine here).
    hit = docs_search("aurora borealis")
    assert "aurora" in hit.lower()


# ── documents.ingest_text helper ──────────────────────────────────────────────


def test_ingest_text_chunks_and_stores():
    from kai.memory import documents as D

    meta = D.ingest_text(
        "hello world " * 200, source_name="https://src/x", user_id=0, file_type="url"
    )
    assert meta["chunk_count"] >= 1
    assert meta["file_type"] == "url"
    assert meta["filename"] == "https://src/x"


def test_ingest_text_rejects_empty():
    from kai.memory import documents as D

    with pytest.raises(ValueError):
        D.ingest_text("   ", source_name="x")


# ── Cleanup ──────────────────────────────────────────────────────────────────


def teardown_module(module):
    try:
        os.unlink(_tmp.name)
    except Exception:
        pass
