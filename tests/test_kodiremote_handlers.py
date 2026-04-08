"""Tests for kodiremote handler registration and callback dispatch (lines 187-268)."""

import asyncio

import config
import kodi
import kodiremote
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

    def __init__(self, data=b"", sender=None, message_text=""):
        self.data = data
        self._sender = sender or FakeSender()
        self._responded = None
        self._edited = None
        self._answered = False
        self._answer_text = None
        self._message_text = message_text

    async def get_sender(self):
        return self._sender

    async def respond(self, text, **kwargs):
        self._responded = text

    async def edit(self, text, **kwargs):
        self._edited = text

    async def answer(self, text=None, **kwargs):
        self._answered = True
        self._answer_text = text

    async def get_message(self):
        return type("Msg", (), {"text": self._message_text})()


def _fresh_locks(monkeypatch):
    """Replace throttle locks so they work in a new asyncio.run() event loop."""
    monkeypatch.setattr(throttle, "_handler_lock", asyncio.Lock())
    monkeypatch.setattr(throttle, "_tg_lock", asyncio.Lock())


def _register():
    """Register handlers on a FakeClient and return (cmd_handler, cb_handler)."""
    client = FakeClient()
    kodiremote.register_kodi_remote(client)
    assert len(client.handlers) == 2
    return client.handlers[0], client.handlers[1]


def _mock_kodi_idle(monkeypatch):
    """Patch kodi module to simulate no active player."""

    async def fake_player_id():
        return None

    async def fake_volume():
        return (50, False)

    monkeypatch.setattr(kodi, "get_active_player_id", fake_player_id)
    monkeypatch.setattr(kodi, "get_volume", fake_volume)


def _mock_kodi_playing(monkeypatch):
    """Patch kodi module to simulate an active player."""

    async def fake_player_id():
        return 1

    async def fake_volume():
        return (50, False)

    async def fake_info(pid):
        return {
            "percentage": 50,
            "speed": 1,
            "time": {"hours": 0, "minutes": 1, "seconds": 0},
            "totaltime": {"hours": 0, "minutes": 2, "seconds": 0},
        }

    async def fake_now_playing(pid):
        return "Test"

    monkeypatch.setattr(kodi, "get_active_player_id", fake_player_id)
    monkeypatch.setattr(kodi, "get_now_playing", fake_now_playing)
    monkeypatch.setattr(kodi, "get_player_info", fake_info)
    monkeypatch.setattr(kodi, "get_volume", fake_volume)


# ── Registration ──


def test_register_creates_two_handlers():
    """Lines 187-188: register_kodi_remote registers command + callback handlers."""
    client = FakeClient()
    kodiremote.register_kodi_remote(client)
    assert len(client.handlers) == 2


# ── Command handler (_kodi_cmd) ──


def test_cmd_authorized(monkeypatch):
    """Lines 198-205: authorized user receives remote control message."""
    _fresh_locks(monkeypatch)
    monkeypatch.setattr(config, "ALLOWED_USER_IDS", set())
    monkeypatch.setattr(config, "ALLOWED_USERNAMES", set())
    _mock_kodi_idle(monkeypatch)

    cmd_handler, _ = _register()
    event = FakeEvent()

    asyncio.run(cmd_handler(event))
    assert event._responded is not None
    assert "Kodi Remote" in event._responded


def test_cmd_unauthorized(monkeypatch):
    """Lines 199-202: unauthorized user gets rejected."""
    _fresh_locks(monkeypatch)
    monkeypatch.setattr(config, "ALLOWED_USER_IDS", {99999})
    monkeypatch.setattr(config, "ALLOWED_USERNAMES", set())

    cmd_handler, _ = _register()
    event = FakeEvent(sender=FakeSender(user_id=12345, username="other"))

    asyncio.run(cmd_handler(event))
    assert event._responded is not None
    assert "Not authorized" in event._responded


# ── Callback handler (_kodi_cb) — playback actions ──


def test_cb_playback_actions(monkeypatch):
    """Lines 214-225: playback actions (pp, st, nx, pv, ff, rw) dispatch correctly."""
    _fresh_locks(monkeypatch)
    _mock_kodi_playing(monkeypatch)
    calls = []

    async def rec_pp(pid):
        calls.append(("pp", pid))

    async def rec_st(pid):
        calls.append(("st", pid))

    async def rec_nx(pid):
        calls.append(("nx", pid))

    async def rec_pv(pid):
        calls.append(("pv", pid))

    async def rec_seek(pid, step):
        calls.append(("seek", step))

    monkeypatch.setattr(kodi, "play_pause", rec_pp)
    monkeypatch.setattr(kodi, "stop_player", rec_st)
    monkeypatch.setattr(kodi, "go_next", rec_nx)
    monkeypatch.setattr(kodi, "go_previous", rec_pv)
    monkeypatch.setattr(kodi, "seek_step", rec_seek)

    _, cb = _register()

    async def _run():
        for data in [b"k:pp", b"k:st", b"k:nx", b"k:pv", b"k:ff", b"k:rw"]:
            await cb(FakeEvent(data=data))

    asyncio.run(_run())
    assert ("pp", 1) in calls
    assert ("st", 1) in calls
    assert ("nx", 1) in calls
    assert ("pv", 1) in calls
    assert ("seek", "smallforward") in calls
    assert ("seek", "smallbackward") in calls


