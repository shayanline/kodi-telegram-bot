import asyncio

from downloader.queue import DownloadQueue, QueuedItem


class DummyEvent:
    def __init__(self):
        self.responded = []

    def respond(self, text, **_):  # pragma: no cover simple stub
        self.responded.append(text)


async def dummy_runner(client, qi):
    await asyncio.sleep(0)  # simulate async boundary


def test_queue_basic():  # simple smoke test
    async def _inner():
        q = DownloadQueue(limit=1)
        q.set_runner(lambda c, qi: dummy_runner(None, qi))
        loop = asyncio.get_event_loop()
        q.ensure_worker(loop, None)
        ev = DummyEvent()
        qi = QueuedItem("file.bin", object(), 10, "/tmp/file.bin", ev)
        await q.enqueue(qi)
        await asyncio.sleep(0.05)
        assert "file.bin" not in q.items  # processed
        await q.stop()

    asyncio.run(_inner())


# Basic smoke test executed when run directly
if __name__ == "__main__":  # pragma: no cover
    test_queue_basic()
