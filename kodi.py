"""Thin async helper layer for interacting with Kodi JSON-RPC."""

from __future__ import annotations

import asyncio
from typing import Any

import requests

import config
from logger import log


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


async def _rpc(method: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    return await asyncio.to_thread(_rpc_sync, method, params)


async def notify(title: str, message: str) -> None:
    log.debug("Notify: %s - %s", title, message)
    await _rpc("GUI.ShowNotification", {"title": title, "message": message, "displaytime": 2000})


async def play(filepath: str) -> None:
    log.info("Play: %s", filepath)
    await _rpc("Player.Open", {"item": {"file": filepath}})


async def is_playing() -> bool:
    data = await _rpc("Player.GetActivePlayers") or {}
    playing = bool(data.get("result"))
    log.debug("is_playing=%s", playing)
    return playing


async def progress_notify(filename: str, percent: int, speed: str) -> None:
    bar = "▓" * (percent // 10) + "░" * (10 - percent // 10)
    await notify(f"Downloading: {filename}", f"{bar} {percent}% | {speed}/s")


__all__ = ["is_playing", "notify", "play", "progress_notify"]
