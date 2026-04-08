import os

from logger import get_logger


def test_logger_truncates(tmp_path, monkeypatch):
    # Configure env for small max size (50 KB) to force truncation
    log_file = tmp_path / "test.log"
    monkeypatch.setenv("LOG_FILE", str(log_file))
    monkeypatch.setenv("LOG_MAX_MB", "1")  # 1 MB cap for safety (won't hit) but we'll simulate

    logger = get_logger()

    # Write lines until size threshold for artificial small cap using direct handler
    # Replace handler with a much smaller cap (1KB) for the test
    from logger import TruncatingFileHandler

    # Remove existing file handlers
    for h in logger.handlers:
        if isinstance(h, TruncatingFileHandler):
            logger.removeHandler(h)
    handler = TruncatingFileHandler(str(log_file), max_bytes=1024)  # 1KB
    logger.addHandler(handler)

    for i in range(200):
        logger.info("line %s %s", i, "x" * 30)

    size = os.path.getsize(log_file)
    assert size <= 1024
    # Ensure header marker present after truncation
    with open(log_file) as f:
        content = f.read()
    assert "log truncated" in content
