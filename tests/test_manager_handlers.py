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
    _fan_out_mirrors,
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

    message_tracker.register_message("track.mp4", mirror, MessageType.PROGRESS)

    async def _run():
        await _update_tracked_messages("track.mp4", st)

    asyncio.run(_run())
    assert mirror.edited is not None
    message_tracker.cleanup_file("track.mp4")


def test_update_tracked_messages_download_list(monkeypatch):
    list_msg = FakeMsg(msg_id=10)
    st = DownloadState("track2.mp4", "/tmp/track2.mp4", 100)

    message_tracker.register_message("track2.mp4", list_msg, MessageType.DOWNLOAD_LIST)

    async def _run():
        await _update_tracked_messages("track2.mp4", st)

    asyncio.run(_run())
    assert list_msg.edited is not None
    message_tracker.cleanup_file("track2.mp4")


def test_update_tracked_messages_skips_frozen_list_message(monkeypatch):
    """Download list messages whose ID is in frozen_list_msg_ids are skipped."""
    from downloader.state import frozen_list_msg_ids

    list_msg = FakeMsg(msg_id=11)
    st = DownloadState("skip.mp4", "/tmp/skip.mp4", 100)

    message_tracker.register_message("skip.mp4", list_msg, MessageType.DOWNLOAD_LIST)
    frozen_list_msg_ids.add(11)

    async def _run():
        await _update_tracked_messages("skip.mp4", st)

    try:
        asyncio.run(_run())
        assert list_msg.edited is None
    finally:
        message_tracker.cleanup_file("skip.mp4")
        frozen_list_msg_ids.discard(11)


def test_update_tracked_messages_updates_non_frozen_list_message(monkeypatch):
    """Non-frozen download list messages are still updated."""
    from downloader.state import frozen_list_msg_ids

    list_msg = FakeMsg(msg_id=12)
    st = DownloadState("ok.mp4", "/tmp/ok.mp4", 100)

    message_tracker.register_message("ok.mp4", list_msg, MessageType.DOWNLOAD_LIST)
    # Freeze a different message
    frozen_list_msg_ids.add(99)

    async def _run():
        await _update_tracked_messages("ok.mp4", st)

    try:
        asyncio.run(_run())
        assert list_msg.edited is not None
    finally:
        message_tracker.cleanup_file("ok.mp4")
        frozen_list_msg_ids.discard(99)


# ── _fan_out_mirrors ──


def test_fan_out_mirrors_edits_non_primary_mirrors():
    """_fan_out_mirrors edits mirror messages but skips the primary."""
    primary = FakeMsg(msg_id=1)
    mirror = FakeMsg(msg_id=2)
    st = DownloadState("fan.mp4", "/tmp/fan.mp4", 100)
    st.message = primary

    message_tracker.register_message("fan.mp4", primary, MessageType.PROGRESS)
    message_tracker.register_message("fan.mp4", mirror, MessageType.PROGRESS)

    async def _run():
        await _fan_out_mirrors(st, "progress text", {})

    asyncio.run(_run())
    assert primary.edited is None
    assert mirror.edited == "progress text"
    message_tracker.cleanup_file("fan.mp4")


def test_fan_out_mirrors_fallback_on_failure():
    """_fan_out_mirrors replaces a broken mirror via respond."""
    primary = FakeMsg(msg_id=1)

    class FailMirror:
        id = 2
        responded = None

        async def edit(self, text, **kw):
            raise RuntimeError("gone")

        async def respond(self, text, **kw):
            self.responded = text
            return FakeMsg(msg_id=200)

    mirror = FailMirror()
    st = DownloadState("fall.mp4", "/tmp/fall.mp4", 100)
    st.message = primary

    message_tracker.register_message("fall.mp4", mirror, MessageType.PROGRESS)
    tracked = message_tracker.get_messages("fall.mp4", MessageType.PROGRESS)[0]

    async def _run():
        await _fan_out_mirrors(st, "update", {})

    asyncio.run(_run())
    assert mirror.responded == "update"
    assert tracked.message.id == 200
    message_tracker.cleanup_file("fall.mp4")


# ── _ensure_disk_space (TEST_AUTO_ACCEPT path) ──


