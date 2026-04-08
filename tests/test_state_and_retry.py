import asyncio

import config
from downloader.manager import download_with_retries
from downloader.state import DownloadState


class FakeMessage:
    def __init__(self):
        self.edits = []

    async def edit(self, text, **_):  # pragma: no cover
        self.edits.append(text)
        await asyncio.sleep(0)


class FlakyClient:
    def __init__(self, fail_times: int):
        self.calls = 0
        self.fail_times = fail_times

    async def download_media(self, document, file, progress_callback=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise TimeoutError()
        await asyncio.sleep(0)


async def _retry_scenario(fail_times: int, max_attempts: int):
    orig = config.MAX_RETRY_ATTEMPTS
    config.MAX_RETRY_ATTEMPTS = max_attempts
    try:
        client = FlakyClient(fail_times)
        state = DownloadState("file.bin", "/tmp/file.bin", 100)
        msg = FakeMessage()

        async def progress(*_a, **_k):  # pragma: no cover
            await asyncio.sleep(0)

        ok = await download_with_retries(client, object(), "/tmp/file.bin", progress, msg, state)
        return ok, client.calls
    finally:
        config.MAX_RETRY_ATTEMPTS = orig


def test_download_state_transitions():
    st = DownloadState("a.bin", "/tmp/a.bin", 123)
    assert not st.paused and not st.cancelled
    st.mark_paused()
    assert st.paused
    st.mark_resumed()
    assert not st.paused
    st.mark_cancelled()
    assert st.cancelled
    st.mark_resumed()
    assert st.cancelled


def test_retry_logic_success_after_retries():
    ok, calls = asyncio.run(_retry_scenario(fail_times=2, max_attempts=3))
    assert ok is True and calls == 3


def test_retry_logic_failure_when_exceeding():
    ok, calls = asyncio.run(_retry_scenario(fail_times=5, max_attempts=3))
    assert ok is False and calls == 4
