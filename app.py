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
import socket
import sys
import threading
import time
from pathlib import Path
from urllib import request as urlreq
from urllib.error import URLError

# ── Constants ──────────────────────────────────────────────────────────────────
PORT = 7860
HOST = "127.0.0.1"
URL  = f"http://{HOST}:{PORT}"

_ROOT    = Path(__file__).parent
_ICON    = _ROOT / "kai" / "static" / "kai.ico"
_SETTINGS_DIR  = _ROOT / "kai" / "memory" / "kai's memory"
_SETTINGS_FILE = _SETTINGS_DIR / "app_settings.json"

# ── Global state ──────────────────────────────────────────────────────────────
_window = None          # pywebview window
_tray   = None          # pystray Icon
_server_ready = threading.Event()


# ── Settings ──────────────────────────────────────────────────────────────────

def _load_settings() -> dict:
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
        req = urlreq.Request(f"{URL}/api/show-window", method="POST",
                             data=b"", headers={"Content-Length": "0"})
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
            _window.restore()   # un-minimise if needed
        return {"ok": True}

    class _SignalReady(uvicorn.Config):
        """Subclass so we can detect when the server socket is bound."""
        pass

    config = uvicorn.Config(
        web.app, host=HOST, port=PORT,
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
    import pystray

    image = _create_tray_icon()

    def on_show(_icon, _item):
        if _window:
            _window.show()
            _window.restore()

    def on_quit(_icon, _item):
        _icon.stop()
        if _window:
            _window.destroy()
        os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem("Show Kai", on_show, default=True),
        pystray.MenuItem("Quit", on_quit),
    )

    global _tray
    _tray = pystray.Icon("Kai", image, "Kai \u2014 Local AI Agent", menu)
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


# ── JS bridge (exposed to the webview) ───────────────────────────────────────

class _Api:
    """Methods callable from JavaScript via pywebview.api.*"""

    def close_action(self, action: str, remember: bool = False):
        if remember:
            settings = _load_settings()
            settings["close_action"] = action
            _save_settings(settings)

        if action == "minimize":
            if _window:
                _window.hide()
        elif action == "quit":
            if _tray:
                _tray.stop()
            if _window:
                _window.destroy()
            os._exit(0)


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
        # Let pywebview close, then exit
        if _tray:
            _tray.stop()
        # Return True to allow the close
        threading.Timer(0.2, lambda: os._exit(0)).start()
        return True

    # No remembered choice — show the dialog
    if _window:
        _window.evaluate_js(_CLOSE_DIALOG_JS)
    return False  # prevent close; dialog handles it


# ── Global hotkey ─────────────────────────────────────────────────────────────

def _setup_hotkey():
    try:
        import keyboard
        keyboard.add_hotkey("ctrl+shift+k", lambda: (
            _window.show(), _window.restore()
        ) if _window else None)
    except ImportError:
        print("[~] 'keyboard' package not installed — global hotkey disabled.")
    except Exception as exc:
        print(f"[~] Global hotkey failed: {exc}")


# ── Startup shortcut ─────────────────────────────────────────────────────────

def _get_startup_folder() -> Path:
    return Path(os.environ.get(
        "APPDATA", Path.home() / "AppData" / "Roaming"
    )) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def is_startup_enabled() -> bool:
    return (_get_startup_folder() / "Kai.lnk").exists()


def set_startup(enabled: bool) -> None:
    lnk_path = _get_startup_folder() / "Kai.lnk"
    if not enabled:
        lnk_path.unlink(missing_ok=True)
        return

    # Create .lnk shortcut using PowerShell (no extra deps)
    target = str(Path(sys.executable).parent / "pythonw.exe")
    args = f'"{Path(__file__).resolve()}"'
    working_dir = str(_ROOT)

    ps_script = (
        f'$ws = New-Object -ComObject WScript.Shell; '
        f'$sc = $ws.CreateShortcut("{lnk_path}"); '
        f'$sc.TargetPath = "{target}"; '
        f'$sc.Arguments = {args}; '
        f'$sc.WorkingDirectory = "{working_dir}"; '
        f'$sc.Description = "Kai - Local AI Agent"; '
        f'$sc.Save()'
    )
    os.system(f'powershell -Command "{ps_script}"')


# ── Entry point ──────────────────────────────────────────────────────────────

def main():
    global _window

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
        print("[!] Server failed to start within 30 seconds.")
        sys.exit(1)
    print(f"[+] Server ready at {URL}")

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
    webview.start()

    # If we reach here, webview exited normally (e.g. quit via tray)
    if _tray:
        _tray.stop()
    os._exit(0)


if __name__ == "__main__":
    main()
