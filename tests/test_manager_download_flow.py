"""Tests for download flow paths in downloader/manager.py.

Covers: _spawn, _periodic_list_updater, _prune_stale_categories,
download_with_retries, _queued_runner, _start_direct_download,
_download handler, and _register_category_selection callback.
"""

from __future__ import annotations

import asyncio
import contextlib
import time

import config
import kodi
import throttle
import utils
from downloader import manager
from downloader.manager import (
    _bg_tasks,
    _pending_categories,
    _prune_stale_categories,
    _spawn,
    download_with_retries,
)
from downloader.queue import QueuedItem, queue
from downloader.state import DownloadState, file_id_map, states

_real_sleep = asyncio.sleep

# ── Fakes ──


class FakeClient:
    def __init__(self):
        self.handlers = []
        self.loop = asyncio.new_event_loop()

    def on(self, event_type):
        def decorator(fn):
            self.handlers.append(fn)
            return fn

        return decorator


class FakeEvent:
    is_private = True
    raw_text = ""

    def __init__(self, document=None, data=None, chat_id=100, sender_id=1):
        self.document = document
        self.data = data or b""
        self.id = 1
        self.chat_id = chat_id
        self.sender_id = sender_id
        self._responded = None

    async def get_sender(self):
        return type("S", (), {"id": self.sender_id, "username": "test"})()

    async def respond(self, text, **kw):
        self._responded = text
        return self

    async def edit(self, text, **kw):
        pass

    async def answer(self, text=None, **kw):
        pass


class FakeDocument:
    def __init__(self, attrs=None, mime_type="video/mp4", size=1000):
        self.attributes = attrs or []
        self.mime_type = mime_type
        self.size = size


# ── 1. _spawn ──


def test_spawn_runs_and_cleans_up():
    """_spawn creates a task, runs it, and removes it from _bg_tasks on completion."""
    results = []

    async def _run():
        async def work():
            results.append("done")

        _spawn(work())
        await asyncio.sleep(0.05)

    asyncio.run(_run())
    assert results == ["done"]
    assert all(not t.done() for t in _bg_tasks)


def test_spawn_task_is_tracked():
    """_spawn adds the task to _bg_tasks before it completes."""
    captured = []

    async def _run():
        evt = asyncio.Event()

        async def slow():
            await evt.wait()

        _spawn(slow())
        await asyncio.sleep(0.01)
        captured.append(len(_bg_tasks))
        evt.set()
        await asyncio.sleep(0.01)

    asyncio.run(_run())
    assert captured[0] >= 1


# ── 2. _periodic_list_updater ──


def test_periodic_list_updater_active_state(monkeypatch):
    """When there are active downloads, update_all_lists is called."""
    calls = []

    async def fake_update(**kw):
        calls.append(kw)

    monkeypatch.setattr(manager, "update_all_lists", fake_update)
    monkeypatch.setattr(manager, "_LIST_UPDATE_INTERVAL", 0.01)

    st = DownloadState("active.mp4", "/tmp/active.mp4", 1000)
    states["active.mp4"] = st

    async def _run():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(manager._periodic_list_updater(), timeout=0.05)

    try:
        asyncio.run(_run())
    finally:
        states.pop("active.mp4", None)

    assert len(calls) >= 1
    assert calls[0]["priority"] == throttle.PRIORITY_PROGRESS


def test_periodic_list_updater_no_work_skips(monkeypatch):
    """When no active downloads and was_active=False, update_all_lists is NOT called."""
    calls = []

    async def fake_update(**kw):
        calls.append(kw)

    monkeypatch.setattr(manager, "update_all_lists", fake_update)
    monkeypatch.setattr(manager, "_LIST_UPDATE_INTERVAL", 0.01)

    async def _run():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(manager._periodic_list_updater(), timeout=0.05)

    asyncio.run(_run())
    assert len(calls) == 0


