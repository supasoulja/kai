"""
Security and auth tests for Kai's web layer.

Fast — no Ollama, no model loading. All tests use an isolated temp DB.

Run with:
    python -m pytest tests/test_security.py -v

Covers:
  - Registration gate (first-run only)
  - Login rate limiting
  - Session token lifecycle: issue → use → revoke → replay blocked
  - WebSocket auth rejection
  - Protected endpoint auth guard (H2a, H2b)
  - Cross-user session isolation (IDOR)
  - Voice upload size cap (M1)
  - TTS text size cap (M4)
  - Path traversal resistance
  - Unit: hash helpers, user_count, get_owner_id, voice WAV generation
"""

import hashlib
import io
import os
import secrets
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("KAI_TEST_MODE", "1")

# ── Override DB paths BEFORE any kai import touches them ──────────────────────
import kai.config as cfg

_tmp_main = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_main.close()
cfg.DB_PATH = Path(_tmp_main.name)

from kai.store.db import _reset_for_tests, get_conn

_reset_for_tests()

# Import and configure the app — no _init(), so no model loading.
# Import from the package path, not the root web.py shim: the shim re-exports
# with `import *`, which skips the private helpers these tests reach for.
from kai.apps.web import app, setup_app

# Call setup_app exactly once for the whole test session
_setup_done = False


def _ensure_setup():
    global _setup_done
    if not _setup_done:
        setup_app(host="127.0.0.1", port=7860, scheme="http")
        _setup_done = True


_ensure_setup()

import pytest
from fastapi.testclient import TestClient

# Module-level client for stateless tests (registration, login rate limit, path traversal)
client = TestClient(app, raise_server_exceptions=False)


def _fresh_client(token: str | None = None) -> TestClient:
    """
    Create a fresh TestClient with a new ASGI event-loop thread.
    A fresh thread gets a fresh SQLite connection, which reads the current
    committed DB state — avoiding the stale-snapshot problem that occurs
    when the module-level client's long-lived ASGI thread holds an older view.
    """
    c = TestClient(app, raise_server_exceptions=False)
    if token:
        c.cookies.set("kai_session", token)
    return c


# ── Test helpers ──────────────────────────────────────────────────────────────

_MACHINE_HASH = "test_machine_hash_for_security_tests"
_user_counter = 0


def _unique_name(prefix: str = "user") -> str:
    global _user_counter
    _user_counter += 1
    return f"{prefix}_{_user_counter}"


def _create_user(name: str, pin: str = "1234") -> int:
    """Insert a user directly, bypassing machine key and the HTTP endpoint."""
    from kai.store import users as u

    result = u.create_user(name, pin, _MACHINE_HASH)
    assert result is not None, f"Failed to create user {name!r}"
    return result["id"]


def _make_session(user_id: int, user_name: str) -> str:
    """Inject a valid hashed session token into the DB. Returns the raw token."""
    token = secrets.token_urlsafe(32)
    tok_hash = hashlib.sha256(token.encode()).hexdigest()
    expires = (datetime.now() + timedelta(hours=1)).isoformat()
    conn = get_conn()
    conn.execute(
        "INSERT INTO session_tokens (token, user_id, user_name, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (tok_hash, user_id, user_name, datetime.now().isoformat(), expires),
    )
    conn.commit()
    return token


def _auth(token: str) -> dict:
    """Cookie dict for use in TestClient requests."""
    return {"kai_session": token}


def _clear_users() -> None:
    """Remove all users and their sessions — used by tests that need an empty DB."""
    from kai.store.users import _ensure_table

    _ensure_table()  # users table is created lazily; ensure it exists first
    conn = get_conn()
    conn.execute("DELETE FROM session_tokens")
    conn.execute("DELETE FROM users")
    conn.commit()


def _clear_login_attempts() -> None:
    conn = get_conn()
    conn.execute("DELETE FROM login_attempts")
    conn.commit()


# ── Registration gate ─────────────────────────────────────────────────────────


class TestRegistration:
    def test_first_user_can_register(self):
        """Registration succeeds when no users exist (first-run setup)."""
        _clear_users()
        with patch("kai.system.device.key_hash", return_value=_MACHINE_HASH):
            r = client.post("/users/register", json={"name": "first", "pin": "1234"})
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True

    def test_second_registration_blocked(self):
        """After the first user exists, registration returns 403."""
        _clear_users()
        with patch("kai.system.device.key_hash", return_value=_MACHINE_HASH):
            client.post("/users/register", json={"name": "owner", "pin": "1234"})
            r = client.post("/users/register", json={"name": "intruder", "pin": "0000"})
        assert r.status_code == 403
        assert "closed" in r.json()["detail"].lower()

    def test_registration_with_existing_users_blocked(self):
        """With pre-existing users in DB, any registration attempt is 403."""
        _create_user(_unique_name("existing"))
        r = client.post("/users/register", json={"name": "attacker", "pin": "0000"})
        assert r.status_code == 403

    def test_registration_short_pin_rejected(self):
        """PIN shorter than 4 digits is rejected with 400."""
        _clear_users()
        with patch("kai.system.device.key_hash", return_value=_MACHINE_HASH):
            r = client.post("/users/register", json={"name": "alice", "pin": "12"})
        assert r.status_code == 400

    def test_registration_empty_name_rejected(self):
        _clear_users()
        with patch("kai.system.device.key_hash", return_value=_MACHINE_HASH):
            r = client.post("/users/register", json={"name": "", "pin": "1234"})
        assert r.status_code in (400, 422)


