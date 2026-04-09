import importlib

import config


def reload_env(monkeypatch, value: str | None):
    monkeypatch.setenv("SKIP_DOTENV", "1")  # ensure .env values ignored during test reloads
    if value is None:
        monkeypatch.delenv("ALLOWED_USERS", raising=False)
    else:
        monkeypatch.setenv("ALLOWED_USERS", value)
    importlib.reload(config)


def test_open_access(monkeypatch):
    reload_env(monkeypatch, None)
    assert set() == config.ALLOWED_USER_IDS
    assert set() == config.ALLOWED_USERNAMES
    assert config.is_user_allowed(123, "anyone") is True


def test_id_restriction(monkeypatch):
    reload_env(monkeypatch, "12345, 67890")
    assert config.is_user_allowed(12345, None)
    assert not config.is_user_allowed(111, None)


def test_username_restriction(monkeypatch):
    reload_env(monkeypatch, "alice,Bob")
    assert config.is_user_allowed(None, "alice")
    assert config.is_user_allowed(None, "bob")  # case insensitive
    assert not config.is_user_allowed(None, "charlie")


def test_mixed(monkeypatch):
    reload_env(monkeypatch, "@alice, 42, bob")
    assert config.is_user_allowed(42, None)
    assert config.is_user_allowed(None, "Alice")
    assert config.is_user_allowed(None, "BOB")
    assert not config.is_user_allowed(99, "zzz")
