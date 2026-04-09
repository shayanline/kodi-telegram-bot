import time

from downloader.progress import RateLimiter, _calc


def test_rate_limiter(monkeypatch):
    rl = RateLimiter(min_kodi=2.0)
    base = 1000.0
    times_k = [base, base + 1.0, base + 2.1]
    monkeypatch.setattr(time, "time", lambda: times_k.pop(0))
    assert rl.kodi_ok() is True
    assert rl.kodi_ok() is False
    assert rl.kodi_ok() is True


def test_calc_and_notify(monkeypatch):
    p, speed = _calc(500, 1000, 5)
    assert p == 50 and speed == "100.0 B"
    rl = RateLimiter(min_kodi=1)
    # kodi_ok should respect rate limiting
    assert rl.kodi_ok() is True
    # Rapid second call blocked by rate.kodi_ok timing (since min_kodi=1s)
    assert rl.kodi_ok() is False