def test_periodic_list_updater_was_active_then_idle(monkeypatch):
    """After work ends, calls once more (was_active=True) then skips."""
    calls = []
    iteration = {"n": 0}

    async def fake_update(**kw):
        calls.append(kw)

    monkeypatch.setattr(manager, "update_all_lists", fake_update)
    monkeypatch.setattr(manager, "_LIST_UPDATE_INTERVAL", 0.01)

    original_sleep = asyncio.sleep

    async def counting_sleep(s):
        iteration["n"] += 1
        if iteration["n"] <= 2:
            if "active.mp4" not in states:
                states["active.mp4"] = DownloadState("active.mp4", "/tmp/a", 100)
        else:
            states.pop("active.mp4", None)
        await original_sleep(s)

    monkeypatch.setattr(asyncio, "sleep", counting_sleep)

    async def _run():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(manager._periodic_list_updater(), timeout=0.15)

    try:
        asyncio.run(_run())
    finally:
        states.pop("active.mp4", None)

    assert len(calls) >= 2


# ── 3. _prune_stale_categories ──


def test_prune_stale_categories_removes_old():
    """Entries older than TTL are removed."""
    old_ts = time.time() - 9999
    _pending_categories["old1"] = (None, None, 0, old_ts)
    _pending_categories["old2"] = (None, None, 0, old_ts)
    try:
        _prune_stale_categories()
        assert "old1" not in _pending_categories
        assert "old2" not in _pending_categories
    finally:
        _pending_categories.clear()


def test_prune_stale_categories_keeps_fresh():
    """Fresh entries are kept."""
    fresh_ts = time.time()
    _pending_categories["fresh"] = (None, None, 0, fresh_ts)
    try:
        _prune_stale_categories()
        assert "fresh" in _pending_categories
    finally:
        _pending_categories.clear()


# ── 4. download_with_retries ──


def test_download_with_retries_success(monkeypatch):
    """Successful download returns True."""
    monkeypatch.setattr(config, "MAX_RETRY_ATTEMPTS", 3)

    class Client:
        async def download_media(self, media, file=None, progress_callback=None):
            return "/tmp/file.mp4"

    st = DownloadState("ok.mp4", "/tmp/ok.mp4", 1000)
    doc = FakeDocument()

    async def noop_progress(a, b):
        pass

    result = asyncio.run(download_with_retries(Client(), doc, "/tmp/ok.mp4", noop_progress, st))
    assert result is True


def test_download_with_retries_returns_none(monkeypatch):
    """download_media returning None -> returns False."""
    monkeypatch.setattr(config, "MAX_RETRY_ATTEMPTS", 3)

    class Client:
        async def download_media(self, media, file=None, progress_callback=None):
            return None

    st = DownloadState("none.mp4", "/tmp/none.mp4", 1000)
    doc = FakeDocument()

    async def noop_progress(a, b):
        pass

    result = asyncio.run(download_with_retries(Client(), doc, "/tmp/none.mp4", noop_progress, st))
    assert result is False


def test_download_with_retries_timeout_then_success(monkeypatch):
    """TimeoutError on first attempt, succeeds on retry."""
    monkeypatch.setattr(config, "MAX_RETRY_ATTEMPTS", 3)

    attempt = {"n": 0}

    class Client:
        async def download_media(self, media, file=None, progress_callback=None):
            attempt["n"] += 1
            if attempt["n"] == 1:
                raise TimeoutError
            return "/tmp/file.mp4"

    st = DownloadState("retry.mp4", "/tmp/retry.mp4", 1000)
    doc = FakeDocument()

    async def noop_progress(a, b):
        pass

    monkeypatch.setattr(asyncio, "sleep", lambda s: _real_sleep(0))

    result = asyncio.run(download_with_retries(Client(), doc, "/tmp/retry.mp4", noop_progress, st))
    assert result is True
    assert attempt["n"] == 2


def test_download_with_retries_timeout_exhausted(monkeypatch):
    """All retries exhausted on TimeoutError -> returns False."""
    monkeypatch.setattr(config, "MAX_RETRY_ATTEMPTS", 2)

    class Client:
        async def download_media(self, media, file=None, progress_callback=None):
            raise TimeoutError

    st = DownloadState("timeout.mp4", "/tmp/timeout.mp4", 1000)
    doc = FakeDocument()

    async def noop_progress(a, b):
        pass

    monkeypatch.setattr(asyncio, "sleep", lambda s: _real_sleep(0))

    result = asyncio.run(download_with_retries(Client(), doc, "/tmp/timeout.mp4", noop_progress, st))
    assert result is False


