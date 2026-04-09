import contextlib
import importlib
import logging
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


# ── _env_int ValueError ──


def test_env_int_invalid_value(monkeypatch):
    """Non-integer env var falls back to default."""
    monkeypatch.setenv("LOG_MAX_MB", "abc")
    result = logger._env_int("LOG_MAX_MB", 200)
    assert result == 200


# ── emit OSError on getsize ──


def test_emit_oserror_on_getsize(tmp_path):
    """Handler with non-existent baseFilename initially: current_size falls to 0."""
    handler = logger.TruncatingFileHandler(
        str(tmp_path / "nonexistent_dir" / "test.log"),
        max_bytes=10 * 1024 * 1024,
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    # Manually set baseFilename to a path where getsize will fail
    handler.baseFilename = str(tmp_path / "no_such_file.log")
    # Open a real writable stream so the write succeeds
    real_log = tmp_path / "real.log"
    handler.stream = open(str(real_log), "w", encoding="utf-8")  # noqa: SIM115
    record = logging.LogRecord("test", logging.INFO, "", 0, "hello", (), None)
    handler.emit(record)  # should not crash; current_size falls to 0
    handler.stream.close()
    assert real_log.read_text().strip() == "hello"


# ── emit format error ──


def test_emit_format_error(tmp_path):
    """When format() raises, handleError is called (no crash)."""
    log_file = tmp_path / "err.log"
    handler = logger.TruncatingFileHandler(str(log_file), max_bytes=10 * 1024 * 1024)
    handler.setFormatter(logging.Formatter("%(message)s"))

    handled = []

    def _track_handle_error(record):
        handled.append(record)

    handler.handleError = _track_handle_error
    handler.format = lambda record: (_ for _ in ()).throw(ValueError("bad format"))

    record = logging.LogRecord("test", logging.INFO, "", 0, "hello", (), None)
    handler.emit(record)  # should not crash
    assert len(handled) == 1
