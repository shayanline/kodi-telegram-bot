"""Tests for download state and per-chat download list tracking."""

from unittest.mock import AsyncMock

import pytest

from downloader.state import ChatDownloadList, DownloadState, chat_lists


class MockMessage:
    """Mock Telegram message for testing."""

    def __init__(self, msg_id=1):
        self.id = msg_id
        self.edit = AsyncMock()
        self.delete = AsyncMock()
        self.raw_text = "Mock message"


# ── DownloadState tests ──


def test_download_state_creation_defaults():
    """Test DownloadState is created with correct defaults."""
    state = DownloadState("test_file.mp4", "/tmp/test_file.mp4", 1000)

    assert state.filename == "test_file.mp4"
    assert state.path == "/tmp/test_file.mp4"
    assert state.size == 1000
    assert state.original_event is None
    assert not state.paused
    assert not state.cancelled
    assert not state.completed
    assert not state.waiting_for_space
    assert state.downloaded_bytes == 0
    assert state.progress_percent == 0
    assert state.speed == "0 B/s"


def test_download_state_update_progress():
    """Test DownloadState.update_progress sets fields correctly."""
    state = DownloadState("file.mp4", "/tmp/file.mp4", 2000)

    state.update_progress(500, 25, "1 MB/s")
    assert state.downloaded_bytes == 500
    assert state.progress_percent == 25
    assert state.speed == "1 MB/s"

    # Update again with new values
    state.update_progress(1500, 75, "2.5 MB/s")
    assert state.downloaded_bytes == 1500
    assert state.progress_percent == 75
    assert state.speed == "2.5 MB/s"


def test_download_state_mark_paused_and_resumed():
    """Test pausing and resuming a download."""
    state = DownloadState("file.mp4", "/tmp/file.mp4", 1000)

    assert not state.paused

    state.mark_paused()
    assert state.paused

    state.mark_resumed()
    assert not state.paused


def test_download_state_mark_cancelled():
    """Test cancelling a download."""
    state = DownloadState("file.mp4", "/tmp/file.mp4", 1000)

    state.mark_cancelled()
    assert state.cancelled


def test_download_state_mark_completed():
    """Test completing a download clears paused flag."""
    state = DownloadState("file.mp4", "/tmp/file.mp4", 1000)

    state.mark_paused()
    assert state.paused

    state.mark_completed()
    assert state.completed
    assert not state.paused


def test_download_state_cancelled_blocks_pause():
    """Pausing should be ignored once the download is cancelled."""
    state = DownloadState("file.mp4", "/tmp/file.mp4", 1000)

    state.mark_cancelled()
    assert state.cancelled

    state.mark_paused()
    assert not state.paused  # pause must not take effect


def test_download_state_cancelled_blocks_resume():
    """Resuming should be ignored once the download is cancelled."""
    state = DownloadState("file.mp4", "/tmp/file.mp4", 1000)

    state.mark_paused()
    state.mark_cancelled()

    state.mark_resumed()
    assert state.paused  # resume must not take effect


def test_download_state_cancelled_blocks_completed():
    """Completing should be ignored once the download is cancelled."""
    state = DownloadState("file.mp4", "/tmp/file.mp4", 1000)

    state.mark_cancelled()

    state.mark_completed()
    assert not state.completed  # complete must not take effect


# ── ChatDownloadList tests ──


def test_chat_download_list_defaults():
    """Test ChatDownloadList is created with correct defaults."""
    cdl = ChatDownloadList(chat_id=12345)

    assert cdl.chat_id == 12345
    assert cdl.message is None
    assert cdl.page == 0
    assert cdl.confirming is None


def test_chat_download_list_custom_values():
    """Test ChatDownloadList with explicit field values."""
    msg = MockMessage(42)
    cdl = ChatDownloadList(chat_id=99, message=msg, page=3, confirming="abc123")

    assert cdl.chat_id == 99
    assert cdl.message is msg
    assert cdl.message.id == 42
    assert cdl.page == 3
    assert cdl.confirming == "abc123"


# ── chat_lists global dict tests ──


def test_chat_lists_add_and_get():
    """Test adding to and reading from the global chat_lists dict."""
    try:
        cdl = ChatDownloadList(chat_id=111)
        chat_lists[111] = cdl

        assert 111 in chat_lists
        assert chat_lists[111] is cdl
        assert chat_lists[111].chat_id == 111
    finally:
        chat_lists.pop(111, None)


def test_chat_lists_remove():
    """Test removing an entry from the global chat_lists dict."""
    try:
        chat_lists[222] = ChatDownloadList(chat_id=222)
        assert 222 in chat_lists

        del chat_lists[222]
        assert 222 not in chat_lists
    finally:
        chat_lists.pop(222, None)


def test_chat_lists_multiple_chats():
    """Test managing multiple chat entries simultaneously."""
    try:
        chat_lists[1] = ChatDownloadList(chat_id=1, page=0)
        chat_lists[2] = ChatDownloadList(chat_id=2, page=5)
        chat_lists[3] = ChatDownloadList(chat_id=3, confirming="x")

        assert len({k for k in chat_lists if k in (1, 2, 3)}) == 3
        assert chat_lists[2].page == 5
        assert chat_lists[3].confirming == "x"

        del chat_lists[2]
        assert 2 not in chat_lists
        assert 1 in chat_lists
        assert 3 in chat_lists
    finally:
        chat_lists.pop(1, None)
        chat_lists.pop(2, None)
        chat_lists.pop(3, None)


def test_chat_lists_update_existing():
    """Test updating fields of an existing ChatDownloadList entry."""
    try:
        msg = MockMessage(7)
        chat_lists[500] = ChatDownloadList(chat_id=500)

        # Mutate in place
        chat_lists[500].message = msg
        chat_lists[500].page = 2
        chat_lists[500].confirming = "file_abc"

        assert chat_lists[500].message.id == 7
        assert chat_lists[500].page == 2
        assert chat_lists[500].confirming == "file_abc"
    finally:
        chat_lists.pop(500, None)


if __name__ == "__main__":
    pytest.main([__file__])
