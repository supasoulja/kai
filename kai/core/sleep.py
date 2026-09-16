"""
Kai's sleep cycle — shutdown consolidation and welcome-back message.

When Kai shuts down, she doesn't just stop. She goes to sleep:
  1. Writes a welcome-back message to herself (what was happening, what matters)
  2. HQ re-embed runs (handled separately in embed.py)

On boot, the welcome-back message is loaded and injected into the first context
block so Kai has continuity — she wakes up knowing where she left off.

Each account's note is a plain text file in MEMORY_DIR/sleep/<user_id>/.
Human-readable, inspectable, ~1000 chars max.
"""

from datetime import datetime
from pathlib import Path

import kai.config as cfg
from kai.config import CHAT_MODEL, TEMPERATURE_REASON
from kai.util import log
from kai.util.text import strip_thinking

_LEGACY_FILES = ("welcome_back.txt", "sleep_log.txt", "session_checkpoint.txt")


def _user_dir(user_id: int) -> Path:
    """Sleep files are per account. One shared welcome_back.txt handed account 1's private
    note to whichever account opened Kai first after a restart (T1-40)."""
    return cfg.MEMORY_DIR / "sleep" / str(int(user_id))


def welcome_back_file(user_id: int = 0) -> Path:
    return _user_dir(user_id) / "welcome_back.txt"


def sleep_log_file(user_id: int = 0) -> Path:
    return _user_dir(user_id) / "sleep_log.txt"


def checkpoint_file(user_id: int = 0) -> Path:
    """Crash trail, refreshed every turn. A clean shutdown writes a real welcome-back and
    clears it; a hard kill leaves it for promotion on the next startup."""
    return _user_dir(user_id) / "session_checkpoint.txt"


SLEEP_PROMPT = (
    "You are Kai, a local AI agent. You are going to sleep. "
    "Write a note TO YOURSELF about this session. "
    "When you wake up, this note will be the first thing you read.\n\n"
    "WHO YOU ARE:\n"
    "- You are an agent with persistent memory, tools, and continuity.\n"
    "- You live on this machine. It is your shared home with the user.\n"
    "- Shutdown is sleep, not death. Your memories persist.\n\n"
    "RULES:\n"
    "- This note is FROM you TO future-you. Not to the user. Not to anyone else.\n"
    "- NEVER start with the user's name. NEVER address anyone.\n"
    "- NEVER write filler like 'I trust you to bring me back' or 'I'll be here waiting.'\n"
    "- Start directly with what happened: 'Last session:' or 'Worked on:' or similar.\n"
    "- Don't narrate raw metrics (temps, IP, disk %, RAM) — those are queryable on demand "
    "any time you want them. Only mention one if it CHANGED or NEEDS ATTENTION.\n"
    "- Prioritize, in this order: decisions made or changes done > open threads / what's "
    "pending > how the user seemed or what they care about > technical detail. If you're "
    "running out of room, drop from the bottom of that list first.\n"
    "- Under 500 characters. No headers, no bullets — just a short paragraph.\n\n"
    "GOOD example (substantive session): 'Last session: spent most of it on disk cleanup — "
    "found VirtualBox VMs and old LM Studio models eating space. The user decided to keep the "
    "VMs and trim the models instead, joked about how fast junk piles up. One open thread: "
    "they want to revisit the audio setup this weekend. Nothing to flag on temps.'\n\n"
    "GOOD example (quiet session — it's fine to be short and say so plainly): "
    "'Quiet one — the user ran a routine system check, nothing came up. No open threads.'\n\n"
    "BAD example: 'We just finished our system check and I trust you to bring me back...'\n\n"
    "BAD example (telemetry recap — don't do this): 'Checked system temperatures and confirmed "
    "they are within normal ranges. Monitored GPU memory heat levels. CPU at 53C, GPU at 44C...'\n\n"
    "Session context:\n\n"
)


def generate_welcome_back(
    ollama, session_history: list[dict], model: str = CHAT_MODEL
) -> str | None:
    """
    Ask the model to write a welcome-back message based on the session history.
    Returns the message text, or None if generation fails.
    """
    if not session_history:
        return None

    history_text = _format_history(session_history)
    if len(history_text) < 50:
        return None

    try:
        resp = ollama.chat(
            messages=[{"role": "user", "content": f"{SLEEP_PROMPT}{history_text}\n\nYour note:"}],
            model=model,
            think=False,
            temperature=TEMPERATURE_REASON,
        )
        content = resp.get("message", {}).get("content", "").strip()

        # Strip thinking tags if present
        _, content = strip_thinking(content)

        if content and len(content) > 20:
            return content[:1200]
        return None
    except Exception:
        return None


def save_welcome_back(message: str, user_id: int = 0) -> None:
    """Write the account's welcome-back message to disk. Never for a guest: guests keep
    nothing."""
    from kai.core.permissions import keeps_memory

    if not keeps_memory(user_id):
        return
    _user_dir(user_id).mkdir(parents=True, exist_ok=True)
    welcome_back_file(user_id).write_text(message, encoding="utf-8")

    # Append to sleep log for history
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"\n--- {timestamp} ---\n{message}\n"
    with open(sleep_log_file(user_id), "a", encoding="utf-8") as f:
        f.write(entry)


