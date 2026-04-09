import os
import tempfile

from downloader.manager import validate_size


def test_validate_size_thresholds():
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "file.bin")
        expected = 1000
        with open(path, "wb") as f:
            f.write(b"0" * int(expected * 0.979))  # just below 98%
        assert not validate_size(expected, path)
        with open(path, "ab") as f:
            f.write(b"0" * 10_000)  # push over threshold
        assert validate_size(expected, path)
