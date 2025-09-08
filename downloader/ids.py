"""Shared helpers for deriving stable short identifiers.

Currently used for associating inline button callback data with filenames
without exceeding Telegram's callback data size limits. The MD5 hash of
the filename is truncated to 8 hex chars (32 bits) which is ample while
keeping collision probability negligible for typical small queues.
"""

from __future__ import annotations

import hashlib

__all__ = ["get_file_id"]


def get_file_id(filename: str) -> str:
    """Return 8-char hex digest for filename.

    Rationale: short, deterministic, cheap to compute, low collision risk
    for the expected scale (handful of concurrent / queued items).
    """
    return hashlib.md5(filename.encode()).hexdigest()[:8]
