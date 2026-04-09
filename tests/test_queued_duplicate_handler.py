import asyncio

from downloader.manager import _handle_queued_duplicate


class Msg:
    def __init__(self, mid):
        self.id = mid


class Ev:
    def __init__(self, sender_id, mid):
        self.sender_id = sender_id
        self.id = mid
        self.replies = []

    async def respond(self, text, reply_to=None, **_):  # pragma: no cover trivial stub
        self.replies.append((text, reply_to))
        await asyncio.sleep(0)
        return Msg(self.id + 100)


def test_handle_queued_duplicate_replies():
    dup = Ev(10, 2)
    asyncio.run(_handle_queued_duplicate(dup, "dup.bin"))
    assert any("Already queued" in t for t, _ in dup.replies)
    # Reply is threaded to the duplicate event's message id
    assert any(reply_to == 2 for _, reply_to in dup.replies)


def test_handle_queued_duplicate_different_user():
    dup = Ev(11, 3)
    asyncio.run(_handle_queued_duplicate(dup, "dup2.bin"))
    assert any("queued" in t.lower() for t, _ in dup.replies)
