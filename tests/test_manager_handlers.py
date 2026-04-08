"""Tests for downloader/manager.py helper functions and download flow."""

from __future__ import annotations

import asyncio
import os
import time

import config
import kodi
import utils
from downloader.manager import (
    _current_reserved_bytes,
    _final_cleanup,
    _handle_error,
    _handle_success,
    _infer_category_root,
    _init_state,
    _list_files_under,
    _post_download_check,
    _projected_free_mb,
    _safe_edit,
    _select_deletion_candidate,
    _send_start_message,
    _update_tracked_messages,
    filename_for_document,
    pre_checks,
)
from downloader.state import DownloadState, MessageType, file_id_map, message_tracker, states

# ── Fakes ──


class FakeMsg:
    """Minimal message fake with edit/respond."""

    def __init__(self, msg_id=1):
        self.id = msg_id
        self.edited = None
        self.responded = None

    async def edit(self, text, **kw):
        self.edited = text

    async def respond(self, text, **kw):
        self.responded = text
        return FakeMsg(msg_id=self.id + 100)


class FakeEvent:
    is_private = True
    raw_text = ""

    def __init__(self, document=None):
        self.document = document
        self.id = 1
        self._responded = None

    async def get_sender(self):
        return type("S", (), {"id": 1, "username": "test"})()

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


# ── _safe_edit ──


def test_safe_edit_success():
    msg = FakeMsg()

    async def _run():
        return await _safe_edit(msg, "hello")

    result = asyncio.run(_run())
    assert result is msg


def test_safe_edit_fallback_on_failure(monkeypatch):
    class FailMsg:
        id = 1

        async def edit(self, text, **kw):
            raise RuntimeError("gone")

        async def respond(self, text, **kw):
            return FakeMsg(99)

    msg = FailMsg()
    st = DownloadState("x.mp4", "/tmp/x.mp4", 100)

    async def _run():
        return await _safe_edit(msg, "hello", state=st)

    result = asyncio.run(_run())
    assert result is not None
    assert st.message is not None


# ── filename_for_document ──


def test_filename_for_document_with_attr():
    from telethon.tl.types import DocumentAttributeFilename

    doc = FakeDocument(attrs=[DocumentAttributeFilename("test.mkv")])
    assert filename_for_document(doc) == "test.mkv"


def test_filename_for_document_without_attr():
    doc = FakeDocument(attrs=[], mime_type="video/mp4")
    name = filename_for_document(doc)
    assert name.startswith("media_")
    assert name.endswith(".mp4")


# ── pre_checks ──


