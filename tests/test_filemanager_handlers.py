"""Tests for filemanager event handlers and uncovered OSError branches.

Covers lines 85-86, 95-96, 190-191, 268-269, 351-352, 382-384, 392-393,
397-412, 416-532 of filemanager.py.
"""

from __future__ import annotations

import asyncio
import os
import re
from types import SimpleNamespace
from unittest.mock import patch

import config
import filemanager
import throttle

# ── Helpers ──


def _make_tree(tmp_path, structure: dict) -> str:
    """Create a directory tree from a dict and return the root path."""
    for name, content in structure.items():
        if isinstance(content, dict):
            sub = tmp_path / name
            sub.mkdir(parents=True, exist_ok=True)
            _make_tree(sub, content)
        else:
            f = tmp_path / name
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"\x00" * content)
    return str(tmp_path)


class FakeClient:
    def __init__(self):
        self.handlers = []

    def on(self, event_type):
        def decorator(fn):
            self.handlers.append(fn)
            return fn

        return decorator


class FakeEvent:
    def __init__(self, data=b"", pattern_match=None, user_id=1, username="test"):
        self.data = data
        self.pattern_match = pattern_match
        self._user_id = user_id
        self._username = username
        self._responded = None
        self._responded_kw = {}
        self._edited = None
        self._edited_kw = {}
        self._answered = None
        self._answer_kw = {}
        self._all_responds = []
        self._answer_called = False

    async def get_sender(self):
        return type("S", (), {"id": self._user_id, "username": self._username})()

    async def respond(self, text, **kw):
        self._responded = text
        self._responded_kw = kw
        self._all_responds.append(text)
        return self

    async def edit(self, text, **kw):
        self._edited = text
        self._edited_kw = kw
        return self

    async def answer(self, text=None, **kw):
        self._answered = text
        self._answer_kw = kw
        self._answer_called = True


def _setup(monkeypatch, tmp_path):
    """Monkeypatch throttle + config, register handlers, return dict by name."""
    monkeypatch.setattr(throttle, "serialized", lambda fn: fn)

    async def fake_send(target, text, **kw):
        return await target.respond(text, **kw)

    async def fake_edit(target, text, **kw):
        return await target.edit(text, **kw)

    async def fake_answer(event, text=None, **kw):
        await event.answer(text, **kw)

    monkeypatch.setattr(throttle, "send_message", fake_send)
    monkeypatch.setattr(throttle, "edit_message", fake_edit)
    monkeypatch.setattr(throttle, "answer_callback", fake_answer)

    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(config, "ALLOWED_USER_IDS", set())
    monkeypatch.setattr(config, "ALLOWED_USERNAMES", set())

    client = FakeClient()
    filemanager.register_filemanager(client)
    return {h.__name__: h for h in client.handlers}


def _cb_match(pattern, data):
    """Create a regex match object for callback data."""
    return re.match(pattern, data)


# ── OSError branches ──


def test_dir_summary_walk_oserror(monkeypatch, tmp_path):
    """Lines 85-86: os.walk itself raises OSError."""
    _make_tree(tmp_path, {"sub": {"f.txt": 10}})

    def raising_walk(path, **kw):
        raise OSError("mocked")

    monkeypatch.setattr(os, "walk", raising_walk)
    count, total = filemanager._dir_summary(str(tmp_path / "sub"))
    assert count == 0
    assert total == 0


def test_entry_size_file_getsize_oserror(monkeypatch, tmp_path):
    """Lines 95-96: os.path.getsize raises for an existing file."""
    f = tmp_path / "f.bin"
    f.write_bytes(b"\x00" * 42)
    original = os.path.getsize

    def fake_getsize(path):
        if str(f) in str(path):
            raise OSError("mocked")
        return original(path)

    monkeypatch.setattr(os.path, "getsize", fake_getsize)
    assert filemanager._entry_size(str(f)) == 0


def test_render_root_file_getsize_oserror(monkeypatch, tmp_path):
    """Lines 190-191: getsize fails for a file in root listing."""
    _make_tree(tmp_path, {"broken.mp4": 100})
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    original = os.path.getsize

    def fake_getsize(path):
        if "broken.mp4" in str(path):
            raise OSError("mocked")
        return original(path)

    monkeypatch.setattr(os.path, "getsize", fake_getsize)
    text, _ = filemanager._render_root()
    assert "broken.mp4" in text


