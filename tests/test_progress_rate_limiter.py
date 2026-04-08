import time

import kodi
from downloader.progress import RateLimiter, _calc, _should_notify_kodi


def test_rate_limiter(monkeypatch):
    rl = RateLimiter(min_tg=1.0, min_kodi=2.0)
    base = 1000.0
    times = [base, base + 0.5, base + 1.1, base + 3.2]
    monkeypatch.setattr(time, "time", lambda: times.pop(0))
    assert rl.telegram_ok() is True  # first
    assert rl.telegram_ok() is False  # within 1s
    assert rl.telegram_ok() is True  # after 1s
    # kodi_ok separate timing
    times_k = [base, base + 1.0, base + 2.1]
    monkeypatch.setattr(time, "time", lambda: times_k.pop(0))
    assert rl.kodi_ok() is True
    assert rl.kodi_ok() is False
    assert rl.kodi_ok() is True


def test_calc_and_notify(monkeypatch):
    p, speed = _calc(500, 1000, 5)
    assert p == 50 and speed.endswith("B")
    monkeypatch.setattr(kodi, "is_playing", lambda: False)
    rl = RateLimiter(min_tg=0, min_kodi=1)
    # Trigger first call at 50% (not multiple of 10 except 50 yes) -> should notify
    assert _should_notify_kodi(50, rl) is True
    # Rapid second call same percent blocked by rate.kodi_ok timing (since min_kodi=1s)
    assert _should_notify_kodi(50, rl) is False
