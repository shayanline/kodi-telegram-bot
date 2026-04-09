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
        return file


class NoneClient:
    """download_media that returns None (silent Telethon failure)."""

    async def download_media(self, document, file, progress_callback=None):
        return None


class GenericErrorClient:
    """download_media that raises a non-timeout error N times then succeeds."""

    def __init__(self, fail_times: int):
        self.calls = 0
        self.fail_times = fail_times

    async def download_media(self, document, file, progress_callback=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("Connection reset")
        return file


class CapturingClient:
    """Records what media arg was passed to download_media."""

    def __init__(self):
        self.media_arg = None

    async def download_media(self, document, file, progress_callback=None):
        self.media_arg = document
        return file


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


def test_download_media_none_returns_false():
    """download_media returning None (silent Telethon failure) should fail."""

    async def run():
        client = NoneClient()
        state = DownloadState("file.bin", "/tmp/file.bin", 100)
        msg = FakeMessage()

        async def progress(*_a, **_k):  # pragma: no cover
            pass

        return await download_with_retries(client, object(), "/tmp/file.bin", progress, msg, state)

    ok = asyncio.run(run())
    assert ok is False


def test_source_message_preferred_over_document():
    """When source_message is given, it should be passed to download_media."""

    async def run():
        client = CapturingClient()
        state = DownloadState("file.bin", "/tmp/file.bin", 100)
        msg = FakeMessage()
        sentinel_doc = object()
        sentinel_msg = object()

        async def progress(*_a, **_k):  # pragma: no cover
            pass

        await download_with_retries(
            client, sentinel_doc, "/tmp/file.bin", progress, msg, state, source_message=sentinel_msg
        )
        return client.media_arg

    media = asyncio.run(run())
    assert media is not None
    # source_message should be used, not document
    assert media.__class__.__name__ == "object"


def test_source_message_none_falls_back_to_document():
    """Without source_message, download_media should receive the document."""

    async def run():
        client = CapturingClient()
        state = DownloadState("file.bin", "/tmp/file.bin", 100)
        msg = FakeMessage()
        sentinel_doc = object()

        async def progress(*_a, **_k):  # pragma: no cover
            pass

        await download_with_retries(client, sentinel_doc, "/tmp/file.bin", progress, msg, state)
        return client.media_arg, sentinel_doc

    media, doc = asyncio.run(run())
    assert media is doc


def test_generic_error_retries_edit_message():
    """Non-timeout errors should show retry info to the user."""

    async def run():
        orig = config.MAX_RETRY_ATTEMPTS
        config.MAX_RETRY_ATTEMPTS = 2
        try:
            client = GenericErrorClient(fail_times=1)
            state = DownloadState("file.bin", "/tmp/file.bin", 100)
            msg = FakeMessage()

            async def progress(*_a, **_k):  # pragma: no cover
                pass

            ok = await download_with_retries(client, object(), "/tmp/file.bin", progress, msg, state)
            return ok, msg.edits, client.calls
        finally:
            config.MAX_RETRY_ATTEMPTS = orig

    ok, edits, calls = asyncio.run(run())
    assert ok is True
    assert calls == 2
    assert any("retrying" in e.lower() for e in edits)


def test_generic_error_all_retries_fail():
    """All generic-error retries exhausted should return False."""

    async def run():
        orig = config.MAX_RETRY_ATTEMPTS
        config.MAX_RETRY_ATTEMPTS = 1
        try:
            client = GenericErrorClient(fail_times=10)
            state = DownloadState("file.bin", "/tmp/file.bin", 100)
            msg = FakeMessage()

            async def progress(*_a, **_k):  # pragma: no cover
                pass

            ok = await download_with_retries(client, object(), "/tmp/file.bin", progress, msg, state)
            return ok, client.calls
        finally:
            config.MAX_RETRY_ATTEMPTS = orig

    ok, calls = asyncio.run(run())
    assert ok is False
    assert calls == 2  # initial + 1 retry