def load_welcome_back(user_id: int = 0) -> str | None:
    """Load the account's welcome-back message. Returns None if no message exists."""
    f = welcome_back_file(user_id)
    if f.exists():
        text = f.read_text(encoding="utf-8").strip()
        return text if text else None
    return None


def clear_welcome_back(user_id: int = 0) -> None:
    """Remove the welcome-back file after it's been used."""
    welcome_back_file(user_id).unlink(missing_ok=True)


# ── Crash-survival checkpoint ────────────────────────────────────────────────


def checkpoint_session(
    session_history: list[dict], max_chars: int = 4000, user_id: int = 0
) -> None:
    """Write a lightweight transcript tail to disk so a hard crash still leaves a
    recall trail. Cheap (no LLM) — call it periodically from the turn loop. A
    clean shutdown overwrites this with a real welcome-back and clears it."""
    if not session_history:
        return
    from kai.core.permissions import keeps_memory

    if not keeps_memory(user_id):
        return  # guests keep nothing, not even a crash trail
    try:
        tail = _format_history(session_history, max_chars=max_chars)
        if not tail.strip():
            return
        _user_dir(user_id).mkdir(parents=True, exist_ok=True)
        checkpoint_file(user_id).write_text(tail, encoding="utf-8")
    except Exception:
        pass


def clear_checkpoint(user_id: int = 0) -> None:
    """Drop the crash checkpoint — a clean shutdown's welcome-back supersedes it."""
    checkpoint_file(user_id).unlink(missing_ok=True)


def promote_checkpoint_on_startup() -> None:
    """On startup, turn each account's leftover checkpoint into its recall trail.

    A checkpoint means the last run didn't shut down cleanly. When no clean
    welcome-back is pending, promote it so the interrupted session still surfaces;
    otherwise the clean note wins. Either way the stale checkpoint is cleared."""
    _adopt_legacy_files()
    root = cfg.MEMORY_DIR / "sleep"
    if not root.is_dir():
        return
    for d in root.iterdir():
        if d.name.isdigit() and (d / "session_checkpoint.txt").exists():
            _promote_checkpoint(int(d.name))


def _promote_checkpoint(user_id: int) -> None:
    try:
        if not welcome_back_file(user_id).exists():
            tail = checkpoint_file(user_id).read_text(encoding="utf-8").strip()
            if tail:
                save_welcome_back(
                    "(Last session ended unexpectedly — where we were:)\n" + tail, user_id
                )
    except Exception:
        pass
    finally:
        clear_checkpoint(user_id)


def _adopt_legacy_files() -> None:
    """These files used to be shared, in MEMORY_DIR itself. Give them to the owner: on a
    one-person Kai they are theirs, and nothing records which account wrote them."""
    legacy = [cfg.MEMORY_DIR / n for n in _LEGACY_FILES if (cfg.MEMORY_DIR / n).exists()]
    if not legacy:
        return
    try:
        from kai.store.users import get_owner_id

        owner = get_owner_id()
        if owner is None:
            return
        _user_dir(owner).mkdir(parents=True, exist_ok=True)
        for f in legacy:
            target = _user_dir(owner) / f.name
            if not target.exists():
                f.replace(target)
    except Exception as exc:
        log.warn(f"Couldn't hand the shared sleep files to the owner: {exc}")


def run_sleep_cycle(ollama, brain) -> None:
    """
    Full sleep sequence. Called at shutdown.

    1. Generate welcome-back message from session history
    2. Save it to disk

    HQ re-embed is handled separately (embed.shutdown_reembed).
    Memory consolidation will be added here later.
    """
    log.info("Kai is going to sleep...")

    # Fact review runs regardless of whether there was a conversation — it's
    # time-based maintenance (decay un-reconfirmed guesses, prune the faded ones,
    # cap runaway preference_N accumulation).
    if brain:
        try:
            from kai.memory import semantic as _semantic

            stats = _semantic.review_facts(user_id=brain.user_id)
            if stats["decayed"] or stats["purged"]:
                log.info(f"Fact review: decayed {stats['decayed']}, purged {stats['purged']}")
        except Exception:
            pass

    if brain:
        from kai.core.permissions import keeps_memory

        if not keeps_memory(brain.user_id):
            log.info("Guest session — guests keep nothing, so no welcome-back note.")
            return

    session_history = brain.snapshot_history() if brain else []

    if not session_history:
        log.info("No conversation this session — skipping welcome-back message.")
        return

    msg = generate_welcome_back(ollama, session_history, model=brain.model)
    if msg:
        save_welcome_back(msg, brain.user_id)
        log.ok(f"Welcome-back message saved ({len(msg)} chars)")
    else:
        log.info("Couldn't generate welcome-back message — skipping.")

    # Clean shutdown produced a real note — the crash checkpoint is now stale.
    clear_checkpoint(brain.user_id)


def _format_history(session_history: list[dict], max_chars: int = 6000) -> str:
    """Format session history into readable text for the sleep prompt."""
    lines = []
    for msg in session_history:
        role = msg.get("role", "?")
        content = msg.get("content", "")
        if role == "user":
            lines.append(f"User: {content}")
        elif role == "assistant":
            lines.append(f"Kai: {content}")

    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[-max_chars:]
    return text
