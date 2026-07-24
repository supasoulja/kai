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

# Import and configure the app — no _init(), so no model loading
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))
from web import app, setup_app

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
        from web import _hash_token

        assert _hash_token("abc") == _hash_token("abc")

    def test_hash_token_is_always_64_chars(self):
        from web import _hash_token

        for val in ["short", "a" * 100, secrets.token_urlsafe(32)]:
            assert len(_hash_token(val)) == 64

    def test_hash_token_different_inputs_differ(self):
        from web import _hash_token

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
