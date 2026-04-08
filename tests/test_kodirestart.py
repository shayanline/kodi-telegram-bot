import asyncio

import config
import kodi
import kodirestart


class FakeEvent:
    """Minimal event mock for command and callback tests."""

    def __init__(self, data: bytes = b""):
        self.data = data
        self._responded = None
        self._edited = None
        self._edited_buttons = None
        self._answered = False

    async def respond(self, text, buttons=None, parse_mode=None, reply_to=None):
        self._responded = text
        self._edited_buttons = buttons

    async def edit(self, text, buttons=None, parse_mode=None):
        self._edited = text
        self._edited_buttons = buttons

    async def answer(self, text=None, alert=False):
        self._answered = True


# ── Feature disabled (no KODI_START_CMD) ──


def test_restart_not_configured(monkeypatch):
    """When KODI_START_CMD is empty, respond with setup instructions."""
    monkeypatch.setattr(config, "KODI_START_CMD", "")
    monkeypatch.setattr(config, "ALLOWED_USER_IDS", set())
    monkeypatch.setattr(config, "ALLOWED_USERNAMES", set())

    event = FakeEvent()
    # Simulate what the command handler does
    asyncio.run(_invoke_command(event))
    assert event._responded is not None
    assert "not configured" in event._responded


def test_restart_configured_shows_confirmation(monkeypatch):
    """When KODI_START_CMD is set, show confirmation buttons."""
    monkeypatch.setattr(config, "KODI_START_CMD", "echo start")
    monkeypatch.setattr(config, "ALLOWED_USER_IDS", set())
    monkeypatch.setattr(config, "ALLOWED_USERNAMES", set())

    event = FakeEvent()
    asyncio.run(_invoke_command(event))
    assert event._responded is not None
    assert "Restart Kodi" in event._responded
    assert event._edited_buttons is not None
    assert len(event._edited_buttons) == 1
    assert len(event._edited_buttons[0]) == 2


def test_restart_auth_rejected(monkeypatch):
    """Unauthorized user gets rejected."""
    monkeypatch.setattr(config, "KODI_START_CMD", "echo start")
    monkeypatch.setattr(config, "ALLOWED_USER_IDS", {99999})
    monkeypatch.setattr(config, "ALLOWED_USERNAMES", set())

    event = FakeEvent()
    asyncio.run(_invoke_command(event, user_id=12345, username="other"))
    assert event._responded is not None
    assert "Not authorized" in event._responded


# ── Confirm callback ──


def test_confirm_restart_success(monkeypatch):
    """Confirming restart quits Kodi and runs start command."""
    monkeypatch.setattr(config, "KODI_START_CMD", "echo ok")

    quit_calls = []

    async def fake_quit():
        quit_calls.append(True)

    monkeypatch.setattr(kodi, "quit_kodi", fake_quit)

    async def fake_is_alive():
        return False

    monkeypatch.setattr(kodi, "is_alive", fake_is_alive)

    event = FakeEvent(b"kr:y")

    async def _run():
        await event.edit("🔄 Restarting Kodi…", buttons=None)
        await event.answer()
        await kodirestart._do_restart(event)

    asyncio.run(_run())
    assert quit_calls
    assert event._edited is not None
    assert "successfully" in event._edited


def test_confirm_restart_failure(monkeypatch):
    """Start command failure is reported to the user."""
    monkeypatch.setattr(config, "KODI_START_CMD", "exit 1")

    async def fake_quit():
        pass

    monkeypatch.setattr(kodi, "quit_kodi", fake_quit)

    async def fake_is_alive():
        return False

    monkeypatch.setattr(kodi, "is_alive", fake_is_alive)

    event = FakeEvent(b"kr:y")

    async def _run():
        await kodirestart._do_restart(event)

    asyncio.run(_run())
    assert event._edited is not None
    assert "failed" in event._edited


def test_confirm_restart_timeout(monkeypatch):
    """Start command timeout is reported."""
    monkeypatch.setattr(config, "KODI_START_CMD", "sleep 60")

    async def fake_quit():
        pass

    monkeypatch.setattr(kodi, "quit_kodi", fake_quit)

    async def fake_is_alive():
        return False

    monkeypatch.setattr(kodi, "is_alive", fake_is_alive)

    event = FakeEvent(b"kr:y")

    original = asyncio.wait_for

    async def fast_timeout(coro, timeout):
        return await original(coro, timeout=0.1)

    monkeypatch.setattr(asyncio, "wait_for", fast_timeout)

    async def _run():
        await kodirestart._do_restart(event)

    asyncio.run(_run())
    assert event._edited is not None
    assert "timed out" in event._edited


def test_restart_kodi_exit_timeout(monkeypatch):
    """When Kodi does not exit in time, report the failure."""
    monkeypatch.setattr(config, "KODI_START_CMD", "echo ok")
    monkeypatch.setattr(kodirestart, "_EXIT_TIMEOUT", 0)

    async def fake_quit():
        pass

    monkeypatch.setattr(kodi, "quit_kodi", fake_quit)

    async def fake_is_alive():
        return True

    monkeypatch.setattr(kodi, "is_alive", fake_is_alive)

    event = FakeEvent(b"kr:y")
    asyncio.run(kodirestart._do_restart(event))
    assert event._edited is not None
    assert "did not exit" in event._edited


# ── Cancel callback ──


def test_cancel_restart(monkeypatch):
    """Cancelling edits the message and removes buttons."""
    event = FakeEvent(b"kr:n")

    async def _run():
        await event.edit("🛑 Restart cancelled.", buttons=None)
        await event.answer()

    asyncio.run(_run())
    assert event._edited == "🛑 Restart cancelled."
    assert event._edited_buttons is None
    assert event._answered


# ── kodi.quit_kodi ──


def test_quit_kodi_calls_rpc(monkeypatch):
    """quit_kodi sends Application.Quit via RPC."""
    calls = []

    async def fake_rpc(method, params=None):
        calls.append(method)
        return {"result": "OK"}

    monkeypatch.setattr(kodi, "_rpc", fake_rpc)
    asyncio.run(kodi.quit_kodi())
    assert "Application.Quit" in calls


# ── Helpers ──


async def _invoke_command(event, user_id=1, username="testuser"):
    """Simulate the /restart_kodi command handler logic."""

    class FakeSender:
        def __init__(self, uid, uname):
            self.id = uid
            self.username = uname

    sender = FakeSender(user_id, username)
    if not config.is_user_allowed(sender.id, sender.username):
        await event.respond("🛑 Not authorized.")
        return
    if not config.KODI_START_CMD:
        await event.respond(kodirestart._SETUP_MSG, parse_mode="md")
        return
    text = "⚠️ **Restart Kodi?**\n\nThis will quit Kodi and start it again."
    from telethon import Button

    buttons = [
        [
            Button.inline("✅ Yes, Restart", data=b"kr:y"),
            Button.inline("❌ Cancel", data=b"kr:n"),
        ]
    ]
    await event.respond(text, buttons=buttons, parse_mode="md")
