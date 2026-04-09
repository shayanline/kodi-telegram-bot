import asyncio

import kodi
import kodiremote
import throttle


def _playing_state():
    return {
        "player_id": 1,
        "label": "Test.Movie.2024.mkv",
        "info": {
            "percentage": 42.5,
            "time": {"hours": 1, "minutes": 23, "seconds": 45},
            "totaltime": {"hours": 2, "minutes": 15, "seconds": 30},
            "speed": 1,
        },
        "volume": 85,
        "muted": False,
    }


def _idle_state():
    return {
        "player_id": None,
        "label": None,
        "info": None,
        "volume": 60,
        "muted": False,
    }


# ── Renderer tests ──


def test_render_playback_playing():
    state = _playing_state()
    text, buttons = kodiremote._render_playback(state)
    assert "Kodi Remote" in text
    assert "Test.Movie.2024.mkv" in text
    assert "1:23:45" in text
    assert "2:15:30" in text
    assert "42%" in text
    assert "85%" in text
    # Should have playback controls + volume + nav/refresh
    assert len(buttons) == 4
    # First row: Prev, Play/Pause, Next
    assert len(buttons[0]) == 3
    # Second row: -30s, Stop, +30s
    assert len(buttons[1]) == 3


def test_render_playback_idle():
    state = _idle_state()
    text, buttons = kodiremote._render_playback(state)
    assert "Nothing playing" in text
    assert "60%" in text
    # No playback controls, just volume + nav/refresh
    assert len(buttons) == 2


def test_render_playback_paused():
    state = _playing_state()
    state["info"]["speed"] = 0
    text, _ = kodiremote._render_playback(state)
    assert "⏸️" in text


def test_render_playback_muted():
    state = _playing_state()
    state["muted"] = True
    text, _ = kodiremote._render_playback(state)
    assert "muted" in text
    assert "🔇" in text


def test_render_playback_no_label():
    state = _playing_state()
    state["label"] = None
    text, _ = kodiremote._render_playback(state)
    assert "▶️ Playing" in text


def test_render_navigation():
    text, buttons = kodiremote._render_navigation()
    assert "Navigation" in text
    # 5 rows: Up, Left/OK/Right, Down, Back/Home/Info, Menu/OSD, Playback
    assert len(buttons) == 6
    # D-pad center row has 3 buttons
    assert len(buttons[1]) == 3


# ── format_time tests ──


def test_format_time_with_hours():
    assert kodiremote._format_time({"hours": 1, "minutes": 5, "seconds": 3}) == "1:05:03"


def test_format_time_no_hours():
    assert kodiremote._format_time({"hours": 0, "minutes": 5, "seconds": 3}) == "5:03"


def test_format_time_empty():
    assert kodiremote._format_time({}) == "0:00"


# ── fetch_playback_state tests ──


def test_fetch_state_playing(monkeypatch):
    async def fake_player_id():
        return 1

    async def fake_now_playing(pid):
        return "Movie.mkv"

    async def fake_info(pid):
        return {"percentage": 50, "speed": 1}

    async def fake_volume():
        return (80, False)

    monkeypatch.setattr(kodi, "get_active_player_id", fake_player_id)
    monkeypatch.setattr(kodi, "get_now_playing", fake_now_playing)
    monkeypatch.setattr(kodi, "get_player_info", fake_info)
    monkeypatch.setattr(kodi, "get_volume", fake_volume)

    state = asyncio.run(kodiremote._fetch_playback_state())
    assert state["player_id"] == 1
    assert state["label"] == "Movie.mkv"
    assert state["volume"] == 80


def test_fetch_state_idle(monkeypatch):
    async def fake_player_id():
        return None

    async def fake_volume():
        return (50, True)

    monkeypatch.setattr(kodi, "get_active_player_id", fake_player_id)
    monkeypatch.setattr(kodi, "get_volume", fake_volume)

    state = asyncio.run(kodiremote._fetch_playback_state())
    assert state["player_id"] is None
    assert state["label"] is None
    assert state["muted"] is True


