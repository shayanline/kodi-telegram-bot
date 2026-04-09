import importlib

import pytest

import config


def _reload(monkeypatch, api_id="0", api_hash="", bot_token=""):
    monkeypatch.setenv("SKIP_DOTENV", "1")
    monkeypatch.setenv("TELEGRAM_API_ID", api_id)
    if api_hash is not None:
        monkeypatch.setenv("TELEGRAM_API_HASH", api_hash)
    else:
        monkeypatch.delenv("TELEGRAM_API_HASH", raising=False)
    if bot_token is not None:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", bot_token)
    else:
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    importlib.reload(config)


def test_validate_missing(monkeypatch):
    _reload(monkeypatch, api_id="0", api_hash="abc", bot_token="tok")
    with pytest.raises(SystemExit):
        config.validate()


def test_validate_ok(monkeypatch):
    _reload(monkeypatch, api_id="123", api_hash="abc", bot_token="tok")
    # Should not raise
    config.validate()


# ── _env_int ValueError ──


def test_env_int_invalid_value(monkeypatch):
    """Non-integer env var falls back to default."""
    monkeypatch.setenv("SKIP_DOTENV", "1")
    monkeypatch.setenv("TELEGRAM_API_ID", "123")
    monkeypatch.setenv("TELEGRAM_API_HASH", "abc")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("MAX_RETRY_ATTEMPTS", "not_a_number")
    importlib.reload(config)
    assert config.MAX_RETRY_ATTEMPTS == 3


# ── _parse_allowed bare @ sign ──


def test_parse_allowed_bare_at():
    """Bare '@' token is stripped to empty and skipped."""
    ids, names = config._parse_allowed("@")
    assert ids == set()
    assert names == set()
