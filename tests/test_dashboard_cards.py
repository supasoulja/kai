"""Dashboard Models + Routine cards: only what a sensor said, only the caller's own data."""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from kai.store import sessions
from kai.store.db import get_conn
from kai.system import devstats

GB = 1 << 30


def _client(user_id):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from kai.api.routes import dashboard as route

    app = FastAPI()

    @app.middleware("http")
    async def as_user(request, call_next):
        request.state.user = {"name": "t", "user_id": user_id} if user_id else None
        return await call_next(request)

    app.include_router(route.router)
    return TestClient(app)


class _Ollama:
    def __init__(self, alive=True, installed=None, loaded=None):
        self.alive = alive
        self.installed = installed or {}
        self.loaded = loaded or {}

    def is_alive(self):
        return self.alive

    def model_sizes(self):
        return self.installed

    def running(self):
        return self.loaded


def _brain(ollama, model="chat:1", tool_model=None, cloud=False):
    return SimpleNamespace(
        model=model,
        ollama=ollama,
        _chat_client=object() if cloud else ollama,
        _engine=SimpleNamespace(_resolve_tool_model=lambda: (tool_model, tool_model is not None)),
    )


@pytest.fixture
def models_route(monkeypatch):
    from kai.api.routes import dashboard
    from kai.core import permissions

    gpu = {"index": 1, "name": "Example GPU", "total_bytes": 16 * GB, "used_bytes": 9 * GB}
    monkeypatch.setattr(devstats, "collect_gpus", lambda: [gpu])
    monkeypatch.setattr(
        devstats, "collect_ram", lambda: {"total_bytes": 32 * GB, "used_bytes": 8 * GB}
    )
    monkeypatch.setattr(dashboard.cfg, "CREW_ENABLED", False)
    level = {"value": "member"}
    monkeypatch.setattr(permissions, "level_of", lambda uid: level["value"])

    def use(brain):
        monkeypatch.setattr(dashboard, "get_or_create_brain", lambda uid: brain)

    return SimpleNamespace(use=use, level=level)


def _roles(body):
    return {r["role"]: r for r in body["roles"]}


def test_models_reports_loaded_installed_and_missing(models_route):
    from kai.api.routes import dashboard

    ollama = _Ollama(
        installed={"chat:1": 8 * GB, "tool:1": 3 * GB},
        loaded={"chat:1": {"bytes": 8 * GB, "vram_bytes": 7 * GB}},
    )
    models_route.use(_brain(ollama, tool_model="tool:1"))
    body = _client(5).get("/dashboard/models").json()
    roles = _roles(body)

    assert roles["chat"] == {
        "role": "chat",
        "model": "chat:1",
        "state": "loaded",
        "bytes": 8 * GB,
        "vram_bytes": 7 * GB,
    }
    assert roles["tools"]["state"] == "installed"
    assert roles["search_deep"]["model"] == dashboard.cfg.HQ_EMBED_MODEL
    assert roles["search_deep"]["state"] == "missing"
    assert roles["search_fast"]["state"] == "cpu"
    assert "crew" not in roles
    assert body["machine"]["gpus"][0]["name"] == "Example GPU"
    assert body["machine"]["loaded"] == {"chat:1": {"bytes": 8 * GB, "vram_bytes": 7 * GB}}


def test_model_running_on_cpu_reports_no_vram(models_route):
    ollama = _Ollama(
        installed={"chat:1": 8 * GB},
        loaded={"chat:1": {"bytes": 10 * GB, "vram_bytes": 0}},
    )
    models_route.use(_brain(ollama))
    chat = _roles(_client(5).get("/dashboard/models").json())["chat"]
    assert chat["state"] == "loaded"
    assert chat["vram_bytes"] == 0


def test_tool_rounds_on_chat_model_show_the_chat_model(models_route):
    models_route.use(_brain(_Ollama(installed={"chat:1": GB})))
    assert _roles(_client(5).get("/dashboard/models").json())["tools"]["model"] == "chat:1"


def test_ollama_down_is_unknown_not_missing(models_route):
    models_route.use(_brain(_Ollama(alive=False)))
    body = _client(5).get("/dashboard/models").json()
    assert _roles(body)["chat"]["state"] == "unknown"
    assert body["machine"]["loaded"] is None


def test_cloud_chat_brain_is_marked_cloud(models_route):
    models_route.use(_brain(_Ollama(installed={"chat:1": GB}), cloud=True))
    assert _roles(_client(5).get("/dashboard/models").json())["chat"]["state"] == "cloud"


def test_guests_see_models_but_not_hardware(models_route):
    models_route.use(_brain(_Ollama(installed={"chat:1": GB})))
    models_route.level["value"] = "guest"
    body = _client(5).get("/dashboard/models").json()
    assert body["roles"]
    assert body["machine"] is None


def test_cards_need_a_signed_in_user():
    assert _client(None).get("/dashboard/models").status_code == 401
    assert _client(None).get("/dashboard/routine").status_code == 401


def _say(user_id, when, role="user"):
    sid = sessions.new_session("t", user_id=user_id)
    rowid = sessions.append_message(sid, role, "hi", 0, user_id=user_id)
    conn = get_conn()
    conn.execute(
        "UPDATE session_messages SET timestamp = ? WHERE id = ?", (when.isoformat(), rowid)
    )
    conn.commit()


def test_routine_is_unknown_until_enough_days():
    today = datetime.now().replace(hour=23, minute=0, second=0, microsecond=0) - timedelta(days=1)
    for d in range(3):
        _say(41, today - timedelta(days=d))
    body = _client(41).get("/dashboard/routine").json()
    assert body["hours"] is None
    assert body["days_seen"] == 3
    assert body["messages"] == 3


def test_routine_counts_only_the_callers_recent_messages():
    base = datetime.now().replace(minute=0, second=0, microsecond=0) - timedelta(days=1)
    for d in range(7):
        _say(42, base.replace(hour=23) - timedelta(days=d))
    _say(42, base.replace(hour=8))
    _say(42, base.replace(hour=3), role="assistant")
    _say(42, base.replace(hour=4) - timedelta(days=45))
    _say(43, base.replace(hour=12))

    body = _client(42).get("/dashboard/routine").json()
    assert body["days_seen"] == 7
    assert body["hours"][23] == 7
    assert body["hours"][8] == 1
    assert sum(body["hours"]) == 8


def test_collect_gpus_and_ram_shape():
    for i, g in enumerate(devstats.collect_gpus(), 1):
        assert set(g) == {"index", "name", "total_bytes", "used_bytes"}
        assert g["index"] == i
        assert 0 <= g["used_bytes"] <= g["total_bytes"]
    ram = devstats.collect_ram()
    assert 0 < ram["used_bytes"] <= ram["total_bytes"]


def test_gpu_name_prefers_the_bracketed_marketing_name(tmp_path, monkeypatch):
    (tmp_path / "uevent").write_text("DRIVER=amdgpu\nPCI_SLOT_NAME=0000:01:00.0\n")
    out = "Slot:\t01:00.0\nVendor:\tExample Vendor\nDevice:\tChip 99 [Example Card 1000]\n"
    monkeypatch.setattr(devstats.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(
        devstats.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=out, returncode=0)
    )
    assert devstats._gpu_name(tmp_path) == "Example Card 1000"

    monkeypatch.setattr(devstats.shutil, "which", lambda name: None)
    assert devstats._gpu_name(tmp_path) is None
