"""
Tests for auto-generated tool docs in the memory tree + the [TOOLS] prompt block.

Tree DB is isolated per-test via monkeypatch on mtree._TREE_DIR (same convention as
tests/test_tools.py) so nothing touches the real kai/memory/tree/{0,2}.db files.
"""
import kai.tools  # noqa: F401 — triggers every @registry.tool registration
from kai.tools.registry import registry
from kai.memory import tree as mtree
from kai.memory import tool_docs


# ── Sync ──────────────────────────────────────────────────────────────────────

def test_sync_creates_node_for_every_tool(tmp_path, monkeypatch):
    monkeypatch.setattr(mtree, "_TREE_DIR", tmp_path)
    tool_docs.sync_tool_docs("1")
    for name in registry.list_tools():
        namespace, _, tool_name = name.partition(".")
        node = mtree.read("1", f"tools/{namespace}/{tool_name}")
        assert node is not None, f"missing tree node for {name}"
        assert node.source == "seed"
        assert node.decays is False
        assert node.domain == "tools"


def test_node_content_has_signature_and_example(tmp_path, monkeypatch):
    monkeypatch.setattr(mtree, "_TREE_DIR", tmp_path)
    tool_docs.sync_tool_docs("1")
    node = mtree.read("1", "tools/tree/save")
    assert "Signature: tree.save(" in node.value
    assert "Example: tree.save(" in node.value
    # both required params surface in the example call
    assert "path=" in node.value and "fact=" in node.value


def test_namespace_folder_nodes_created(tmp_path, monkeypatch):
    monkeypatch.setattr(mtree, "_TREE_DIR", tmp_path)
    tool_docs.sync_tool_docs("1")
    folder = mtree.read("1", "tools/tree")
    assert folder is not None
    assert folder.source == "seed"


def test_sync_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(mtree, "_TREE_DIR", tmp_path)
    first = tool_docs.sync_tool_docs("1")
    second = tool_docs.sync_tool_docs("1")
    assert first["created"] > 0
    assert second["created"] == 0
    assert second["deleted"] == 0
    assert len(mtree.subtree("1", "tools")) == len(mtree.subtree("1", "tools"))


def test_stale_tool_node_is_deleted(tmp_path, monkeypatch):
    monkeypatch.setattr(mtree, "_TREE_DIR", tmp_path)
    tool_docs.sync_tool_docs("1")
    # Simulate a tool that was removed from the registry between syncs.
    mtree.write("1", mtree.Node(
        path="tools/ghost/old_tool", value="stale doc",
        source="seed", decays=False, domain="tools",
    ))
    result = tool_docs.sync_tool_docs("1")
    assert mtree.read("1", "tools/ghost/old_tool") is None
    assert result["deleted"] >= 1
    # the now-orphaned namespace folder is cleaned up too
    assert mtree.read("1", "tools/ghost") is None


# ── Render ──────────────────────────────────────────────────────────────────

def test_render_tool_index_format(tmp_path, monkeypatch):
    monkeypatch.setattr(mtree, "_TREE_DIR", tmp_path)
    tool_docs.sync_tool_docs("1")
    block = tool_docs.render_tool_index("1")
    assert block.startswith("[TOOLS")
    assert "tree.save() — tree: tools/tree/save" in block
    assert "time.now() — tree: tools/time/now" in block
    # one line per tool + 1 header line
    assert len(block.splitlines()) == len(registry.list_tools()) + 1


def test_render_tool_index_empty_before_sync(tmp_path, monkeypatch):
    monkeypatch.setattr(mtree, "_TREE_DIR", tmp_path)
    assert tool_docs.render_tool_index("nobody") == ""


# ── Tool docs stay invisible to user-fact retrieval ──────────────────────────

def test_count_facts_unaffected_by_tool_docs(tmp_path, monkeypatch):
    """[MEMORY CONTEXT] gating in brain.py depends on count_facts() == 0 — tool docs
    must not make an otherwise-empty tree look like it holds real facts."""
    monkeypatch.setattr(mtree, "_TREE_DIR", tmp_path)
    tool_docs.sync_tool_docs("1")
    assert mtree.count_facts("1") == 0


def test_tool_docs_excluded_from_memory_context_scoring(tmp_path, monkeypatch):
    from kai.memory import scorer
    monkeypatch.setattr(mtree, "_TREE_DIR", tmp_path)
    tool_docs.sync_tool_docs("1")
    assert scorer.select_for_context("1", None) == []


# ── tree.* tool integration (landmine fixes) ─────────────────────────────────

def test_tree_read_reaches_tool_doc(tmp_path, monkeypatch):
    from kai.tools import memory_tools as mt
    from kai.core._app_state import set_current_user_id
    monkeypatch.setattr(mtree, "_TREE_DIR", tmp_path)
    mt._TREE_SEEDED.clear()
    set_current_user_id(1)
    tool_docs.sync_tool_docs("1")

    out = mt.tree_read("tools/tree/save")
    assert "Signature: tree.save(" in out


def test_tree_browse_root_excludes_tools(tmp_path, monkeypatch):
    from kai.tools import memory_tools as mt
    from kai.core._app_state import set_current_user_id
    monkeypatch.setattr(mtree, "_TREE_DIR", tmp_path)
    mt._TREE_SEEDED.clear()
    set_current_user_id(1)
    tool_docs.sync_tool_docs("1")

    out = mt.tree_browse("")          # _ensure_tree seeds the user skeleton
    assert "tools/" not in out
    assert "identity/" in out         # user skeleton still visible


def test_tree_browse_tools_path_shows_index(tmp_path, monkeypatch):
    from kai.tools import memory_tools as mt
    from kai.core._app_state import set_current_user_id
    monkeypatch.setattr(mtree, "_TREE_DIR", tmp_path)
    mt._TREE_SEEDED.clear()
    set_current_user_id(1)
    tool_docs.sync_tool_docs("1")

    out = mt.tree_browse("tools/tree")
    assert "save/" in out


# ── [TOOLS] placement in the rendered context ────────────────────────────────

def test_tools_block_after_procedural_in_context(tmp_path, monkeypatch):
    from kai.store.schema import ContextBlock, ProceduralRule
    monkeypatch.setattr(mtree, "_TREE_DIR", tmp_path)
    tool_docs.sync_tool_docs("1")
    tool_index = tool_docs.render_tool_index("1")

    block = ContextBlock(
        identity="persona text",
        procedural=[ProceduralRule(key="rule1", value="be nice")],
        semantic=[], episodic=[],
        tool_index=tool_index,
    )
    rendered = block.render()
    proc_idx = rendered.index("[PROCEDURAL]")
    tools_idx = rendered.index("[TOOLS")
    assert proc_idx < tools_idx
    assert "[SEMANTIC" not in rendered[proc_idx:tools_idx]


# ── Regression guard ──────────────────────────────────────────────────────────

def test_audit_metadata_still_clean():
    audit = registry.audit_metadata()
    assert audit["missing_label"] == []
    assert audit["uncategorized"] == []