def test_render_dir_file_getsize_oserror(monkeypatch, tmp_path):
    """Lines 268-269: getsize fails for a file in directory listing."""
    _make_tree(tmp_path, {"Movies": {"broken.mkv": 100}})
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    original = os.path.getsize

    def fake_getsize(path):
        if "broken.mkv" in str(path):
            raise OSError("mocked")
        return original(path)

    monkeypatch.setattr(os.path, "getsize", fake_getsize)
    text, _ = filemanager._render_dir("Movies", 1)
    assert "broken.mkv" in text


def test_render_delete_confirm_file_getsize_oserror(monkeypatch, tmp_path):
    """Lines 351-352: getsize fails in delete confirmation."""
    _make_tree(tmp_path, {"broken.mkv": 100})
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    original = os.path.getsize

    def fake_getsize(path):
        if "broken.mkv" in str(path):
            raise OSError("mocked")
        return original(path)

    monkeypatch.setattr(os.path, "getsize", fake_getsize)
    text, _ = filemanager._render_delete_confirm("broken.mkv")
    assert "Delete this file" in text


def test_do_delete_oserror(monkeypatch, tmp_path):
    """Lines 382-384: delete raises OSError."""
    f = tmp_path / "locked.txt"
    f.write_bytes(b"\x00")
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))

    def raising_remove(path):
        raise OSError("permission denied")

    monkeypatch.setattr(os, "remove", raising_remove)
    assert not filemanager._do_delete(str(f))


# ── register_filemanager ──


def test_register_filemanager_registers_all_handlers(monkeypatch):
    """Lines 392-393: register_filemanager wires both sub-functions."""
    monkeypatch.setattr(throttle, "serialized", lambda fn: fn)
    client = FakeClient()
    filemanager.register_filemanager(client)
    assert len(client.handlers) == 8
    names = {h.__name__ for h in client.handlers}
    assert "_files" in names
    assert "_root" in names
    assert "_navigate" in names
    assert "_file_info" in names
    assert "_delete_prompt" in names
    assert "_delete_confirm" in names
    assert "_delete_cancel" in names
    assert "_noop" in names


# ── _files command handler (lines 397-412) ──


def test_files_command_authorized(monkeypatch, tmp_path):
    """Authorized user sees file manager dashboard."""
    handlers = _setup(monkeypatch, tmp_path)
    _make_tree(tmp_path, {"movie.mkv": 100})
    event = FakeEvent()
    asyncio.run(handlers["_files"](event))
    assert event._responded is not None
    assert "File Manager" in event._responded


def test_files_command_unauthorized(monkeypatch, tmp_path):
    """Unauthorized user gets rejection message."""
    handlers = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "ALLOWED_USER_IDS", {99999})
    event = FakeEvent(user_id=1, username="hacker")
    asyncio.run(handlers["_files"](event))
    assert "Not authorized" in event._responded


# ── _root callback (lines 416-423) ──


def test_root_callback(monkeypatch, tmp_path):
    """Root callback edits message with file manager view."""
    handlers = _setup(monkeypatch, tmp_path)
    _make_tree(tmp_path, {"movie.mkv": 100})
    data = b"f:r:1:S"
    event = FakeEvent(
        data=data,
        pattern_match=_cb_match(rb"f:r:(\d+):([SND])", data),
    )
    asyncio.run(handlers["_root"](event))
    assert "File Manager" in event._edited
    assert event._answer_called


# ── _navigate callback (lines 425-439) ──


def test_navigate_callback_valid(monkeypatch, tmp_path):
    """Navigate into a valid directory."""
    handlers = _setup(monkeypatch, tmp_path)
    _make_tree(tmp_path, {"Movies": {"a.mkv": 100}})
    pid = filemanager._path_id("Movies")
    data = f"f:n:{pid}:1:S".encode()
    event = FakeEvent(
        data=data,
        pattern_match=_cb_match(rb"f:n:([a-f0-9]{8}):(\d+):([SND])", data),
    )
    asyncio.run(handlers["_navigate"](event))
    assert "Movies" in event._edited
    assert event._answer_called


