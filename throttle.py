"""Telegram API publisher with priority-based delivery.

Three priority levels determine message ordering:

- ``PRIORITY_USER`` (0) — command / callback responses (highest).
- ``PRIORITY_EVENT`` (1) — download lifecycle notifications.
- ``PRIORITY_PROGRESS`` (2) — periodic list refreshes (lowest).

``answer_callback`` bypasses the queue entirely because callback
query IDs expire in ~30 s and cannot afford to wait.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from typing import Any

from telethon.errors import FloodWaitError, MessageNotModifiedError

from logger import log

# ── Priority levels ──

PRIORITY_USER = 0
PRIORITY_EVENT = 1
PRIORITY_PROGRESS = 2

# ── Publisher internals ──

_TG_MIN_INTERVAL = 0.035  # ~28 ops/sec; Telegram allows ~30/sec
_last_call = 0.0
_seq = 0
_queue: asyncio.PriorityQueue[_Item] | None = None
_publisher_task: asyncio.Task[None] | None = None


@dataclass(order=True, slots=True)
class _Item:
    priority: int
    seq: int
    future: asyncio.Future[Any] = field(compare=False)
    fn: Any = field(compare=False)
    args: tuple[Any, ...] = field(compare=False)
    kwargs: dict[str, Any] = field(compare=False)


def start_publisher() -> None:
    """Start the background publisher task.  Call once at startup."""
    global _queue, _publisher_task
    _queue = asyncio.PriorityQueue()
    _publisher_task = asyncio.create_task(_publisher_loop())


def stop_publisher() -> None:
    """Cancel the publisher task.  Used for clean shutdown and testing."""
    global _publisher_task
    if _publisher_task is not None:
        _publisher_task.cancel()
        _publisher_task = None


async def _enqueue(priority: int, fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Enqueue a Telegram API call and return its result."""
    global _seq
    assert _queue is not None, "start_publisher() not called"
    _seq += 1
    future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    await _queue.put(_Item(priority, _seq, future, fn, args, kwargs))
    return await future


async def _publisher_loop() -> None:
    """Consume the priority queue, executing API calls with rate limiting."""
    global _last_call
    while True:
        item: _Item = await _queue.get()  # type: ignore[union-attr]
        wait = _TG_MIN_INTERVAL - (time.monotonic() - _last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            result = await item.fn(*item.args, **item.kwargs)
            if not item.future.done():
                item.future.set_result(result)
        except FloodWaitError as e:
            _last_call = time.monotonic()
            log.warning("Telegram FloodWait: sleeping %ds", e.seconds)
            await asyncio.sleep(e.seconds)
            try:
                result = await item.fn(*item.args, **item.kwargs)
                if not item.future.done():
                    item.future.set_result(result)
            except Exception as exc:
                if not item.future.done():
                    item.future.set_exception(exc)
        except Exception as exc:
            if not item.future.done():
                item.future.set_exception(exc)
        finally:
            _last_call = time.monotonic()


# ── Public helpers ──


async def edit_message(target: Any, text: str, *, priority: int = PRIORITY_USER, **kwargs: Any) -> Any:
    """Priority-queued message edit.  Returns *target* on success, ``None`` on failure."""
    try:
        await _enqueue(priority, target.edit, text, **kwargs)
        return target
    except MessageNotModifiedError:
        return target
    except Exception as e:
        log.debug("edit_message failed: %s", e)
        return None


async def send_message(target: Any, text: str, *, priority: int = PRIORITY_USER, **kwargs: Any) -> Any:
    """Priority-queued message send.  Returns the new message or ``None``."""
    try:
        return await _enqueue(priority, target.respond, text, **kwargs)
    except Exception as e:
        log.debug("send_message failed: %s", e)
        return None


async def answer_callback(event: Any, text: str | None = None, **kwargs: Any) -> None:
    """Best-effort callback answer — bypasses the queue entirely."""
    with contextlib.suppress(Exception):
        await event.answer(text, **kwargs)


__all__ = [
    "PRIORITY_EVENT",
    "PRIORITY_PROGRESS",
    "PRIORITY_USER",
    "answer_callback",
    "edit_message",
    "send_message",
    "start_publisher",
    "stop_publisher",
]
