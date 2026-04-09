from __future__ import annotations

import asyncio
import time

import config
import kodi
import utils

from .state import CancelledDownload, DownloadState


class RateLimiter:
    """Rate limiter for Kodi notifications during download progress."""

    def __init__(self, min_kodi: float = 2.0):
        self.last_kodi = 0.0
        self.min_kodi = min_kodi

    def kodi_ok(self) -> bool:
        now = time.time()
        if now - self.last_kodi >= self.min_kodi:
            self.last_kodi = now
            return True
        return False


async def wait_if_paused(state: DownloadState):
    while state.paused and not state.cancelled:
        await asyncio.sleep(0.4)
    if state.cancelled:
        raise CancelledDownload


def create_progress_callback(filename: str, start: float, rate: RateLimiter, state: DownloadState):
    """Create a progress callback that updates in-memory state and sends Kodi notifications."""
    last = {"received": 0, "change": start}

    async def progress(received: int, total: int):
        if await _check_state(state):
            raise CancelledDownload
        now = time.time()
        if not _update_activity(last, received, now):
            return
        percent, speed = _calc(received, total, now - start)

        state.update_progress(received, percent, speed)

        # Memory warning and download progress are independent Kodi notifications;
        # only send one per tick to avoid stacking two popups back-to-back.
        mem_warned = False
        try:
            if utils.maybe_memory_warning(config.MEMORY_WARNING_PERCENT):
                await kodi.notify("Memory Warning", f"High RAM usage > {config.MEMORY_WARNING_PERCENT}%")
                mem_warned = True
        except Exception:
            pass

        if not mem_warned and percent > 0 and percent % 10 == 0 and rate.kodi_ok() and not await kodi.is_playing():
            await kodi.progress_notify(filename, percent, speed)

    return progress


async def _check_state(state: DownloadState) -> bool:
    if state.cancelled:
        return True
    await wait_if_paused(state)
    return state.cancelled


def _update_activity(last: dict, received: int, now: float) -> bool:
    if received != last["received"]:
        last["received"] = received
        last["change"] = now
        return True
    return (now - last["change"]) <= 30


def _calc(received: int, total: int, elapsed: float):
    elapsed = max(elapsed, 0.001)
    percent = int(received / total * 100) if total else 0
    speed = utils.humanize_size(received / elapsed)
    return percent, speed


__all__ = ["RateLimiter", "create_progress_callback", "wait_if_paused"]
