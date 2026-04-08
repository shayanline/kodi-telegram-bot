import asyncio
import os

import config
from downloader import manager
from downloader.state import DownloadState


class StubEvent:
    def __init__(self):
        self.messages = []
        self.id = 1

    async def respond(self, text, **_):  # pragma: no cover trivial
        self.messages.append(text)
        await asyncio.sleep(0)


def test_ensure_disk_space_paths(monkeypatch):
    ev = StubEvent()
    # Generous free space -> immediate True
    monkeypatch.setattr(manager.utils, "free_disk_mb", lambda p: 5000)
    monkeypatch.setattr(config, "MIN_FREE_DISK_MB", 200)
    assert asyncio.run(manager._ensure_disk_space(ev, "f.bin", 100 * 1024 * 1024)) is True


def test_ensure_disk_space_interactive_auto_accept(monkeypatch, tmp_path):
    # Activate TEST_AUTO_ACCEPT to bypass interaction for test determinism
    monkeypatch.setattr(manager, "TEST_AUTO_ACCEPT", True)
    # Create candidate old file
    f = tmp_path / "old.bin"
    f.write_bytes(b"0" * 1024)
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    # Free space function returns low values until deletion loops
    frees = [100, 400]  # after deletion pretend enough (projected 400-50=350 >= 300)
    monkeypatch.setattr(manager.utils, "free_disk_mb", lambda p: frees.pop(0) if frees else 500)
    monkeypatch.setattr(config, "MIN_FREE_DISK_MB", 300)
    ev = StubEvent()
    ok = asyncio.run(manager._ensure_disk_space(ev, "big.bin", 50 * 1024 * 1024, str(tmp_path / "big.bin")))
    assert ok is True


def test_ensure_disk_space_failure(monkeypatch):
    ev = StubEvent()
    frees = [100, 100, 100]  # still low after cleanup; extra for safety
    monkeypatch.setattr(manager.utils, "free_disk_mb", lambda p: frees.pop(0) if frees else 100)
    monkeypatch.setattr(config, "MIN_FREE_DISK_MB", 300)
    # Mock _select_deletion_candidate to return None (no deletion candidates found)
    monkeypatch.setattr(manager, "_select_deletion_candidate", lambda target_path, exclude: None)
    ok = asyncio.run(manager._ensure_disk_space(ev, "file.bin", 50 * 1024 * 1024))
    assert ok is False
    assert any("Not enough disk space" in m or "no deletable files found" in m for m in ev.messages)


def test_post_download_check_success(tmp_path, monkeypatch):
    # Create file meeting expected size threshold
    path = tmp_path / "ok.bin"
    expected = 1000
    path.write_bytes(b"0" * expected)
    st = DownloadState("ok.bin", str(path), expected)

    class M:
        async def edit(self, *_a, **_k):  # minimal stub used by test
            import asyncio

            await asyncio.sleep(0)
            return None

    msg = M()
    ok = asyncio.run(manager._post_download_check(True, expected, str(path), st, msg, "ok.bin"))
    assert ok is True


def test_post_download_check_cancel_cleanup(tmp_path, monkeypatch):
    path = tmp_path / "bad.bin"
    expected = 1000
    path.write_bytes(b"0" * 100)  # too small
    st = DownloadState("bad.bin", str(path), expected)
    st.cancelled = True
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))

    class M:
        last = None

        async def edit(self, txt, **_):  # capture edit text for assertion
            import asyncio

            self.last = txt
            await asyncio.sleep(0)

    msg = M()
    ok = asyncio.run(manager._post_download_check(False, expected, str(path), st, msg, "bad.bin"))
    assert ok is False and not os.path.exists(path)
    assert "cancelled" in msg.last.lower()
