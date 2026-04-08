from __future__ import annotations

import asyncio

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

    class FakeMsg:
        edited = None

        async def edit(self, text, **kw):
            FakeMsg.edited = text

    st = DownloadState("test.mp4", str(tmp_path / "test.mp4"), 100)
    st.message = FakeMsg()
    states["test.mp4"] = st

    client = FakeClient()
    event = asyncio.Event()

    async def _run():
        queue.items.clear()
        await main._graceful_shutdown(client, event)

    asyncio.run(_run())
    assert st.cancelled
    assert event.is_set()
    assert client.disconnected
    assert FakeMsg.edited is not None and "Cancelling" in FakeMsg.edited
    states.pop("test.mp4", None)


def test_graceful_shutdown_skips_if_already_set(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    client = FakeClient()
    event = asyncio.Event()
    event.set()
    asyncio.run(main._graceful_shutdown(client, event))
    assert not client.disconnected


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