def test_navigate_callback_expired(monkeypatch, tmp_path):
    """Navigate with unknown pid shows expired alert."""
    handlers = _setup(monkeypatch, tmp_path)
    data = b"f:n:deadbeef:1:S"
    event = FakeEvent(
        data=data,
        pattern_match=_cb_match(rb"f:n:([a-f0-9]{8}):(\d+):([SND])", data),
    )
    asyncio.run(handlers["_navigate"](event))
    assert event._answered == "Session expired \u2014 use /files again"


def test_navigate_callback_not_dir(monkeypatch, tmp_path):
    """Navigate to a path that is a file (not a directory) shows expired."""
    handlers = _setup(monkeypatch, tmp_path)
    _make_tree(tmp_path, {"file.mkv": 100})
    pid = filemanager._path_id("file.mkv")
    data = f"f:n:{pid}:1:S".encode()
    event = FakeEvent(
        data=data,
        pattern_match=_cb_match(rb"f:n:([a-f0-9]{8}):(\d+):([SND])", data),
    )
    asyncio.run(handlers["_navigate"](event))
    assert event._answered == "Session expired \u2014 use /files again"


# ── _file_info callback (lines 441-456) ──


def test_file_info_callback_file(monkeypatch, tmp_path):
    """File info for a regular file shows detail view."""
    handlers = _setup(monkeypatch, tmp_path)
    _make_tree(tmp_path, {"movie.mkv": 1024})
    pid = filemanager._path_id("movie.mkv")
    data = f"f:i:{pid}:S".encode()
    event = FakeEvent(
        data=data,
        pattern_match=_cb_match(rb"f:i:([a-f0-9]{8}):([SND])", data),
    )
    asyncio.run(handlers["_file_info"](event))
    assert "movie.mkv" in event._edited
    assert "Size" in event._edited
    assert event._answer_called


def test_file_info_callback_dir(monkeypatch, tmp_path):
    """File info for a directory renders dir listing instead."""
    handlers = _setup(monkeypatch, tmp_path)
    _make_tree(tmp_path, {"Movies": {"a.mkv": 100}})
    pid = filemanager._path_id("Movies")
    data = f"f:i:{pid}:S".encode()
    event = FakeEvent(
        data=data,
        pattern_match=_cb_match(rb"f:i:([a-f0-9]{8}):([SND])", data),
    )
    asyncio.run(handlers["_file_info"](event))
    assert "Movies" in event._edited
    assert event._answer_called


def test_file_info_callback_expired(monkeypatch, tmp_path):
    """File info with unknown pid shows expired alert."""
    handlers = _setup(monkeypatch, tmp_path)
    data = b"f:i:deadbeef:S"
    event = FakeEvent(
        data=data,
        pattern_match=_cb_match(rb"f:i:([a-f0-9]{8}):([SND])", data),
    )
    asyncio.run(handlers["_file_info"](event))
    assert event._answered == "Session expired \u2014 use /files again"


# ── _delete_prompt callback (lines 458-473) ──


def test_delete_prompt_callback(monkeypatch, tmp_path):
    """Delete prompt for a valid file shows confirmation."""
    handlers = _setup(monkeypatch, tmp_path)
    _make_tree(tmp_path, {"movie.mkv": 100})
    pid = filemanager._path_id("movie.mkv")
    data = f"f:d:{pid}:S".encode()
    event = FakeEvent(
        data=data,
        pattern_match=_cb_match(rb"f:d:([a-f0-9]{8}):([SND])", data),
    )
    asyncio.run(handlers["_delete_prompt"](event))
    assert "Delete this file" in event._edited
    assert event._answer_called


def test_delete_prompt_expired(monkeypatch, tmp_path):
    """Delete prompt with expired pid shows alert."""
    handlers = _setup(monkeypatch, tmp_path)
    data = b"f:d:deadbeef:S"
    event = FakeEvent(
        data=data,
        pattern_match=_cb_match(rb"f:d:([a-f0-9]{8}):([SND])", data),
    )
    asyncio.run(handlers["_delete_prompt"](event))
    assert event._answered == "Session expired \u2014 use /files again"


