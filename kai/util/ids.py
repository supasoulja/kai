"""One place to mint opaque entity ids, so the same kind of record gets the
same kind of id. Full uuid4 for durable rows; short hex where a compact
handle is enough."""

import uuid


def new_id() -> str:
    """Full canonical id — 36-char uuid4 string."""
    return str(uuid.uuid4())


def short_id(n: int = 8) -> str:
    """Short opaque id — n hex chars drawn from a uuid4."""
    return uuid.uuid4().hex[:n]
