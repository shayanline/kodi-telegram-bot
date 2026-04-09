import asyncio
import time

import pytest

import kodi
import utils
from downloader.progress import (
    RateLimiter,
    _calc,
    _check_state,
    _update_activity,
    create_progress_callback,
    wait_if_paused,
)
from downloader.state import CancelledDownload, DownloadState


def _make_state(**kw):
    defaults = {"filename": "f.bin", "path": "/tmp/f.bin", "size": 1000}
    defaults.update(kw)
    return DownloadState(**defaults)


def test_rate_limiter(monkeypatch):
    rl = RateLimiter(min_kodi=2.0)
    base = 1000.0
    times_k = [base, base + 1.0, base + 2.1]
    monkeypatch.setattr(time, "time", lambda: times_k.pop(0))
    assert rl.kodi_ok() is True
    assert rl.kodi_ok() is False
    assert rl.kodi_ok() is True


def test_calc_and_notify(monkeypatch):
    p, speed = _calc(500, 1000, 5)
    assert p == 50 and speed == "100.0 B"
    rl = RateLimiter(min_kodi=1)
    # kodi_ok should respect rate limiting
    assert rl.kodi_ok() is True
    # Rapid second call blocked by rate.kodi_ok timing (since min_kodi=1s)
    assert rl.kodi_ok() is False


# ── wait_if_paused ──


def test_wait_if_paused_resumes():
    """Paused then resumed: returns normally."""
    _bg = set()

    async def _run():
        state = _make_state()
        state.mark_paused()

        async def _resume():
            await asyncio.sleep(0.05)
            state.mark_resumed()

        task = asyncio.create_task(_resume())
        _bg.add(task)
        task.add_done_callback(_bg.discard)
        await wait_if_paused(state)

    asyncio.run(_run())


def test_wait_if_paused_cancelled():
    """Paused then cancelled: raises CancelledDownload."""
    _bg = set()

    async def _run():
        state = _make_state()
        state.mark_paused()

        async def _cancel():
            await asyncio.sleep(0.05)
            state.mark_cancelled()

        task = asyncio.create_task(_cancel())
        _bg.add(task)
        task.add_done_callback(_bg.discard)
        with pytest.raises(CancelledDownload):
            await wait_if_paused(state)

    asyncio.run(_run())


# ── _check_state ──


def test_check_state_cancelled_returns_true():
    async def _run():
        state = _make_state()
        state.mark_cancelled()
        assert await _check_state(state) is True

    asyncio.run(_run())


# ── _update_activity ──


def test_update_activity_no_change_within_30s():
    last = {"received": 100, "change": 1000.0}
    assert _update_activity(last, 100, 1020.0) is True  # 20s < 30s


def test_update_activity_no_change_past_30s():
    last = {"received": 100, "change": 1000.0}
    assert _update_activity(last, 100, 1031.0) is False  # 31s > 30s


# ── progress callback memory warning ──


def test_progress_callback_memory_warning(monkeypatch):
    """Memory warning triggers kodi.notify."""
    notified = {}

    async def fake_notify(title, msg):
        notified["title"] = title
        notified["msg"] = msg

    async def fake_is_playing():
        return True

    monkeypatch.setattr(utils, "maybe_memory_warning", lambda pct: True)
    monkeypatch.setattr(kodi, "notify", fake_notify)
    monkeypatch.setattr(kodi, "is_playing", fake_is_playing)

    state = _make_state()
    rl = RateLimiter(min_kodi=0)
    cb = create_progress_callback("f.bin", time.time() - 1, rl, state)

    asyncio.run(cb(500, 1000))
    assert notified.get("title") == "Memory Warning"


# ── progress callback cancelled raises ──


def test_progress_callback_cancelled_raises():
    state = _make_state()
    state.mark_cancelled()
    rl = RateLimiter(min_kodi=0)
    cb = create_progress_callback("f.bin", time.time() - 1, rl, state)

    with pytest.raises(CancelledDownload):
        asyncio.run(cb(500, 1000))