def test_download_with_retries_cancelled_before_download(monkeypatch):
    """Cancelled state before download -> returns False."""
    monkeypatch.setattr(config, "MAX_RETRY_ATTEMPTS", 3)

    class Client:
        async def download_media(self, media, file=None, progress_callback=None):
            raise AssertionError("should not be called")

    st = DownloadState("cancel.mp4", "/tmp/cancel.mp4", 1000)
    st.mark_cancelled()
    doc = FakeDocument()

    async def noop_progress(a, b):
        pass

    result = asyncio.run(download_with_retries(Client(), doc, "/tmp/cancel.mp4", noop_progress, st))
    assert result is False


def test_download_with_retries_generic_exception_exhausted(monkeypatch):
    """Generic exceptions exhaust retries -> returns False."""
    monkeypatch.setattr(config, "MAX_RETRY_ATTEMPTS", 1)

    class Client:
        async def download_media(self, media, file=None, progress_callback=None):
            raise RuntimeError("network error")

    st = DownloadState("err.mp4", "/tmp/err.mp4", 1000)
    doc = FakeDocument()

    async def noop_progress(a, b):
        pass

    monkeypatch.setattr(asyncio, "sleep", lambda s: _real_sleep(0))

    result = asyncio.run(download_with_retries(Client(), doc, "/tmp/err.mp4", noop_progress, st))
    assert result is False


def test_download_with_retries_uses_source_message(monkeypatch):
    """When source_message is provided, it is passed to download_media."""
    monkeypatch.setattr(config, "MAX_RETRY_ATTEMPTS", 3)

    received_media = {}

    class Client:
        async def download_media(self, media, file=None, progress_callback=None):
            received_media["media"] = media
            return "/tmp/file.mp4"

    st = DownloadState("src.mp4", "/tmp/src.mp4", 1000)
    doc = FakeDocument()
    source_msg = object()

    async def noop_progress(a, b):
        pass

    result = asyncio.run(
        download_with_retries(Client(), doc, "/tmp/src.mp4", noop_progress, st, source_message=source_msg)
    )
    assert result is True
    assert received_media["media"] is source_msg


# ── 5. _queued_runner ──


def test_queued_runner_space_denied(monkeypatch, tmp_path):
    """When disk space check fails, cleanup is called."""
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(config, "MIN_FREE_DISK_MB", 999999)
    monkeypatch.setattr(config, "ORGANIZE_MEDIA", False)
    monkeypatch.setattr(utils, "free_disk_mb", lambda _: 1)

    async def fake_notify(t, m):
        pass

    monkeypatch.setattr(kodi, "notify", fake_notify)

    qi = QueuedItem(
        filename="queued.mp4",
        document=FakeDocument(),
        size=1000,
        path=str(tmp_path / "queued.mp4"),
        event=FakeEvent(),
    )

    async def _run():
        from downloader.manager import _queued_runner

        await _queued_runner(FakeClient(), qi)

    try:
        asyncio.run(_run())
    finally:
        states.pop("queued.mp4", None)
        file_id_map.clear()

    assert "queued.mp4" not in states


def test_queued_runner_space_ok_but_cancelled(monkeypatch, tmp_path):
    """Space OK but state cancelled -> cleanup."""
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(config, "MIN_FREE_DISK_MB", 10)
    monkeypatch.setattr(utils, "free_disk_mb", lambda _: 1000)

    qi = QueuedItem(
        filename="qcancel.mp4",
        document=FakeDocument(),
        size=1000,
        path=str(tmp_path / "qcancel.mp4"),
        event=FakeEvent(),
    )

    async def _run():
        from downloader.manager import _queued_runner

        async def fake_ensure(event, filename, size, path=None, existing_message=None):
            st = states.get(filename)
            if st:
                st.mark_cancelled()
            return True, None

        monkeypatch.setattr(manager, "_ensure_disk_space", fake_ensure)
        await _queued_runner(FakeClient(), qi)

    try:
        asyncio.run(_run())
    finally:
        states.pop("qcancel.mp4", None)
        file_id_map.clear()

    assert "qcancel.mp4" not in states


