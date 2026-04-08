"""Thin helper layer for interacting with Kodi JSON-RPC."""

from __future__ import annotations

from typing import Any

import requests

import config
from logger import log


def _rpc(method: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    payload = {"jsonrpc": "2.0", "method": method, "params": params or {}, "id": 1}
    try:
        r = requests.post(
            config.KODI_URL,
            headers=config.HEADERS,
            json=payload,
            auth=config.KODI_AUTH,
            timeout=5,
        )
        data = r.json()
        if r.status_code != 200:
            log.warning("Kodi RPC non-200 (%s) method=%s", r.status_code, method)
        return data
    except Exception as e:
        log.error("Kodi RPC error (%s): %s", method, e)
        return None


def notify(title: str, message: str) -> None:
    log.debug("Notify: %s - %s", title, message)
    _rpc("GUI.ShowNotification", {"title": title, "message": message, "displaytime": 2000})


def play(filepath: str) -> None:
    log.info("Play: %s", filepath)
    _rpc("Player.Open", {"item": {"file": filepath}})


def is_playing() -> bool:
    data = _rpc("Player.GetActivePlayers") or {}
    playing = bool(data.get("result"))
    log.debug("is_playing=%s", playing)
    return playing


def progress_notify(filename: str, percent: int, speed: str) -> None:
    bar = "▓" * (percent // 10) + "░" * (10 - percent // 10)
    notify(f"Downloading: {filename}", f"{bar} {percent}% | {speed}/s")


__all__ = ["is_playing", "notify", "play", "progress_notify"]
