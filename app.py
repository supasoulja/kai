"""
Kai — native desktop app.

Wraps the FastAPI web UI in a pywebview native window with system tray
support, global hotkey, and single-instance locking.

Usage:
    pythonw app.py          # no console window
    python  app.py          # with console (for debugging)
"""

import json
import os
import sys
import threading
import time
from pathlib import Path
from urllib import request as urlreq
from urllib.error import URLError

import kai.config as cfg
from kai.system.platform import IS_LINUX as _IS_LINUX
from kai.util import log

# ── Constants ──────────────────────────────────────────────────────────────────
PORT = 7860
HOST = "127.0.0.1"
URL = f"http://{HOST}:{PORT}"

_ROOT = Path(__file__).parent

# Use PNG on Linux (no .ico support in GTK), .ico on Windows
_ICON = _ROOT / "kai" / "static" / ("icon-192.png" if _IS_LINUX else "kai.ico")
# App settings now live under var/ (honors KAI_VAR_DIR) instead of the fragile
# "kai's memory" path inside the source package.
_SETTINGS_FILE = cfg.APP_SETTINGS_PATH
_SETTINGS_DIR = _SETTINGS_FILE.parent
_OLD_SETTINGS_FILE = _ROOT / "kai" / "memory" / "kai's memory" / "app_settings.json"

# ── Global state ──────────────────────────────────────────────────────────────
_window = None  # pywebview window
_tray = None  # pystray Icon
_server_ready = threading.Event()


# ── Settings ──────────────────────────────────────────────────────────────────


def _load_settings() -> dict:
    # One-time migration from the old in-package location.
    if not _SETTINGS_FILE.exists() and _OLD_SETTINGS_FILE.exists():
        try:
            _SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
            _SETTINGS_FILE.write_text(
                _OLD_SETTINGS_FILE.read_text(encoding="utf-8"), encoding="utf-8"
            )
            _OLD_SETTINGS_FILE.unlink(missing_ok=True)
        except Exception:
            pass
    try:
        return json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_settings(data: dict) -> None:
    _SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    _SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ── Single-instance lock ─────────────────────────────────────────────────────


def _is_already_running() -> bool:
    """POST to the running instance's show-window endpoint."""
    try:
        req = urlreq.Request(
            f"{URL}/api/show-window", method="POST", data=b"", headers={"Content-Length": "0"}
        )
        resp = urlreq.urlopen(req, timeout=2)
        return resp.status == 200
    except (URLError, OSError):
        return False


# ── Server ────────────────────────────────────────────────────────────────────


def _start_server() -> None:
    """Start uvicorn in a background thread.  Sets _server_ready when listening."""
    import uvicorn

    import web

    # Reuse the shared setup (middleware + init)
    web.setup_app(host=HOST, port=PORT, scheme="http")

    # Add the desktop-only endpoint for single-instance bring-to-front
    @web.app.post("/api/show-window")
    async def _show_window():
        if _window:
            _window.show()
            _window.restore()  # un-minimise if needed
        return {"ok": True}

    class _SignalReady(uvicorn.Config):
        """Subclass so we can detect when the server socket is bound."""

        pass

    config = uvicorn.Config(
        web.app,
        host=HOST,
        port=PORT,
        log_level="warning",
    )
    server = uvicorn.Server(config)

    # Monkey-patch startup to signal readiness
    _original_startup = server.startup

    async def _patched_startup(sockets=None):
        await _original_startup(sockets)
        _server_ready.set()

    server.startup = _patched_startup
    server.run()


def _wait_for_server(timeout: float = 30) -> bool:
    """Block until the server is accepting connections."""
    return _server_ready.wait(timeout=timeout)


# ── System tray ───────────────────────────────────────────────────────────────


