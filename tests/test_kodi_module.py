import asyncio

import kodi


class DummyResp:
    def __init__(self, status_code=200, data=None):
        self.status_code = status_code
        self._data = data or {"result": []}

    def json(self):  # pragma: no cover - trivial
        return self._data


def test_rpc_exception(monkeypatch):
    calls = {"count": 0}

    def boom(*a, **k):
        calls["count"] += 1
        raise RuntimeError("fail")

    monkeypatch.setattr(kodi, "requests", type("R", (), {"post": staticmethod(boom)}))
    # Should swallow and return None
    assert asyncio.run(kodi._rpc("Test.Method")) is None
    assert calls["count"] == 1


def test_helpers_call_rpc(monkeypatch):
    seen = []

    async def fake_rpc(method, params=None):
        seen.append((method, params))
        if method == "Player.GetActivePlayers":
            return {"result": []}  # not playing
        return {"result": True}

    monkeypatch.setattr(kodi, "_rpc", fake_rpc)

    async def _run():
        await kodi.notify("T", "M")
        await kodi.play("/tmp/f.mp4")
        assert await kodi.is_playing() is False
        await kodi.progress_notify("f.mp4", 50, "10 MB")

    asyncio.run(_run())
    methods = [m for m, _ in seen]
    assert "GUI.ShowNotification" in methods and "Player.Open" in methods
    assert methods.count("GUI.ShowNotification") >= 2


def test_get_active_player_id_found(monkeypatch):
    async def fake_rpc(method, params=None):
        return {"result": [{"playerid": 1, "type": "video"}]}

    monkeypatch.setattr(kodi, "_rpc", fake_rpc)
    assert asyncio.run(kodi.get_active_player_id()) == 1


def test_get_active_player_id_none(monkeypatch):
    async def fake_rpc(method, params=None):
        return {"result": []}

    monkeypatch.setattr(kodi, "_rpc", fake_rpc)
    assert asyncio.run(kodi.get_active_player_id()) is None


def test_get_active_player_id_rpc_none(monkeypatch):
    async def fake_rpc(method, params=None):
        return None

    monkeypatch.setattr(kodi, "_rpc", fake_rpc)
    assert asyncio.run(kodi.get_active_player_id()) is None


def test_playback_controls(monkeypatch):
    seen = []

    async def fake_rpc(method, params=None):
        seen.append((method, params))
        return {"result": "OK"}

    monkeypatch.setattr(kodi, "_rpc", fake_rpc)

    async def _run():
        await kodi.play_pause(1)
        await kodi.stop_player(1)
        await kodi.go_previous(1)
        await kodi.go_next(1)
        await kodi.seek_step(1, "smallforward")

    asyncio.run(_run())
    methods = [m for m, _ in seen]
    assert "Player.PlayPause" in methods
    assert "Player.Stop" in methods
    assert "Player.GoTo" in methods
    assert "Player.Seek" in methods


def test_get_player_info(monkeypatch):
    async def fake_rpc(method, params=None):
        return {
            "result": {
                "percentage": 42.5,
                "time": {"hours": 0, "minutes": 5, "seconds": 30},
                "totaltime": {"hours": 0, "minutes": 12, "seconds": 0},
                "speed": 1,
            }
        }

    monkeypatch.setattr(kodi, "_rpc", fake_rpc)
    info = asyncio.run(kodi.get_player_info(1))
    assert info is not None
    assert info["percentage"] == 42.5
    assert info["speed"] == 1


def test_get_player_info_none(monkeypatch):
    async def fake_rpc(method, params=None):
        return None

    monkeypatch.setattr(kodi, "_rpc", fake_rpc)
    assert asyncio.run(kodi.get_player_info(1)) is None


def test_get_now_playing(monkeypatch):
    async def fake_rpc(method, params=None):
        return {"result": {"item": {"label": "Test Movie", "title": ""}}}

    monkeypatch.setattr(kodi, "_rpc", fake_rpc)
    assert asyncio.run(kodi.get_now_playing(1)) == "Test Movie"


def test_get_now_playing_title_fallback(monkeypatch):
    async def fake_rpc(method, params=None):
        return {"result": {"item": {"label": "", "title": "Fallback Title"}}}

    monkeypatch.setattr(kodi, "_rpc", fake_rpc)
    assert asyncio.run(kodi.get_now_playing(1)) == "Fallback Title"


def test_get_now_playing_none(monkeypatch):
    async def fake_rpc(method, params=None):
        return None

    monkeypatch.setattr(kodi, "_rpc", fake_rpc)
    assert asyncio.run(kodi.get_now_playing(1)) is None


def test_get_volume(monkeypatch):
    async def fake_rpc(method, params=None):
        return {"result": {"volume": 75, "muted": False}}

    monkeypatch.setattr(kodi, "_rpc", fake_rpc)
    vol, muted = asyncio.run(kodi.get_volume())
    assert vol == 75
    assert muted is False


def test_get_volume_rpc_none(monkeypatch):
    async def fake_rpc(method, params=None):
        return None

    monkeypatch.setattr(kodi, "_rpc", fake_rpc)
    vol, muted = asyncio.run(kodi.get_volume())
    assert vol == 0
    assert muted is False


def test_set_volume_clamps(monkeypatch):
    seen = []

    async def fake_rpc(method, params=None):
        seen.append(params)
        return {"result": "OK"}

    monkeypatch.setattr(kodi, "_rpc", fake_rpc)
    asyncio.run(kodi.set_volume(150))
    assert seen[-1]["volume"] == 100
    asyncio.run(kodi.set_volume(-10))
    assert seen[-1]["volume"] == 0


def test_toggle_mute(monkeypatch):
    seen = []

    async def fake_rpc(method, params=None):
        seen.append((method, params))
        return {"result": "OK"}

    monkeypatch.setattr(kodi, "_rpc", fake_rpc)
    asyncio.run(kodi.toggle_mute())
    assert seen[-1] == ("Application.SetMute", {"mute": "toggle"})


def test_input_command_valid(monkeypatch):
    seen = []

    async def fake_rpc(method, params=None):
        seen.append(method)
        return {"result": "OK"}

    monkeypatch.setattr(kodi, "_rpc", fake_rpc)
    asyncio.run(kodi.input_command("Up"))
    assert "Input.Up" in seen
    asyncio.run(kodi.input_command("Select"))
    assert "Input.Select" in seen


def test_input_command_invalid(monkeypatch):
    seen = []

    async def fake_rpc(method, params=None):
        seen.append(method)
        return {"result": "OK"}

    monkeypatch.setattr(kodi, "_rpc", fake_rpc)
    asyncio.run(kodi.input_command("InvalidCommand"))
    assert len(seen) == 0
