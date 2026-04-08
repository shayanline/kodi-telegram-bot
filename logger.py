"""Project-wide logger with hard size cap (default 200MB).

Environment variables:
  LOG_FILE     Path to log file (default: bot.log)
  LOG_LEVEL    Logging level (default: INFO)
  LOG_MAX_MB   Max size in megabytes before truncation (default: 200)

The handler ensures the log file never exceeds the configured max size. When
an incoming record would overflow the file, the file is truncated in-place and
an informational header line is written, then logging continues. Only a single
log file is maintained (no multiple rotations) to satisfy the strict size cap
requirement.
"""

from __future__ import annotations

import contextlib
import logging
import os
from datetime import UTC, datetime

__all__ = ["get_logger", "log"]


class TruncatingFileHandler(logging.FileHandler):
    """File handler that truncates the file when size limit would be exceeded.

    This avoids creating rotated copies so the on-disk footprint stays bounded
    by ``max_bytes``. It formats the record first so we can size-check precisely.
    """

    def __init__(self, filename: str, max_bytes: int, encoding: str | None = "utf-8"):
        # Use append mode so existing logs preserved until first overflow.
        super().__init__(filename, mode="a", encoding=encoding, delay=True)
        self.max_bytes = max_bytes

    def _ensure_stream(self):
        if self.stream is None:
            self.stream = self._open()
        with contextlib.suppress(Exception):  # pragma: no cover
            self.stream.flush()

    def _truncate_and_header(self, current_size: int):
        with contextlib.suppress(Exception):  # pragma: no cover
            if self.stream:
                self.stream.close()
        self.stream = open(self.baseFilename, "w", encoding=self.encoding or "utf-8")  # noqa: SIM115
        header = f"--- log truncated at {datetime.now(UTC).isoformat()} (previous size {current_size} bytes) ---"
        self.stream.write(header + "\n")

    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
            self._ensure_stream()
            try:
                current_size = os.path.getsize(self.baseFilename)
            except OSError:
                current_size = 0
            if current_size + len(msg) + 1 > self.max_bytes:
                self._truncate_and_header(current_size)
            self.stream.write(msg + "\n")
            with contextlib.suppress(Exception):  # pragma: no cover
                self.stream.flush()
        except Exception:
            self.handleError(record)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def get_logger() -> logging.Logger:
    logger = logging.getLogger("kodi_telegram_bot")
    if logger.handlers:  # Already configured (maybe from previous import)
        # If reloaded module created a *new* TruncatingFileHandler class, existing
        # handler instances won't pass isinstance checks. Ensure at least one
        # handler named TruncatingFileHandler is present; if not, add a fresh one.
        has_trunc = any(h.__class__.__name__ == "TruncatingFileHandler" for h in logger.handlers)
        if has_trunc:
            return logger
        # Fall through to (re)configure a file handler while preserving existing stderr handlers.

    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    log_file = os.getenv("LOG_FILE", "bot.log")
    max_mb = _env_int("LOG_MAX_MB", 200)
    # Hard lower bound for safety in case of misconfiguration
    if max_mb < 1:
        max_mb = 1
    max_bytes = max_mb * 1024 * 1024

    handler = TruncatingFileHandler(log_file, max_bytes=max_bytes)
    fmt = "%(asctime)s %(levelname).1s %(name)s:%(lineno)d | %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    handler.setFormatter(logging.Formatter(fmt, datefmt=datefmt))

    logger.setLevel(level)
    logger.addHandler(handler)
    logger.propagate = False

    # Add stderr handler only if one not already present (avoid duplicates on reload)
    if not any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, TruncatingFileHandler) for h in logger.handlers
    ):
        stderr_handler = logging.StreamHandler()
        stderr_handler.setLevel(level)
        stderr_handler.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
        logger.addHandler(stderr_handler)

    logger.debug("Logger initialized (file=%s, max_mb=%s, level=%s)", log_file, max_mb, level_name)
    return logger


# Eagerly create shared logger instance
log = get_logger()