def test_pre_checks_new_file(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(config, "ORGANIZE_MEDIA", False)
    monkeypatch.setattr(config, "DISK_WARNING_MB", 0)

    from telethon.tl.types import DocumentAttributeFilename

    doc = FakeDocument(attrs=[DocumentAttributeFilename("movie.mp4")], size=500)
    ev = FakeEvent(document=doc)

    result = asyncio.run(pre_checks(ev))
    assert result is not None
    assert result[1] == "movie.mp4"


def test_pre_checks_existing_complete(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(config, "ORGANIZE_MEDIA", False)
    monkeypatch.setattr(config, "DISK_WARNING_MB", 0)

    (tmp_path / "movie.mp4").write_bytes(b"x" * 500)

    from telethon.tl.types import DocumentAttributeFilename

    doc = FakeDocument(attrs=[DocumentAttributeFilename("movie.mp4")], size=500)
    ev = FakeEvent(document=doc)

    result = asyncio.run(pre_checks(ev))
    assert result is None


def test_pre_checks_incomplete_redownload(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(config, "ORGANIZE_MEDIA", False)
    monkeypatch.setattr(config, "DISK_WARNING_MB", 0)

    (tmp_path / "movie.mp4").write_bytes(b"x" * 50)

    from telethon.tl.types import DocumentAttributeFilename

    doc = FakeDocument(attrs=[DocumentAttributeFilename("movie.mp4")], size=500)
    ev = FakeEvent(document=doc)

    result = asyncio.run(pre_checks(ev))
    assert result is not None
    assert not (tmp_path / "movie.mp4").exists()


def test_pre_checks_low_disk_warning(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(config, "ORGANIZE_MEDIA", False)
    monkeypatch.setattr(config, "DISK_WARNING_MB", 999999999)
    monkeypatch.setattr(utils, "free_disk_mb", lambda _: 1)

    from telethon.tl.types import DocumentAttributeFilename

    doc = FakeDocument(attrs=[DocumentAttributeFilename("movie.mp4")], size=500)
    ev = FakeEvent(document=doc)

    result = asyncio.run(pre_checks(ev))
    assert result is not None
    assert ev._responded is not None and "Low disk" in ev._responded


# ── _list_files_under ──


def test_list_files_under(tmp_path):
    (tmp_path / "a.txt").write_text("a")
    time.sleep(0.01)
    (tmp_path / "b.txt").write_text("b")

    result = _list_files_under(str(tmp_path), set())
    assert len(result) == 2
    assert result[0][1].endswith("a.txt")


def test_list_files_under_excludes(tmp_path):
    f = tmp_path / "skip.txt"
    f.write_text("x")
    result = _list_files_under(str(tmp_path), {str(f)})
    assert len(result) == 0


def test_list_files_under_oserror(tmp_path):
    f = tmp_path / "bad.txt"
    f.write_text("x")
    os.chmod(str(f), 0o000)
    try:
        result = _list_files_under(str(tmp_path), set())
        # Still returns the file (getmtime may or may not fail depending on OS)
        assert isinstance(result, list)
    finally:
        os.chmod(str(f), 0o644)


# ── _infer_category_root ──


def test_infer_category_root_movie(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ORGANIZE_MEDIA", True)
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(config, "MOVIES_DIR_NAME", "Movies")
    monkeypatch.setattr(config, "SERIES_DIR_NAME", "Series")
    monkeypatch.setattr(config, "OTHER_DIR_NAME", "Other")

    path = os.path.join(str(tmp_path), "Movies", "file.mp4")
    assert _infer_category_root(path) == os.path.join(str(tmp_path), "Movies")


def test_infer_category_root_no_match(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ORGANIZE_MEDIA", True)
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(config, "MOVIES_DIR_NAME", "Movies")
    monkeypatch.setattr(config, "SERIES_DIR_NAME", "Series")
    monkeypatch.setattr(config, "OTHER_DIR_NAME", "Other")

    assert _infer_category_root("/elsewhere/file.mp4") is None


def test_infer_category_root_disabled(monkeypatch):
    monkeypatch.setattr(config, "ORGANIZE_MEDIA", False)
    assert _infer_category_root("/any/path") is None


# ── _select_deletion_candidate ──


def test_select_deletion_candidate(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ORGANIZE_MEDIA", False)
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    f = tmp_path / "old.bin"
    f.write_text("data")
    result = _select_deletion_candidate(str(tmp_path / "new.bin"), set())
    assert result == str(f)


def test_select_deletion_candidate_organized(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ORGANIZE_MEDIA", True)
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(config, "MOVIES_DIR_NAME", "Movies")
    monkeypatch.setattr(config, "SERIES_DIR_NAME", "Series")
    monkeypatch.setattr(config, "OTHER_DIR_NAME", "Other")

    movies = tmp_path / "Movies"
    movies.mkdir()
    (movies / "old.mp4").write_text("x")

    target = str(movies / "new.mp4")
    result = _select_deletion_candidate(target, set())
    assert result == str(movies / "old.mp4")


def test_select_deletion_candidate_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ORGANIZE_MEDIA", False)
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    assert _select_deletion_candidate(str(tmp_path / "x"), set()) is None


# ── _projected_free_mb / _current_reserved_bytes ──


def test_projected_free_mb(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(utils, "free_disk_mb", lambda _: 500)
    assert _projected_free_mb(100 * 1024 * 1024) == 400


def test_current_reserved_bytes(monkeypatch):
    st = DownloadState("a.mp4", "/tmp/a.mp4", 1000)
    st.downloaded_bytes = 200
    states["a.mp4"] = st
    try:
        assert _current_reserved_bytes() >= 800
    finally:
        states.pop("a.mp4", None)


# ── _init_state ──


def test_init_state_new():
    ev = FakeEvent()
    try:
        st = _init_state("new.mp4", "/tmp/new.mp4", 500, ev)
        assert st.filename == "new.mp4"
        assert "new.mp4" in states
    finally:
        states.pop("new.mp4", None)
        file_id_map.clear()


def test_init_state_existing():
    ev = FakeEvent()
    existing = DownloadState("exist.mp4", "/tmp/old.mp4", 100)
    states["exist.mp4"] = existing
    try:
        st = _init_state("exist.mp4", "/tmp/new.mp4", 500, ev)
        assert st is existing
        assert st.path == "/tmp/new.mp4"
        assert st.size == 500
    finally:
        states.pop("exist.mp4", None)
        file_id_map.clear()


# ── _send_start_message ──


def test_send_start_message(monkeypatch):
    async def fake_notify(t, m):
        pass

    monkeypatch.setattr(kodi, "notify", fake_notify)

    ev = FakeEvent()
    st = DownloadState("test.mp4", "/tmp/test.mp4", 1000)

    async def _run():
        return await _send_start_message(ev, st)

    asyncio.run(_run())
    assert st.message is not None
    assert st.last_text is not None
    message_tracker.cleanup_file("test.mp4")


# ── _post_download_check ──


def test_post_download_check_success(tmp_path):
    f = tmp_path / "ok.bin"
    f.write_bytes(b"x" * 1000)
    st = DownloadState("ok.bin", str(f), 1000)
    msg = FakeMsg()
    result = asyncio.run(_post_download_check(True, 1000, str(f), st, msg, "ok.bin"))
    assert result is True


def test_post_download_check_cancelled(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    f = tmp_path / "cancel.bin"
    f.write_bytes(b"x" * 50)
    st = DownloadState("cancel.bin", str(f), 1000)
    st.mark_cancelled()
    msg = FakeMsg()
    result = asyncio.run(_post_download_check(False, 1000, str(f), st, msg, "cancel.bin"))
    assert result is False
    assert not f.exists()


def test_post_download_check_incomplete(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))

    async def fake_notify(t, m):
        pass

    monkeypatch.setattr(kodi, "notify", fake_notify)

    f = tmp_path / "inc.bin"
    f.write_bytes(b"x" * 50)
    st = DownloadState("inc.bin", str(f), 1000)
    msg = FakeMsg()
    result = asyncio.run(_post_download_check(False, 1000, str(f), st, msg, "inc.bin"))
    assert result is False
    assert msg.edited is not None and "incomplete" in msg.edited.lower()


# ── _handle_success ──


def test_handle_success_not_playing(monkeypatch, tmp_path):
    async def fake_is_playing():
        return False

    async def fake_play(path):
        pass

    async def fake_notify(t, m):
        pass

    monkeypatch.setattr(kodi, "is_playing", fake_is_playing)
    monkeypatch.setattr(kodi, "play", fake_play)
    monkeypatch.setattr(kodi, "notify", fake_notify)

    msg = FakeMsg()
    st = DownloadState("done.mp4", str(tmp_path / "done.mp4"), 100)
    states["done.mp4"] = st

    asyncio.run(_handle_success(msg, "done.mp4", str(tmp_path / "done.mp4"), st))
    assert st.completed
    assert msg.edited is not None and "complete" in msg.edited.lower()
    states.pop("done.mp4", None)


def test_handle_success_playing(monkeypatch, tmp_path):
    async def fake_is_playing():
        return True

    played = []

    async def fake_play(path):
        played.append(path)

    async def fake_notify(t, m):
        pass

    monkeypatch.setattr(kodi, "is_playing", fake_is_playing)
    monkeypatch.setattr(kodi, "play", fake_play)
    monkeypatch.setattr(kodi, "notify", fake_notify)

    msg = FakeMsg()
    st = DownloadState("done2.mp4", str(tmp_path / "done2.mp4"), 100)
    states["done2.mp4"] = st

    asyncio.run(_handle_success(msg, "done2.mp4", str(tmp_path / "done2.mp4"), st))
    assert len(played) == 0  # should not play when already playing
    states.pop("done2.mp4", None)


# ── _handle_error ──


def test_handle_error_cancelled(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))

    async def fake_notify(t, m):
        pass

    monkeypatch.setattr(kodi, "notify", fake_notify)

    f = tmp_path / "err.bin"
    f.write_bytes(b"x" * 10)
    st = DownloadState("err.bin", str(f), 100)
    st.mark_cancelled()
    msg = FakeMsg()
    states["err.bin"] = st

    asyncio.run(_handle_error(RuntimeError("boom"), st, msg, "err.bin", str(f)))
    assert not f.exists()
    states.pop("err.bin", None)


def test_handle_error_not_cancelled(monkeypatch, tmp_path):
    async def fake_notify(t, m):
        pass

    monkeypatch.setattr(kodi, "notify", fake_notify)

    st = DownloadState("err2.bin", str(tmp_path / "err2.bin"), 100)
    msg = FakeMsg()

    asyncio.run(_handle_error(RuntimeError("oops"), st, msg, "err2.bin", str(tmp_path / "err2.bin")))
    assert msg.edited is not None and "oops" in msg.edited


# ── _final_cleanup ──


def test_final_cleanup():
    st = DownloadState("clean.mp4", "/tmp/clean.mp4", 100)
    states["clean.mp4"] = st
    from downloader.ids import get_file_id

    fid = get_file_id("clean.mp4")
    file_id_map[fid] = "clean.mp4"

    _final_cleanup("clean.mp4")
    assert "clean.mp4" not in states
    assert fid not in file_id_map


# ── _update_tracked_messages ──


def test_update_tracked_messages_progress(monkeypatch):
    primary = FakeMsg(msg_id=1)
    mirror = FakeMsg(msg_id=2)
    st = DownloadState("track.mp4", "/tmp/track.mp4", 100)
    st.message = primary

    message_tracker.register_message("track.mp4", mirror, MessageType.PROGRESS, 1)

    async def _run():
        await _update_tracked_messages("track.mp4", st)

    asyncio.run(_run())
    assert mirror.edited is not None
    message_tracker.cleanup_file("track.mp4")


def test_update_tracked_messages_download_list(monkeypatch):
    list_msg = FakeMsg(msg_id=10)
    st = DownloadState("track2.mp4", "/tmp/track2.mp4", 100)

    message_tracker.register_message("track2.mp4", list_msg, MessageType.DOWNLOAD_LIST, 1)

    async def _run():
        await _update_tracked_messages("track2.mp4", st)

    asyncio.run(_run())
    assert list_msg.edited is not None
    message_tracker.cleanup_file("track2.mp4")


# ── _ensure_disk_space (TEST_AUTO_ACCEPT path) ──


def test_ensure_disk_space_enough(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(config, "MIN_FREE_DISK_MB", 10)
    monkeypatch.setattr(utils, "free_disk_mb", lambda _: 1000)

    ev = FakeEvent()

    async def _run():
        from downloader import manager

        return await manager._ensure_disk_space(ev, "f.mp4", 100)

    assert asyncio.run(_run()) is True


def test_ensure_disk_space_auto_accept(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(config, "MIN_FREE_DISK_MB", 999999)
    monkeypatch.setattr(config, "ORGANIZE_MEDIA", False)

    free_calls = {"n": 0}

    def fake_free(_):
        free_calls["n"] += 1
        return 999999 if free_calls["n"] > 1 else 1

    monkeypatch.setattr(utils, "free_disk_mb", fake_free)

    victim = tmp_path / "old.bin"
    victim.write_text("delete me")

    ev = FakeEvent()

    async def _run():
        from downloader import manager

        orig = manager.TEST_AUTO_ACCEPT
        manager.TEST_AUTO_ACCEPT = True
        try:
            return await manager._ensure_disk_space(ev, "new.mp4", 100, str(tmp_path / "new.mp4"))
        finally:
            manager.TEST_AUTO_ACCEPT = orig

    assert asyncio.run(_run()) is True
    assert not victim.exists()


def test_ensure_disk_space_no_double_count_with_preregistered_state(monkeypatch, tmp_path):
    """Pre-registered state must not double-count file size in projected free calculation."""
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(config, "MIN_FREE_DISK_MB", 200)
    # 800 MB free, 500 MB file → projected should be 300 MB (>= 200), no deletion needed.
    # Without the fix, projected would be 800 - (500+500) = -200 → wrongly triggers autoremove.
    monkeypatch.setattr(utils, "free_disk_mb", lambda _: 800)
    monkeypatch.setattr(config, "ORGANIZE_MEDIA", False)

    file_size = 500 * 1024 * 1024
    st = DownloadState("pre.mp4", str(tmp_path / "pre.mp4"), file_size)
    states["pre.mp4"] = st

    victim = tmp_path / "old.bin"
    victim.write_text("should survive")

    ev = FakeEvent()

    async def _run():
        from downloader import manager

        return await manager._ensure_disk_space(ev, "pre.mp4", file_size, str(tmp_path / "pre.mp4"))

    try:
        assert asyncio.run(_run()) is True
        assert victim.exists(), "Old file should NOT be deleted when space is sufficient"
    finally:
        states.pop("pre.mp4", None)


def test_ensure_disk_space_no_candidate(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(config, "MIN_FREE_DISK_MB", 999999)
    monkeypatch.setattr(config, "ORGANIZE_MEDIA", False)
    monkeypatch.setattr(utils, "free_disk_mb", lambda _: 1)

    ev = FakeEvent()

    async def _run():
        from downloader import manager

        return await manager._ensure_disk_space(ev, "f.mp4", 100)

    assert asyncio.run(_run()) is False


# ── Handler registration ──


class FakeClient:
    def __init__(self):
        self.handlers = []
        self.loop = asyncio.new_event_loop()

    def on(self, event_type):
        def decorator(fn):
            self.handlers.append(fn)
            return fn

        return decorator


def test_register_handlers_does_not_crash(monkeypatch):
    from downloader import manager

    monkeypatch.setattr(manager, "_queue_started", False)

    client = FakeClient()
    manager.register_handlers(client)
    assert len(client.handlers) > 0
    # Reset
    monkeypatch.setattr(manager, "_queue_started", False)
    client.loop.close()


# ── _status handler ──


def test_status_handler(monkeypatch):
    from downloader import manager

    monkeypatch.setattr(config, "is_user_allowed", lambda uid, uname: True)
    monkeypatch.setattr(config, "MAX_CONCURRENT_DOWNLOADS", 2)

    client = FakeClient()
    manager._register_status_handler(client)
    handler = client.handlers[0]

    ev = FakeEvent()

    asyncio.run(handler(ev))
    assert ev._responded is not None and "Active" in ev._responded
    client.loop.close()


def test_status_handler_unauthorized(monkeypatch):
    from downloader import manager

    monkeypatch.setattr(config, "is_user_allowed", lambda uid, uname: False)

    client = FakeClient()
    manager._register_status_handler(client)
    handler = client.handlers[0]

    ev = FakeEvent()
    asyncio.run(handler(ev))
    assert ev._responded is not None and "Not authorized" in ev._responded
    client.loop.close()


# ── _start handler ──


def test_start_handler(monkeypatch):
    from downloader import manager

    monkeypatch.setattr(config, "is_user_allowed", lambda uid, uname: True)
    monkeypatch.setattr(config, "MEMORY_WARNING_PERCENT", 0)

    client = FakeClient()
    manager._register_start_handler(client)
    handler = client.handlers[0]

    ev = FakeEvent()
    asyncio.run(handler(ev))
    assert ev._responded is not None and "Commands" in ev._responded
    client.loop.close()


def test_start_handler_unauthorized(monkeypatch):
    from downloader import manager

    monkeypatch.setattr(config, "is_user_allowed", lambda uid, uname: False)

    client = FakeClient()
    manager._register_start_handler(client)
    handler = client.handlers[0]

    ev = FakeEvent()
    asyncio.run(handler(ev))
    assert ev._responded is not None and "Not authorized" in ev._responded
    client.loop.close()
