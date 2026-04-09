"""Centralized throttle for Telegram API calls.

Provides two mechanisms that ensure reliable message delivery:

1. ``serialized`` decorator — serializes all event handlers (commands +
   callbacks) through a global lock so only one processes at a time.
2. Telegram API helpers — rate-limited wrappers (``edit_message``,
   ``send_message``, ``answer_callback``) that prevent exceeding
   Telegram's rate limits and handle common errors.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import time
from typing import Any

from telethon.errors import FloodWaitError, MessageNotModifiedError

from logger import log

# ── Handler serialization ──

_handler_lock = asyncio.Lock()


def serialized(fn):
    """Decorator: serialize event handler execution through a global lock.

    Ensures only ONE handler (command or callback) runs at a time, preventing
    interleaved Kodi read-then-write operations and concurrent Telegram edits.
    """

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        async with _handler_lock:
            return await fn(*args, **kwargs)

    return wrapper


# ── Telegram rate limiter ──

_tg_lock = asyncio.Lock()
_TG_MIN_INTERVAL = 0.035  # ~28 ops/sec; Telegram allows ~30/sec
_last_call = 0.0


async def _tg_call(fn, *args, **kwargs) -> Any:
    """Execute a Telegram API call with rate limiting and FloodWait retry.

    Releases ``_tg_lock`` during FloodWait sleeps so other API calls can
    proceed instead of being blocked for the entire penalty duration.
    """
    global _last_call
    async with _tg_lock:
        wait = _TG_MIN_INTERVAL - (time.monotonic() - _last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            return await fn(*args, **kwargs)
        except FloodWaitError as e:
            flood_seconds = e.seconds
        finally:
            _last_call = time.monotonic()

    # Lock released — other API calls can proceed during the wait
    log.warning("Telegram FloodWait: sleeping %ds", flood_seconds)
    await asyncio.sleep(flood_seconds)

    async with _tg_lock:
        wait = _TG_MIN_INTERVAL - (time.monotonic() - _last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            return await fn(*args, **kwargs)
        finally:
            _last_call = time.monotonic()


async def edit_message(target, text: str, **kwargs) -> Any:
    """Rate-limited message edit. Suppresses MessageNotModifiedError.

    Returns *target* on success or no-change, ``None`` on failure.
    """
    try:
        await _tg_call(target.edit, text, **kwargs)
        return target
    except MessageNotModifiedError:
        return target
    except Exception as e:
        log.debug("edit_message failed: %s", e)
        return None


async def send_message(target, text: str, **kwargs) -> Any:
    """Rate-limited message send. Returns the new message or ``None``."""
    try:
        return await _tg_call(target.respond, text, **kwargs)
    except Exception as e:
        log.debug("send_message failed: %s", e)
        return None


async def answer_callback(event, text: str | None = None, **kwargs) -> None:
    """Best-effort callback query answer — bypasses rate-limit lock.

    Callback query IDs expire quickly (~30 s), so waiting behind a
    FloodWait sleep or the API lock would guarantee failure.  Call the
    Telegram API directly and suppress all errors.
    """
    with contextlib.suppress(Exception):
        await event.answer(text, **kwargs)


__all__ = [
    "answer_callback",
    "edit_message",
    "send_message",
    "serialized",
]
