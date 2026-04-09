import asyncio

from downloader.ids import get_file_id
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


def test_pause_and_resume_state():
    """Verify pause/resume state transitions work correctly."""
    st = DownloadState("fileC.bin", "/tmp/fileC.bin", 1000)
    states[st.filename] = st
    register_file_id(st.filename)
    st.update_progress(500, 50, "1 MB/s")
    st.mark_paused()
    assert st.paused
    assert st.progress_percent == 50
    st.mark_resumed()
    assert not st.paused
    # cleanup
    states.pop(st.filename, None)
    fid = register_file_id(st.filename)
    file_id_map.pop(fid, None)


def test_queued_cancel_via_queue():
    """Queue.cancel removes item and returns True."""
    qi = QueuedItem("fileD.bin", object(), 10, "/tmp/fileD.bin", DummyEvent())
    global_queue.items[qi.filename] = qi
    register_file_id(qi.filename)
    assert global_queue.cancel(qi.filename) is True
    assert qi.filename not in global_queue.items
    file_id_map.pop(get_file_id(qi.filename), None)


def test_cancel_file_id_roundtrip():
    """File ID for cancel callbacks resolves correctly."""
    filename = "roundtrip.mp4"
    fid = register_file_id(filename)
    try:
        from downloader.state import resolve_file_id

        assert resolve_file_id(fid) == filename
        assert f"cy:{fid}" == f"cy:{get_file_id(filename)}"
        assert f"cn:{fid}" == f"cn:{get_file_id(filename)}"
    finally:
        file_id_map.pop(fid, None)


def test_progress_updates_state():
    """Progress callback updates in-memory state (no per-file messages)."""

    async def _inner():
        st = DownloadState("fileB.bin", "/tmp/fileB.bin", 1000)
        from downloader.progress import RateLimiter, create_progress_callback

        cb = create_progress_callback(st.filename, 0.0, RateLimiter(min_kodi=9999), st)
        await cb(100, 1000)
        assert st.progress_percent == 10
        await cb(500, 1000)
        assert st.progress_percent == 50

    asyncio.run(_inner())


def test_cancel_marks_state():
    """Cancelling a download marks state as cancelled."""
    st = DownloadState("cancel.bin", "/tmp/cancel.bin", 1000)
    assert not st.cancelled
    st.mark_cancelled()
    assert st.cancelled
    # Further state changes are blocked after cancel
    st.mark_paused()
    assert not st.paused  # pausing after cancel is a no-op


def test_waiting_for_space_state():
    """waiting_for_space flag is tracked in DownloadState."""
    st = DownloadState("space.mp4", "/tmp/space.mp4", 1000)
    assert not st.waiting_for_space
    st.waiting_for_space = True
    assert st.waiting_for_space
