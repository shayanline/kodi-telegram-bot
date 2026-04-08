"""Tests for the filemanager module."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

import filemanager


def _make_tree(tmp_path, structure: dict) -> str:
    """Create a directory tree from a dict and return the root path.

    Keys ending with '/' create directories; values are file sizes in bytes.
    """
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


def _patch_download_dir(tmp_path):
    return patch.object(filemanager.config, "DOWNLOAD_DIR", str(tmp_path))


# ── Path registry ──


def test_path_id_returns_8_char_hex():
    pid = filemanager._path_id("Movies/foo")
    assert len(pid) == 8
    assert all(c in "0123456789abcdef" for c in pid)


def test_path_id_deterministic():
    assert filemanager._path_id("abc") == filemanager._path_id("abc")


def test_path_id_registers():
    filemanager._path_registry.clear()
    pid = filemanager._path_id("test/path")
    assert filemanager._path_registry[pid] == "test/path"


def test_resolve_valid(tmp_path):
    _make_tree(tmp_path, {"a.txt": 10})
    with _patch_download_dir(tmp_path):
        pid = filemanager._path_id("a.txt")
        result = filemanager._resolve(pid)
        assert result is not None
        assert result.endswith("a.txt")


def test_resolve_unknown_hash():
    assert filemanager._resolve("zzzzzzzz") is None


def test_resolve_blocks_traversal(tmp_path):
    _make_tree(tmp_path, {"ok.txt": 5})
    with _patch_download_dir(tmp_path):
        pid = filemanager._path_id("../../etc/passwd")
        result = filemanager._resolve(pid)
        assert result is None


# ── Protected file detection ──


def test_is_protected_matches_active(tmp_path):
    f = tmp_path / "movie.mkv"
    f.write_bytes(b"\x00")
    state = SimpleNamespace(path=str(f))
    with patch.dict(filemanager.states, {"movie.mkv": state}):
        assert filemanager._is_protected(str(f))


def test_is_protected_false_when_idle(tmp_path):
    f = tmp_path / "movie.mkv"
    f.write_bytes(b"\x00")
    assert not filemanager._is_protected(str(f))


def test_is_protected_matches_queued(tmp_path):
    f = tmp_path / "queued.mkv"
    f.write_bytes(b"\x00")
    qi = SimpleNamespace(path=str(f))
    with patch.object(filemanager.queue, "items", {"queued.mkv": qi}):
        assert filemanager._is_protected(str(f))


def test_is_protected_recursive_dir(tmp_path):
    root = _make_tree(tmp_path, {"Movies": {"file.mkv": 100}})
    fpath = os.path.join(root, "Movies", "file.mkv")
    state = SimpleNamespace(path=fpath)
    with patch.dict(filemanager.states, {"file.mkv": state}):
        assert filemanager._is_protected_recursive(os.path.join(root, "Movies"))


def test_is_protected_recursive_false_when_idle(tmp_path):
    _make_tree(tmp_path, {"Movies": {"file.mkv": 100}})
    assert not filemanager._is_protected_recursive(str(tmp_path / "Movies"))


# ── Dir summary ──


def test_dir_summary_counts(tmp_path):
    _make_tree(tmp_path, {"a.txt": 10, "b.txt": 20, "sub": {"c.txt": 30}})
    count, total = filemanager._dir_summary(str(tmp_path))
    assert count == 3
    assert total == 60


def test_dir_summary_empty(tmp_path):
    count, total = filemanager._dir_summary(str(tmp_path))
    assert count == 0
    assert total == 0


def test_dir_summary_nonexistent(tmp_path):
    count, total = filemanager._dir_summary(str(tmp_path / "nope"))
    assert count == 0
    assert total == 0


# ── Entry size ──


def test_entry_size_file(tmp_path):
    f = tmp_path / "f.bin"
    f.write_bytes(b"\x00" * 42)
    assert filemanager._entry_size(str(f)) == 42


def test_entry_size_dir(tmp_path):
    _make_tree(tmp_path, {"sub": {"a.txt": 10, "b.txt": 20}})
    assert filemanager._entry_size(str(tmp_path / "sub")) == 30


# ── Disk bar ──


def test_disk_bar_contains_bar_chars(tmp_path):
    result = filemanager._disk_bar(str(tmp_path))
    assert "💾" in result
    assert "█" in result or "░" in result
    assert "free" in result


def test_disk_bar_oserror():
    result = filemanager._disk_bar("/nonexistent/path/that/does/not/exist")
    assert "unavailable" in result


# ── Sorted entries ──


def test_sorted_entries_largest_first(tmp_path):
    _make_tree(tmp_path, {"small.txt": 10, "big.txt": 1000, "mid.txt": 500})
    entries = filemanager._sorted_entries(str(tmp_path))
    assert entries[0] == "big.txt"
    assert entries[1] == "mid.txt"
    assert entries[2] == "small.txt"


def test_sorted_entries_empty(tmp_path):
    assert filemanager._sorted_entries(str(tmp_path)) == []


def test_sorted_entries_nonexistent(tmp_path):
    assert filemanager._sorted_entries(str(tmp_path / "nope")) == []


# ── Render root ──


def test_render_root_empty(tmp_path):
    with _patch_download_dir(tmp_path):
        text, buttons = filemanager._render_root()
        assert "empty" in text.lower()
        assert len(buttons) >= 1


def test_render_root_with_dirs(tmp_path):
    _make_tree(tmp_path, {"Movies": {"a.mkv": 100}, "Series": {"b.mkv": 200}})
    with _patch_download_dir(tmp_path):
        text, _buttons = filemanager._render_root()
        assert "File Manager" in text
        assert "Movies" in text
        assert "Series" in text
        assert "💾" in text


def test_render_root_with_files(tmp_path):
    _make_tree(tmp_path, {"standalone.mp4": 500})
    with _patch_download_dir(tmp_path):
        text, _buttons = filemanager._render_root()
        assert "standalone.mp4" in text


# ── Render dir ──


def test_render_dir_basic(tmp_path):
    _make_tree(tmp_path, {"Movies": {"a.mkv": 100, "b.mkv": 200}})
    with _patch_download_dir(tmp_path):
        text, buttons = filemanager._render_dir("Movies", 1)
        assert "Movies" in text
        assert "2 items" in text
        assert "b.mkv" in text  # largest first
        # Should have nav row, del row, bottom row
        assert len(buttons) >= 3


def test_render_dir_empty_folder(tmp_path):
    (tmp_path / "Empty").mkdir()
    with _patch_download_dir(tmp_path):
        text, buttons = filemanager._render_dir("Empty", 1)
        assert "Empty folder" in text
        # Should have back + delete folder buttons
        found_delete = any("Delete" in str(b) for row in buttons for b in row)
        assert found_delete


def test_render_dir_pagination(tmp_path):
    # Create more than _ITEMS_PER_PAGE files
    files = {f"file_{i:02d}.mkv": (100 - i) for i in range(12)}
    _make_tree(tmp_path, {"Big": files})
    with _patch_download_dir(tmp_path):
        text_p1, _buttons_p1 = filemanager._render_dir("Big", 1)
        assert "page 1/" in text_p1
        assert "12 items" in text_p1

        text_p2, _buttons_p2 = filemanager._render_dir("Big", 2)
        assert "page 2/" in text_p2

        # Last page
        text_p3, _buttons_p3 = filemanager._render_dir("Big", 3)
        assert "page 3/" in text_p3


def test_render_dir_page_clamping(tmp_path):
    _make_tree(tmp_path, {"D": {"a.txt": 10}})
    with _patch_download_dir(tmp_path):
        text, _ = filemanager._render_dir("D", 999)
        assert "1 item" in text


def test_render_dir_protected_shows_lock(tmp_path):
    _make_tree(tmp_path, {"Movies": {"active.mkv": 100}})
    fpath = str(tmp_path / "Movies" / "active.mkv")
    state = SimpleNamespace(path=fpath)
    with _patch_download_dir(tmp_path), patch.dict(filemanager.states, {"active.mkv": state}):
        _text, buttons = filemanager._render_dir("Movies", 1)
        # The delete row should have a lock button
        all_data = [b.data.decode() if hasattr(b, "data") else "" for row in buttons for b in row]
        assert "f:noop" in all_data


# ── Render file ──


def test_render_file_normal(tmp_path):
    _make_tree(tmp_path, {"video.mkv": 1024})
    with _patch_download_dir(tmp_path):
        text, buttons = filemanager._render_file("video.mkv")
        assert "video.mkv" in text
        assert "Size" in text
        assert "Modified" in text
        # Should have delete + back buttons
        all_data = [b.data.decode() if hasattr(b, "data") else "" for row in buttons for b in row]
        assert any("f:d:" in d for d in all_data)


def test_render_file_protected(tmp_path):
    _make_tree(tmp_path, {"active.mkv": 1024})
    fpath = str(tmp_path / "active.mkv")
    state = SimpleNamespace(path=fpath)
    with _patch_download_dir(tmp_path), patch.dict(filemanager.states, {"active.mkv": state}):
        text, buttons = filemanager._render_file("active.mkv")
        assert "downloading" in text.lower()
        # No delete button
        all_data = [b.data.decode() if hasattr(b, "data") else "" for row in buttons for b in row]
        assert not any("f:d:" in d for d in all_data)


def test_render_file_missing(tmp_path):
    with _patch_download_dir(tmp_path):
        text, _buttons = filemanager._render_file("gone.mkv")
        assert "not found" in text.lower()


# ── Render delete confirm ──


def test_render_delete_confirm_file(tmp_path):
    _make_tree(tmp_path, {"f.mkv": 500})
    with _patch_download_dir(tmp_path):
        text, buttons = filemanager._render_delete_confirm("f.mkv")
        assert "Delete this file" in text
        assert "f.mkv" in text
        all_data = [b.data.decode() if hasattr(b, "data") else "" for row in buttons for b in row]
        assert any("f:y:" in d for d in all_data)
        assert any("f:x:" in d for d in all_data)


def test_render_delete_confirm_dir(tmp_path):
    _make_tree(tmp_path, {"Movies": {"a.mkv": 100, "b.mkv": 200}})
    with _patch_download_dir(tmp_path):
        text, _buttons = filemanager._render_delete_confirm("Movies")
        assert "folder" in text.lower()
        assert "2 item" in text


def test_render_delete_confirm_missing(tmp_path):
    with _patch_download_dir(tmp_path):
        text, _ = filemanager._render_delete_confirm("nope")
        assert "not found" in text.lower()


# ── Do delete ──


def test_do_delete_file(tmp_path):
    _make_tree(tmp_path, {"sub": {"f.txt": 10}})
    fpath = str(tmp_path / "sub" / "f.txt")
    with _patch_download_dir(tmp_path):
        assert filemanager._do_delete(fpath)
        assert not os.path.exists(fpath)
        # Empty parent should also be cleaned
        assert not os.path.exists(str(tmp_path / "sub"))


def test_do_delete_dir(tmp_path):
    _make_tree(tmp_path, {"Movies": {"a.mkv": 10, "b.mkv": 20}})
    dpath = str(tmp_path / "Movies")
    with _patch_download_dir(tmp_path):
        assert filemanager._do_delete(dpath)
        assert not os.path.exists(dpath)


def test_do_delete_nonexistent(tmp_path):
    assert not filemanager._do_delete(str(tmp_path / "nope"))


# ── Large directory pagination stress ──


def test_large_dir_pagination_consistency(tmp_path):
    """Ensure all items appear exactly once across all pages."""
    n = 53  # prime number to stress edge cases
    files = {f"file_{i:03d}.dat": (n - i) for i in range(n)}
    _make_tree(tmp_path, {"bulk": files})
    with _patch_download_dir(tmp_path):
        seen = set()
        pages = (n + filemanager._ITEMS_PER_PAGE - 1) // filemanager._ITEMS_PER_PAGE
        for p in range(1, pages + 1):
            text, _ = filemanager._render_dir("bulk", p)
            for line in text.splitlines():
                if line and line[0].isdigit() and "📄" in line:
                    # Extract the filename
                    parts = line.split("📄")
                    if len(parts) > 1:
                        name = parts[1].split("—")[0].strip()
                        seen.add(name)
        assert len(seen) == n


# ── Nested navigation ──


def test_deep_nesting(tmp_path):
    _make_tree(tmp_path, {"Series": {"Show (2020)": {"Season 1": {"ep01.mkv": 50}}}})
    with _patch_download_dir(tmp_path):
        text, _ = filemanager._render_dir("Series", 1)
        assert "Show (2020)" in text

        text2, _ = filemanager._render_dir(os.path.join("Series", "Show (2020)"), 1)
        assert "Season 1" in text2

        text3, _ = filemanager._render_dir(os.path.join("Series", "Show (2020)", "Season 1"), 1)
        assert "ep01.mkv" in text3


# ── Safe edit helper ──


def test_safe_edit_is_async():
    """Verify _safe_edit is defined and callable."""
    assert callable(filemanager._safe_edit)


# ── Register function ──


def test_register_filemanager_callable():
    """Verify register_filemanager is exported and callable."""
    assert callable(filemanager.register_filemanager)


# ── Render root with many entries ──


def test_render_root_numbered_buttons(tmp_path):
    long_name = "A" * 40
    (tmp_path / long_name).mkdir()
    with _patch_download_dir(tmp_path):
        _text, buttons = filemanager._render_root()
        # Buttons should use compact numbered labels, not full names
        for row in buttons:
            for b in row:
                label = b.text if hasattr(b, "text") else ""
                assert len(label) <= 32


# ── Render dir with subdirs ──


def test_render_dir_with_subdirs(tmp_path):
    _make_tree(tmp_path, {"Root": {"SubA": {"x.txt": 50}, "SubB": {"y.txt": 30}}})
    with _patch_download_dir(tmp_path):
        text, buttons = filemanager._render_dir("Root", 1)
        assert "SubA" in text
        assert "SubB" in text
        # Nav row should have folder icons
        nav_data = [b.data.decode() for b in buttons[0]]
        assert all("f:n:" in d for d in nav_data)


# ── Render dir back button goes to root ──


def test_render_dir_back_to_root(tmp_path):
    _make_tree(tmp_path, {"Top": {"file.txt": 10}})
    with _patch_download_dir(tmp_path):
        _text, buttons = filemanager._render_dir("Top", 1)
        bottom_row = buttons[-1]
        back_btn = bottom_row[0]
        assert back_btn.data == b"f:r:1"


# ── Render file back to parent ──


def test_render_file_back_to_parent(tmp_path):
    _make_tree(tmp_path, {"Movies": {"video.mkv": 100}})
    with _patch_download_dir(tmp_path):
        _text, buttons = filemanager._render_file(os.path.join("Movies", "video.mkv"))
        back_data = [
            b.data.decode()
            for row in buttons
            for b in row
            if b"Back" in (b.text.encode() if hasattr(b, "text") else b"")
        ]
        assert all("f:n:" in d for d in back_data)


# ── Delete confirm for file includes yes/no ──


def test_delete_confirm_buttons_present(tmp_path):
    _make_tree(tmp_path, {"target.mkv": 200})
    with _patch_download_dir(tmp_path):
        _text, buttons = filemanager._render_delete_confirm("target.mkv")
        all_data = [b.data.decode() for row in buttons for b in row]
        assert any("f:y:" in d for d in all_data)
        assert any("f:x:" in d for d in all_data)


# ── Do delete returns False for missing ──


def test_do_delete_missing_returns_false(tmp_path):
    assert not filemanager._do_delete(str(tmp_path / "nonexistent"))


# ── Render root with mixed content ──


def test_render_root_mixed(tmp_path):
    _make_tree(tmp_path, {"Movies": {"a.mkv": 500}, "loose.mp4": 300})
    with _patch_download_dir(tmp_path):
        text, buttons = filemanager._render_root()
        assert "Movies" in text
        assert "loose.mp4" in text
        # At least nav buttons + refresh
        assert len(buttons) >= 2


# ── Root pagination ──


def test_render_root_pagination(tmp_path):
    """Root view paginates when entries exceed _ITEMS_PER_PAGE."""
    items = {f"folder_{i:02d}": {"file.mkv": 100 * (20 - i)} for i in range(20)}
    _make_tree(tmp_path, items)
    with _patch_download_dir(tmp_path):
        text_p1, _ = filemanager._render_root(1)
        assert "page 1/" in text_p1
        assert "20 items" in text_p1

        text_p2, _ = filemanager._render_root(2)
        assert "page 2/" in text_p2


def test_render_root_pagination_all_items(tmp_path):
    """All items appear exactly once across paginated root pages."""
    n = 13
    items = {f"dir_{i:02d}": {"f.dat": 100 - i} for i in range(n)}
    _make_tree(tmp_path, items)
    with _patch_download_dir(tmp_path):
        seen = set()
        pages = (n + filemanager._ITEMS_PER_PAGE - 1) // filemanager._ITEMS_PER_PAGE
        for p in range(1, pages + 1):
            text, _ = filemanager._render_root(p)
            for line in text.splitlines():
                if line and line[0].isdigit() and "📁" in line:
                    name = line.split("📁")[1].split("—")[0].strip()
                    seen.add(name)
        assert len(seen) == n


def test_render_root_page_clamping(tmp_path):
    _make_tree(tmp_path, {"one": {"f.txt": 10}})
    with _patch_download_dir(tmp_path):
        text, _ = filemanager._render_root(999)
        assert "1 item" in text


def test_render_root_message_fits_telegram_limit(tmp_path):
    """Even with many entries, a single page stays under 4096 chars."""
    items = {f"very_long_folder_name_{i:03d}": {"big_file.mkv": 1000} for i in range(50)}
    _make_tree(tmp_path, items)
    with _patch_download_dir(tmp_path):
        text, _ = filemanager._render_root(1)
        assert len(text) < 4096


# ── Edge case: single-item page ──


def test_single_item_no_pagination(tmp_path):
    _make_tree(tmp_path, {"D": {"only.txt": 10}})
    with _patch_download_dir(tmp_path):
        text, _buttons = filemanager._render_dir("D", 1)
        assert "page" not in text  # No pagination indicator for single page
