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


# ── _register_bot_commands ──


def test_register_bot_commands_success():
    called = []

    class MockClient:
        async def __call__(self, request):
            called.append(request)

    asyncio.run(main._register_bot_commands(MockClient()))
    assert len(called) == 1


def test_register_bot_commands_api_error():
    class MockClient:
        async def __call__(self, request):
            raise RuntimeError("API error")

    asyncio.run(main._register_bot_commands(MockClient()))  # should not raise


# ── _cleanup_partials: complete file left untouched ──


def test_cleanup_partials_complete_file_untouched(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    f = tmp_path / "complete.mp4"
    f.write_bytes(b"x" * 1000)
    st = DownloadState("complete.mp4", str(f), 1000)
    removed = main._cleanup_partials([st])
    assert removed == 0
    assert f.exists()


# ── _graceful_shutdown with partial removal (removed > 0 branch) ──


def test_graceful_shutdown_removes_partials(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    partial = tmp_path / "partial.mp4"
    partial.write_bytes(b"x" * 50)
    st = DownloadState("partial.mp4", str(partial), 1000)
    states["partial.mp4"] = st

    client = FakeClient()
    event = asyncio.Event()

    async def _run():
        queue.items.clear()
        with patch("main.update_all_lists", new_callable=AsyncMock):
            await main._graceful_shutdown(client, event)

    asyncio.run(_run())
    assert not partial.exists()
    states.pop("partial.mp4", None)


# ── _setup_client ──


def test_setup_client(monkeypatch):
    monkeypatch.setattr(config, "API_ID", 123)
    monkeypatch.setattr(config, "API_HASH", "fakehash")
    monkeypatch.setattr(config, "BOT_TOKEN", "faketoken")

    fake_client = FakeClient()
    fake_client.run_until_disconnected = AsyncMock()
    fake_client.start = AsyncMock()
    fake_client.catch_up = AsyncMock()

    with (
        patch("main.TelegramClient", return_value=fake_client) as mock_tc,
        patch("main.register_handlers") as mock_rh,
        patch("main.register_filemanager") as mock_rf,
        patch("main.register_kodi_remote") as mock_rk,
        patch("main.register_kodi_restart") as mock_rkr,
        patch("main.throttle"),
        patch("main._register_bot_commands", new_callable=AsyncMock) as mock_cmds,
        patch("main.startup_message", new_callable=AsyncMock) as mock_startup,
        patch("config.validate") as mock_validate,
    ):

        async def _run():
            return await main._setup_client()

        client, event = asyncio.run(_run())

    assert client is fake_client
    assert not event.is_set()
    mock_validate.assert_called_once()
    mock_tc.assert_called_once_with("bot", 123, "fakehash", catch_up=True)
    mock_rh.assert_called_once_with(fake_client)
    mock_rf.assert_called_once_with(fake_client)
    mock_rk.assert_called_once_with(fake_client)
    mock_rkr.assert_called_once_with(fake_client)
    fake_client.start.assert_called_once_with(bot_token="faketoken")
    mock_cmds.assert_called_once_with(fake_client)
    mock_startup.assert_called_once()


# ── _main ──


def test_main_runs_and_shuts_down():
    fake_client = FakeClient()
    fake_event = asyncio.Event()

    disconnect_future: asyncio.Future | None = None

    async def fake_run_until_disconnected():
        nonlocal disconnect_future
        disconnect_future = asyncio.get_running_loop().create_future()
        await disconnect_future

    fake_client.run_until_disconnected = fake_run_until_disconnected

    async def fake_setup_client():
        return fake_client, fake_event

    with (
        patch("main._setup_client", side_effect=fake_setup_client),
        patch("main._install_signal_handlers"),
        patch("main._graceful_shutdown", new_callable=AsyncMock) as mock_shutdown,
    ):

        async def _run():
            task = asyncio.create_task(main._main())
            await asyncio.sleep(0)
            if disconnect_future is not None:
                disconnect_future.set_result(None)
            await task

        asyncio.run(_run())

    mock_shutdown.assert_called_once_with(fake_client, fake_event)
