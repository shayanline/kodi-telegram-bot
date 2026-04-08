import asyncio

from downloader.progress import RateLimiter, create_progress_callback
from downloader.queue import DownloadQueue, QueuedItem
from downloader.queue import queue as global_queue
from downloader.state import (
    DownloadState,
    file_id_map,
    register_file_id,
    states,
)


class DummyEvent:
    def __init__(self):
        self.responded = []

    async def respond(self, text, **_):  # pragma: no cover - emulate telethon async
        self.responded.append(text)
        await asyncio.sleep(0)


class DummyMsg:
    def __init__(self):
        self.last = None
        self.buttons_history = []

    async def edit(self, text, buttons=None):  # pragma: no cover
        self.last = text
        if buttons is not None:
            self.buttons_history.append(buttons)
        await asyncio.sleep(0)


class FakeStMsg(DummyMsg):  # simple subclass just to mirror real object usage
    pass


def test_pause_uses_last_text():
    # Simulate started download and verify paused state reflects in progress text
    st = DownloadState("fileC.bin", "/tmp/fileC.bin", 1000)
    msg = FakeStMsg()
    st.message = msg
    states[st.filename] = st
    register_file_id(st.filename)
    # Simulate some progress first
    st.update_progress(500, 50, "1 MB/s")
    st.mark_paused()
    # Verify paused state is reflected in progress text
    progress_text = st.get_progress_text()
    assert "⏸️ Paused" in progress_text
    assert "50%" in progress_text
    assert "Queued" not in progress_text
    # cleanup
    states.pop(st.filename, None)
    fid = register_file_id(st.filename)
    file_id_map.pop(fid, None)


def test_queue_cancel_removes_item():
    async def _inner():
        q = DownloadQueue(limit=1)
        ev = DummyEvent()
        qi = QueuedItem("fileA.bin", object(), 10, "/tmp/fileA.bin", ev)
        await q.enqueue(qi)
        assert "fileA.bin" in q.items
        assert q.cancel("fileA.bin") is True
        assert "fileA.bin" not in q.items

    asyncio.run(_inner())


def test_progress_keeps_buttons():
    async def _inner():
        st = DownloadState("fileB.bin", "/tmp/fileB.bin", 1000)
        msg = DummyMsg()
        cb = create_progress_callback(st.filename, 0.0, RateLimiter(min_tg=0, min_kodi=9999), msg, st)
        await cb(100, 1000)
        await cb(500, 1000)
        assert msg.buttons_history

    asyncio.run(_inner())


def test_queued_cancel_ui(monkeypatch):
    # Simulate a queued item with a message then cancel through queue.cancel + manager logic
    class StubMsg(DummyMsg):
        pass

    stub = StubMsg()
    qi = QueuedItem("fileD.bin", object(), 10, "/tmp/fileD.bin", DummyEvent())
    qi.message = stub
    global_queue.items[qi.filename] = qi  # inject directly without using async enqueue
    register_file_id(qi.filename)
    # Call queue.cancel (normally invoked via callback handler) and then mimic manager UI update
    assert global_queue.cancel(qi.filename) is True
    # Simulate what handler does
    asyncio.run(stub.edit(f"🛑 Cancelled (queued): {qi.filename}", buttons=None))
    assert "Cancelled" in (stub.last or "")


def test_run():  # entry point to ensure file executes, minimal smoke
    test_queue_cancel_removes_item()
    test_progress_keeps_buttons()
