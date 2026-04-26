"""TMDB API helper for resolving TV show IDs.

Used to write accurate tvshow.nfo files so Kodi correctly identifies
each show folder instead of merging unrelated series together.
"""

from __future__ import annotations

import asyncio

import requests

import config
from logger import log

_SEARCH_URL = "https://api.themoviedb.org/3/search/tv"
_TIMEOUT = 5


def _search_sync(title: str, year: int | None) -> int | None:
    """Blocking TMDB search — returns the best-match TMDB ID or None."""
    if not config.TMDB_API_KEY:
        return None
    params: dict[str, str | int] = {"api_key": config.TMDB_API_KEY, "query": title}
    if year:
        params["first_air_date_year"] = year
    try:
        r = requests.get(_SEARCH_URL, params=params, timeout=_TIMEOUT)
        if r.status_code != 200:
            log.warning("TMDB search non-200 (%s) for %r", r.status_code, title)
            return None
        results = r.json().get("results")
        if not results:
            return None
        return int(results[0]["id"])
    except Exception as e:
        log.warning("TMDB search error for %r: %s", title, e)
        return None


async def search_show(title: str, year: int | None = None) -> int | None:
    """Async wrapper — runs the blocking search in a thread."""
    return await asyncio.to_thread(_search_sync, title, year)


__all__ = ["search_show"]