# ── Login rate limiting ───────────────────────────────────────────────────────


class TestLoginRateLimit:
    def setup_method(self):
        _clear_login_attempts()

    def test_five_failures_allowed(self):
        """Attempts 1-5 return 401 (wrong creds), not 429."""
        for i in range(5):
            r = client.post("/users/login", json={"name": "nobody", "pin": f"wrong{i}"})
            assert r.status_code == 401, f"Attempt {i + 1} should be 401, got {r.status_code}"

    def test_sixth_attempt_rate_limited(self):
        """Attempt 6 from the same IP returns 429."""
        for _ in range(5):
            client.post("/users/login", json={"name": "nobody", "pin": "wrong"})
        r = client.post("/users/login", json={"name": "nobody", "pin": "wrong"})
        assert r.status_code == 429

    def test_rate_limit_stays_locked(self):
        """Subsequent attempts after lockout remain 429."""
        for _ in range(5):
            client.post("/users/login", json={"name": "nobody", "pin": "wrong"})
        for _ in range(3):
            r = client.post("/users/login", json={"name": "nobody", "pin": "wrong"})
            assert r.status_code == 429


# ── Session token lifecycle ───────────────────────────────────────────────────


class TestSessionLifecycle:
    def setup_method(self):
        self.uid = _create_user(_unique_name("sess"))
        self.name = f"sess_{self.uid}"
        self.tok = _make_session(self.uid, self.name)
        # Fresh client: new ASGI thread, new DB connection → sees just-committed data
        self.c = _fresh_client()

    def test_valid_token_grants_access(self):
        self.c.cookies.set("kai_session", self.tok)
        r = self.c.get("/memory/facts")
        assert r.status_code == 200

    def test_missing_token_returns_401(self):
        r = self.c.get("/memory/facts")
        assert r.status_code == 401

    def test_garbage_token_returns_401(self):
        self.c.cookies.set("kai_session", "notavalidtoken")
        r = self.c.get("/memory/facts")
        assert r.status_code == 401

    def test_replay_after_logout_blocked(self):
        """Token must be invalidated after logout; replaying returns 401."""
        self.c.cookies.set("kai_session", self.tok)
        r = self.c.get("/memory/facts")
        assert r.status_code == 200

        self.c.post("/users/logout")

        r = self.c.get("/memory/facts")
        assert r.status_code == 401

    def test_expired_token_returns_401(self):
        """An expired session token is rejected."""
        token = secrets.token_urlsafe(32)
        tok_hash = hashlib.sha256(token.encode()).hexdigest()
        past = (datetime.now() - timedelta(hours=1)).isoformat()
        conn = get_conn()
        conn.execute(
            "INSERT INTO session_tokens (token, user_id, user_name, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (tok_hash, self.uid, self.name, datetime.now().isoformat(), past),
        )
        conn.commit()
        exp_client = _fresh_client(token)
        r = exp_client.get("/memory/facts")
        assert r.status_code == 401


# ── Protected endpoints (H2a) ─────────────────────────────────────────────────


class TestAuthGuard:
    PROTECTED = [
        ("GET", "/memory/facts"),
        ("GET", "/memory/episodic"),
        ("GET", "/sessions"),
        ("GET", "/info"),
        ("GET", "/dashboard/stats"),
        ("GET", "/docs/list"),
    ]

    @pytest.mark.parametrize("method,path", PROTECTED)
    def test_protected_endpoint_blocks_without_cookie(self, method, path):
        r = client.request(method, path)
        assert r.status_code == 401, f"{method} {path} returned {r.status_code} — expected 401"

    def test_event_stream_blocked_without_cookie(self):
        """H2a: /api/events/{id} must require auth."""
        r = client.get("/api/events/fake-session-id")
        assert r.status_code == 401

    def test_event_stream_accessible_with_cookie(self):
        """H2a: valid session can read event history."""
        uid = _create_user(_unique_name("evt"))
        tok = _make_session(uid, f"evt_{uid}")
        c = _fresh_client(tok)
        r = c.get("/api/events/fake-session-id")
        assert r.status_code in (200, 404)  # 404 = no such session, but auth passed


# ── WebSocket auth (H2b) ──────────────────────────────────────────────────────


class TestWebSocketAuth:
    def test_websocket_rejects_without_cookie(self):
        """H2b: unauthenticated WS connection must be closed with 1008."""
        with client.websocket_connect("/ws/activity/fake-session") as ws:
            with pytest.raises(Exception) as exc_info:
                ws.receive_text()
            e = exc_info.value
            code = getattr(e, "code", None)
            assert code == 1008, f"Expected WS close code 1008, got code={code!r} err={e!r}"

    def test_websocket_accepts_valid_cookie(self):
        """Valid session cookie allows WS handshake to complete."""
        uid = _create_user(_unique_name("ws"))
        tok = _make_session(uid, f"ws_{uid}")
        c = _fresh_client(tok)
        try:
            with c.websocket_connect("/ws/activity/test-session") as ws:
                pass  # connection established without 1008 close
        except Exception as e:
            assert "1008" not in str(e), f"Auth rejected a valid cookie: {e}"