def test_ensure_disk_space_enough(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(config, "MIN_FREE_DISK_MB", 10)
    monkeypatch.setattr(utils, "free_disk_mb", lambda _: 1000)

    ev = FakeEvent()

    async def _run():
        from downloader import manager

        return await manager._ensure_disk_space(ev, "f.mp4", 100)

    ok, _msg = asyncio.run(_run())
    assert ok is True


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

    ok, _msg = asyncio.run(_run())
    assert ok is True
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
        ok, _msg = asyncio.run(_run())
        assert ok is True
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

    ok, _msg = asyncio.run(_run())
    assert ok is False


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
    monkeypatch.setattr(manager, "_spawn", lambda coro: coro.close())

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


# ── Deletion callback ──


class FakeCallbackEvent:
    """Minimal callback query event for testing deletion callbacks."""

    def __init__(self, data: bytes, msg_id: int = 1):
        self.data = data
        self._message_id = msg_id
        self._answered = None
        self._edited_text = None
        self._edited_buttons = None

    async def answer(self, text=None, **kw):
        self._answered = text

    async def edit(self, text, **kw):
        self._edited_text = text
        self._edited_buttons = kw.get("buttons")


def test_deletion_callback_accept(monkeypatch):
    """Clicking Yes edits the message, clears buttons, and resolves the future."""
    from downloader import manager
    from downloader.state import PendingDeletion, pending_deletions

    client = FakeClient()
    manager._register_deletion_callbacks(client)
    handler = client.handlers[0]

    async def _run():
        msg = FakeMsg()
        pending = PendingDeletion(filename="movie.mp4", candidate="old.mkv")
        pending.message = msg
        pending_deletions["abc123"] = pending

        ev = FakeCallbackEvent(b"delok:abc123")
        await handler(ev)

        assert pending.choice == "yes"
        assert pending.future.done()
        assert msg.edited is not None
        assert "old.mkv" in msg.edited
        assert ev._answered == "Deleting"
        pending_deletions.pop("abc123", None)

    asyncio.run(_run())
    client.loop.close()


def test_deletion_callback_decline(monkeypatch):
    """Clicking No edits the message with cancellation text and resolves the future."""
    from downloader import manager
    from downloader.state import PendingDeletion, pending_deletions

    client = FakeClient()
    manager._register_deletion_callbacks(client)
    handler = client.handlers[0]

    async def _run():
        msg = FakeMsg()
        pending = PendingDeletion(filename="movie.mp4", candidate="old.mkv")
        pending.message = msg
        pending_deletions["def456"] = pending

        ev = FakeCallbackEvent(b"delnx:def456")
        await handler(ev)

        assert pending.choice == "no"
        assert pending.future.done()
        assert msg.edited is not None
        assert "movie.mp4" in msg.edited
        assert ev._answered == "Cancelled"
        pending_deletions.pop("def456", None)

    asyncio.run(_run())
    client.loop.close()


def test_deletion_callback_not_found():
    """Unknown pid returns not-found message."""
    from downloader import manager

    client = FakeClient()
    manager._register_deletion_callbacks(client)
    handler = client.handlers[0]

    ev = FakeCallbackEvent(b"delok:unknown")
    asyncio.run(handler(ev))
    assert ev._answered is not None and "no longer active" in ev._answered
    client.loop.close()


def test_deletion_callback_already_processed():
    """Double-clicking after processing returns already-processed message."""
    from downloader import manager
    from downloader.state import PendingDeletion, pending_deletions

    client = FakeClient()
    manager._register_deletion_callbacks(client)
    handler = client.handlers[0]

    async def _run():
        pending = PendingDeletion(filename="f.mp4", candidate="old.mkv")
        pending.message = FakeMsg()
        pending.future.set_result(True)
        pending_deletions["dup789"] = pending

        ev = FakeCallbackEvent(b"delok:dup789")
        await handler(ev)
        assert ev._answered == "Already processed"
        pending_deletions.pop("dup789", None)

    asyncio.run(_run())
    client.loop.close()


# ── _ensure_disk_space with existing_message ──


def test_ensure_disk_space_edits_existing_message(monkeypatch, tmp_path):
    """When existing_message is provided and no candidate, edit that message."""
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(config, "MIN_FREE_DISK_MB", 999999)
    monkeypatch.setattr(config, "ORGANIZE_MEDIA", False)
    monkeypatch.setattr(utils, "free_disk_mb", lambda _: 1)

    existing = FakeMsg()
    ev = FakeEvent()

    async def _run():
        from downloader import manager

        return await manager._ensure_disk_space(ev, "f.mp4", 100, existing_message=existing)

    ok, _msg = asyncio.run(_run())
    assert ok is False
    assert existing.edited is not None
    assert "no deletable files found" in existing.edited
    assert ev._responded is None


def test_ensure_disk_space_existing_message_not_touched_when_enough(monkeypatch, tmp_path):
    """When space is sufficient, existing_message is not edited."""
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(config, "MIN_FREE_DISK_MB", 10)
    monkeypatch.setattr(utils, "free_disk_mb", lambda _: 1000)

    existing = FakeMsg()
    ev = FakeEvent()

    async def _run():
        from downloader import manager

        return await manager._ensure_disk_space(ev, "f.mp4", 100, existing_message=existing)

    ok, _msg = asyncio.run(_run())
    assert ok is True
    assert existing.edited is None


# ── Queue cancel fallthrough to active cancel ──


def test_qcancel_falls_through_to_active():
    """Queue cancel falls through to active cancel when item has started."""
    from downloader import manager
    from downloader.state import register_file_id

    filename = "started.mp4"
    file_id = register_file_id(filename)
    st = DownloadState(filename, "/tmp/started.mp4", 1000)
    states[filename] = st

    client = FakeClient()
    manager._register_qcancel(client)
    handler = client.handlers[0]

    async def _run():
        ev = FakeCallbackEvent(f"qcancel:{file_id}".encode())
        await handler(ev)
        return ev

    try:
        ev = asyncio.run(_run())
        assert st.confirming_cancel is True
        assert ev._edited_text is not None
        assert "Cancel this download" in ev._edited_text
    finally:
        states.pop(filename, None)
        file_id_map.pop(file_id, None)
        st.confirming_cancel = False
        client.loop.close()


def test_qcancel_confirm_falls_through_to_active():
    """Queue cancel confirm cancels the active download when item has started."""
    from downloader import manager
    from downloader.state import register_file_id

    filename = "started2.mp4"
    file_id = register_file_id(filename)
    st = DownloadState(filename, "/tmp/started2.mp4", 1000)
    states[filename] = st

    client = FakeClient()
    manager._register_qcancel_confirm(client)
    handler = client.handlers[0]

    async def _run():
        ev = FakeCallbackEvent(f"qcy:{file_id}".encode())
        await handler(ev)
        return ev

    try:
        ev = asyncio.run(_run())
        assert st.cancelled is True
        assert ev._answered == "Cancelling"
    finally:
        states.pop(filename, None)
        file_id_map.pop(file_id, None)
        client.loop.close()


def test_qcancel_decline_shows_active_state():
    """Declining queue cancel shows active download progress when item has started."""
    from downloader import manager
    from downloader.state import register_file_id

    filename = "started3.mp4"
    file_id = register_file_id(filename)
    st = DownloadState(filename, "/tmp/started3.mp4", 1000)
    st.update_progress(500, 50, "1 MB/s")
    states[filename] = st

    client = FakeClient()
    manager._register_qcancel_confirm(client)
    handler = client.handlers[0]

    async def _run():
        ev = FakeCallbackEvent(f"qcn:{file_id}".encode())
        await handler(ev)
        return ev

    try:
        ev = asyncio.run(_run())
        assert st.cancelled is False
        assert ev._edited_text is not None
        assert "Downloading" in ev._edited_text
    finally:
        states.pop(filename, None)
        file_id_map.pop(file_id, None)
        client.loop.close()


# ── frozen_list_msg_ids for cancel from list ──


def test_lcancel_freezes_message_id():
    """lcancel from download list adds the message ID to frozen_list_msg_ids."""
    from downloader import manager
    from downloader.state import frozen_list_msg_ids, register_file_id

    filename = "freeze.mp4"
    file_id = register_file_id(filename)
    st = DownloadState(filename, "/tmp/freeze.mp4", 1000)
    states[filename] = st

    client = FakeClient()
    manager._register_pause_resume_cancel(client)
    handler = client.handlers[0]

    async def _run():
        ev = FakeCallbackEvent(f"lcancel:{file_id}".encode(), msg_id=42)
        await handler(ev)
        return ev

    try:
        asyncio.run(_run())
        assert 42 in frozen_list_msg_ids
    finally:
        states.pop(filename, None)
        file_id_map.pop(file_id, None)
        frozen_list_msg_ids.discard(42)
        st.confirming_cancel = False
        client.loop.close()


def test_cancel_confirm_unfreezes_message_id():
    """Cancel confirm (from list) removes the message ID from frozen_list_msg_ids."""
    from downloader import manager
    from downloader.state import frozen_list_msg_ids, register_file_id

    filename = "unfreeze.mp4"
    file_id = register_file_id(filename)
    st = DownloadState(filename, "/tmp/unfreeze.mp4", 1000)
    st.confirming_cancel = True
    states[filename] = st
    frozen_list_msg_ids.add(42)

    client = FakeClient()
    manager._register_cancel_confirm(client)
    handler = client.handlers[0]

    async def _run():
        ev = FakeCallbackEvent(f"cnl:{file_id}".encode(), msg_id=42)
        await handler(ev)
        return ev

    try:
        asyncio.run(_run())
        assert 42 not in frozen_list_msg_ids
    finally:
        states.pop(filename, None)
        file_id_map.pop(file_id, None)
        frozen_list_msg_ids.discard(42)
        client.loop.close()


# ── find_pending_deletion ──


def test_find_pending_deletion_found():
    """find_pending_deletion returns (pid, pending) when a match exists."""
    from downloader.state import PendingDeletion, find_pending_deletion, pending_deletions

    async def _run():
        pd = PendingDeletion(filename="target.mp4", candidate="old.mkv")
        pending_deletions["findme"] = pd
        try:
            result = find_pending_deletion("target.mp4")
            assert result is not None
            pid, found = result
            assert pid == "findme"
            assert found.candidate == "old.mkv"
        finally:
            pending_deletions.pop("findme", None)

    asyncio.run(_run())


def test_find_pending_deletion_not_found():
    """find_pending_deletion returns None when no match."""
    from downloader.state import find_pending_deletion, pending_deletions

    orig = dict(pending_deletions)
    pending_deletions.clear()
    try:
        assert find_pending_deletion("nonexistent.mp4") is None
    finally:
        pending_deletions.update(orig)


def test_find_pending_deletion_skips_done_future():
    """find_pending_deletion skips entries with already-resolved futures."""
    from downloader.state import PendingDeletion, find_pending_deletion, pending_deletions

    async def _run():
        pd = PendingDeletion(filename="done.mp4", candidate="old.mkv")
        pd.future.set_result(True)
        pending_deletions["done_pid"] = pd
        try:
            assert find_pending_deletion("done.mp4") is None
        finally:
            pending_deletions.pop("done_pid", None)

    asyncio.run(_run())


# ── _unblock_pending_deletion ──


def test_unblock_pending_deletion_resolves_future():
    """_unblock_pending_deletion resolves the future with choice='no'."""
    from downloader.manager import _unblock_pending_deletion
    from downloader.state import PendingDeletion, pending_deletions

    async def _run():
        pd = PendingDeletion(filename="block.mp4", candidate="old.mkv")
        pending_deletions["block_pid"] = pd
        try:
            _unblock_pending_deletion("block.mp4")
            assert pd.future.done()
            assert pd.choice == "no"
        finally:
            pending_deletions.pop("block_pid", None)

    asyncio.run(_run())


def test_unblock_pending_deletion_noop_when_no_match():
    """_unblock_pending_deletion does nothing when no pending deletion exists."""
    from downloader.manager import _unblock_pending_deletion
    from downloader.state import pending_deletions

    orig = dict(pending_deletions)
    pending_deletions.clear()
    try:
        _unblock_pending_deletion("nothing.mp4")  # should not raise
    finally:
        pending_deletions.update(orig)


# ── _ensure_disk_space respects cancelled state ──


def test_ensure_disk_space_returns_false_when_cancelled(monkeypatch, tmp_path):
    """After future resolves, if state is cancelled, _ensure_disk_space returns False cleanly."""
    from downloader.state import PendingDeletion

    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(config, "MIN_FREE_DISK_MB", 999999)
    monkeypatch.setattr(config, "ORGANIZE_MEDIA", False)
    monkeypatch.setattr(utils, "free_disk_mb", lambda _: 1)
    # Create a candidate file
    (tmp_path / "old.bin").write_bytes(b"x" * 100)

    ev = FakeEvent()

    async def _run():
        from downloader import manager

        # Create a state and mark it cancelled
        st = DownloadState("cancel.mp4", str(tmp_path / "cancel.mp4"), 100)
        st.cancelled = True
        states["cancel.mp4"] = st
        try:
            # Monkeypatch: make the pending deletion auto-resolve on creation
            orig_init = PendingDeletion.__post_init__

            def patched_init(self):
                orig_init(self)
                self.choice = "no"
                self.future.set_result(True)

            monkeypatch.setattr(PendingDeletion, "__post_init__", patched_init)

            ok, _msg = await manager._ensure_disk_space(ev, "cancel.mp4", 100, str(tmp_path / "cancel.mp4"))
            assert ok is False
        finally:
            states.pop("cancel.mp4", None)

    asyncio.run(_run())
