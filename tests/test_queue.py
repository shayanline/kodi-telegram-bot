import asyncio

from downloader.queue import DownloadQueue, QueuedItem


class DummyEvent:
    def __init__(self):
        self.responded = []

    async def respond(self, text, **_):
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


# ── stop() timeout cancels worker ──


def test_stop_timeout_cancels_worker():
    """Queue with a runner that hangs forever: stop() should not crash."""

    async def _inner():
        q = DownloadQueue(limit=1)

        async def hanging_runner(client, qi):
            await asyncio.sleep(999)

        q.set_runner(hanging_runner)
        loop = asyncio.get_event_loop()
        q.ensure_worker(loop, None)
        ev = DummyEvent()
        qi = QueuedItem("hang.bin", object(), 10, "/tmp/hang.bin", ev)
        await q.enqueue(qi)
        await asyncio.sleep(0.05)
        await q.stop()

    asyncio.run(_inner())


# ── _process_item cancelled between dequeue and semaphore ──


def test_process_item_cancelled_before_processing():
    """Enqueue item, cancel it before processing, verify it's skipped."""

    async def _inner():
        q = DownloadQueue(limit=1)
        ran = {"called": False}

        async def track_runner(client, qi):
            ran["called"] = True

        q.set_runner(track_runner)
        ev = DummyEvent()
        qi = QueuedItem("cancel_me.bin", object(), 10, "/tmp/cancel_me.bin", ev)
        await q.enqueue(qi)
        q.cancel("cancel_me.bin")
        await q._process_item(None, "cancel_me.bin")
        assert ran["called"] is False

    asyncio.run(_inner())


# ── _process_item runner exception ──


def test_process_item_runner_exception():
    """Runner that raises sends error message."""

    async def _inner():
        q = DownloadQueue(limit=1)

        async def failing_runner(client, qi):
            raise RuntimeError("boom")

        q.set_runner(failing_runner)
        ev = DummyEvent()
        qi = QueuedItem("fail.bin", object(), 10, "/tmp/fail.bin", ev)
        await q.enqueue(qi)
        await q._process_item(None, "fail.bin")
        assert any("Failed" in msg for msg in ev.responded)

    asyncio.run(_inner())


# Basic smoke test executed when run directly
if __name__ == "__main__":  # pragma: no cover
    test_queue_basic()
