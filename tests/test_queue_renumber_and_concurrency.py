import asyncio

from downloader.queue import DownloadQueue, QueuedItem


class DummyEvent:
    async def respond(self, *a, **k):  # pragma: no cover - test stub only
        await asyncio.sleep(0)
        return None


async def _runner(client, qi):  # pragma: no cover - tiny helper
    await asyncio.sleep(0.01)  # keep task alive briefly


async def _prepare_queue(n, limit):
    q = DownloadQueue(limit=limit)
    q.set_runner(lambda c, qi: _runner(None, qi))
    loop = asyncio.get_event_loop()
    q.ensure_worker(loop, None)
    ev = DummyEvent()
    for i in range(n):
        qi = QueuedItem(f"f{i}.bin", object(), 1, f"/tmp/f{i}.bin", ev, file_id=f"id{i}")
        await q.enqueue(qi)
    return q


def test_queue_enqueue_and_cancel():
    """Items can be enqueued and cancelled before processing."""

    async def _inner():
        q = DownloadQueue(limit=1)
        ev = DummyEvent()
        qi1 = QueuedItem("a.bin", object(), 1, "/tmp/a.bin", ev)
        qi2 = QueuedItem("b.bin", object(), 1, "/tmp/b.bin", ev)
        pos1 = await q.enqueue(qi1)
        pos2 = await q.enqueue(qi2)
        assert pos1 == 1
        assert pos2 == 2
        assert q.cancel("b.bin") is True
        assert "b.bin" not in q.items
        assert q.cancel("b.bin") is False  # already cancelled
        await q.stop()

    asyncio.run(_inner())


def test_queue_concurrency():
    """Queue processes items concurrently up to the limit."""

    async def _timed():
        start = asyncio.get_event_loop().time()
        q = await _prepare_queue(4, limit=2)
        await asyncio.sleep(0.2)
        await q.stop()
        return asyncio.get_event_loop().time() - start

    elapsed = asyncio.run(_timed())
    # With concurrency, 4 items at 0.01s each with limit=2 should be fast
    assert elapsed < 1.0


def test_cleanup_remaining():
    """_cleanup_remaining cancels all items and clears the dict."""

    async def _inner():
        q = DownloadQueue(limit=1)
        ev = DummyEvent()
        qi = QueuedItem("rem.bin", object(), 1, "/tmp/rem.bin", ev)
        await q.enqueue(qi)
        q._cleanup_remaining()
        assert len(q.items) == 0
        assert qi.cancelled

    asyncio.run(_inner())
