"""
Kai's sleep cycle — shutdown consolidation and welcome-back message.

When Kai shuts down, she doesn't just stop. She goes to sleep:
  1. Writes a welcome-back message to herself (what was happening, what matters)
  2. HQ re-embed runs (handled separately in embed.py)

On boot, the welcome-back message is loaded and injected into the first context
block so Kai has continuity — she wakes up knowing where she left off.

The welcome-back message is stored as a plain text file in MEMORY_DIR.
Human-readable, inspectable, ~1000 chars max.
"""
from datetime import datetime

from kai.config import MEMORY_DIR, CHAT_MODEL, TEMPERATURE_REASON
from kai.util.text import strip_thinking

_WELCOME_BACK_FILE = MEMORY_DIR / "welcome_back.txt"
_SLEEP_LOG_FILE = MEMORY_DIR / "sleep_log.txt"
# Crash trail — refreshed periodically mid-session. A clean shutdown writes a
# real welcome-back and clears this; a hard kill leaves it for promotion on the
# next startup so the session isn't a total recall blank.
_CHECKPOINT_FILE = MEMORY_DIR / "session_checkpoint.txt"


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


def generate_welcome_back(ollama, session_history: list[dict], model: str = CHAT_MODEL) -> str | None:
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


def save_welcome_back(message: str) -> None:
    """Write the welcome-back message to disk."""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    _WELCOME_BACK_FILE.write_text(message, encoding="utf-8")

    # Append to sleep log for history
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"\n--- {timestamp} ---\n{message}\n"
    with open(_SLEEP_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(entry)


def load_welcome_back() -> str | None:
    """Load the welcome-back message. Returns None if no message exists."""
    if _WELCOME_BACK_FILE.exists():
        text = _WELCOME_BACK_FILE.read_text(encoding="utf-8").strip()
        return text if text else None
    return None


def clear_welcome_back() -> None:
    """Remove the welcome-back file after it's been used."""
    if _WELCOME_BACK_FILE.exists():
        _WELCOME_BACK_FILE.unlink()


# ── Crash-survival checkpoint ────────────────────────────────────────────────

def checkpoint_session(session_history: list[dict], max_chars: int = 4000) -> None:
    """Write a lightweight transcript tail to disk so a hard crash still leaves a
    recall trail. Cheap (no LLM) — call it periodically from the turn loop. A
    clean shutdown overwrites this with a real welcome-back and clears it."""
    if not session_history:
        return
    try:
        tail = _format_history(session_history, max_chars=max_chars)
        if not tail.strip():
            return
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        _CHECKPOINT_FILE.write_text(tail, encoding="utf-8")
    except Exception:
        pass


def clear_checkpoint() -> None:
    """Drop the crash checkpoint — a clean shutdown's welcome-back supersedes it."""
    if _CHECKPOINT_FILE.exists():
        _CHECKPOINT_FILE.unlink()


def promote_checkpoint_on_startup() -> None:
    """On startup, turn a leftover checkpoint into the recall trail.

    If a checkpoint exists it means last run didn't shut down cleanly. When no
    clean welcome-back is pending, promote the checkpoint so the interrupted
    session still surfaces; otherwise the clean note wins. Either way the stale
    checkpoint is cleared."""
    if not _CHECKPOINT_FILE.exists():
        return
    try:
        if not _WELCOME_BACK_FILE.exists():
            tail = _CHECKPOINT_FILE.read_text(encoding="utf-8").strip()
            if tail:
                save_welcome_back(
                    "(Last session ended unexpectedly — where we were:)\n" + tail
                )
    except Exception:
        pass
    finally:
        clear_checkpoint()


def run_sleep_cycle(ollama, brain) -> None:
    """
    Full sleep sequence. Called at shutdown.

    1. Generate welcome-back message from session history
    2. Save it to disk

    HQ re-embed is handled separately (embed.shutdown_reembed).
    Memory consolidation will be added here later.
    """
    print("[~] Kai is going to sleep...")

    # Fact review runs regardless of whether there was a conversation — it's
    # time-based maintenance (decay un-reconfirmed guesses, prune the faded ones,
    # cap runaway preference_N accumulation).
    if brain:
        try:
            from kai.memory import semantic as _semantic
            stats = _semantic.review_facts(user_id=brain.user_id)
            if stats["decayed"] or stats["purged"]:
                print(f"[~] Fact review: decayed {stats['decayed']}, purged {stats['purged']}")
        except Exception:
            pass

    session_history = brain.snapshot_history() if brain else []

    if not session_history:
        print("[~] No conversation this session — skipping welcome-back message.")
        return

    msg = generate_welcome_back(ollama, session_history, model=brain.model)
    if msg:
        save_welcome_back(msg)
        print(f"[+] Welcome-back message saved ({len(msg)} chars)")
    else:
        print("[~] Couldn't generate welcome-back message — skipping.")

    # Clean shutdown produced a real note — the crash checkpoint is now stale.
    clear_checkpoint()


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
