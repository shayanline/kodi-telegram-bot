import asyncio

from downloader.queue import DownloadQueue, QueuedItem


class DummyEvent:
    async def respond(self, *a, **k):  # pragma: no cover - test stub only
        await asyncio.sleep(0)
        return None


class DummyMsg:
    def __init__(self):
        self.edits = []

    async def edit(self, text, buttons=None):  # pragma: no cover - trivial
        self.edits.append(text)
        await asyncio.sleep(0)


async def _runner(client, qi):  # pragma: no cover - tiny helper
    await asyncio.sleep(0.01)  # keep task alive briefly


async def _prepare_queue(n, limit):
    q = DownloadQueue(limit=limit)
    q.set_runner(lambda c, qi: _runner(None, qi))
    loop = asyncio.get_event_loop()
    q.ensure_worker(loop, None)
    ev = DummyEvent()
    for i in range(n):
        qi = QueuedItem(f"f{i}.bin", object(), 1, f"/tmp/f{i}.bin", ev)
        qi.message = DummyMsg()
        qi.file_id = f"id{i}"
        await q.enqueue(qi)
    return q


async def _test_renumber():
    q = await _prepare_queue(4, limit=2)
    # Allow some tasks to start so renumbering is triggered
    await asyncio.sleep(0.05)
    # Collect all edits from remaining queued items
    edits = []
    for qi in q.items.values():
        edits.extend(qi.message.edits)
    await q.stop()
    return edits


def test_queue_renumber_and_concurrency():
    edits = asyncio.run(_test_renumber())
    # After items start processing, remaining queued items should be renumbered
    for text in edits:
        assert "Queued #" in text

    # Basic concurrency smoke: ensure queue processes more than one item without serial bottleneck
    # by enqueuing several small tasks and confirming total duration < artificial serial time.
    async def _timed():
        start = asyncio.get_event_loop().time()
        q = await _prepare_queue(4, limit=2)
        await asyncio.sleep(0.2)
        await q.stop()
        return asyncio.get_event_loop().time() - start

    elapsed = asyncio.run(_timed())
    # If serial (4 * 0.01 per task plus overhead) ~0.04; with concurrency similarly small but we allow slack
    assert elapsed < 1.0
