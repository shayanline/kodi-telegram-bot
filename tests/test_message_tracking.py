"""Tests for the message tracking system."""

from unittest.mock import AsyncMock

import pytest

from downloader.state import DownloadState, MessageTracker, MessageType, TrackedMessage


class MockMessage:
    """Mock Telegram message for testing."""

    def __init__(self, msg_id=1):
        self.id = msg_id
        self.edit = AsyncMock()
        self.delete = AsyncMock()
        self.raw_text = "Mock message"


def test_message_tracker_basic_operations():
    """Test basic message tracker operations."""
    tracker = MessageTracker()
    msg = MockMessage(1)

    # Register a message
    tracker.register_message("test_file.mp4", msg, MessageType.PROGRESS, 123)

    # Check it's registered
    messages = tracker.get_messages("test_file.mp4")
    assert len(messages) == 1
    assert messages[0].message_type == MessageType.PROGRESS
    assert messages[0].user_id == 123

    # Check filtered retrieval
    progress_msgs = tracker.get_messages("test_file.mp4", MessageType.PROGRESS)
    assert len(progress_msgs) == 1

    queue_msgs = tracker.get_messages("test_file.mp4", MessageType.QUEUE_LIST)
    assert len(queue_msgs) == 0

    # Cleanup
    tracker.cleanup_file("test_file.mp4")
    assert len(tracker.get_messages("test_file.mp4")) == 0


def test_message_tracker_multiple_messages():
    """Test tracking multiple messages for one file."""
    tracker = MessageTracker()

    progress_msg = MockMessage(1)
    list_msg = MockMessage(2)

    tracker.register_message("test_file.mp4", progress_msg, MessageType.PROGRESS, 123)
    tracker.register_message("test_file.mp4", list_msg, MessageType.DOWNLOAD_LIST, 123)

    all_messages = tracker.get_messages("test_file.mp4")
    assert len(all_messages) == 2

    progress_messages = tracker.get_messages("test_file.mp4", MessageType.PROGRESS)
    assert len(progress_messages) == 1

    list_messages = tracker.get_all_list_messages()
    assert len(list_messages) == 1
    assert list_messages[0].message_type == MessageType.DOWNLOAD_LIST


def test_download_state_progress_tracking():
    """Test DownloadState progress tracking features."""
    state = DownloadState("test_file.mp4", "/tmp/test_file.mp4", 1000)

    # Test initial values
    assert state.downloaded_bytes == 0
    assert state.progress_percent == 0
    assert state.speed == "0 B/s"
    assert state.get_progress_text() == ""

    # Test progress update
    state.update_progress(500, 50, "1 MB/s")
    assert state.downloaded_bytes == 500
    assert state.progress_percent == 50
    assert state.speed == "1 MB/s"

    # Test progress text
    progress_text = state.get_progress_text()
    assert "50%" in progress_text
    assert "500 B" in progress_text or "500.0 B" in progress_text
    assert "1 MB/s" in progress_text

    # Test paused state
    state.mark_paused()
    paused_text = state.get_progress_text()
    assert "⏸️ Paused" in paused_text
    assert "50%" in paused_text


def test_message_tracker_cleanup():
    """Test message tracker cleanup functionality."""
    tracker = MessageTracker()

    msg1 = MockMessage(1)
    msg2 = MockMessage(2)

    tracker.register_message("test_file.mp4", msg1, MessageType.PROGRESS, 123)
    tracker.register_message("test_file.mp4", msg2, MessageType.DOWNLOAD_LIST, 123)

    assert len(tracker.get_messages("test_file.mp4")) == 2

    tracker.cleanup_file("test_file.mp4")

    assert len(tracker.get_messages("test_file.mp4")) == 0


def test_message_tracker_filtering():
    """Test message tracker filtering by type."""
    tracker = MessageTracker()

    # Register different types of messages
    msg1 = MockMessage(1)
    msg2 = MockMessage(2)
    msg3 = MockMessage(3)

    tracker.register_message("test_file.mp4", msg1, MessageType.PROGRESS, 123)
    tracker.register_message("test_file.mp4", msg2, MessageType.DOWNLOAD_LIST, 123)
    tracker.register_message("other_file.mp4", msg3, MessageType.QUEUE_LIST, 456)

    # Test filtering by type
    progress_msgs = tracker.get_messages("test_file.mp4", MessageType.PROGRESS)
    assert len(progress_msgs) == 1
    assert progress_msgs[0].message.id == 1

    download_list_msgs = tracker.get_messages("test_file.mp4", MessageType.DOWNLOAD_LIST)
    assert len(download_list_msgs) == 1
    assert download_list_msgs[0].message.id == 2

    # Test get all list messages
    all_lists = tracker.get_all_list_messages()
    assert len(all_lists) == 2
    list_types = {msg.message_type for msg in all_lists}
    assert MessageType.DOWNLOAD_LIST in list_types
    assert MessageType.QUEUE_LIST in list_types


def test_tracked_message_basic_functionality():
    """Test TrackedMessage basic functionality."""
    msg = MockMessage(1)
    tracked = TrackedMessage(msg, MessageType.PROGRESS, 123)

    assert tracked.message == msg
    assert tracked.message_type == MessageType.PROGRESS
    assert tracked.user_id == 123


def test_download_state_basic_functionality():
    """Test DownloadState basic functionality."""
    state = DownloadState("test_file.mp4", "/tmp/test_file.mp4", 1000)

    # Test initial state
    assert not state.paused
    assert not state.cancelled
    assert not state.completed
    assert state.filename == "test_file.mp4"
    assert state.path == "/tmp/test_file.mp4"
    assert state.size == 1000

    # Test pause
    state.mark_paused()
    assert state.paused
    assert not state.cancelled

    # Test resume
    state.mark_resumed()
    assert not state.paused

    # Test cancel
    state.mark_cancelled()
    assert state.cancelled
    # Should not be able to pause when cancelled
    state.mark_paused()
    assert not state.paused  # Should remain False since cancelled


if __name__ == "__main__":
    pytest.main([__file__])