# ── Cross-user isolation (IDOR) ───────────────────────────────────────────────


class TestUserIsolation:
    def setup_method(self):
        self.uid_a = _create_user(_unique_name("alice"))
        self.uid_b = _create_user(_unique_name("bob"))
        self.tok_a = _make_session(self.uid_a, f"alice_{self.uid_a}")
        self.tok_b = _make_session(self.uid_b, f"bob_{self.uid_b}")

        # Create a session owned by user B
        self.bob_session_id = secrets.token_hex(16)
        conn = get_conn()
        conn.execute(
            "INSERT INTO sessions (id, user_id, title, started_at, last_active, message_count) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                self.bob_session_id,
                self.uid_b,
                "Bob's session",
                datetime.now().isoformat(),
                datetime.now().isoformat(),
                2,
            ),
        )
        conn.execute(
            "INSERT INTO session_messages "
            "(session_id, user_id, role, content, timestamp, turn_order) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                self.bob_session_id,
                self.uid_b,
                "user",
                "Bob's secret message",
                datetime.now().isoformat(),
                1,
            ),
        )
        conn.commit()
        self.c = _fresh_client()

    def test_user_a_cannot_read_user_b_messages(self):
        """Alice's token must not return Bob's session messages."""
        self.c.cookies.set("kai_session", self.tok_a)
        r = self.c.get(f"/sessions/{self.bob_session_id}/messages")
        assert r.status_code == 200
        assert r.json() == [], (
            "User A should get empty list for another user's session, not their messages"
        )

    def test_user_a_cannot_load_user_b_session(self):
        """Loading another user's session must return 404."""
        self.c.cookies.set("kai_session", self.tok_a)
        r = self.c.post(f"/sessions/{self.bob_session_id}/load")
        assert r.status_code == 404

    def test_sessions_list_scoped_to_authenticated_user(self):
        """GET /sessions must return only the requesting user's sessions."""
        self.c.cookies.set("kai_session", self.tok_a)
        r = self.c.get("/sessions")
        assert r.status_code == 200
        for session in r.json():
            assert session.get("user_id", self.uid_a) == self.uid_a, (
                "Session list must not include another user's sessions"
            )


class TestDataExport:
    """GET /users/account/export returns the requester's data as a zip and never
    another user's, never auth secrets."""

    def setup_method(self):
        self.uid = _create_user(_unique_name("export"))
        self.tok = _make_session(self.uid, f"export_{self.uid}")
        conn = get_conn()
        conn.execute(
            "INSERT INTO session_messages "
            "(session_id, user_id, role, content, timestamp, turn_order) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("export_sess", self.uid, "user", "remember this fact", datetime.now().isoformat(), 1),
        )
        conn.commit()
        self.c = _fresh_client(self.tok)

    def test_export_requires_auth(self):
        r = _fresh_client().get("/users/account/export")
        assert r.status_code == 401

    def test_export_returns_zip_with_data_json(self):
        import json
        import zipfile

        r = self.c.get("/users/account/export")
        assert r.status_code == 200, r.text
        assert r.headers["content-type"] == "application/zip"

        zf = zipfile.ZipFile(io.BytesIO(r.content))
        assert "data.json" in zf.namelist()
        data = json.loads(zf.read("data.json"))

        assert data["user_id"] == self.uid
        assert data["account"], "account row should be present"
        # Auth material must never be in the export.
        assert "pin_hash" not in data["account"][0]
        assert "session_tokens" not in data["tables"]
        # The user's own content is included.
        msgs = data["tables"].get("session_messages", [])
        assert any(m["content"] == "remember this fact" for m in msgs)


# ── Voice upload size cap (M1) ────────────────────────────────────────────────


class TestVoiceUploadCap:
    def setup_method(self):
        uid = _create_user(_unique_name("voicecap"))
        self.tok = _make_session(uid, f"voicecap_{uid}")
        self.c = _fresh_client()

    def test_oversized_upload_rejected(self):
        """26 MB audio upload must return 413."""
        self.c.cookies.set("kai_session", self.tok)
        oversized = b"x" * (26 * 1024 * 1024)
        r = self.c.post(
            "/voice/transcribe",
            content=oversized,
            headers={"Content-Type": "audio/webm"},
        )
        assert r.status_code == 413

    def test_small_upload_not_rejected_for_size(self):
        """A tiny upload should not trigger the size cap (may fail for other reasons)."""
        self.c.cookies.set("kai_session", self.tok)
        r = self.c.post(
            "/voice/transcribe",
            content=b"tiny",
            headers={"Content-Type": "audio/webm"},
        )
        assert r.status_code != 413


# ── TTS text size cap (M4) ────────────────────────────────────────────────────