def test_queued_runner_exception_during_run(monkeypatch, tmp_path):
    """Exception in run_download -> cleanup + reraise."""
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(config, "MIN_FREE_DISK_MB", 10)
    monkeypatch.setattr(utils, "free_disk_mb", lambda _: 1000)

    qi = QueuedItem(
        filename="qerr.mp4",
        document=FakeDocument(),
        size=1000,
        path=str(tmp_path / "qerr.mp4"),
        event=FakeEvent(),
    )

    async def fake_ensure(event, filename, size, path=None, existing_message=None):
        return True, None

    async def fake_run_download(client, event, document, filename, size, path):
        raise RuntimeError("download boom")

    monkeypatch.setattr(manager, "_ensure_disk_space", fake_ensure)
    monkeypatch.setattr(manager, "run_download", fake_run_download)

    raised = False

    async def _run():
        nonlocal raised
        from downloader.manager import _queued_runner

        try:
            await _queued_runner(FakeClient(), qi)
        except RuntimeError:
            raised = True

    try:
        asyncio.run(_run())
    finally:
        states.pop("qerr.mp4", None)
        file_id_map.clear()

    assert raised
    assert "qerr.mp4" not in states


# ── 6. _start_direct_download ──


def test_start_direct_download_cancelled_before_space(monkeypatch, tmp_path):
    """State cancelled before space check -> cleanup."""
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))

    filename = "direct_cancel.mp4"
    st = DownloadState(filename, str(tmp_path / filename), 1000)
    st.mark_cancelled()
    states[filename] = st

    async def _run():
        from downloader.manager import _start_direct_download

        await _start_direct_download(
            FakeClient(), FakeEvent(), FakeDocument(), filename, 1000, str(tmp_path / filename)
        )

    try:
        asyncio.run(_run())
    finally:
        states.pop(filename, None)
        file_id_map.clear()

    assert filename not in states


def test_start_direct_download_no_state(monkeypatch, tmp_path):
    """No state registered -> cleanup path."""
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))

    filename = "nostate.mp4"

    async def _run():
        from downloader.manager import _start_direct_download

        await _start_direct_download(
            FakeClient(), FakeEvent(), FakeDocument(), filename, 1000, str(tmp_path / filename)
        )

    asyncio.run(_run())
    assert filename not in states


def test_start_direct_download_space_denied(monkeypatch, tmp_path):
    """Space denied -> cleanup."""
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(config, "MIN_FREE_DISK_MB", 999999)
    monkeypatch.setattr(config, "ORGANIZE_MEDIA", False)
    monkeypatch.setattr(utils, "free_disk_mb", lambda _: 1)

    filename = "direct_nospace.mp4"
    st = DownloadState(filename, str(tmp_path / filename), 1000)
    states[filename] = st

    async def _run():
        from downloader.manager import _start_direct_download

        await _start_direct_download(
            FakeClient(), FakeEvent(), FakeDocument(), filename, 1000, str(tmp_path / filename)
        )

    try:
        asyncio.run(_run())
    finally:
        states.pop(filename, None)
        file_id_map.clear()

    assert filename not in states


def test_start_direct_download_exception_cleanup(monkeypatch, tmp_path):
    """Exception in run_download -> cleanup."""
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(config, "MIN_FREE_DISK_MB", 10)
    monkeypatch.setattr(utils, "free_disk_mb", lambda _: 1000)

    filename = "direct_err.mp4"
    st = DownloadState(filename, str(tmp_path / filename), 1000)
    states[filename] = st

    async def fake_ensure(event, fn, size, path=None, existing_message=None):
        return True, None

    async def fake_run_download(client, event, document, fn, size, path):
        raise RuntimeError("boom")

    monkeypatch.setattr(manager, "_ensure_disk_space", fake_ensure)
    monkeypatch.setattr(manager, "run_download", fake_run_download)

    async def _run():
        from downloader.manager import _start_direct_download

        await _start_direct_download(
            FakeClient(), FakeEvent(), FakeDocument(), filename, 1000, str(tmp_path / filename)
        )

    try:
        asyncio.run(_run())
    finally:
        states.pop(filename, None)
        file_id_map.clear()

    assert filename not in states


# ── 7. _download handler ──


def _get_download_handler(monkeypatch):
    """Register the _download handler and return the handler function."""
    client = FakeClient()
    manager._register_download_handler(client)
    return client.handlers[0], client