def test_fetch_state_exceptions(monkeypatch):
    """All kodi calls failing should return safe defaults."""

    async def boom(*a, **k):
        raise RuntimeError("fail")

    monkeypatch.setattr(kodi, "get_active_player_id", boom)
    monkeypatch.setattr(kodi, "get_volume", boom)

    state = asyncio.run(kodiremote._fetch_playback_state())
    assert state["player_id"] is None
    assert state["volume"] == 0


# ── Callback dispatch tests ──


class FakeEvent:
    """Minimal event mock for callback handler tests."""

    def __init__(self, data: bytes, message_text: str = ""):
        self.data = data
        self._answered = False
        self._answer_text = None
        self._edited = False
        self._message_text = message_text

    async def answer(self, text=None, alert=False):
        self._answered = True
        self._answer_text = text

    async def edit(self, text, buttons=None, parse_mode=None):
        self._edited = True

    async def get_message(self):
        return type("Msg", (), {"text": self._message_text})()


def test_player_action_nothing_playing(monkeypatch):
    async def fake_player_id():
        return None

    async def fake_volume():
        return (50, False)

    monkeypatch.setattr(kodi, "get_active_player_id", fake_player_id)
    monkeypatch.setattr(kodi, "get_volume", fake_volume)

    event = FakeEvent(b"k:pp")

    async def _run():
        await kodiremote._player_action(event, kodi.play_pause)

    asyncio.run(_run())
    assert event._answered
    assert event._answer_text == "Nothing playing"


def test_player_action_with_player(monkeypatch):
    calls = []

    async def fake_player_id():
        return 1

    async def fake_action(pid):
        calls.append(("action", pid))

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

    event = FakeEvent(b"k:pp")

    async def _run():
        await kodiremote._player_action(event, fake_action)

    asyncio.run(_run())
    assert ("action", 1) in calls
    assert event._answered


def test_render_playback_no_info():
    """Playing but player_info is None (e.g. RPC timeout)."""
    state = _playing_state()
    state["info"] = None
    text, buttons = kodiremote._render_playback(state)
    assert "Test.Movie.2024.mkv" in text
    # Should still show playback controls
    assert len(buttons) == 4


# ── Callback handler integration tests ──


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


def _mock_kodi_idle(monkeypatch):
    """Patch kodi module to simulate no active player."""

    async def fake_player_id():
        return None

    async def fake_volume():
        return (50, False)

    monkeypatch.setattr(kodi, "get_active_player_id", fake_player_id)
    monkeypatch.setattr(kodi, "get_volume", fake_volume)


def test_callback_play_pause(monkeypatch):
    _mock_kodi_playing(monkeypatch)
    calls = []

    async def fake_play_pause(pid):
        calls.append(("play_pause", pid))

    monkeypatch.setattr(kodi, "play_pause", fake_play_pause)
    event = FakeEvent(b"k:pp")
    asyncio.run(kodiremote._player_action(event, kodi.play_pause))
    assert ("play_pause", 1) in calls


def test_callback_stop(monkeypatch):
    _mock_kodi_playing(monkeypatch)
    calls = []

    async def fake_stop(pid):
        calls.append(("stop", pid))

    monkeypatch.setattr(kodi, "stop_player", fake_stop)
    event = FakeEvent(b"k:st")
    asyncio.run(kodiremote._player_action(event, kodi.stop_player))
    assert ("stop", 1) in calls


def test_callback_seek_forward(monkeypatch):
    _mock_kodi_playing(monkeypatch)
    calls = []

    async def fake_seek(pid, step):
        calls.append(("seek", pid, step))

    monkeypatch.setattr(kodi, "seek_step", fake_seek)
    event = FakeEvent(b"k:ff")
    asyncio.run(kodiremote._player_action(event, lambda pid: kodi.seek_step(pid, "smallforward")))
    assert ("seek", 1, "smallforward") in calls


