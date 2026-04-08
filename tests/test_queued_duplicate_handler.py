import asyncio
from unittest.mock import patch

from downloader.manager import _handle_queued_duplicate
from downloader.queue import QueuedItem


class Ev:
    def __init__(self, sender_id, mid):
        self.sender_id = sender_id
        self.id = mid
        self.replies = []
        self.response_message = MockMessage(mid)

    async def respond(self, text, reply_to=None, **_):  # pragma: no cover trivial stub
        self.replies.append((text, reply_to))
        await asyncio.sleep(0)
        return self.response_message

    async def get_sender(self):  # pragma: no cover trivial stub
        # Return a simple object with id attribute
        await asyncio.sleep(0)  # Make it properly async

        class MockSender:
            def __init__(self, sender_id):
                self.id = sender_id

        return MockSender(self.sender_id)


class MockMessage:
    def __init__(self, mid):
        self.id = mid


def test_handle_queued_duplicate_same_user():
    original = Ev(10, 1)
    qi = QueuedItem("dup.bin", object(), 10, "/tmp/dup.bin", original)
    qi.message = MockMessage(50)  # Provide a message so "Already queued" path is exercised
    dup = Ev(10, 2)  # same sender

    # Mock the message tracker to avoid issues with telethon Message types
    with patch("downloader.manager.message_tracker.register_message"):
        asyncio.run(_handle_queued_duplicate(dup, qi, qi.filename))

    assert any("Already queued" in t for t, _ in dup.replies)
    # reply_to should point to the queued message
    assert any(reply_to == 50 for _, reply_to in dup.replies)
    assert qi.watcher_events is None  # still no watcher for same user


def test_handle_queued_duplicate_different_user():
    original = Ev(10, 1)
    qi = QueuedItem("dup2.bin", object(), 10, "/tmp/dup2.bin", original)
    qi.message = MockMessage(51)  # Provide a message so "Already queued" path is exercised
    dup = Ev(11, 3)

    # Mock the message tracker to avoid issues with telethon Message types
    with patch("downloader.manager.message_tracker.register_message"):
        asyncio.run(_handle_queued_duplicate(dup, qi, qi.filename))

    assert any("queued" in t.lower() for t, _ in dup.replies)
    assert qi.watcher_events and len(qi.watcher_events) == 1
    assert qi.watcher_events[0] == dup