class TestTTSSizeCap:
    def setup_method(self):
        uid = _create_user(_unique_name("ttscap"))
        self.tok = _make_session(uid, f"ttscap_{uid}")
        self.c = _fresh_client()

    def test_oversized_text_rejected(self):
        """Text longer than 4000 chars must return 400."""
        self.c.cookies.set("kai_session", self.tok)
        r = self.c.post("/voice/tts", json={"text": "A" * 4001})
        assert r.status_code == 400

    def test_max_length_text_not_size_rejected(self):
        """Exactly 4000 chars should not trigger the cap (may fail for other reasons)."""
        self.c.cookies.set("kai_session", self.tok)
        r = self.c.post("/voice/tts", json={"text": "A" * 4000})
        assert r.status_code != 400 or "too long" not in r.text.lower()


# ── Path traversal ────────────────────────────────────────────────────────────


class TestPathTraversal:
    def test_url_encoded_dotdot_blocked(self):
        """URL-encoded traversal via static files must not return 200."""
        r = client.get("/static/%2e%2e/kai/db.py")
        assert r.status_code != 200

    def test_double_encoded_slash_blocked(self):
        r = client.get("/static/..%2fkai%2fdb.py")
        assert r.status_code != 200

    def test_normal_static_file_still_works(self):
        r = client.get("/static/login.html")
        assert r.status_code == 200


# ── Voice test WAV generation ─────────────────────────────────────────────────


class TestVoiceTest:
    def test_voice_test_returns_wav(self):
        """Public /voice/test must return 200 with audio/wav (not a 500 crash)."""
        r = client.get("/voice/test")
        assert r.status_code == 200
        assert "audio/wav" in r.headers.get("content-type", "")
        assert r.content[:4] == b"RIFF"

    def test_voice_test_wav_length(self):
        """WAV should be ~44 KB (1-second 22050 Hz 16-bit mono)."""
        r = client.get("/voice/test")
        assert r.status_code == 200
        assert len(r.content) > 40_000


# ── Unit: hash helpers ────────────────────────────────────────────────────────


class TestHashHelpers:
    def test_hash_token_is_deterministic(self):
        from kai.apps.web import _hash_token

        assert _hash_token("abc") == _hash_token("abc")

    def test_hash_token_is_always_64_chars(self):
        from kai.apps.web import _hash_token

        for val in ["short", "a" * 100, secrets.token_urlsafe(32)]:
            assert len(_hash_token(val)) == 64

    def test_hash_token_different_inputs_differ(self):
        from kai.apps.web import _hash_token

        assert _hash_token("token_a") != _hash_token("token_b")


# ── Unit: user helpers ────────────────────────────────────────────────────────


class TestUserHelpers:
    def setup_method(self):
        _clear_users()
        self.id1 = _create_user("helper_alice")
        self.id2 = _create_user("helper_bob")

    def test_user_count_is_correct(self):
        from kai.store.users import user_count

        assert user_count() == 2

    def test_get_owner_id_returns_first_user(self):
        from kai.store.users import get_owner_id

        assert get_owner_id() == min(self.id1, self.id2)

    def test_user_count_increases_on_create(self):
        from kai.store.users import user_count

        before = user_count()
        _create_user(_unique_name("extra"))
        assert user_count() == before + 1


# ── Admin server controls (shutdown / restart) ────────────────────────────────


class TestAdminControls:
    """Owner-only gating + correct dispatch for /api/admin/* — the terminal
    request_* calls are patched so the test process never actually exits."""

    def setup_method(self):
        _clear_users()
        self.owner_id = _create_user(_unique_name("owner"))  # lowest id = owner
        self.other_id = _create_user(_unique_name("other"))
        self.owner_tok = _make_session(self.owner_id, "owner")
        self.other_tok = _make_session(self.other_id, "other")

    def test_shutdown_requires_auth(self):
        c = _fresh_client()
        assert c.post("/api/admin/shutdown").status_code == 401

    def test_shutdown_non_owner_forbidden(self):
        c = _fresh_client(self.other_tok)
        with patch("kai.core.lifecycle.request_shutdown") as m:
            r = c.post("/api/admin/shutdown")
        assert r.status_code == 403
        m.assert_not_called()

    def test_shutdown_owner_dispatches(self):
        c = _fresh_client(self.owner_tok)
        with patch("kai.core.lifecycle.request_shutdown") as m:
            r = c.post("/api/admin/shutdown")
        assert r.status_code == 200, r.text
        assert r.json()["action"] == "shutdown"
        m.assert_called_once()

    def test_restart_owner_soft_and_hard(self):
        c = _fresh_client(self.owner_tok)
        with patch("kai.core.lifecycle.request_restart") as m:
            assert c.post("/api/admin/restart", json={"mode": "soft"}).status_code == 200
            assert c.post("/api/admin/restart", json={"mode": "hard"}).status_code == 200
        assert [call.args[0] for call in m.call_args_list] == ["soft", "hard"]

    def test_restart_non_owner_forbidden(self):
        c = _fresh_client(self.other_tok)
        with patch("kai.core.lifecycle.request_restart") as m:
            r = c.post("/api/admin/restart", json={"mode": "soft"})
        assert r.status_code == 403
        m.assert_not_called()

    def test_status_owner_only(self):
        assert _fresh_client(self.other_tok).get("/api/admin/shutdown-status").status_code == 403
        r = _fresh_client(self.owner_tok).get("/api/admin/shutdown-status")
        assert r.status_code == 200
        assert "phase" in r.json()


