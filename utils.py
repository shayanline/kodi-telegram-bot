"""Utility helpers (size formatting, media detection, resource checks)."""

from __future__ import annotations

import math
import os
import shutil
import time

import psutil
from telethon.tl.types import (
    DocumentAttributeAudio,
    DocumentAttributeFilename,
    DocumentAttributeVideo,
)


def humanize_size(size_bytes: float) -> str:
    """Return human readable size (KB..TB)."""
    if size_bytes <= 0:
        return "0 B"
    names = ("B", "KB", "MB", "GB", "TB")
    i = int(math.log(size_bytes, 1024))
    if i >= len(names):
        i = len(names) - 1
    p = 1024**i
    return f"{round(size_bytes / p, 2)} {names[i]}"


_VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv", ".webm", ".m4v", ".3gp"}
_AUDIO_EXT = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".wma"}


def is_media_file(document: object) -> bool:
    mime_type = getattr(document, "mime_type", "") or ""
    if mime_type.startswith(("video/", "audio/")):
        return True
    for attr in getattr(document, "attributes", []):
        if isinstance(attr, (DocumentAttributeVideo, DocumentAttributeAudio)):
            return True
        if isinstance(attr, DocumentAttributeFilename):
            ext = os.path.splitext(attr.file_name)[1].lower()
            if ext in _VIDEO_EXT or ext in _AUDIO_EXT:
                return True
    return False


def free_disk_mb(path: str) -> int:
    """Return free disk space for the partition containing path in MB."""
    usage = shutil.disk_usage(path)
    return int(usage.free / (1024 * 1024))


def remove_empty_parents(path: str, stop_dirs: list[str]) -> int:
    """Remove empty parent dirs up to (excluding) any stop directory; return count."""
    removed = 0
    try:
        stop_set = {os.path.abspath(d) for d in stop_dirs}
        cur = os.path.abspath(os.path.dirname(path))
        while cur not in stop_set:
            if not os.path.isdir(cur):  # nothing more to do
                break
            try:
                if os.listdir(cur):  # not empty
                    break
            except OSError:
                break
            try:
                os.rmdir(cur)
                removed += 1
            except OSError:
                break
            cur = os.path.abspath(os.path.dirname(cur))
    except Exception:
        return removed
    return removed


_last_mem_warn: float = 0.0


def maybe_memory_warning(threshold_percent: int) -> bool:
    """Return True on threshold breach (rate-limited to 60s)."""
    global _last_mem_warn
    if threshold_percent <= 0:
        return False
    now = time.time()
    if now - _last_mem_warn < 60:
        return False
    try:
        percent = psutil.virtual_memory().percent
    except Exception:  # pragma: no cover - psutil edge failures
        return False
    if percent >= threshold_percent:
        _last_mem_warn = now
        return True
    return False


__all__ = [
    "free_disk_mb",
    "humanize_size",
    "is_media_file",
    "maybe_memory_warning",
    "remove_empty_parents",
]
