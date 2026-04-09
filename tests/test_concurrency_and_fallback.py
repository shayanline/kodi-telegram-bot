"""Tests for concurrency fixes, _safe_edit fallback, and category TTL cleanup."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

from downloader.manager import (
    _CATEGORY_TTL_SECONDS,
    _download_lock,
    _pending_categories,
    _prune_stale_categories,
    _safe_edit,
)

# ── Helpers ──


def _make_reply_msg(msg_id: int = 101):
    """Create a simple reply message (no recursion)."""
    m = object.__new__(FakeMessage)
    m.id = msg_id
    m.edit = AsyncMock()
    m.respond = AsyncMock()
    return m


class FakeMessage:
    """Minimal message mock that supports .edit() and .respond()."""

    def __init__(self, msg_id: int = 1, *, edit_fails: bool = False):
        self.id = msg_id
        self._edit_fails = edit_fails
        self.edit = AsyncMock(side_effect=RuntimeError("deleted") if edit_fails else None)
        self.respond = AsyncMock(return_value=_make_reply_msg(msg_id + 100))


class FakeMessageNotModified(FakeMessage):
    """Message whose edit raises MessageNotModifiedError."""

    def __init__(self, msg_id: int = 1):
        super().__init__(msg_id)
        from telethon.errors import MessageNotModifiedError

        self.edit = AsyncMock(side_effect=MessageNotModifiedError(None))


# ── _safe_edit tests ──


def test_safe_edit_success():
    async def _run():
        msg = FakeMessage(1)
        result = await _safe_edit(msg, "hello")
        msg.edit.assert_awaited_once_with("hello", buttons=None)
        assert result is msg

    asyncio.run(_run())


def test_safe_edit_not_modified_suppressed():
    async def _run():
        msg = FakeMessageNotModified(1)
        result = await _safe_edit(msg, "same text")
        msg.edit.assert_awaited_once()
        msg.respond.assert_not_awaited()
        assert result is msg

    asyncio.run(_run())


def test_safe_edit_falls_back_to_respond():
    async def _run():
        msg = FakeMessage(1, edit_fails=True)
        result = await _safe_edit(msg, "fallback text")
        msg.edit.assert_awaited_once()
        msg.respond.assert_awaited_once_with("fallback text", buttons=None)
        assert result is not None
        assert result.id == 101  # FakeMessage returns id+100

    asyncio.run(_run())


def test_safe_edit_both_fail():
    async def _run():
        msg = FakeMessage(1, edit_fails=True)
        msg.respond = AsyncMock(side_effect=RuntimeError("chat gone"))
        result = await _safe_edit(msg, "doomed")
        assert result is None

    asyncio.run(_run())


def test_safe_edit_with_buttons():
    async def _run():
        msg = FakeMessage(1)
        buttons = [["btn"]]
        await _safe_edit(msg, "text", buttons=buttons)
        msg.edit.assert_awaited_once_with("text", buttons=buttons)

    asyncio.run(_run())


# ── _prune_stale_categories tests ──


def test_prune_removes_stale_entries():
    _pending_categories.clear()
    _pending_categories["old"] = (None, None, 0, time.time() - _CATEGORY_TTL_SECONDS - 10)
    _pending_categories["fresh"] = (None, None, 0, time.time())
    _prune_stale_categories()
    assert "old" not in _pending_categories
    assert "fresh" in _pending_categories
    _pending_categories.clear()


def test_prune_keeps_all_when_fresh():
    _pending_categories.clear()
    _pending_categories["a"] = (None, None, 0, time.time())
    _pending_categories["b"] = (None, None, 0, time.time() - 60)
    _prune_stale_categories()
    assert len(_pending_categories) == 2
    _pending_categories.clear()


def test_prune_empty_is_noop():
    _pending_categories.clear()
    _prune_stale_categories()
    assert len(_pending_categories) == 0


# ── _download_lock tests ──


def test_download_lock_exists_and_is_asyncio_lock():
    assert isinstance(_download_lock, asyncio.Lock)


def test_download_lock_serializes_access():
    """Verify two tasks cannot hold _download_lock simultaneously."""

    async def _run():
        order: list[str] = []

        async def task(name: str, delay: float):
            async with _download_lock:
                order.append(f"{name}_enter")
                await asyncio.sleep(delay)
                order.append(f"{name}_exit")

        t1 = asyncio.create_task(task("A", 0.05))
        await asyncio.sleep(0)  # let A acquire lock
        t2 = asyncio.create_task(task("B", 0.01))
        await asyncio.gather(t1, t2)
        assert order == ["A_enter", "A_exit", "B_enter", "B_exit"]

    asyncio.run(_run())
