"""
Anthropic adapter tests — request normalization (system hoist, tool_use /
tool_result pairing, tool schema, adaptive thinking, no temperature), response
normalization, and SSE stream assembly. HTTP seams are monkeypatched.
"""
import json

from kai.llm.providers.anthropic import AnthropicClient


def _client():
    return AnthropicClient(api_key="sk-ant-test", default_model="claude-opus-4-8")


# ── Request normalization ────────────────────────────────────────────────────

def test_system_is_hoisted_and_tools_paired():
    c = _client()
    messages = [
        {"role": "system", "content": "You are Kai."},
        {"role": "user", "content": "check temps"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"function": {"name": "system.temps", "arguments": {"unit": "c"}}}]},
        {"role": "tool", "content": '{"output": "70C"}'},
    ]
    payload = c._build(messages, None, "", False, False)
    # system hoisted to a top-level field, not left in messages
    assert payload["system"] == "You are Kai."
    assert all(m["role"] != "system" for m in payload["messages"])
    # assistant tool call became a tool_use block with an id + dict input
    asst = payload["messages"][1]
    tu = asst["content"][0]
    assert tu["type"] == "tool_use" and tu["name"] == "system.temps"
    assert tu["input"] == {"unit": "c"}
    # tool result became a tool_result block in a user message, id matched
    tr = payload["messages"][2]["content"][0]
    assert tr["type"] == "tool_result" and tr["tool_use_id"] == tu["id"]
    # max_tokens is always present (required by the API)
    assert payload["max_tokens"] > 0


def test_tool_schema_conversion():
    c = _client()
    tools = [{"type": "function", "function": {
        "name": "weather.current", "description": "get weather",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}]
    payload = c._build([{"role": "user", "content": "hi"}], tools, "", False, False)
    t = payload["tools"][0]
    assert t["name"] == "weather.current"
    assert t["description"] == "get weather"
    assert t["input_schema"]["properties"]["city"]["type"] == "string"
    assert "function" not in t  # unwrapped


def test_think_maps_to_adaptive_and_no_temperature():
    c = _client()
    payload = c._build([{"role": "user", "content": "q"}], None, "", True, False)
    assert payload["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert "temperature" not in payload  # rejected on Opus 4.7/4.8 — never sent

    payload2 = c._build([{"role": "user", "content": "q"}], None, "", False, False)
    assert "thinking" not in payload2


# ── Response normalization ───────────────────────────────────────────────────

def test_response_text_and_thinking(monkeypatch):
    c = _client()
    monkeypatch.setattr(c, "_post_json", lambda p, payload: {"content": [
        {"type": "thinking", "thinking": "let me see"},
        {"type": "text", "text": "It's 70C."},
    ]})
    resp = c.chat([{"role": "user", "content": "temps?"}])
    assert resp["message"]["content"] == "It's 70C."
    assert resp["message"]["thinking"] == "let me see"


def test_response_tool_use(monkeypatch):
    c = _client()
    monkeypatch.setattr(c, "_post_json", lambda p, payload: {"content": [
        {"type": "tool_use", "id": "toolu_1", "name": "weather.current",
         "input": {"city": "NYC"}},
    ]})
    resp = c.chat([{"role": "user", "content": "weather?"}], tools=[{"function": {"name": "x"}}])
    tc = resp["message"]["tool_calls"][0]
    assert tc["id"] == "toolu_1"
    assert tc["function"]["name"] == "weather.current"
    assert tc["function"]["arguments"] == {"city": "NYC"}    # already a dict in Anthropic


# ── Streaming (SSE events) ───────────────────────────────────────────────────

def _events(*objs):
    return [f"data: {json.dumps(o)}" for o in objs]


def test_stream_text_and_thinking(monkeypatch):
    c = _client()
    monkeypatch.setattr(c, "_post_stream", lambda p, payload: iter(_events(
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "hmm"}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hel"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "lo"}},
        {"type": "message_stop"},
    )))
    out = list(c.chat_stream([{"role": "user", "content": "hi"}]))
    assert "".join(t for t, done, m in out if not done) == "Hello"
    assert [m["think_token"] for t, done, m in out if m.get("think_token")] == ["hmm"]
    assert out[-1][1] is True and out[-1][2]["content"] == "Hello"


def test_stream_tool_use_assembled(monkeypatch):
    c = _client()
    monkeypatch.setattr(c, "_post_stream", lambda p, payload: iter(_events(
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "tool_use", "id": "toolu_9", "name": "weather.current"}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "input_json_delta", "partial_json": '{"ci'}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "input_json_delta", "partial_json": 'ty": "NYC"}'}},
        {"type": "message_stop"},
    )))
    final = list(c.chat_stream([{"role": "user", "content": "weather"}]))[-1]
    tc = final[2]["tool_calls"][0]
    assert tc["id"] == "toolu_9"
    assert tc["function"]["name"] == "weather.current"
    assert tc["function"]["arguments"] == {"city": "NYC"}    # reassembled across deltas


# ── Registered with the factory ──────────────────────────────────────────────

def test_anthropic_registered():
    import kai.llm.providers  # noqa: F401 — ensures registration
    from kai.llm.client import get_client, available_providers
    assert "anthropic" in available_providers()
    c = get_client("anthropic", api_key="k", default_model="claude-opus-4-8")
    assert isinstance(c, AnthropicClient)