def test_download_handler_unauthorized(monkeypatch):
    """Unauthorized user gets rejection."""
    monkeypatch.setattr(config, "is_user_allowed", lambda uid, uname: False)
    handler, client = _get_download_handler(monkeypatch)

    doc = FakeDocument(mime_type="video/mp4")
    ev = FakeEvent(document=doc)

    asyncio.run(handler(ev))
    assert ev._responded is not None
    assert "not authorized" in ev._responded.lower()
    client.loop.close()


def test_download_handler_non_media(monkeypatch):
    """Non-media document gets rejection."""
    monkeypatch.setattr(config, "is_user_allowed", lambda uid, uname: True)
    monkeypatch.setattr(utils, "is_media_file", lambda doc: False)
    handler, client = _get_download_handler(monkeypatch)

    doc = FakeDocument(mime_type="application/pdf")
    ev = FakeEvent(document=doc)

    asyncio.run(handler(ev))
    assert ev._responded is not None
    assert "video and audio" in ev._responded.lower()
    client.loop.close()


def test_download_handler_duplicate_active(monkeypatch, tmp_path):
    """Duplicate active download gets acknowledgement."""
    monkeypatch.setattr(config, "is_user_allowed", lambda uid, uname: True)
    monkeypatch.setattr(utils, "is_media_file", lambda doc: True)
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(config, "ORGANIZE_MEDIA", False)

    from telethon.tl.types import DocumentAttributeFilename

    handler, client = _get_download_handler(monkeypatch)

    doc = FakeDocument(attrs=[DocumentAttributeFilename("dup.mp4")], mime_type="video/mp4", size=1000)
    ev = FakeEvent(document=doc)

    st = DownloadState("dup.mp4", str(tmp_path / "dup.mp4"), 1000)
    states["dup.mp4"] = st

    try:
        asyncio.run(handler(ev))
        assert ev._responded is not None
        assert "already downloading" in ev._responded.lower()
    finally:
        states.pop("dup.mp4", None)
        file_id_map.clear()
        client.loop.close()


def test_download_handler_duplicate_queued(monkeypatch, tmp_path):
    """Duplicate queued download gets acknowledgement."""
    monkeypatch.setattr(config, "is_user_allowed", lambda uid, uname: True)
    monkeypatch.setattr(utils, "is_media_file", lambda doc: True)
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(config, "ORGANIZE_MEDIA", False)

    from telethon.tl.types import DocumentAttributeFilename

    handler, client = _get_download_handler(monkeypatch)

    doc = FakeDocument(attrs=[DocumentAttributeFilename("qdup.mp4")], mime_type="video/mp4", size=1000)
    ev = FakeEvent(document=doc)

    qi = QueuedItem("qdup.mp4", doc, 1000, str(tmp_path / "qdup.mp4"), ev)
    queue.items["qdup.mp4"] = qi

    try:
        asyncio.run(handler(ev))
        assert ev._responded is not None
        assert "already queued" in ev._responded.lower()
    finally:
        queue.items.pop("qdup.mp4", None)
        states.clear()
        file_id_map.clear()
        client.loop.close()


def test_download_handler_enqueue_when_full(monkeypatch, tmp_path):
    """When at max concurrent, new download is enqueued."""
    monkeypatch.setattr(config, "is_user_allowed", lambda uid, uname: True)
    monkeypatch.setattr(utils, "is_media_file", lambda doc: True)
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(config, "ORGANIZE_MEDIA", False)
    monkeypatch.setattr(config, "MAX_CONCURRENT_DOWNLOADS", 1)
    monkeypatch.setattr(config, "DISK_WARNING_MB", 0)

    async def fake_update(**kw):
        pass

    monkeypatch.setattr(manager, "update_all_lists", fake_update)

    from telethon.tl.types import DocumentAttributeFilename

    handler, client = _get_download_handler(monkeypatch)

    st = DownloadState("existing.mp4", str(tmp_path / "existing.mp4"), 1000)
    states["existing.mp4"] = st

    doc = FakeDocument(attrs=[DocumentAttributeFilename("new.mp4")], mime_type="video/mp4", size=500)
    ev = FakeEvent(document=doc)

    try:
        asyncio.run(handler(ev))
        assert ev._responded is not None
        assert "queued" in ev._responded.lower()
    finally:
        states.pop("existing.mp4", None)
        queue.items.pop("new.mp4", None)
        states.pop("new.mp4", None)
        file_id_map.clear()
        client.loop.close()