# ── Account deletion: atomicity ───────────────────────────────────────────────


class TestDeleteUserRollback:
    """delete_user runs ~10 DELETEs as one transaction; a failure partway must
    roll back rather than leave a half-deleted account or a dangling open txn."""

    def setup_method(self):
        _clear_users()
        self.uid = _create_user(_unique_name("doomed"))

    def test_delete_user_rolls_back_on_failure(self):
        from kai.store import users as u

        real_conn = get_conn()
        name = _name_of(self.uid)
        before = u.user_count()

        # Proxy the connection so the final DELETE (the users row) blows up after
        # earlier per-table deletes have already run. rollback/commit delegate to
        # the real connection so we exercise the genuine rollback path.
        class ConnProxy:
            def execute(self, sql, *args, **kwargs):
                if sql.strip().startswith("DELETE FROM users"):
                    raise sqlite3.OperationalError("simulated failure mid-delete")
                return real_conn.execute(sql, *args, **kwargs)

            def commit(self):
                return real_conn.commit()

            def rollback(self):
                return real_conn.rollback()

        with patch.object(u, "get_conn", return_value=ConnProxy()):
            with pytest.raises(sqlite3.OperationalError):
                u.delete_user(self.uid)

        # The account and its row count are unchanged — the partial deletes rolled back.
        assert u.user_count() == before
        assert _name_of(self.uid) == name
        # And the real connection is still usable (no dangling/aborted transaction).
        real_conn.execute("SELECT 1").fetchone()

    def test_delete_user_happy_path_removes_account(self):
        from kai.store import users as u

        before = u.user_count()
        assert u.delete_user(self.uid) is True
        assert u.user_count() == before - 1


def _name_of(uid: int) -> str:
    conn = get_conn()
    row = conn.execute("SELECT name FROM users WHERE id = ?", (uid,)).fetchone()
    return row[0] if row else ""


# ── Cleanup ───────────────────────────────────────────────────────────────────


def teardown_module(module):
    try:
        os.unlink(_tmp_main.name)
    except Exception:
        pass


# ── Hardening pass 2026-09-10: small web fixes ────────────────────────────────


class TestHardeningWebFixes:
    def test_microphone_allowed_for_the_page(self):
        """T2-26: Permissions-Policy microphone=() made the mic fail in every browser."""
        from kai.apps.web import _SecurityHeaders

        policy = dict(_SecurityHeaders._BASE)[b"permissions-policy"]
        assert b"microphone=(self)" in policy
        assert b"camera=()" in policy

    @pytest.mark.parametrize("pin", ["ab12", "123456789", "12 34"])
    def test_pin_must_be_4_to_8_digits(self, pin):
        """T2-37: the form says 4-8 digits but 'ab12' was accepted."""
        _clear_users()
        with patch("kai.system.device.key_hash", return_value=_MACHINE_HASH):
            r = client.post("/users/register", json={"name": "pinuser", "pin": pin})
        assert r.status_code == 400

    def test_info_names_the_signed_in_account(self):
        """T2-01: the app showed "User" for every account."""
        from types import SimpleNamespace

        uid = _create_user(_unique_name("infouser"))
        token = _make_session(uid, "Info Person")
        stub = SimpleNamespace(
            model="m",
            memory=SimpleNamespace(list_facts=lambda **kw: [], recent_episodes=lambda limit=1: []),
        )
        with patch("kai.api.routes.dashboard.brain_for", return_value=stub):
            r = _fresh_client(token).get("/info")
        assert r.status_code == 200, r.text
        assert r.json()["user"]["name"] == "Info Person"
        assert r.json()["max_input_chars"] == cfg.MAX_INPUT_CHARS

    def test_confirm_cancel_route_drops_the_parked_call(self):
        """T1-11 / T2-24: Skip now cancels on the server."""
        from types import SimpleNamespace

        uid = _create_user(_unique_name("skipper"))
        token = _make_session(uid, "skipper")
        state = {"pending": {"name": "pc.deep_scan"}}

        def cancel():
            pending, state["pending"] = state["pending"], None
            return pending["name"] if pending else None

        stub = SimpleNamespace(cancel_pending_confirm=cancel)
        with patch("kai.apps.web._brain_for", return_value=stub):
            r = _fresh_client(token).post(
                "/chat/confirm/cancel", headers={"Origin": "http://127.0.0.1:7860"}
            )
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True, "cancelled": "pc.deep_scan"}
        assert state["pending"] is None