def test_delete_prompt_protected(monkeypatch, tmp_path):
    """Delete prompt for a file being downloaded shows lock alert."""
    handlers = _setup(monkeypatch, tmp_path)
    _make_tree(tmp_path, {"active.mkv": 100})
    fpath = str(tmp_path / "active.mkv")
    pid = filemanager._path_id("active.mkv")
    state = SimpleNamespace(path=fpath)
    with patch.dict(filemanager.states, {"active.mkv": state}):
        data = f"f:d:{pid}:S".encode()
        event = FakeEvent(
            data=data,
            pattern_match=_cb_match(rb"f:d:([a-f0-9]{8}):([SND])", data),
        )
        asyncio.run(handlers["_delete_prompt"](event))
        assert "Cannot delete" in (event._answered or "")


# ── _delete_confirm callback (lines 475-502) ──


def test_delete_confirm_success_root(monkeypatch, tmp_path):
    """Successful file deletion from root shows root view after."""
    handlers = _setup(monkeypatch, tmp_path)
    _make_tree(tmp_path, {"movie.mkv": 100})
    pid = filemanager._path_id("movie.mkv")
    data = f"f:y:{pid}:S".encode()
    event = FakeEvent(
        data=data,
        pattern_match=_cb_match(rb"f:y:([a-f0-9]{8}):([SND])", data),
    )
    asyncio.run(handlers["_delete_confirm"](event))
    assert not os.path.exists(tmp_path / "movie.mkv")
    assert "Deleted" in (event._answered or "")
    assert "File Manager" in event._edited


def test_delete_confirm_success_subdir(monkeypatch, tmp_path):
    """Delete file in subdirectory shows parent dir after."""
    handlers = _setup(monkeypatch, tmp_path)
    _make_tree(tmp_path, {"Movies": {"movie.mkv": 100, "other.mkv": 50}})
    relpath = os.path.join("Movies", "movie.mkv")
    pid = filemanager._path_id(relpath)
    data = f"f:y:{pid}:S".encode()
    event = FakeEvent(
        data=data,
        pattern_match=_cb_match(rb"f:y:([a-f0-9]{8}):([SND])", data),
    )
    asyncio.run(handlers["_delete_confirm"](event))
    assert not os.path.exists(tmp_path / "Movies" / "movie.mkv")
    assert "Deleted" in (event._answered or "")
    assert "Movies" in event._edited


def test_delete_confirm_expired(monkeypatch, tmp_path):
    """Delete confirm with expired pid shows 'already deleted'."""
    handlers = _setup(monkeypatch, tmp_path)
    data = b"f:y:deadbeef:S"
    event = FakeEvent(
        data=data,
        pattern_match=_cb_match(rb"f:y:([a-f0-9]{8}):([SND])", data),
    )
    asyncio.run(handlers["_delete_confirm"](event))
    assert "Already deleted" in (event._answered or "")
    assert "File Manager" in event._edited


def test_delete_confirm_file_gone(monkeypatch, tmp_path):
    """Delete confirm when pid resolves but file was already removed."""
    handlers = _setup(monkeypatch, tmp_path)
    _make_tree(tmp_path, {"gone.mkv": 100})
    pid = filemanager._path_id("gone.mkv")
    os.remove(tmp_path / "gone.mkv")
    data = f"f:y:{pid}:S".encode()
    event = FakeEvent(
        data=data,
        pattern_match=_cb_match(rb"f:y:([a-f0-9]{8}):([SND])", data),
    )
    asyncio.run(handlers["_delete_confirm"](event))
    assert "Already deleted" in (event._answered or "")
    assert "File Manager" in event._edited


def test_delete_confirm_protected(monkeypatch, tmp_path):
    """Delete confirm for a protected file shows lock alert."""
    handlers = _setup(monkeypatch, tmp_path)
    _make_tree(tmp_path, {"active.mkv": 100})
    fpath = str(tmp_path / "active.mkv")
    pid = filemanager._path_id("active.mkv")
    state = SimpleNamespace(path=fpath)
    with patch.dict(filemanager.states, {"active.mkv": state}):
        data = f"f:y:{pid}:S".encode()
        event = FakeEvent(
            data=data,
            pattern_match=_cb_match(rb"f:y:([a-f0-9]{8}):([SND])", data),
        )
        asyncio.run(handlers["_delete_confirm"](event))
        assert "Cannot delete" in (event._answered or "")