def test_download_handler_direct_download(monkeypatch, tmp_path):
    """When slots available, direct download is started."""
    monkeypatch.setattr(config, "is_user_allowed", lambda uid, uname: True)
    monkeypatch.setattr(utils, "is_media_file", lambda doc: True)
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(config, "ORGANIZE_MEDIA", False)
    monkeypatch.setattr(config, "MAX_CONCURRENT_DOWNLOADS", 5)
    monkeypatch.setattr(config, "DISK_WARNING_MB", 0)

    spawned = []
    monkeypatch.setattr(manager, "_spawn", lambda coro: (spawned.append(True), coro.close()))

    from telethon.tl.types import DocumentAttributeFilename

    handler, client = _get_download_handler(monkeypatch)

    doc = FakeDocument(attrs=[DocumentAttributeFilename("direct.mp4")], mime_type="video/mp4", size=500)
    ev = FakeEvent(document=doc)

    try:
        asyncio.run(handler(ev))
        assert ev._responded is not None
        assert "added" in ev._responded.lower()
        assert len(spawned) == 1
    finally:
        states.pop("direct.mp4", None)
        file_id_map.clear()
        queue.items.clear()
        client.loop.close()


def test_download_handler_ambiguous_category(monkeypatch, tmp_path):
    """Ambiguous category with ORGANIZE_MEDIA shows category buttons."""
    monkeypatch.setattr(config, "is_user_allowed", lambda uid, uname: True)
    monkeypatch.setattr(utils, "is_media_file", lambda doc: True)
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(config, "ORGANIZE_MEDIA", True)
    monkeypatch.setattr(config, "MOVIES_DIR_NAME", "Movies")
    monkeypatch.setattr(config, "SERIES_DIR_NAME", "Series")
    monkeypatch.setattr(config, "OTHER_DIR_NAME", "Other")

    from telethon.tl.types import DocumentAttributeFilename

    from organizer import ParsedMedia

    monkeypatch.setattr(
        manager,
        "parse_filename",
        lambda fn, text=None: ParsedMedia(category="other", title="Ambiguous", year=2023),
    )

    handler, client = _get_download_handler(monkeypatch)

    doc = FakeDocument(attrs=[DocumentAttributeFilename("ambiguous.mp4")], mime_type="video/mp4", size=500)
    ev = FakeEvent(document=doc)

    try:
        asyncio.run(handler(ev))
        assert ev._responded is not None
        assert "select category" in ev._responded.lower()
    finally:
        _pending_categories.clear()
        file_id_map.clear()
        states.clear()
        client.loop.close()


# ── 8. _register_category_selection ──


def _get_category_handler(monkeypatch):
    """Register the category selection handler and return it."""
    client = FakeClient()
    manager._register_category_selection(client)
    return client.handlers[0], client


def test_category_selection_unknown_file_id(monkeypatch):
    """Unknown file_id -> not-found."""
    handler, client = _get_category_handler(monkeypatch)

    ev = FakeEvent(data=b"catm:unknown123")

    answered = {}

    async def fake_answer(text=None, **kw):
        answered["text"] = text

    ev.answer = fake_answer

    asyncio.run(handler(ev))
    assert "no longer active" in answered.get("text", "").lower()
    client.loop.close()


def test_category_selection_expired(monkeypatch):
    """Valid file_id but no pending category -> expired."""
    from downloader.state import register_file_id

    fname = "expired_cat.mp4"
    file_id = register_file_id(fname)

    handler, client = _get_category_handler(monkeypatch)

    ev = FakeEvent(data=f"catm:{file_id}".encode())

    answered = {}

    async def fake_answer(text=None, **kw):
        answered["text"] = text

    ev.answer = fake_answer

    try:
        asyncio.run(handler(ev))
        assert "expired" in answered.get("text", "").lower()
    finally:
        file_id_map.clear()
        client.loop.close()