class TestHardeningIsolation:
    @staticmethod
    def _session_row(sid, uid):
        conn = get_conn()
        conn.execute(
            "INSERT INTO sessions (id, user_id, title, started_at, last_active, message_count) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (sid, uid, "t", datetime.now().isoformat(), datetime.now().isoformat(), 0),
        )
        conn.commit()

    def test_events_of_another_accounts_session_are_not_readable(self):
        """T1-10: any signed-in account could read another's tool args and outputs."""
        a = _create_user(_unique_name("evta"))
        b = _create_user(_unique_name("evtb"))
        sid = secrets.token_hex(16)
        self._session_row(sid, a)
        r = _fresh_client(_make_session(b, "evtb")).get(f"/api/events/{sid}")
        assert r.status_code == 404

    def test_event_session_list_only_shows_your_own(self):
        a = _create_user(_unique_name("lista"))
        b = _create_user(_unique_name("listb"))
        sid_a, sid_b = secrets.token_hex(16), secrets.token_hex(16)
        self._session_row(sid_a, a)
        self._session_row(sid_b, b)
        with patch("kai.core.events.get_session_ids", return_value=[sid_a, sid_b]):
            r = _fresh_client(_make_session(b, "listb")).get("/api/events")
        assert r.status_code == 200 and r.json() == [sid_b]

    def test_non_owner_cannot_change_machine_wide_settings(self):
        """T2-54: a guest account created recipes and deleted the owner's models."""
        from kai.store import users as u

        if u.get_owner_id() is None:
            _create_user(_unique_name("owner"))
        guest = _create_user(_unique_name("guest"))
        c = _fresh_client(_make_session(guest, "guest"))
        hdr = {"Origin": "http://127.0.0.1:7860"}
        recipe = {"name": "guest-made", "description": "", "triggers": [], "steps": ["time.now"]}
        assert c.post("/settings/recipes", json=recipe, headers=hdr).status_code == 403
        assert c.delete("/settings/models/anything", headers=hdr).status_code == 403

    def test_successful_logins_do_not_lock_the_machine(self):
        """T1-12 / T2-55: five correct sign-ins in 15 minutes locked every account out."""
        _clear_login_attempts()
        name = _unique_name("frequent")
        _create_user(name, pin="2468")
        with (
            patch("kai.system.device.key_hash", return_value=_MACHINE_HASH),
            patch("kai.apps.web._get_or_create_brain"),
        ):
            for _ in range(7):
                r = client.post("/users/login", json={"name": name, "pin": "2468"})
                assert r.status_code == 200, r.text


class TestHardeningSettingsTruth:
    HDR = {"Origin": "http://127.0.0.1:7860"}

    @staticmethod
    def _stub_brain(uid):
        from types import SimpleNamespace

        from kai.memory.manager import MemoryManager

        memory = MemoryManager(embed_fn=lambda t: [0.0] * 384, user_id=uid)
        return SimpleNamespace(
            memory=memory, active_brain_name="Kai", apply_preset=lambda k, t=None: {}
        )

    def test_settings_are_not_memories_in_the_memory_panel(self):
        """T2-38: settings were listed, searchable, editable and deletable as memories."""
        from kai.memory import semantic

        uid = _create_user(_unique_name("facts"))
        semantic.set_fact("tool_level", "balanced", source="user_setting", user_id=uid)
        semantic.set_fact("favorite_color", "teal", source="stated", user_id=uid)
        c = _fresh_client(_make_session(uid, "facts"))
        with patch("kai.api.routes.memory.brain_for", return_value=self._stub_brain(uid)):
            keys = {f["key"] for f in c.get("/memory/facts").json()}
            found = {f["key"] for f in c.get("/memory/search?q=l").json()["facts"]}
            put = c.put("/memory/facts/tool_level", json={"value": "off"}, headers=self.HDR)
            delete = c.delete("/memory/facts/tool_level", headers=self.HDR)
        assert "favorite_color" in keys and "tool_level" not in keys
        assert "tool_level" not in found
        assert put.status_code == 400 and delete.status_code == 400
        assert semantic.get_fact("tool_level", user_id=uid) == "balanced"

    def test_active_model_is_matched_by_name(self):
        """T2-41: two entries with the same Ollama id were both marked active."""
        from types import SimpleNamespace

        uid = _create_user(_unique_name("models"))
        entries = [
            {"name": "Kai", "ollama_id": cfg.CHAT_MODEL, "think": False, "builtin": True},
            {"name": "kai_think", "ollama_id": cfg.CHAT_MODEL, "think": True, "builtin": False},
        ]
        brain = SimpleNamespace(active_brain_name="kai_think", model=cfg.CHAT_MODEL)
        c = _fresh_client(_make_session(uid, "models"))
        with (
            patch("kai.api.routes.settings.brain_for", return_value=brain),
            patch("kai.llm.models.list_models", return_value=entries),
        ):
            models = c.get("/settings/models").json()["models"]
        assert [m["name"] for m in models if m["active"]] == ["kai_think"]

    def test_the_active_model_cannot_be_removed(self):
        """T2-43: removing the active model left Kai on a model the list no longer showed."""
        from types import SimpleNamespace

        from kai.api import state
        from kai.store import users as u

        owner = u.get_owner_id() or _create_user(_unique_name("owner"))
        c = _fresh_client(_make_session(owner, "owner"))
        with (
            patch.dict(
                state.user_brains, {987654: SimpleNamespace(active_brain_name="tmp_granite")}
            ),
            patch("kai.llm.models.remove_model") as remove,
        ):
            r = c.delete("/settings/models/tmp_granite", headers=self.HDR)
        assert r.status_code == 400 and "active model" in r.json()["detail"]
        remove.assert_not_called()

    def test_mode_shows_the_rule_actually_in_force(self):
        """T2-10: a fresh account showed "Short answers" while the seed rule applied."""
        from types import SimpleNamespace

        from kai.api.routes.settings import _MODE_RULES
        from kai.memory import procedural

        uid = _create_user(_unique_name("mode"))
        procedural.seed_defaults(user_id=uid)
        c = _fresh_client(_make_session(uid, "mode"))
        brain = SimpleNamespace(memory=SimpleNamespace(user_id=uid))
        with patch("kai.api.routes.settings.brain_for", return_value=brain):
            fresh = c.get("/settings/mode").json()
            procedural.set_rule("response_length", _MODE_RULES["long"], user_id=uid)
            chosen = c.get("/settings/mode").json()
        assert fresh == {"mode": "default", "label": "Default"}
        assert chosen["mode"] == "long"

    def test_clamped_temperatures_are_reported(self):
        """T2-19: typing 5 saved 2.0 without a word."""
        uid = _create_user(_unique_name("temps"))
        c = _fresh_client(_make_session(uid, "temps"))
        with patch("kai.api.routes.settings.brain_for", return_value=self._stub_brain(uid)):
            r = c.post(
                "/settings/preset-temps",
                json={"temps": {"creative": 5, "normal": 0.3}},
                headers=self.HDR,
            )
        assert r.status_code == 200
        assert r.json()["clamped"] == {"creative": cfg.TEMP_MAX}


