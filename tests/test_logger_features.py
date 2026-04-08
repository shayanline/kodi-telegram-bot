import contextlib
import importlib
import os

import logger


def test_logger_lower_bound_and_idempotent(tmp_path, monkeypatch):
    log_file = tmp_path / "l.log"
    monkeypatch.setenv("LOG_FILE", str(log_file))
    # Intentionally misconfigure to 0 to trigger lower bound logic
    monkeypatch.setenv("LOG_MAX_MB", "0")
    importlib.reload(logger)
    lg = logger.get_logger()
    # Find truncating handler
    # Handler instance may belong to previous class object before reload; match by name
    handlers = [h for h in lg.handlers if h.__class__.__name__ == "TruncatingFileHandler"]
    assert handlers, "No truncating handler found"
    h = handlers[0]
    assert h.max_bytes >= 1024 * 1024  # coerced to >= 1MB
    # Call get_logger again: shouldn't duplicate handlers
    lg2 = logger.get_logger()
    handlers2 = [h for h in lg2.handlers if h.__class__.__name__ == "TruncatingFileHandler"]
    assert len(handlers2) == 1
    # Write a line to ensure file created (handler opens lazily on first emit)
    lg.info("test line")
    # Force stream flush/open if still delayed
    file_handler = None
    for _h in lg.handlers:
        if _h.__class__.__name__ == "TruncatingFileHandler":
            file_handler = _h
            with contextlib.suppress(Exception):  # pragma: no cover - defensive
                _h.flush()
    assert file_handler is not None
    handler_path = file_handler.baseFilename
    assert os.path.exists(handler_path)