def test_category_selection_already_queued(monkeypatch, tmp_path):
    """Valid selection but already queued -> already queued."""
    from downloader.state import register_file_id

    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(config, "ORGANIZE_MEDIA", True)
    monkeypatch.setattr(config, "MOVIES_DIR_NAME", "Movies")
    monkeypatch.setattr(config, "SERIES_DIR_NAME", "Series")
    monkeypatch.setattr(config, "OTHER_DIR_NAME", "Other")

    fname = "catdup.mp4"
    file_id = register_file_id(fname)

    doc = FakeDocument(size=500)
    orig_event = FakeEvent()
    _pending_categories[file_id] = (doc, orig_event, 500, time.time())

    handler, client = _get_category_handler(monkeypatch)

    from organizer import build_final_path

    _path, final_name = build_final_path(fname, forced_category="movie")

    st = DownloadState(final_name, _path, 500)
    states[final_name] = st

    ev = FakeEvent(data=f"catm:{file_id}".encode())

    answered = {}

    async def fake_answer(text=None, **kw):
        answered["text"] = text

    ev.answer = fake_answer

    try:
        asyncio.run(handler(ev))
        assert "already queued" in answered.get("text", "").lower()
    finally:
        states.pop(final_name, None)
        _pending_categories.clear()
        file_id_map.clear()
        queue.items.clear()
        client.loop.close()


def test_category_selection_starts_download(monkeypatch, tmp_path):
    """Valid category selection starts a download."""
    from downloader.state import register_file_id

    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(config, "ORGANIZE_MEDIA", True)
    monkeypatch.setattr(config, "MOVIES_DIR_NAME", "Movies")
    monkeypatch.setattr(config, "SERIES_DIR_NAME", "Series")
    monkeypatch.setattr(config, "OTHER_DIR_NAME", "Other")
    monkeypatch.setattr(config, "MAX_CONCURRENT_DOWNLOADS", 5)

    fname = "moviefile.2023.mp4"
    file_id = register_file_id(fname)

    doc = FakeDocument(size=500)
    orig_event = FakeEvent()
    _pending_categories[file_id] = (doc, orig_event, 500, time.time())

    spawned = []
    monkeypatch.setattr(manager, "_spawn", lambda coro: (spawned.append(True), coro.close()))

    handler, client = _get_category_handler(monkeypatch)

    ev = FakeEvent(data=f"cato:{file_id}".encode())

    answered = {}

    async def fake_answer(text=None, **kw):
        answered["text"] = text

    ev.answer = fake_answer

    try:
        asyncio.run(handler(ev))
        assert answered.get("text") == "Started"
        assert len(spawned) == 1
    finally:
        states.clear()
        _pending_categories.clear()
        file_id_map.clear()
        queue.items.clear()
        client.loop.close()


def test_category_selection_enqueues_when_full(monkeypatch, tmp_path):
    """Category selection enqueues when at max concurrent."""
    from downloader.state import register_file_id

    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(config, "ORGANIZE_MEDIA", True)
    monkeypatch.setattr(config, "MOVIES_DIR_NAME", "Movies")
    monkeypatch.setattr(config, "SERIES_DIR_NAME", "Series")
    monkeypatch.setattr(config, "OTHER_DIR_NAME", "Other")
    monkeypatch.setattr(config, "MAX_CONCURRENT_DOWNLOADS", 1)

    async def fake_update(**kw):
        pass

    monkeypatch.setattr(manager, "update_all_lists", fake_update)

    fname = "qmovie.2023.mp4"
    file_id = register_file_id(fname)

    doc = FakeDocument(size=500)
    orig_event = FakeEvent()
    _pending_categories[file_id] = (doc, orig_event, 500, time.time())

    st = DownloadState("filling.mp4", str(tmp_path / "filling.mp4"), 1000)
    states["filling.mp4"] = st

    handler, client = _get_category_handler(monkeypatch)

    ev = FakeEvent(data=f"cato:{file_id}".encode())

    answered = {}

    async def fake_answer(text=None, **kw):
        answered["text"] = text

    ev.answer = fake_answer

    try:
        asyncio.run(handler(ev))
        assert answered.get("text") == "Queued"
    finally:
        states.clear()
        _pending_categories.clear()
        file_id_map.clear()
        queue.items.clear()
        client.loop.close()
