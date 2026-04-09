"""Download state, message tracking, and shared mutable registries.

Houses the global ``states`` dict and ``file_id_map`` to break circular
imports between downloader sub-modules.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from telethon.tl.custom.message import Message

import utils
from logger import log

from .ids import get_file_id


class MessageType(Enum):
    """Types of messages that can be tracked."""

    PROGRESS = "progress"
    DOWNLOAD_LIST = "download_list"
    QUEUE_LIST = "queue_list"
    QUEUED = "queued"


@dataclass(slots=True)
class TrackedMessage:
    """Represents a message being tracked for updates."""

    message: Message
    message_type: MessageType


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
    message: Message | None = None
    original_event: Any | None = None
    paused: bool = False
    cancelled: bool = False
    completed: bool = False
    confirming_cancel: bool = False
    waiting_for_space: bool = False
    downloaded_bytes: int = 0
    progress_percent: int = 0
    speed: str = "0 B/s"

    def update_progress(self, received: int, percent: int, speed: str):
        self.downloaded_bytes = received
        self.progress_percent = percent
        self.speed = speed

    def get_progress_text(self) -> str:
        if self.cancelled or self.completed:
            return ""
        if self.paused:
            return f"⏸️ Paused • {self.progress_percent}% • {utils.humanize_size(self.downloaded_bytes)}"
        if self.progress_percent > 0:
            return f"📊 {self.progress_percent}% • {utils.humanize_size(self.downloaded_bytes)} • {self.speed}/s"
        return ""

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


class MessageTracker:
    """Central registry for Telegram messages associated with downloads.

    Single source of truth — replaces the previous dual-tracking approach
    (per-state lists + global registry).
    """

    def __init__(self):
        self._messages: dict[str, list[TrackedMessage]] = {}

    def register_message(self, filename: str, message, message_type: MessageType):
        tracked = TrackedMessage(message, message_type)
        if filename not in self._messages:
            self._messages[filename] = []
        self._messages[filename].append(tracked)

    def get_messages(self, filename: str, message_type: MessageType | None = None) -> list[TrackedMessage]:
        messages = self._messages.get(filename, [])
        if message_type is not None:
            return [tm for tm in messages if tm.message_type == message_type]
        return list(messages)

    def get_all_list_messages(self) -> list[TrackedMessage]:
        result: list[TrackedMessage] = []
        for messages in self._messages.values():
            result.extend(
                tm for tm in messages if tm.message_type in (MessageType.DOWNLOAD_LIST, MessageType.QUEUE_LIST)
            )
        return result

    def cleanup_file(self, filename: str):
        self._messages.pop(filename, None)

    def trim_list_messages(self, sentinel_key: str, max_kept: int = 5):
        """Keep only the most recent list messages for a sentinel key."""
        msgs = self._messages.get(sentinel_key)
        if msgs and len(msgs) > max_kept:
            self._messages[sentinel_key] = msgs[-max_kept:]


message_tracker = MessageTracker()

# ── Shared mutable state (central location to avoid circular imports) ──

states: dict[str, DownloadState] = {}
file_id_map: dict[str, str] = {}

# Message IDs of list messages currently showing cancel confirmation prompts.
# Prevents periodic list updates from overwriting the interactive prompt.
frozen_list_msg_ids: set[int] = set()


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
    "DownloadState",
    "MessageTracker",
    "MessageType",
    "PendingDeletion",
    "TrackedMessage",
    "file_id_map",
    "find_pending_deletion",
    "frozen_list_msg_ids",
    "message_tracker",
    "pending_deletions",
    "register_file_id",
    "resolve_file_id",
    "states",
]