class TestHardeningToolContracts:
    def test_a_recipe_needs_a_description(self):
        """T2-33: the recipe form saved recipes with no description for Kai to choose by."""
        from kai.store import users as u

        owner = u.get_owner_id() or _create_user(_unique_name("owner"))
        c = _fresh_client(_make_session(owner, "owner"))
        r = c.post(
            "/settings/recipes",
            json={"name": "qa-nodesc", "description": " ", "triggers": [], "steps": ["time.now"]},
            headers={"Origin": "http://127.0.0.1:7860"},
        )
        assert r.status_code == 400 and "Describe" in r.json()["detail"]


class TestRedesignReceipts:
    def test_chat_stream_carries_receipts_and_provenance(self):
        """UI redesign step 2: an unmapped meta used to become an empty token event."""
        import json
        from types import SimpleNamespace

        receipt = {
            "name": "time.now",
            "args": {},
            "ok": True,
            "error": "",
            "kind": "",
            "duration_ms": 3,
            "output": "12:00",
        }

        def run_stream(user_input, on_status=None):
            yield "", False, {"tool_result": receipt}
            yield "It's noon.", False, {}
            yield (
                "",
                True,
                {
                    "message_id": 7,
                    "latency_ms": 10,
                    "receipts": [receipt],
                    "grounding": {"state": "ran"},
                },
            )

        uid = _create_user(_unique_name("receipts"))
        c = _fresh_client(_make_session(uid, "receipts"))
        with patch("kai.apps.web._brain_for", return_value=SimpleNamespace(run_stream=run_stream)):
            body = c.post(
                "/chat",
                json={"message": "what time is it"},
                headers={"Origin": "http://127.0.0.1:7860"},
            ).text
        events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
        assert {"type": "tool_result", **receipt} in events
        assert not any(e["type"] == "token" and e["text"] == "" for e in events)
        done = next(e for e in events if e["type"] == "done")
        assert done["grounding"] == {"state": "ran"} and done["receipts"] == [receipt]


class TestRedesignApprovalCard:
    HDR = {"Origin": "http://127.0.0.1:7860"}

    @staticmethod
    def _client(name):
        uid = _create_user(_unique_name(name))
        return _fresh_client(_make_session(uid, name))

    def test_pending_route_needs_a_signed_in_user(self):
        assert _fresh_client().get("/chat/pending").status_code == 401

    def test_pending_route_returns_the_waiting_call(self):
        from types import SimpleNamespace

        waiting = {"call_id": "c1", "name": "system.kill_process", "label": "Stop", "args": {}}
        stub = SimpleNamespace(pending_confirm_summary=lambda: waiting)
        with patch("kai.apps.web._brain_for", return_value=stub):
            r = self._client("pending").get("/chat/pending")
        assert r.status_code == 200 and r.json() == waiting

    def test_skip_with_a_stale_call_id_is_refused(self):
        from types import SimpleNamespace

        stub = SimpleNamespace(cancel_pending_confirm=lambda call_id=None: None)
        with patch("kai.apps.web._brain_for", return_value=stub):
            r = self._client("staleskip").post(
                "/chat/confirm/cancel", json={"call_id": "old"}, headers=self.HDR
            )
        assert r.status_code == 409

    def test_chat_passes_the_cards_call_id(self):
        from types import SimpleNamespace

        seen = {}

        def run_stream(user_input, on_status=None, confirm_call_id=None):
            seen["id"] = confirm_call_id
            yield "", True, {}

        with patch("kai.apps.web._brain_for", return_value=SimpleNamespace(run_stream=run_stream)):
            self._client("cardgo").post(
                "/chat", json={"message": "go ahead", "confirm": "c9"}, headers=self.HDR
            )
        assert seen["id"] == "c9"


