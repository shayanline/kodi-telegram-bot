from __future__ import annotations

import contextlib
import time

import config
import kodi
import utils

from .buttons import build_buttons
from .state import CancelledDownload, DownloadState


class RateLimiter:
    def __init__(self, min_tg: float = 3.0, min_kodi: float = 2.0):
        self.last_tg = 0.0
        self.last_kodi = 0.0
        self.min_tg = min_tg
        self.min_kodi = min_kodi

    def telegram_ok(self) -> bool:
        now = time.time()
        if now - self.last_tg >= self.min_tg:
            self.last_tg = now
            return True
        return False

    def kodi_ok(self) -> bool:
        now = time.time()
        if now - self.last_kodi >= self.min_kodi:
            self.last_kodi = now
            return True
        return False


async def wait_if_paused(state: DownloadState):
    import asyncio

    while state.paused and not state.cancelled:
        await asyncio.sleep(0.4)
    if state.cancelled:
        raise CancelledDownload


def create_progress_callback(filename: str, start: float, rate: RateLimiter, msg, state: DownloadState | None):
    last = {"received": 0, "change": start}

    def maybe_warn_memory():
        try:
            if utils.maybe_memory_warning(config.MEMORY_WARNING_PERCENT):
                kodi.notify("Memory Warning", f"High RAM usage > {config.MEMORY_WARNING_PERCENT}%")
        except Exception:
            pass

    def _build_edit_kwargs():
        return {"buttons": build_buttons(state)} if state else {}

    async def send_tg_update(percent: int, received: int, total: int, speed: str):
        bar = "▓" * (percent // 10) + "░" * (10 - percent // 10)
        with contextlib.suppress(Exception):
            await msg.edit(
                f"Downloading: {filename}\n"
                f"Progress: {bar} {percent}%\n"
                f"Size: {utils.humanize_size(received)}/{utils.humanize_size(total)}\n"
                f"Speed: {speed}/s",
                **_build_edit_kwargs(),
            )
        maybe_warn_memory()

    async def progress(received: int, total: int):
        if await _check_state(state):
            raise CancelledDownload
        now = time.time()
        if not _update_activity(last, received, now):
            return
        percent, speed = _calc(received, total, now - start)

        if state:
            state.update_progress(received, percent, speed)

        if rate.telegram_ok():
            await send_tg_update(percent, received, total, speed)

        if _should_notify_kodi(percent, rate):
            kodi.progress_notify(filename, percent, speed)

    return progress


async def _check_state(state: DownloadState | None) -> bool:  # returns True if cancelled
    if not state:
        return False
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


def _should_notify_kodi(percent: int, rate: RateLimiter) -> bool:
    return percent % 10 == 0 and rate.kodi_ok() and not kodi.is_playing()


__all__ = ["RateLimiter", "create_progress_callback", "wait_if_paused"]
