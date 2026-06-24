"""
Centralized diagnostic logging.

The codebase historically used bare print("[+] ...") / "[!]" / "[~]" for
operational messages, with no way to silence or redirect them. These helpers
keep that familiar prefix style but route through the stdlib logging module, so
verbosity and destination are configurable in one place.

Convention:
  log.ok("...")    → "[+] ..."   success / ready
  log.info("...")  → "[~] ..."   in-progress / status
  log.warn("...")  → "[!] ..."   non-fatal problem
  log.debug("...")               only emitted when cfg.DEBUG is on

Plain print() remains correct for the CLI's user-facing chat output — that's an
interface, not a log.
"""
import logging

import kai.config as cfg

_logger = logging.getLogger("kai")

# Attach a plain stdout handler once. The format is just the message so output
# looks the same as the old print() calls (the prefix is part of the message).
if not _logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(_handler)
    _logger.setLevel(logging.INFO)
    _logger.propagate = False


def ok(msg: str) -> None:
    _logger.info(f"[+] {msg}")


def info(msg: str) -> None:
    _logger.info(f"[~] {msg}")


def warn(msg: str) -> None:
    _logger.warning(f"[!] {msg}")


def debug(msg: str) -> None:
    if cfg.DEBUG:
        _logger.info(f"[debug] {msg}")