class TestAccountsAndLevels:
    """docs/active/PERMISSIONS.md increment 1: levels, the accounts API and the audit log."""

    HDR = {"Origin": "http://127.0.0.1:7860"}

    def setup_method(self):
        from kai.store import users as u

        _clear_users()
        self.owner = _create_user(_unique_name("owner"))
        self.admin = _create_user(_unique_name("admin"))
        u.update_account(self.admin, level="admin")
        self.member = _create_user(_unique_name("member"))
        self.tok = {
            "owner": _make_session(self.owner, "owner"),
            "admin": _make_session(self.admin, "admin"),
            "member": _make_session(self.member, "member"),
        }

    def _as(self, who):
        return _fresh_client(self.tok[who])

    def test_admins_now_hold_server_control(self):
        with patch("kai.core.lifecycle.request_restart") as m:
            r = self._as("admin").post(
                "/api/admin/restart", json={"mode": "soft"}, headers=self.HDR
            )
        assert r.status_code == 200, r.text
        m.assert_called_once_with("soft")
        with patch("kai.core.lifecycle.request_restart") as m:
            r = self._as("member").post(
                "/api/admin/restart", json={"mode": "soft"}, headers=self.HDR
            )
        assert r.status_code == 403
        m.assert_not_called()

    def test_only_owner_and_admins_see_accounts(self):
        assert self._as("member").get("/accounts").status_code == 403
        r = self._as("admin").get("/accounts")
        assert r.status_code == 200, r.text
        by_id = {a["id"]: a for a in r.json()["accounts"]}
        assert {self.owner, self.admin, self.member} <= set(by_id)
        assert by_id[self.owner]["level"] == "owner" and not by_id[self.owner]["can_edit"]
        assert r.json()["assignable_levels"] == ["member", "guest"]

    def test_admins_create_members_and_guests_but_never_admins(self):
        from kai.store import audit

        c = self._as("admin")
        body = {"name": _unique_name("new"), "pin": "1234", "level": "guest"}
        r = c.post("/accounts", json=body, headers=self.HDR)
        assert r.status_code == 200, r.text
        assert r.json()["account"]["created_by"] == self.admin
        assert r.json()["account"]["level"] == "guest"

        body = {"name": _unique_name("sneaky"), "pin": "1234", "level": "admin"}
        assert c.post("/accounts", json=body, headers=self.HDR).status_code == 403
        refused = [
            e for e in audit.list_entries() if e["action"] == "account.create" and not e["ok"]
        ]
        assert refused and refused[0]["actor_id"] == self.admin

    def test_admins_edit_only_what_they_created(self):
        c = self._as("admin")
        rename = {"name": _unique_name("renamed")}
        assert c.patch(f"/accounts/{self.member}", json=rename, headers=self.HDR).status_code == 403
        body = {"name": _unique_name("mine"), "pin": "1234"}
        mine = c.post("/accounts", json=body, headers=self.HDR).json()["account"]["id"]
        r = c.patch(f"/accounts/{mine}", json={"level": "guest"}, headers=self.HDR)
        assert r.status_code == 200 and r.json()["account"]["level"] == "guest"
        assert (
            c.patch(f"/accounts/{mine}", json={"level": "admin"}, headers=self.HDR).status_code
            == 403
        )

    def test_only_the_owner_kicks_and_a_kick_signs_them_out_at_once(self):
        kick = f"/accounts/{self.member}/kick"
        assert self._as("admin").post(kick, json={}, headers=self.HDR).status_code == 403
        assert self._as("member").get("/accounts").status_code == 403  # signed in, not allowed
        r = self._as("owner").post(kick, json={}, headers=self.HDR)
        assert r.status_code == 200 and r.json()["sessions_ended"] >= 1
        assert self._as("member").get("/accounts").status_code == 401  # signed out

    def test_only_the_owner_grants_and_a_grant_must_fit_the_level(self):
        body = {"grants": ["tools.destructive"]}
        url = f"/accounts/{self.member}/grants"
        assert self._as("admin").put(url, json=body, headers=self.HDR).status_code == 403
        r = self._as("owner").put(url, json=body, headers=self.HDR)
        assert r.status_code == 200 and r.json()["account"]["grants"] == ["tools.destructive"]
        url = f"/accounts/{self.admin}/grants"
        assert self._as("owner").put(url, json=body, headers=self.HDR).status_code == 400

    def test_ownership_transfer_and_the_audit_trail(self):
        from kai.store import users as u

        r = self._as("owner").post(
            "/accounts/transfer-owner", json={"to_id": self.admin}, headers=self.HDR
        )
        assert r.status_code == 200, r.text
        assert u.get_owner_id() == self.admin and u.get_level(self.owner) == "admin"
        entries = self._as("admin").get("/accounts/audit").json()["entries"]
        assert entries[0]["action"] == "account.transfer_owner"
        assert entries[0]["actor_id"] == self.owner