def _create_tray_icon():
    """Generates a simple orange square placeholder icon."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([4, 4, 60, 60], radius=12, fill=(230, 126, 34))
    draw.text((18, 16), "K", fill="white")
    return img


def _start_tray() -> None:
    """Start pystray in a background thread."""
    # On Linux, pystray's default GTK/AppIndicator backend acquires the GLib
    # default main context on THIS background thread, which then blocks
    # pywebview's GTK/WebKit loop on the main thread (GLib-GIO-CRITICAL:
    # g_application_run cannot acquire the default main context). The xorg
    # backend uses pure Xlib with no GLib loop, so the two don't collide.
    # Must be set before pystray is imported.
    if _IS_LINUX:
        os.environ.setdefault("PYSTRAY_BACKEND", "xorg")
    import pystray

    image = _create_tray_icon()

    def on_show(_icon, _item):
        if _window:
            _window.show()
            _window.restore()

    def on_reload(_icon, _item):
        if _window:
            _window.load_url(URL)

    def on_quit(_icon, _item):
        _clean_quit()

    menu = pystray.Menu(
        pystray.MenuItem("Show Kai", on_show, default=True),
        pystray.MenuItem("Reload", on_reload),
        pystray.MenuItem("Quit", on_quit),
    )

    global _tray
    # Plain hyphen, not an em-dash: the X11 tray backend encodes the title as
    # latin-1 via Xlib and \u2014 raises UnicodeEncodeError there.
    _tray = pystray.Icon("Kai", image, "Kai - Local AI Agent", menu)
    _tray.run()


# ── Close dialog (JS bridge) ─────────────────────────────────────────────────

_CLOSE_DIALOG_JS = """
(function() {
    if (document.getElementById('kai-close-overlay')) {
        document.getElementById('kai-close-overlay').style.display = 'flex';
        return;
    }

    const overlay = document.createElement('div');
    overlay.id = 'kai-close-overlay';
    overlay.style.cssText = `
        position: fixed; inset: 0; z-index: 99999;
        display: flex; align-items: center; justify-content: center;
        background: rgba(0,0,0,0.5); backdrop-filter: blur(4px);
    `;

    overlay.innerHTML = `
    <div style="
        background: #1e1e2e; color: #cdd6f4; border-radius: 12px;
        padding: 28px 32px; min-width: 340px; font-family: system-ui, sans-serif;
        box-shadow: 0 20px 60px rgba(0,0,0,0.5);
    ">
        <h3 style="margin:0 0 16px; font-size:18px; font-weight:600;">Close Kai?</h3>
        <div style="display:flex; flex-direction:column; gap:10px;">
            <button id="kai-close-minimize" style="
                padding:10px 16px; border:1px solid #45475a; border-radius:8px;
                background:#313244; color:#cdd6f4; cursor:pointer; font-size:14px;
                text-align:left;
            ">Minimize to tray <span style='color:#6c7086; font-size:12px;'>— keep running in background</span></button>
            <button id="kai-close-quit" style="
                padding:10px 16px; border:1px solid #45475a; border-radius:8px;
                background:#313244; color:#cdd6f4; cursor:pointer; font-size:14px;
                text-align:left;
            ">Quit completely <span style='color:#6c7086; font-size:12px;'>— stop server, release VRAM</span></button>
        </div>
        <div style="margin-top:14px; display:flex; align-items:center; justify-content:space-between;">
            <label style="font-size:13px; color:#6c7086; cursor:pointer;">
                <input type="checkbox" id="kai-close-remember" style="margin-right:6px;">
                Remember my choice
            </label>
            <button id="kai-close-cancel" style="
                padding:6px 16px; border:1px solid #45475a; border-radius:6px;
                background:transparent; color:#6c7086; cursor:pointer; font-size:13px;
            ">Cancel</button>
        </div>
    </div>
    `;

    document.body.appendChild(overlay);

    document.getElementById('kai-close-minimize').onclick = function() {
        var remember = document.getElementById('kai-close-remember').checked;
        pywebview.api.close_action('minimize', remember);
        overlay.style.display = 'none';
    };
    document.getElementById('kai-close-quit').onclick = function() {
        var remember = document.getElementById('kai-close-remember').checked;
        pywebview.api.close_action('quit', remember);
    };
    document.getElementById('kai-close-cancel').onclick = function() {
        overlay.style.display = 'none';
    };
    overlay.onclick = function(e) {
        if (e.target === overlay) overlay.style.display = 'none';
    };
})();
"""


# ── Clean quit (run the end-of-session ritual before exiting) ────────────────

# Shown while the shutdown ritual runs so the user knows not to force-kill while
# Kai finishes writing the welcome-back note and embedding the session.
_SAVING_OVERLAY_JS = """
(function() {
    var o = document.getElementById('kai-saving-overlay');
    if (!o) {
        o = document.createElement('div');
        o.id = 'kai-saving-overlay';
        o.style.cssText = `
            position: fixed; inset: 0; z-index: 100000;
            display: flex; align-items: center; justify-content: center;
            background: rgba(0,0,0,0.6); backdrop-filter: blur(4px);
            font-family: system-ui, sans-serif; color: #cdd6f4;
        `;
        o.innerHTML = `
        <div style="background:#1e1e2e; border-radius:12px; padding:28px 36px;
                    text-align:center; box-shadow:0 20px 60px rgba(0,0,0,0.5);">
            <div style="font-size:17px; font-weight:600; margin-bottom:8px;">Saving session…</div>
            <div style="font-size:13px; color:#a6adc8;">Finishing embeddings — please don't force-quit.</div>
        </div>`;
        document.body.appendChild(o);
    }
    o.style.display = 'flex';
})();
"""

_clean_quit_started = threading.Event()


# Hard ceiling on the whole quit sequence. If anything wedges — a blocked
# evaluate_js, a dead Ollama mid sleep-cycle/re-embed, a stuck background pool —
# the process still dies instead of leaving the window frozen forever. The
# ritual writes incrementally (INSERT OR REPLACE, idempotent), so a cut-off
# embed just resumes on the next shutdown.
_QUIT_WATCHDOG_SECS = 90.0


def _clean_quit() -> None:
    """Run Kai's graceful shutdown, then exit. Idempotent across all quit paths."""
    if _clean_quit_started.is_set():
        return
    _clean_quit_started.set()

    # Guarantee termination no matter what blocks below.
    watchdog = threading.Timer(_QUIT_WATCHDOG_SECS, lambda: os._exit(0))
    watchdog.daemon = True
    watchdog.start()

    # Show the "saving…" overlay best-effort and OFF the critical path. On GTK,
    # evaluate_js from a worker thread can block indefinitely while the window is
    # closing, so it must never gate the ritual (this was the freeze: the old
    # code ran it first and the hang stalled the whole shutdown before it began).
    def _show_overlay():
        try:
            if _window:
                _window.evaluate_js(_SAVING_OVERLAY_JS)
        except Exception:
            pass

    threading.Thread(target=_show_overlay, name="kai-saving-overlay", daemon=True).start()

    def _worker():
        try:
            from kai.core import lifecycle

            lifecycle.graceful_shutdown(reason="desktop quit")
        except Exception as exc:
            log.warn(f"Clean quit failed: {exc}")
        finally:
            try:
                if _tray:
                    _tray.stop()
            except Exception:
                pass
            os._exit(0)

    threading.Thread(target=_worker, name="kai-clean-quit", daemon=True).start()


