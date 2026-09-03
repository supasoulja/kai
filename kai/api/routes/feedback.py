"""Thumbs up/down on a response — persisted and recorded to episodic memory."""

from fastapi import APIRouter, HTTPException, Request

from kai.api.models import FeedbackRequest
from kai.api.state import brain_for
from kai.store import sessions as _sessions

router = APIRouter()


@router.post("/feedback")
async def post_feedback(req: FeedbackRequest, request: Request):
    if req.value not in (1, -1):
        raise HTTPException(status_code=400, detail="value must be 1 or -1")

    # Persist to DB
    _sessions.save_feedback(req.message_id, req.value)

    brain = brain_for(request)

    # Record in episodic memory so Kai can learn from it
    if req.snippet:
        label = "positive" if req.value == 1 else "negative"
        entry = f"User gave {label} feedback on this response: {req.snippet[:300]}"
        brain.memory.add_episode(entry, entry_type="event", metadata={"feedback": req.value})

    # Reinforcement: feed the relationship/self state stores so the scored-memory
    # context modifier (loop.gather_context) reflects trust built or dented. These
    # hooks were dead code until wired here — thumbs are the one explicit signal.
    from kai.memory import loop as _memloop

    uid = str(brain.user_id)
    if req.value == 1:
        _memloop.record_turn_validation(uid)
    else:
        _memloop.record_turn_correction(uid)

    return {"ok": True}
