"""
watchdog/common.py — shared helpers for Kai's standalone scanner agents.

Self-contained on purpose: stdlib + requests only, no `kai.*` imports. This
folder is meant to be copied to *any* PC — including ones without Kai
installed — so it can't depend on the rest of the repo. Run join.py once per
machine to pair it, then point any number of scanner scripts at the resulting
config file; they all share the same device identity.
"""

import argparse
import json
import socket
import time
from pathlib import Path

import requests

DEFAULT_CONFIG_PATH = Path(__file__).parent / "watchdog_config.json"


def default_pc_label() -> str:
    """A human-friendly default label for this machine — overridable at join time."""
    return socket.gethostname()


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> dict | None:
    """Load this machine's paired identity, or None if it hasn't joined yet."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def save_config(config: dict, path: Path = DEFAULT_CONFIG_PATH) -> None:
    path.write_text(json.dumps(config, indent=2))


def send_event(
    script_id: str,
    severity: str,
    message: str,
    suggestion: str = "",
    config_path: Path = DEFAULT_CONFIG_PATH,
) -> dict:
    """
    Report a triggered condition to Kai.

    Reads this machine's paired identity from disk and signs the report with
    it — Kai checks device_id/device_key against her registry before queueing
    anything, so an unpaired or revoked machine can't spam her.

    Raises RuntimeError if this machine hasn't been paired yet (run join.py first).
    """
    config = load_config(config_path)
    if config is None:
        raise RuntimeError(
            f"No paired identity at {config_path} — run join.py first to pair this machine."
        )

    resp = requests.post(
        f"{config['server_url'].rstrip('/')}/api/watchdog/event",
        json={
            "device_id": config["device_id"],
            "device_key": config["device_key"],
            "script_id": script_id,
            "severity": severity,
            "message": message,
            "suggestion": suggestion,
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def scanner_arg_parser(description: str) -> argparse.ArgumentParser:
    """Shared CLI surface for scanner scripts: how often to check, where the
    paired-identity config lives."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--interval", type=float, default=60.0,
        help="seconds between checks (default: 60)",
    )
    parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG_PATH,
        help=f"path to the paired-identity config (default: {DEFAULT_CONFIG_PATH})",
    )
    return parser


def run_loop(check_fn, interval: float):
    """
    Run `check_fn()` forever, `interval` seconds apart.

    `check_fn` takes no arguments and returns nothing — it's expected to call
    send_event() itself when its trigger condition fires. Keeping the loop this
    thin means every scanner looks the same and the trigger logic is the only
    thing that varies between them.
    """
    while True:
        try:
            check_fn()
        except Exception as e:
            print(f"[watchdog] check failed: {e}")
        time.sleep(interval)
