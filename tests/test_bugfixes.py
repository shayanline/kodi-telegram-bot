"""Tests for bug fixes."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import config
from downloader.ids import get_file_id
from downloader.manager import _current_reserved_bytes, _init_state, _start_direct_download
from downloader.state import DownloadState, file_id_map, states
from filemanager import _path_registry, _render_root, _resolve
from main import _cleanup_partials
from organizer import build_final_path


class _StubEvent:
    id = 1
    sender_id = 42


# ── 1. _init_state reuses pre-registered state ──


def test_init_state_reuses_existing(monkeypatch):
    """When states already has a DownloadState, _init_state returns the same object with updated fields."""
    ev = _StubEvent()
    original = DownloadState("video.mp4", "/old/path", 500, original_event=None)
    states["video.mp4"] = original
    try:
        returned = _init_state("video.mp4", "/new/path", 999, ev)
        assert returned is original
        assert returned.path == "/new/path"
        assert returned.size == 999
        assert returned.original_event is ev
    finally:
        states.pop("video.mp4", None)
        file_id_map.pop(get_file_id("video.mp4"), None)


def test_init_state_preserves_cancelled_flag(monkeypatch):
    """A cancelled pre-registered state keeps cancelled=True after _init_state reuses it."""
    ev = _StubEvent()
    original = DownloadState("cancel.mp4", "/old", 100)
    original.mark_cancelled()
    states["cancel.mp4"] = original
    try:
        returned = _init_state("cancel.mp4", "/new", 200, ev)
        assert returned is original
        assert returned.cancelled is True
    finally:
        states.pop("cancel.mp4", None)
        file_id_map.pop(get_file_id("cancel.mp4"), None)


def test_init_state_creates_new_when_missing(monkeypatch):
    """When states has no entry, _init_state creates a new DownloadState and registers its file id."""
    ev = _StubEvent()
    states.pop("fresh.mp4", None)
    fid = get_file_id("fresh.mp4")
    file_id_map.pop(fid, None)
    try:
        returned = _init_state("fresh.mp4", "/path/fresh.mp4", 1234, ev)
        assert returned.filename == "fresh.mp4"
        assert "fresh.mp4" in states
        assert file_id_map.get(fid) == "fresh.mp4"
    finally:
        states.pop("fresh.mp4", None)
        file_id_map.pop(fid, None)


# ── 2. _current_reserved_bytes subtracts downloaded_bytes ──


def test_reserved_bytes_subtracts_downloaded():
    """Reserved bytes = size - downloaded_bytes for each state."""
    st = DownloadState("a.bin", "/tmp/a.bin", 1000)
    st.downloaded_bytes = 400
    states["a.bin"] = st
    try:
        assert _current_reserved_bytes() == 600
    finally:
        states.pop("a.bin", None)


def test_reserved_bytes_full_size_when_zero_downloaded():
    """When nothing downloaded yet, reserved equals full size."""
    st = DownloadState("b.bin", "/tmp/b.bin", 1000)
    st.downloaded_bytes = 0
    states["b.bin"] = st
    try:
        assert _current_reserved_bytes() == 1000
    finally:
        states.pop("b.bin", None)


def test_reserved_bytes_zero_when_over_downloaded():
    """Edge case: downloaded > size still yields 0 (not negative)."""
    st = DownloadState("c.bin", "/tmp/c.bin", 1000)
    st.downloaded_bytes = 1500
    states["c.bin"] = st
    try:
        assert _current_reserved_bytes() == 0
    finally:
        states.pop("c.bin", None)


# ── 3. Root-level files get correct callback data in file manager ──


def test_render_root_callback_data(tmp_path, monkeypatch):
    """Files use f:i: and directories use f:n: callback data in the root view."""
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    (tmp_path / "mydir").mkdir()
    (tmp_path / "myfile.mp4").write_bytes(b"data")
    try:
        _text, buttons = _render_root()
        # Flatten all button data (exclude refresh button)
        all_data = [btn.data for row in buttons for btn in row if hasattr(btn, "data")]
        entry_data = [d for d in all_data if d != b"f:r"]
        has_nav = any(d.startswith(b"f:n:") for d in entry_data)
        has_info = any(d.startswith(b"f:i:") for d in entry_data)
        assert has_nav, "directory should produce f:n: callback data"
        assert has_info, "file should produce f:i: callback data"
    finally:
        _path_registry.clear()


# ── 4. Path traversal check with os.sep ──


def test_resolve_blocks_prefix_traversal(monkeypatch):
    """A relpath resolving to a sibling whose name starts with DOWNLOAD_DIR basename returns None."""
    monkeypatch.setattr(config, "DOWNLOAD_DIR", "/tmp/abc")
    pid = "testpid1"
    _path_registry[pid] = "../abcdef/file"
    try:
        result = _resolve(pid)
        assert result is None
    finally:
        _path_registry.pop(pid, None)


# ── 5. Season 0 uses "Season 0" folder ──


def test_season_zero_folder(tmp_path, monkeypatch):
    """A filename with S00E01 should produce a path containing 'Season 0'."""
    monkeypatch.setattr(config, "ORGANIZE_MEDIA", True)
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    path, _name = build_final_path("Show.S00E01.mp4", base_dir=str(tmp_path))
    assert "Season 0" in path


# ── 6. _cleanup_partials removes files with size=0 ──


def test_cleanup_partials_removes_zero_size(tmp_path, monkeypatch):
    """A partial file with expected size 0 is always removed."""
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    f = tmp_path / "partial.bin"
    f.write_bytes(b"data")
    st = DownloadState("partial.bin", str(f), 0)
    st.mark_cancelled()
    removed = _cleanup_partials((st,))
    assert removed == 1
    assert not f.exists()


# ── 7. _start_direct_download cleans up on cancellation ──


def test_start_direct_download_cancelled_cleanup(monkeypatch):
    """Pre-registered state cancelled before download runs → cleaned up."""
    import downloader.manager as mgr

    st = DownloadState("test.mp4", "/tmp/test.mp4", 1000)
    st.mark_cancelled()
    fid = get_file_id("test.mp4")
    states["test.mp4"] = st
    file_id_map[fid] = "test.mp4"

    @asynccontextmanager
    async def _fake_slot():
        yield

    monkeypatch.setattr(mgr.queue, "slot", _fake_slot)

    try:
        asyncio.run(_start_direct_download(None, None, None, "test.mp4", 1000, "/tmp/test.mp4"))
    finally:
        # Cleanup in case test fails
        states.pop("test.mp4", None)
        file_id_map.pop(fid, None)

    assert "test.mp4" not in states
