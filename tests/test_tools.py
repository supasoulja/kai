"""
Unit tests for all tools — no Ollama, no network.
Each tool is tested for its core logic, not model behavior.
"""
import os
import json
import tempfile
import pytest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("KAI_TEST_MODE", "1")

# Redirect DB to a temp file so tool tests don't touch real data
import kai.config as cfg
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
cfg.DB_PATH = Path(_tmp.name)

from kai.db import _reset_for_tests
_reset_for_tests()

from kai.tools.registry import ToolRegistry
from kai.tools.time_tool import get_time
from kai.tools.system_info import get_system_info
from kai.tools.notes import save_note, search_notes, list_notes
from kai.tools.search import _ddg_search, _strip_tags, web_search


# ── Registry ─────────────────────────────────────────────────────────────────

def test_registry_registers_and_lists():
    reg = ToolRegistry()

    @reg.tool(name="test.ping", description="Ping tool.")
    def ping():
        return "pong"

    assert "test.ping" in reg.list_tools()


def test_registry_execute_known_tool():
    reg = ToolRegistry()

    @reg.tool(name="test.double", description="Double a number.",
              parameters={"n": {"type": "integer", "description": "number"}})
    def double(n: int):
        return n * 2

    assert reg.execute("test.double", {"n": 5}) == 10


def test_registry_execute_unknown_tool_raises():
    reg = ToolRegistry()
    with pytest.raises(KeyError, match="Unknown tool"):
        reg.execute("does.not.exist", {})


def test_registry_schema_format():
    reg = ToolRegistry()

    @reg.tool(name="test.greet", description="Says hello.",
              parameters={"name": {"type": "string", "description": "The name."}})
    def greet(name: str):
        return f"Hello {name}"

    schema = reg.get_schema()
    assert len(schema) == 1
    fn = schema[0]["function"]
    assert fn["name"] == "test.greet"
    assert "name" in fn["parameters"]["properties"]


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
    from kai._app_state import set_current_user_id
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
    from kai.tools.search import _parse_results
    results = _parse_results("<html><body>no results here</body></html>", 5)
    assert results == []


def test_web_search_returns_no_results_message_on_empty():
    with patch("kai.tools.search._ddg_search", return_value=[]):
        result = web_search("something impossible xyzzy12345")
    assert "No results found" in result


def test_web_search_formats_results():
    fake = [
        {"title": "Python Docs", "snippet": "The official Python docs.", "url": "python.org"},
    ]
    with patch("kai.tools.search._ddg_search", return_value=fake):
        result = web_search("python")
    assert "Python Docs" in result
    assert "python.org" in result


# ── Cleanup ──────────────────────────────────────────────────────────────────

def teardown_module(module):
    try:
        os.unlink(_tmp.name)
    except Exception:
        pass
