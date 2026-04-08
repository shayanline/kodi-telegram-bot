import asyncio

from downloader.queue import DownloadQueue, QueuedItem


class DummyEvent:
    async def respond(self, *a, **k):  # pragma: no cover - placeholder
        pass


async def _bulk_enqueue(n):
    q = DownloadQueue(limit=1)
    # No worker started so items stay queued
    ev = DummyEvent()
    positions = []

    async def enqueue_one(i):
        qi = QueuedItem(f"file{i}.bin", object(), 1, f"/tmp/file{i}.bin", ev)
        pos = await q.enqueue(qi)
        positions.append(pos)

    await asyncio.gather(*(enqueue_one(i) for i in range(n)))
    return positions


def test_bulk_queue_unique_positions():
    positions = asyncio.run(_bulk_enqueue(10))
    # Positions should form a proper 1..n set
    assert sorted(positions) == list(range(1, 11))
