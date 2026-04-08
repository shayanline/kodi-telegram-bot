"""Runtime configuration.

Reads environment variables once (via python-dotenv if present) and exposes
constants for the rest of the code. Keep this lean: only parsing + validation.
Required: TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_BOT_TOKEN.
"""

from __future__ import annotations

import os
import re

from dotenv import load_dotenv

# Allow tests to bypass .env to simulate open access scenarios.
if os.getenv("SKIP_DOTENV", "0") != "1":  # pragma: no cover simple branch
    load_dotenv()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


API_ID: int = _env_int("TELEGRAM_API_ID", 0)
API_HASH: str = os.getenv("TELEGRAM_API_HASH", "")
BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")

# Optional access control: comma/space separated list of allowed user IDs or usernames.
# Examples: "12345,@alice,bob". Usernames are case-insensitive and may include '@'.
# If empty -> bot is open to everyone.
_RAW_ALLOWED_USERS = os.getenv("ALLOWED_USERS", "").strip()


def _parse_allowed(raw: str) -> tuple[set[int], set[str]]:
    ids: set[int] = set()
    names: set[str] = set()
    if not raw:
        return ids, names
    for token in filter(None, re.split(r"[\s,]+", raw)):
        token = token.lstrip("@")
        if not token:
            continue
        if token.isdigit():
            try:
                ids.add(int(token))
            except ValueError:  # pragma: no cover - defensive
                continue
        else:
            names.add(token.lower())
    return ids, names


ALLOWED_USER_IDS, ALLOWED_USERNAMES = _parse_allowed(_RAW_ALLOWED_USERS)

KODI_URL: str = os.getenv("KODI_URL", "http://localhost:8080/jsonrpc")
KODI_USERNAME: str = os.getenv("KODI_USERNAME", "kodi")
KODI_PASSWORD: str = os.getenv("KODI_PASSWORD", "")
KODI_AUTH = (KODI_USERNAME, KODI_PASSWORD)
HEADERS = {"Content-Type": "application/json"}

_RAW_DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "~/Downloads")
DOWNLOAD_DIR = os.path.expanduser(os.path.expandvars(_RAW_DOWNLOAD_DIR))
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Media organization feature flag.
# Only toggle is ORGANIZE_MEDIA (defaults ON). Folder names are fixed constants
# to keep layout predictable and portable (not overridden by env vars).
ORGANIZE_MEDIA: bool = os.getenv("ORGANIZE_MEDIA", "1").lower() in {"1", "true", "yes", "on"}
MOVIES_DIR_NAME: str = "Movies"
SERIES_DIR_NAME: str = "Series"
OTHER_DIR_NAME: str = "Other"

MAX_RETRY_ATTEMPTS: int = _env_int("MAX_RETRY_ATTEMPTS", 3)
MAX_CONCURRENT_DOWNLOADS: int = _env_int("MAX_CONCURRENT_DOWNLOADS", 5)
MIN_FREE_DISK_MB: int = _env_int("MIN_FREE_DISK_MB", 200)
DISK_WARNING_MB: int = _env_int("DISK_WARNING_MB", 500)
MEMORY_WARNING_PERCENT: int = _env_int("MEMORY_WARNING_PERCENT", 90)


def validate() -> None:
    if API_ID == 0 or not API_HASH or not BOT_TOKEN:
        raise SystemExit("Missing TELEGRAM_API_ID / TELEGRAM_API_HASH / TELEGRAM_BOT_TOKEN")


def is_user_allowed(user_id: int | None, username: str | None) -> bool:
    """Return True if user is allowed based on configured allow-list.

    Open access (no restrictions) when both sets empty. Username check is
    case-insensitive. Prefer specifying numeric IDs to survive username changes.
    """
    if not ALLOWED_USER_IDS and not ALLOWED_USERNAMES:
        return True
    if user_id is not None and user_id in ALLOWED_USER_IDS:
        return True
    return bool(username and username.lower() in ALLOWED_USERNAMES)


__all__ = [
    "ALLOWED_USERNAMES",
    "ALLOWED_USER_IDS",
    "API_HASH",
    "API_ID",
    "BOT_TOKEN",
    "DISK_WARNING_MB",
    "DOWNLOAD_DIR",
    "HEADERS",
    "KODI_AUTH",
    "KODI_URL",
    "MAX_CONCURRENT_DOWNLOADS",
    "MAX_RETRY_ATTEMPTS",
    "MEMORY_WARNING_PERCENT",
    "MIN_FREE_DISK_MB",
    "MOVIES_DIR_NAME",
    "ORGANIZE_MEDIA",
    "OTHER_DIR_NAME",
    "SERIES_DIR_NAME",
    "is_user_allowed",
    "validate",
]
