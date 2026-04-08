import os

import config
import main
from downloader.state import DownloadState


def test_cleanup_partials(tmp_path, monkeypatch):
    # Setup temp directory as DOWNLOAD_DIR
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
    complete = tmp_path / "complete.bin"
    partial = tmp_path / "partial.bin"
    complete.write_bytes(b"0" * 1000)
    partial.write_bytes(b"0" * 100)
    # Snapshot states: one complete, one partial
    st_complete = DownloadState("complete.bin", str(complete), 1000)
    st_partial = DownloadState("partial.bin", str(partial), 1000)
    removed = main._cleanup_partials([st_complete, st_partial])
    assert removed >= 1
    assert os.path.exists(complete) and not os.path.exists(partial)
