"""Coverage tests for utils.py -- is_media_file and remove_empty_parents edge cases."""

import os

from telethon.tl.types import (
    DocumentAttributeAudio,
    DocumentAttributeFilename,
    DocumentAttributeVideo,
)

import utils

# ── is_media_file (lines 34-45) ──


class _FakeDoc:
    """Minimal document mock."""

    def __init__(self, mime_type=None, attributes=None):
        self.mime_type = mime_type
        self.attributes = attributes or []


def test_is_media_video_mime():
    """Line 36-37: video/ mime_type returns True."""
    assert utils.is_media_file(_FakeDoc(mime_type="video/mp4")) is True


def test_is_media_audio_mime():
    """Line 36-37: audio/ mime_type returns True."""
    assert utils.is_media_file(_FakeDoc(mime_type="audio/mpeg")) is True


def test_is_media_video_attribute():
    """Line 39-40: DocumentAttributeVideo returns True."""
    attr = DocumentAttributeVideo(duration=0, w=0, h=0)
    assert utils.is_media_file(_FakeDoc(attributes=[attr])) is True


def test_is_media_audio_attribute():
    """Line 39-40: DocumentAttributeAudio returns True."""
    attr = DocumentAttributeAudio(duration=0)
    assert utils.is_media_file(_FakeDoc(attributes=[attr])) is True


def test_is_media_filename_video_ext():
    """Line 41-44: DocumentAttributeFilename with video extension returns True."""
    attr = DocumentAttributeFilename(file_name="movie.mkv")
    assert utils.is_media_file(_FakeDoc(attributes=[attr])) is True


def test_is_media_filename_audio_ext():
    """Line 41-44: DocumentAttributeFilename with audio extension returns True."""
    attr = DocumentAttributeFilename(file_name="song.mp3")
    assert utils.is_media_file(_FakeDoc(attributes=[attr])) is True


def test_is_media_filename_non_media_ext():
    """Lines 41-43 no match + line 45: non-media extension returns False."""
    attr = DocumentAttributeFilename(file_name="readme.pdf")
    assert utils.is_media_file(_FakeDoc(attributes=[attr])) is False


def test_is_media_no_media_attributes():
    """Line 45: non-media mime and no attributes returns False."""
    assert utils.is_media_file(_FakeDoc(mime_type="application/pdf")) is False


def test_is_media_bare_document():
    """Line 45: no mime and no attributes returns False."""
    assert utils.is_media_file(_FakeDoc()) is False


def test_is_media_none_mime():
    """Line 35: None mime_type is handled without error."""
    assert utils.is_media_file(_FakeDoc(mime_type=None)) is False


# ── remove_empty_parents edge cases ──


def test_remove_parents_cur_not_dir(tmp_path):
    """Line 61-62: break when parent directory does not exist."""
    fake_file = tmp_path / "nonexistent_dir" / "file.txt"
    removed = utils.remove_empty_parents(str(fake_file), [str(tmp_path)])
    assert removed == 0


def test_remove_parents_nonempty_dir(tmp_path):
    """Line 64-65: break when directory is not empty."""
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    (tmp_path / "a" / "keep.txt").write_text("keep")
    target = sub / "file.txt"
    target.write_bytes(b"x")
    target.unlink()
    removed = utils.remove_empty_parents(str(target), [str(tmp_path)])
    assert removed == 1
    assert not sub.exists()
    assert (tmp_path / "a").exists()


def test_remove_parents_listdir_oserror(monkeypatch, tmp_path):
    """Line 66-67: OSError from os.listdir triggers break."""
    sub = tmp_path / "a"
    sub.mkdir()
    target = sub / "file.txt"
    target.write_bytes(b"x")
    target.unlink()

    real_listdir = os.listdir

    def _boom(path):
        if os.path.abspath(path) == os.path.abspath(str(sub)):
            raise OSError("Permission denied")
        return real_listdir(path)

    monkeypatch.setattr(os, "listdir", _boom)
    removed = utils.remove_empty_parents(str(target), [str(tmp_path)])
    assert removed == 0


def test_remove_parents_rmdir_oserror(monkeypatch, tmp_path):
    """Line 71-72: OSError from os.rmdir triggers break."""
    sub = tmp_path / "a"
    sub.mkdir()
    target = sub / "file.txt"
    target.write_bytes(b"x")
    target.unlink()

    real_rmdir = os.rmdir

    def _boom(path):
        if os.path.abspath(path) == os.path.abspath(str(sub)):
            raise OSError("Cannot remove")
        return real_rmdir(path)

    monkeypatch.setattr(os, "rmdir", _boom)
    removed = utils.remove_empty_parents(str(target), [str(tmp_path)])
    assert removed == 0


def test_remove_parents_outer_exception(monkeypatch):
    """Line 74-75: outer except catches unexpected errors."""

    def _boom(p):
        raise TypeError("unexpected")

    monkeypatch.setattr(os.path, "dirname", _boom)
    removed = utils.remove_empty_parents("/some/path/file.txt", ["/tmp"])
    assert removed == 0