def test_delete_confirm_failure(monkeypatch, tmp_path):
    """Delete confirm when _do_delete fails reports failure."""
    handlers = _setup(monkeypatch, tmp_path)
    _make_tree(tmp_path, {"movie.mkv": 100})
    pid = filemanager._path_id("movie.mkv")
    monkeypatch.setattr(filemanager, "_do_delete", lambda p: False)
    data = f"f:y:{pid}:S".encode()
    event = FakeEvent(
        data=data,
        pattern_match=_cb_match(rb"f:y:([a-f0-9]{8}):([SND])", data),
    )
    asyncio.run(handlers["_delete_confirm"](event))
    assert "Failed to delete" in (event._answered or "")
    assert "File Manager" in event._edited


# ── _delete_cancel callback (lines 504-527) ──


def test_delete_cancel_file(monkeypatch, tmp_path):
    """Cancel deletion of a file returns to file detail view."""
    handlers = _setup(monkeypatch, tmp_path)
    _make_tree(tmp_path, {"movie.mkv": 100})
    pid = filemanager._path_id("movie.mkv")
    data = f"f:x:{pid}:S".encode()
    event = FakeEvent(
        data=data,
        pattern_match=_cb_match(rb"f:x:([a-f0-9]{8}):([SND])", data),
    )
    asyncio.run(handlers["_delete_cancel"](event))
    assert "movie.mkv" in event._edited
    assert "Size" in event._edited
    assert event._answer_called


def test_delete_cancel_dir(monkeypatch, tmp_path):
    """Cancel deletion of a directory returns to dir listing."""
    handlers = _setup(monkeypatch, tmp_path)
    _make_tree(tmp_path, {"Movies": {"a.mkv": 100}})
    pid = filemanager._path_id("Movies")
    data = f"f:x:{pid}:S".encode()
    event = FakeEvent(
        data=data,
        pattern_match=_cb_match(rb"f:x:([a-f0-9]{8}):([SND])", data),
    )
    asyncio.run(handlers["_delete_cancel"](event))
    assert "Movies" in event._edited
    assert event._answer_called


def test_delete_cancel_expired(monkeypatch, tmp_path):
    """Cancel with expired pid falls back to root view."""
    handlers = _setup(monkeypatch, tmp_path)
    data = b"f:x:deadbeef:S"
    event = FakeEvent(
        data=data,
        pattern_match=_cb_match(rb"f:x:([a-f0-9]{8}):([SND])", data),
    )
    asyncio.run(handlers["_delete_cancel"](event))
    assert event._answered == "Session expired \u2014 use /files again"
    assert "File Manager" in event._edited


def test_delete_cancel_path_gone_root(monkeypatch, tmp_path):
    """Cancel when root-level file was deleted falls back to root view."""
    handlers = _setup(monkeypatch, tmp_path)
    _make_tree(tmp_path, {"doomed.mkv": 100})
    pid = filemanager._path_id("doomed.mkv")
    os.remove(tmp_path / "doomed.mkv")
    data = f"f:x:{pid}:S".encode()
    event = FakeEvent(
        data=data,
        pattern_match=_cb_match(rb"f:x:([a-f0-9]{8}):([SND])", data),
    )
    asyncio.run(handlers["_delete_cancel"](event))
    assert "File Manager" in event._edited


def test_delete_cancel_path_gone_subdir(monkeypatch, tmp_path):
    """Cancel when subdir file was deleted shows parent dir."""
    handlers = _setup(monkeypatch, tmp_path)
    _make_tree(tmp_path, {"Movies": {"doomed.mkv": 100}})
    relpath = os.path.join("Movies", "doomed.mkv")
    pid = filemanager._path_id(relpath)
    os.remove(tmp_path / "Movies" / "doomed.mkv")
    data = f"f:x:{pid}:S".encode()
    event = FakeEvent(
        data=data,
        pattern_match=_cb_match(rb"f:x:([a-f0-9]{8}):([SND])", data),
    )
    asyncio.run(handlers["_delete_cancel"](event))
    assert "Movies" in event._edited


# ── _noop callback (lines 529-532) ──


def test_noop_callback(monkeypatch, tmp_path):
    """Noop just answers the callback query."""
    handlers = _setup(monkeypatch, tmp_path)
    event = FakeEvent(data=b"f:noop")
    asyncio.run(handlers["_noop"](event))
    assert event._answer_called