def test_callback_volume_up(monkeypatch):
    vol_set = []

    async def fake_volume():
        return (75, False)

    async def fake_set_volume(level):
        vol_set.append(level)

    async def fake_player_id():
        return None

    async def fake_now_playing(pid):
        return None

    async def fake_info(pid):
        return None

    monkeypatch.setattr(kodi, "get_active_player_id", fake_player_id)
    monkeypatch.setattr(kodi, "get_volume", fake_volume)
    monkeypatch.setattr(kodi, "set_volume", fake_set_volume)
    monkeypatch.setattr(kodi, "get_now_playing", fake_now_playing)
    monkeypatch.setattr(kodi, "get_player_info", fake_info)

    event = FakeEvent(b"k:vu")

    async def _run():
        vol, _ = await kodi.get_volume()
        await kodi.set_volume(min(100, vol + kodiremote._VOL_STEP))
        await kodiremote._refresh_playback(event)
        await event.answer()

    asyncio.run(_run())
    assert vol_set[-1] == 80


def test_callback_volume_down(monkeypatch):
    vol_set = []

    async def fake_volume():
        return (10, False)

    async def fake_set_volume(level):
        vol_set.append(level)

    async def fake_player_id():
        return None

    monkeypatch.setattr(kodi, "get_active_player_id", fake_player_id)
    monkeypatch.setattr(kodi, "get_volume", fake_volume)
    monkeypatch.setattr(kodi, "set_volume", fake_set_volume)

    event = FakeEvent(b"k:vd")

    async def _run():
        vol, _ = await kodi.get_volume()
        await kodi.set_volume(max(0, vol - kodiremote._VOL_STEP))
        await kodiremote._refresh_playback(event)
        await event.answer()

    asyncio.run(_run())
    assert vol_set[-1] == 5


def test_callback_mute(monkeypatch):
    calls = []

    async def fake_toggle_mute():
        calls.append("mute")

    async def fake_player_id():
        return None

    async def fake_volume():
        return (50, True)

    monkeypatch.setattr(kodi, "toggle_mute", fake_toggle_mute)
    monkeypatch.setattr(kodi, "get_active_player_id", fake_player_id)
    monkeypatch.setattr(kodi, "get_volume", fake_volume)

    event = FakeEvent(b"k:mu")

    async def _run():
        await kodi.toggle_mute()
        await kodiremote._refresh_playback(event)
        await event.answer()

    asyncio.run(_run())
    assert "mute" in calls


def test_callback_switch_to_nav():
    event = FakeEvent(b"k:nv")

    async def _run():
        text, buttons = kodiremote._render_navigation()
        await throttle.edit_message(event, text, buttons=buttons, parse_mode="md")
        await event.answer()

    asyncio.run(_run())
    assert event._edited


def test_callback_switch_to_playback(monkeypatch):
    _mock_kodi_idle(monkeypatch)
    event = FakeEvent(b"k:pb")

    async def _run():
        await kodiremote._refresh_playback(event)
        await event.answer()

    asyncio.run(_run())
    assert event._edited


def test_callback_input_navigation(monkeypatch):
    calls = []

    async def fake_input(name):
        calls.append(name)

    monkeypatch.setattr(kodi, "input_command", fake_input)

    for data, expected in [
        (b"k:up", "Up"),
        (b"k:dn", "Down"),
        (b"k:lt", "Left"),
        (b"k:rt", "Right"),
        (b"k:ok", "Select"),
        (b"k:bk", "Back"),
        (b"k:hm", "Home"),
        (b"k:if", "Info"),
        (b"k:cm", "ContextMenu"),
        (b"k:os", "ShowOSD"),
    ]:
        ev = FakeEvent(data)

        async def _run(d=data, e=ev):
            await kodi.input_command(kodiremote._INPUT_MAP[d])
            await e.answer()

        asyncio.run(_run())
        assert expected in calls


def test_throttle_edit_suppresses_not_modified():
    """throttle.edit_message should suppress MessageNotModifiedError."""
    from telethon.errors import MessageNotModifiedError

    class NotModifiedEvent:
        async def edit(self, text, buttons=None, parse_mode=None):
            raise MessageNotModifiedError(None)

    result = asyncio.run(throttle.edit_message(NotModifiedEvent(), "text"))
    assert result is not None