# ── JS bridge (exposed to the webview) ───────────────────────────────────────


class _Api:
    """Methods callable from JavaScript via pywebview.api.*"""

    def open_link(self, url: str):
        """Open an external web link in its own window.

        The whole app lives in a single chrome-less webview, so letting a chat
        link navigate it strands the user on the target page with no Back
        button. Spawning a separate window gives them the OS title-bar close
        button as an escape hatch, and keeps the chat untouched behind it.
        """
        if not isinstance(url, str):
            return
        u = url.strip()
        # Only real web links — never file://, javascript:, data:, etc.
        if not (u.startswith("http://") or u.startswith("https://")):
            return

        import webview

        # Offset the popup off the main window's top-left so it lands "next to"
        # the chat rather than dead-centre on top of it.
        x = y = None
        if _window is not None:
            try:
                x = int(_window.x) + 60
                y = int(_window.y) + 60
            except Exception:
                x = y = None

        webview.create_window(
            u,  # title — shows the URL until something better
            u,
            width=900,
            height=800,
            x=x,
            y=y,
            text_select=True,
        )

    def close_action(self, action: str, remember: bool = False):
        if remember:
            settings = _load_settings()
            settings["close_action"] = action
            _save_settings(settings)

        if action == "minimize":
            if _window:
                _window.hide()
        elif action == "quit":
            _clean_quit()


# ── Closing handler ───────────────────────────────────────────────────────────


def _on_closing():
    """Intercept the window close button."""
    settings = _load_settings()
    remembered = settings.get("close_action")

    if remembered == "minimize":
        if _window:
            _window.hide()
        return False  # prevent pywebview from closing

    if remembered == "quit":
        # Keep the window up to show the "saving…" overlay; the worker exits.
        _clean_quit()
        return False  # prevent immediate close; _clean_quit handles exit

    # No remembered choice — show the dialog
    if _window:
        _window.evaluate_js(_CLOSE_DIALOG_JS)
    return False  # prevent close; dialog handles it


# ── Global hotkey ─────────────────────────────────────────────────────────────


def _setup_hotkey():
    try:
        import keyboard

        keyboard.add_hotkey(
            "ctrl+shift+k", lambda: (_window.show(), _window.restore()) if _window else None
        )
        keyboard.add_hotkey("ctrl+shift+r", lambda: (_window.load_url(URL)) if _window else None)
    except ImportError:
        log.info("'keyboard' package not installed — global hotkey disabled.")
    except Exception as exc:
        log.info(f"Global hotkey failed: {exc}")


