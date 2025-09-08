"""Download state and shared mutable registries.

Houses the global ``states`` dict, ``file_id_map``, and per-chat download
list tracking to break circular imports between downloader sub-modules.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from logger import log

from .ids import get_file_id


class CancelledDownload(Exception):  # pragma: no cover - simple marker
    pass


@dataclass(slots=True)
class DownloadState:
    """Holds mutable per-download state.

    ``size`` is the *expected* total (bytes) so cumulative disk-space
    prediction across concurrent downloads stays conservative.
    """

    filename: str
    path: str
    size: int
    original_event: Any | None = None
    paused: bool = False
    cancelled: bool = False
    completed: bool = False
    waiting_for_space: bool = False
    downloaded_bytes: int = 0
    progress_percent: int = 0
    speed: str = "0 B/s"

    def update_progress(self, received: int, percent: int, speed: str):
        self.downloaded_bytes = received
        self.progress_percent = percent
        self.speed = speed

    def mark_paused(self):
        if not self.cancelled:
            self.paused = True

    def mark_resumed(self):
        if not self.cancelled:
            self.paused = False

    def mark_cancelled(self):
        self.cancelled = True

    def mark_completed(self):
        if not self.cancelled:
            self.completed = True
            self.paused = False


@dataclass(slots=True)
class ChatDownloadList:
    """Per-chat tracked download list message."""

    chat_id: int
    message: Any | None = None
    page: int = 0
    confirming: str | None = None  # file_id when showing cancel confirmation


# ── Per-chat download list tracking ──

chat_lists: dict[int, ChatDownloadList] = {}

# ── Shared mutable state (central location to avoid circular imports) ──

states: dict[str, DownloadState] = {}
file_id_map: dict[str, str] = {}


def register_file_id(filename: str) -> str:
    """Register filename and return its short ID."""
    file_id = get_file_id(filename)
    file_id_map[file_id] = filename
    log.debug("Registered file id %s for %s", file_id, filename)
    return file_id


def resolve_file_id(file_id: str) -> str | None:
    """Resolve file ID back to filename."""
    return file_id_map.get(file_id)


@dataclass
class PendingDeletion:
    """Tracks an interactive disk-space deletion prompt."""

    filename: str = ""
    candidate: str = ""
    choice: str | None = None
    message: Any | None = None
    future: asyncio.Future = field(init=False)

    def __post_init__(self):
        self.future = asyncio.get_running_loop().create_future()


pending_deletions: dict[str, PendingDeletion] = {}


def find_pending_deletion(filename: str) -> tuple[str, PendingDeletion] | None:
    """Find the active pending deletion prompt for a download filename."""
    for pid, pd in pending_deletions.items():
        if pd.filename == filename and not pd.future.done():
            return pid, pd
    return None


__all__ = [
    "CancelledDownload",
    "ChatDownloadList",
    "DownloadState",
    "PendingDeletion",
    "chat_lists",
    "file_id_map",
    "find_pending_deletion",
    "pending_deletions",
    "register_file_id",
    "resolve_file_id",
    "states",
]
