"""Global test fixtures for the Kodi Telegram Bot test suite."""

from __future__ import annotations

import pytest

import throttle


@pytest.fixture(autouse=True)
def _patch_throttle_for_tests(monkeypatch):
    """Bypass the publisher queue so tests don't need a running event loop task.

    Replaces ``_enqueue`` with a direct executor that calls the API function
    immediately, preserving error semantics but removing the need for
    ``start_publisher()`` in every test.  Tests that explicitly start the
    publisher (e.g. test_throttle.py) still work because the real ``_enqueue``
    is only called when the publisher is running and consuming the queue.
    """
    monkeypatch.setattr(throttle, "_TG_MIN_INTERVAL", 0)

    async def _direct_enqueue(priority: int, fn, *args, **kwargs):
        return await fn(*args, **kwargs)

    monkeypatch.setattr(throttle, "_enqueue", _direct_enqueue)
