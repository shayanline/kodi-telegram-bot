"""Thin async helper layer for interacting with Kodi JSON-RPC.

All calls are serialized through an internal FIFO queue to prevent
overwhelming Kodi (especially on low-power hardware like Raspberry Pi)
and to ensure every command executes in order.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import requests

import config
from logger import log

_RPC_MIN_INTERVAL = 0.05  # 50ms between consecutive Kodi calls


def _rpc_sync(method: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Blocking RPC call — run via asyncio.to_thread from async contexts."""
    payload = {"jsonrpc": "2.0", "method": method, "params": params or {}, "id": 1}
    try:
        r = requests.post(
            config.KODI_URL,
            json=payload,
            auth=config.KODI_AUTH,
            timeout=5,
        )
        if r.status_code != 200:
            log.warning("Kodi RPC non-200 (%s) method=%s", r.status_code, method)
            return None
        return r.json()
    except Exception as e:
        log.error("Kodi RPC error (%s): %s", method, e)
        return None


# ── RPC Queue ──


class _RpcQueue:
    """Async FIFO queue that serializes all Kodi JSON-RPC calls."""

    def __init__(self, min_interval: float = _RPC_MIN_INTERVAL):
        self._queue: asyncio.Queue[tuple[str, dict[str, Any] | None, asyncio.Future[dict[str, Any] | None]]] = (
            asyncio.Queue()
        )
        self._task: asyncio.Task[None] | None = None
        self._min_interval = min_interval
        self._last_call = 0.0

    async def submit(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Submit an RPC call and await its result."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any] | None] = loop.create_future()
        await self._queue.put((method, params, future))
        self._ensure_worker()
        return await future

    def _ensure_worker(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._worker())

    async def _worker(self) -> None:
        while not self._queue.empty():
            method, params, future = await self._queue.get()
            wait = self._min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                result = await asyncio.to_thread(_rpc_sync, method, params)
                if not future.done():
                    future.set_result(result)
            except Exception as e:
                if not future.done():
                    future.set_exception(e)
            finally:
                self._last_call = time.monotonic()
            self._queue.task_done()


_rpc_queue = _RpcQueue()


async def _rpc(method: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    return await _rpc_queue.submit(method, params)


async def notify(title: str, message: str) -> None:
    log.debug("Notify: %s - %s", title, message)
    await _rpc("GUI.ShowNotification", {"title": title, "message": message, "displaytime": 2000})


async def play(filepath: str) -> None:
    log.info("Play: %s", filepath)
    await _rpc("Player.Open", {"item": {"file": filepath}})


async def quit_kodi() -> None:
    """Send Application.Quit to shut down Kodi."""
    log.info("Sending Application.Quit")
    await _rpc("Application.Quit")


async def is_alive() -> bool:
    """Return True if Kodi responds to a JSON-RPC ping."""
    return await _rpc("JSONRPC.Ping") is not None


async def is_playing() -> bool:
    data = await _rpc("Player.GetActivePlayers") or {}
    playing = bool(data.get("result"))
    log.debug("is_playing=%s", playing)
    return playing


async def progress_notify(filename: str, percent: int, speed: str) -> None:
    bar = "▓" * (percent // 10) + "░" * (10 - percent // 10)
    await notify(f"Downloading: {filename}", f"{bar} {percent}% | {speed}/s")


# ── Playback controls ──


async def get_active_player_id() -> int | None:
    """Return the ID of the first active player, or None."""
    data = await _rpc("Player.GetActivePlayers") or {}
    players = data.get("result", [])
    return players[0]["playerid"] if players else None


async def play_pause(player_id: int) -> None:
    await _rpc("Player.PlayPause", {"playerid": player_id})


async def stop_player(player_id: int) -> None:
    await _rpc("Player.Stop", {"playerid": player_id})


async def go_previous(player_id: int) -> None:
    await _rpc("Player.GoTo", {"playerid": player_id, "to": "previous"})


async def go_next(player_id: int) -> None:
    await _rpc("Player.GoTo", {"playerid": player_id, "to": "next"})


async def seek_step(player_id: int, step: str) -> None:
    """Seek using step value (smallforward, smallbackward, etc.)."""
    await _rpc("Player.Seek", {"playerid": player_id, "value": step})


async def get_player_info(player_id: int) -> dict[str, Any] | None:
    """Get player properties (percentage, time, totaltime, speed)."""
    data = await _rpc(
        "Player.GetProperties",
        {"playerid": player_id, "properties": ["percentage", "time", "totaltime", "speed"]},
    )
    return data.get("result") if data else None


async def get_now_playing(player_id: int) -> str | None:
    """Get the label of the currently playing item."""
    data = await _rpc("Player.GetItem", {"playerid": player_id, "properties": ["title"]})
    if not data:
        return None
    item = data.get("result", {}).get("item", {})
    return item.get("label") or item.get("title")


# ── Volume ──


async def get_volume() -> tuple[int, bool]:
    """Return (volume_level, is_muted)."""
    data = await _rpc("Application.GetProperties", {"properties": ["volume", "muted"]})
    result = data.get("result", {}) if data else {}
    return result.get("volume", 0), result.get("muted", False)


async def set_volume(level: int) -> None:
    await _rpc("Application.SetVolume", {"volume": max(0, min(100, level))})


async def toggle_mute() -> None:
    await _rpc("Application.SetMute", {"mute": "toggle"})


# ── Navigation / input ──


async def input_command(name: str) -> None:
    """Send an Input.{name} command (Up, Down, Left, Right, Select, etc.)."""
    await _rpc(f"Input.{name}")


__all__ = [
    "get_active_player_id",
    "get_now_playing",
    "get_player_info",
    "get_volume",
    "go_next",
    "go_previous",
    "input_command",
    "is_alive",
    "is_playing",
    "notify",
    "play",
    "play_pause",
    "progress_notify",
    "quit_kodi",
    "seek_step",
    "set_volume",
    "stop_player",
    "toggle_mute",
]
