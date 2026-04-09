from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import config
import kodi
import main
from downloader.queue import queue
from downloader.state import DownloadState, states

# ── startup_message ──


def test_startup_message_success(monkeypatch):
    called = []

    async def fake_notify(title, msg):
        called.append((title, msg))

    monkeypatch.setattr(kodi, "notify", fake_notify)
    asyncio.run(main.startup_message())
    assert len(called) == 1
    assert "Ready" in called[0][1]


def test_startup_message_failure(monkeypatch):
    async def boom(title, msg):
        raise RuntimeError("no kodi")

    monkeypatch.setattr(kodi, "notify", boom)
    asyncio.run(main.startup_message())  # should not raise


# ── _graceful_shutdown ──


class FakeClient:
    disconnected = False

    async def disconnect(self):
        self.disconnected = True


def test_graceful_shutdown_cancels_active(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))

    st = DownloadState("test.mp4", str(tmp_path / "test.mp4"), 100)
    states["test.mp4"] = st

    client = FakeClient()
    event = asyncio.Event()

    async def _run():
        queue.items.clear()
        with patch("main.update_all_lists", new_callable=AsyncMock):
            await main._graceful_shutdown(client, event)

    asyncio.run(_run())
    assert st.cancelled
    assert event.is_set()
    assert client.disconnected
    states.pop("test.mp4", None)


def test_graceful_shutdown_skips_if_already_set(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    client = FakeClient()
    event = asyncio.Event()
    event.set()
    asyncio.run(main._graceful_shutdown(client, event))
    assert not client.disconnected


def test_graceful_shutdown_cancels_queued_before_active(monkeypatch, tmp_path):
    """Queue items must be marked cancelled before active downloads to prevent them starting."""
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))

    from downloader.queue import QueuedItem

    st = DownloadState("active.mp4", str(tmp_path / "active.mp4"), 100)
    states["active.mp4"] = st
    qi = QueuedItem("queued.mp4", None, 100, str(tmp_path / "queued.mp4"), None)
    queue.items["queued.mp4"] = qi

    cancel_order: list[str] = []
    orig_mark = DownloadState.mark_cancelled

    def tracking_cancel(self):
        cancel_order.append(f"active:{self.filename}")
        orig_mark(self)

    monkeypatch.setattr(DownloadState, "mark_cancelled", tracking_cancel)

    client = FakeClient()
    event = asyncio.Event()

    async def _run():
        with patch("main.update_all_lists", new_callable=AsyncMock):
            await main._graceful_shutdown(client, event)

    asyncio.run(_run())
    assert qi.cancelled, "Queued item must be cancelled"
    assert st.cancelled, "Active download must be cancelled"
    states.pop("active.mp4", None)
    queue.items.pop("queued.mp4", None)


# ── _cleanup_partials with queue items ──


def test_cleanup_partials_with_queue_items(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    partial = tmp_path / "queued.bin"
    partial.write_bytes(b"x" * 50)

    from downloader.queue import QueuedItem

    qi = QueuedItem("queued.bin", None, 1000, str(partial), None)
    queue.items["queued.bin"] = qi

    removed = main._cleanup_partials([])
    assert removed == 1
    assert not partial.exists()
    queue.items.pop("queued.bin", None)


def test_cleanup_partials_nonexistent_path(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    st = DownloadState("gone.mp4", str(tmp_path / "gone.mp4"), 1000)
    removed = main._cleanup_partials([st])
    assert removed == 0


# ── _install_signal_handlers ──


def test_install_signal_handlers():
    loop = asyncio.new_event_loop()
    try:

        async def dummy():
            pass

        main._install_signal_handlers(loop, dummy)
    finally:
        loop.close()
