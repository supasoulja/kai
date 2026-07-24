"""
Vector math — one home for cosine similarity/distance.

This used to be copy-pasted in four places (tool registry, memory router,
cerebellum, scorer), each with slightly different fallback behavior. The core
math lives here now; call sites that need special policy (e.g. the scorer's
neutral fallback for un-embedded nodes) wrap these.

Inputs may be Python lists or numpy arrays — numpy handles both.
"""

from __future__ import annotations

import numpy as np


def cosine(a, b) -> float:
    """Cosine similarity in [-1, 1]. Returns 0.0 if either vector is zero/empty."""
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def cosine_distance(a, b) -> float:
    """Cosine distance in [0, 2]: 0.0 = identical direction, 1.0 = orthogonal."""
    return 1.0 - cosine(a, b)