# ── Startup shortcut ─────────────────────────────────────────────────────────


def _linux_autostart_path() -> Path:
    return Path.home() / ".config" / "autostart" / "kai.desktop"


def _linux_autostart_content() -> str:
    python = str(Path(sys.executable).resolve())
    script = str(Path(__file__).resolve())
    icon = str(_ICON.resolve())
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Kai\n"
        "Comment=Kai — Local AI Agent\n"
        f"Exec={python} {script}\n"
        f"Icon={icon}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


def is_startup_enabled() -> bool:
    if _IS_LINUX:
        return _linux_autostart_path().exists()
    lnk = (
        Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Startup"
        / "Kai.lnk"
    )
    return lnk.exists()


def set_startup(enabled: bool) -> None:
    if _IS_LINUX:
        path = _linux_autostart_path()
        if not enabled:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_linux_autostart_content())
        return

    # Windows: .lnk via PowerShell
    startup = (
        Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Startup"
    )
    lnk_path = startup / "Kai.lnk"
    if not enabled:
        lnk_path.unlink(missing_ok=True)
        return

    target = str(Path(sys.executable).parent / "pythonw.exe")
    args = f'"{Path(__file__).resolve()}"'
    working_dir = str(_ROOT)
    ps_script = (
        f"$ws = New-Object -ComObject WScript.Shell; "
        f'$sc = $ws.CreateShortcut("{lnk_path}"); '
        f'$sc.TargetPath = "{target}"; '
        f"$sc.Arguments = {args}; "
        f'$sc.WorkingDirectory = "{working_dir}"; '
        f'$sc.Description = "Kai - Local AI Agent"; '
        f"$sc.Save()"
    )
    os.system(f'powershell -Command "{ps_script}"')


# ── Entry point ──────────────────────────────────────────────────────────────


def main():
    global _window

    # Record the entry point so a hard restart relaunches the desktop app
    # (not a headless server). Set before the server thread calls setup_app.
    os.environ["KAI_ENTRYPOINT"] = "app"

    # ── Single-instance lock ──────────────────────────────────────────
    if _is_already_running():
        print("Kai is already running — brought existing window to front.")
        sys.exit(0)

    print("Starting Kai...")

    # ── Start server in background thread ─────────────────────────────
    server_thread = threading.Thread(target=_start_server, daemon=True)
    server_thread.start()

    # ── Start tray in background thread ───────────────────────────────
    tray_thread = threading.Thread(target=_start_tray, daemon=True)
    tray_thread.start()

    # ── Global hotkey ─────────────────────────────────────────────────
    _setup_hotkey()

    # ── Wait for server ───────────────────────────────────────────────
    if not _wait_for_server(timeout=30):
        log.warn("Server failed to start within 30 seconds.")
        sys.exit(1)
    log.ok(f"Server ready at {URL}")

    # ── Create pywebview window (must be on main thread) ──────────────
    import webview

    api = _Api()
    _window = webview.create_window(
        "Kai \u2014 Local AI Agent",
        URL,
        js_api=api,
        width=1200,
        height=800,
        min_size=(800, 600),
        text_select=True,
    )

    _window.events.closing += _on_closing

    # ── Start pywebview event loop (blocking) ─────────────────────────
    # debug=True enables the WebKit inspector (right-click → Inspect Element).
    # Set KAI_DEBUG=0 to disable once things are stable.
    _debug = os.environ.get("KAI_DEBUG", "1").lower() not in ("0", "false", "no", "")
    # private_mode=False gives WebKitGTK a PERSISTENT data store. In pywebview's
    # default ephemeral/private mode, WebKitGTK does NOT expose localStorage — a
    # bare reference throws "Can't find variable: localStorage", which aborts
    # app.js on its first preference read and leaves the window dead. A persistent
    # store also keeps the session cookie + theme across restarts (stay logged in).
    _storage_dir = _SETTINGS_DIR / "webview"
    _storage_dir.mkdir(parents=True, exist_ok=True)
    webview.start(debug=_debug, private_mode=False, storage_path=str(_storage_dir))

    # If we reach here, webview exited normally — run the ritual as a backstop
    # (idempotent: a no-op if a quit path already ran it).
    _clean_quit()
    # Give the worker a moment; it calls os._exit when done.
    time.sleep(60)
    os._exit(0)


if __name__ == "__main__":
    main()
