from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

from telethon import TelegramClient

import config
import throttle


class RunnerFunc(Protocol):
    async def __call__(self, client: TelegramClient, qi: QueuedItem) -> Any: ...


@dataclass(slots=True)
class QueuedItem:
    filename: str
    document: Any
    size: int
    path: str
    event: Any  # original enqueue event
    cancelled: bool = False
    file_id: str | None = None


class DownloadQueue:
    """In-memory async FIFO queue for pending downloads with cancellation support."""

    def __init__(self, limit: int):
        self.limit = limit
        self._semaphore = asyncio.Semaphore(limit)
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self.items: dict[str, QueuedItem] = {}
        self._lock = asyncio.Lock()
        self._worker_task: asyncio.Task | None = None
        self._active_tasks: list[asyncio.Task] = []
        self._runner: RunnerFunc | None = None
        self._stopping = False

    def set_runner(self, runner: RunnerFunc):
        self._runner = runner

    async def enqueue(self, qi: QueuedItem) -> int:
        """Enqueue an item and return its 1-based position."""
        async with self._lock:
            position = len(self.items) + 1
            self.items[qi.filename] = qi
            await self._queue.put(qi.filename)
            return position

    def cancel(self, filename: str) -> bool:
        """Cancel a queued (not yet started) item."""
        qi = self.items.get(filename)
        if not qi or qi.cancelled:
            return False
        qi.cancelled = True
        self.items.pop(filename, None)
        return True

    def ensure_worker(self, loop: asyncio.AbstractEventLoop, client: TelegramClient):
        if self._worker_task is None:
            self._worker_task = loop.create_task(self._worker(client))

    async def stop(self):
        self._stopping = True
        if self._worker_task:
            await self._queue.put("__STOP__")
            try:
                await asyncio.wait_for(self._worker_task, timeout=5)
            except TimeoutError:
                self._worker_task.cancel()

    def slot(self):  # pragma: no cover
        return self._semaphore

    async def _worker(self, client: TelegramClient):  # pragma: no cover
        while True:
            fname = await self._queue.get()
            if fname == "__STOP__":
                self._queue.task_done()
                break
            t = asyncio.create_task(self._process_item(client, fname))
            self._active_tasks.append(t)
            t.add_done_callback(lambda _t: self._active_tasks.remove(_t) if _t in self._active_tasks else None)
            self._queue.task_done()
        if self._active_tasks:
            await asyncio.gather(*self._active_tasks, return_exceptions=True)
        if self._stopping:
            self._cleanup_remaining()

    async def _process_item(self, client: TelegramClient, fname: str):
        qi = self.items.get(fname)
        if not qi or qi.cancelled:
            return
        async with self._semaphore:
            qi = self.items.pop(fname, None)
            if not qi or qi.cancelled:
                return
            try:
                if self._runner:
                    await self._runner(client, qi)
            except Exception:
                await throttle.send_message(qi.event, f"❌ Failed: {qi.filename}")

    def _cleanup_remaining(self):
        for qi in self.items.values():
            qi.cancelled = True
        self.items.clear()


queue = DownloadQueue(config.MAX_CONCURRENT_DOWNLOADS)

__all__ = ["DownloadQueue", "QueuedItem", "queue"]
