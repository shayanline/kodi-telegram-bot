import asyncio

from downloader.manager import _handle_active_duplicate


class Msg:
    def __init__(self):
        self.id = 111


class Ev:
    def __init__(self, mid, sender_id=None):
        self.id = mid
        self.sender_id = sender_id
        self.replies = []

    async def respond(self, text, reply_to=None, **_):  # pragma: no cover - test stub
        self.replies.append((text, reply_to))
        await asyncio.sleep(0)
        return Msg()


async def _run():
    dup_event = Ev(99, sender_id=42)
    await _handle_active_duplicate(dup_event, "abc.bin")
    return dup_event.replies


def test_active_duplicate_replies_to_new_message():
    replies = asyncio.run(_run())
    assert replies, "No reply captured"
    text, reply_to = replies[0]
    assert "Already downloading" in text
    # Reply is threaded to the new event's message id
    assert reply_to == 99