# ── Callback handler — volume ──


def test_cb_volume_up(monkeypatch):
    """Lines 227-231: k:vu increases volume by _VOL_STEP."""
    _fresh_locks(monkeypatch)
    _mock_kodi_idle(monkeypatch)
    vol_set = []

    async def fake_set_volume(level):
        vol_set.append(level)

    monkeypatch.setattr(kodi, "set_volume", fake_set_volume)

    _, cb = _register()
    event = FakeEvent(data=b"k:vu")

    asyncio.run(cb(event))
    assert vol_set == [55]  # 50 + 5


def test_cb_volume_down(monkeypatch):
    """Lines 232-236: k:vd decreases volume by _VOL_STEP."""
    _fresh_locks(monkeypatch)
    _mock_kodi_idle(monkeypatch)
    vol_set = []

    async def fake_set_volume(level):
        vol_set.append(level)

    monkeypatch.setattr(kodi, "set_volume", fake_set_volume)

    _, cb = _register()
    event = FakeEvent(data=b"k:vd")

    asyncio.run(cb(event))
    assert vol_set == [45]  # 50 - 5


def test_cb_mute(monkeypatch):
    """Lines 237-240: k:mu toggles mute."""
    _fresh_locks(monkeypatch)
    _mock_kodi_idle(monkeypatch)
    calls = []

    async def fake_toggle_mute():
        calls.append("mute")

    monkeypatch.setattr(kodi, "toggle_mute", fake_toggle_mute)

    _, cb = _register()
    event = FakeEvent(data=b"k:mu")

    asyncio.run(cb(event))
    assert "mute" in calls
    assert event._edited is not None


# ── Callback handler — view switching ──


def test_cb_switch_to_navigation(monkeypatch):
    """Lines 242-245: k:nv switches to navigation view."""
    _fresh_locks(monkeypatch)

    _, cb = _register()
    event = FakeEvent(data=b"k:nv")

    asyncio.run(cb(event))
    assert event._edited is not None
    assert "Navigation" in event._edited
    assert event._answered


def test_cb_switch_to_playback(monkeypatch):
    """Lines 246-248: k:pb switches to playback view."""
    _fresh_locks(monkeypatch)
    _mock_kodi_idle(monkeypatch)

    _, cb = _register()
    event = FakeEvent(data=b"k:pb")

    asyncio.run(cb(event))
    assert event._edited is not None
    assert "Kodi Remote" in event._edited
    assert event._answered


# ── Callback handler — refresh ──


def test_cb_refresh_playback(monkeypatch):
    """Lines 250-261: k:rf refreshes playback view when not in navigation."""
    _fresh_locks(monkeypatch)
    _mock_kodi_idle(monkeypatch)

    _, cb = _register()
    event = FakeEvent(data=b"k:rf", message_text="Kodi Remote")

    asyncio.run(cb(event))
    assert event._edited is not None
    assert event._answer_text == "Refreshed"


def test_cb_refresh_navigation(monkeypatch):
    """Lines 250-258: k:rf refreshes navigation view when message contains Navigation."""
    _fresh_locks(monkeypatch)

    _, cb = _register()
    event = FakeEvent(data=b"k:rf", message_text="Kodi Remote — Navigation")

    asyncio.run(cb(event))
    assert event._edited is not None
    assert "Navigation" in event._edited
    assert event._answer_text == "Refreshed"


def test_cb_refresh_get_message_error(monkeypatch):
    """Lines 253-255: exception in get_message defaults to playback refresh."""
    _fresh_locks(monkeypatch)
    _mock_kodi_idle(monkeypatch)

    _, cb = _register()

    class _ErrorEvent(FakeEvent):
        async def get_message(self):
            raise RuntimeError("message gone")

    event = _ErrorEvent(data=b"k:rf")

    asyncio.run(cb(event))
    assert event._edited is not None
    assert event._answer_text == "Refreshed"


# ── Callback handler — navigation input ──


def test_cb_navigation_input(monkeypatch):
    """Lines 263-265: navigation input commands dispatched via _INPUT_MAP."""
    _fresh_locks(monkeypatch)
    calls = []

    async def fake_input(name):
        calls.append(name)

    monkeypatch.setattr(kodi, "input_command", fake_input)

    _, cb = _register()
    event = FakeEvent(data=b"k:up")

    asyncio.run(cb(event))
    assert "Up" in calls
    assert event._answered


# ── Callback handler — unknown data ──


def test_cb_unknown_data(monkeypatch):
    """Lines 266-268: unknown callback data is handled gracefully."""
    _fresh_locks(monkeypatch)

    _, cb = _register()
    event = FakeEvent(data=b"k:zz")

    asyncio.run(cb(event))
    assert event._answered
