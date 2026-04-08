"""Tests for kodirestart handler registration and dispatch (lines 34-35, 39-73, 82, 114-116)."""

import asyncio

import config
import kodi
import kodirestart
import throttle

# ── Fake helpers ──


class FakeClient:
    """Captures handlers registered via @client.on(...)."""

    def __init__(self):
        self.handlers = []

    def on(self, event_type):
        def decorator(fn):
            self.handlers.append(fn)
            return fn

        return decorator


class FakeSender:
    def __init__(self, user_id=1, username="testuser"):
        self.id = user_id
        self.username = username


class FakeEvent:
    """Minimal event mock for handler tests."""

    def __init__(self, data=b"", sender=None):
        self.data = data
        self._sender = sender or FakeSender()
        self._responded = None
        self._edited = None
        self._answered = False

    async def get_sender(self):
        return self._sender

    async def respond(self, text, **kwargs):
        self._responded = text

    async def edit(self, text, **kwargs):
        self._edited = text

    async def answer(self, text=None, **kwargs):
        self._answered = True


def _fresh_locks(monkeypatch):
    """Replace throttle locks so they work in a new asyncio.run() event loop."""
    monkeypatch.setattr(throttle, "_handler_lock", asyncio.Lock())
    monkeypatch.setattr(throttle, "_tg_lock", asyncio.Lock())


def _register():
    """Register handlers on a FakeClient and return (cmd_handler, cb_handler)."""
    client = FakeClient()
    kodirestart.register_kodi_restart(client)
    assert len(client.handlers) == 2
    return client.handlers[0], client.handlers[1]


# ── Registration (lines 34-35) ──


def test_register_creates_two_handlers():
    """Lines 34-35: register_kodi_restart registers command + callback handlers."""
    client = FakeClient()
    kodirestart.register_kodi_restart(client)
    assert len(client.handlers) == 2


# ── Command handler (_restart_cmd, lines 39-60) ──


def test_cmd_unauthorized(monkeypatch):
    """Lines 46-49: unauthorized user gets rejected."""
    _fresh_locks(monkeypatch)
    monkeypatch.setattr(config, "ALLOWED_USER_IDS", {99999})
    monkeypatch.setattr(config, "ALLOWED_USERNAMES", set())

    cmd_handler, _ = _register()
    event = FakeEvent(sender=FakeSender(user_id=12345, username="other"))

    asyncio.run(cmd_handler(event))
    assert event._responded is not None
    assert "Not authorized" in event._responded


def test_cmd_no_start_cmd(monkeypatch):
    """Lines 50-52: empty KODI_START_CMD shows setup instructions."""
    _fresh_locks(monkeypatch)
    monkeypatch.setattr(config, "ALLOWED_USER_IDS", set())
    monkeypatch.setattr(config, "ALLOWED_USERNAMES", set())
    monkeypatch.setattr(config, "KODI_START_CMD", "")

    cmd_handler, _ = _register()
    event = FakeEvent()

    asyncio.run(cmd_handler(event))
    assert event._responded is not None
    assert "not configured" in event._responded


def test_cmd_shows_confirmation(monkeypatch):
    """Lines 53-60: configured KODI_START_CMD shows confirmation prompt."""
    _fresh_locks(monkeypatch)
    monkeypatch.setattr(config, "ALLOWED_USER_IDS", set())
    monkeypatch.setattr(config, "ALLOWED_USERNAMES", set())
    monkeypatch.setattr(config, "KODI_START_CMD", "echo start")

    cmd_handler, _ = _register()
    event = FakeEvent()

    asyncio.run(cmd_handler(event))
    assert event._responded is not None
    assert "Restart Kodi" in event._responded


# ── Callback handler (_restart_cb, lines 64-73) ──


def test_cb_cancel(monkeypatch):
    """Lines 67-70: cancel button edits message and answers."""
    _fresh_locks(monkeypatch)

    _, cb_handler = _register()
    event = FakeEvent(data=b"kr:n")

    asyncio.run(cb_handler(event))
    assert event._edited is not None
    assert "cancelled" in event._edited
    assert event._answered


def test_cb_confirm_restart(monkeypatch):
    """Lines 71-73: confirm triggers _do_restart flow."""
    _fresh_locks(monkeypatch)
    monkeypatch.setattr(config, "KODI_START_CMD", "echo ok")
    monkeypatch.setattr(kodirestart, "_EXIT_POLL_INTERVAL", 0)

    async def fake_quit():
        pass

    async def fake_is_alive():
        return False

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_subprocess(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr(kodi, "quit_kodi", fake_quit)
    monkeypatch.setattr(kodi, "is_alive", fake_is_alive)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_subprocess)

    _, cb_handler = _register()
    event = FakeEvent(data=b"kr:y")

    asyncio.run(cb_handler(event))
    assert event._edited is not None
    assert "successfully" in event._edited
    assert event._answered


# ── _wait_for_exit sleep (line 82) ──


def test_wait_for_exit_polls_then_exits(monkeypatch):
    """Line 82: sleep between polls when kodi is still alive on first check."""
    call_count = 0

    async def fake_is_alive():
        nonlocal call_count
        call_count += 1
        # Alive on first poll, dead on second
        return call_count <= 1

    monkeypatch.setattr(kodi, "is_alive", fake_is_alive)
    monkeypatch.setattr(kodirestart, "_EXIT_POLL_INTERVAL", 0)
    monkeypatch.setattr(kodirestart, "_EXIT_TIMEOUT", 60)

    result = asyncio.run(kodirestart._wait_for_exit())
    assert result is True
    assert call_count == 2


# ── _do_restart exception handling (lines 114-116) ──


def test_do_restart_generic_exception(monkeypatch):
    """Lines 114-116: generic exception from subprocess is caught and reported."""
    _fresh_locks(monkeypatch)
    monkeypatch.setattr(config, "KODI_START_CMD", "echo ok")
    monkeypatch.setattr(kodirestart, "_EXIT_POLL_INTERVAL", 0)

    async def fake_quit():
        pass

    async def fake_is_alive():
        return False

    async def fake_subprocess(*args, **kwargs):
        raise RuntimeError("spawn failed")

    monkeypatch.setattr(kodi, "quit_kodi", fake_quit)
    monkeypatch.setattr(kodi, "is_alive", fake_is_alive)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_subprocess)

    event = FakeEvent(data=b"kr:y")

    asyncio.run(kodirestart._do_restart(event))
    assert event._edited is not None
    assert "Failed to start Kodi" in event._edited
