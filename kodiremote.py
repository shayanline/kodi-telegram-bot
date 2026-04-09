"""Kodi remote control via Telegram inline buttons.

Provides a `/kodi` command that sends a single-message remote with two
switchable views: **Playback** (media controls + volume) and **Navigation**
(D-pad + menu actions). All actions hit Kodi JSON-RPC and refresh in-place.
"""

from __future__ import annotations

import contextlib
from typing import Any

from telethon import Button, TelegramClient, events

import config
import kodi
import throttle
from logger import log

_VOL_STEP = 5


# ── State fetching ──


async def _fetch_playback_state() -> dict[str, Any]:
    """Gather current player + volume info from Kodi."""
    state: dict[str, Any] = {"player_id": None, "label": None, "info": None, "volume": 0, "muted": False}
    with contextlib.suppress(Exception):
        state["player_id"] = await kodi.get_active_player_id()
    if state["player_id"] is not None:
        with contextlib.suppress(Exception):
            state["label"] = await kodi.get_now_playing(state["player_id"])
        with contextlib.suppress(Exception):
            state["info"] = await kodi.get_player_info(state["player_id"])
    with contextlib.suppress(Exception):
        state["volume"], state["muted"] = await kodi.get_volume()
    return state


def _format_time(t: dict[str, int]) -> str:
    """Format a Kodi time dict {hours, minutes, seconds} as HH:MM:SS or MM:SS."""
    h, m, s = t.get("hours", 0), t.get("minutes", 0), t.get("seconds", 0)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ── Renderers ──


def _render_playback(state: dict[str, Any]) -> tuple[str, list[list[Button]]]:
    """Build the playback remote view."""
    lines = ["🎬 **Kodi Remote**", "━━━━━━━━━━━━━━━━━━━━━"]

    info = state.get("info")
    label = state.get("label")
    playing = state["player_id"] is not None

    if playing and label:
        speed = info.get("speed", 0) if info else 0
        icon = "⏸️" if speed == 0 else "▶️"
        lines.append(f"{icon} {label}")
    elif playing:
        lines.append("▶️ Playing")
    else:
        lines.append("⏹️ Nothing playing")

    if playing and info:
        pct = int(info.get("percentage", 0))
        cur = _format_time(info["time"]) if "time" in info else "?"
        tot = _format_time(info["totaltime"]) if "totaltime" in info else "?"
        filled = round(pct / 100 * 16)
        bar = "▓" * filled + "░" * (16 - filled)
        lines.append(f"⏱️ {cur} {bar} {tot} ({pct}%)")

    vol = state.get("volume", 0)
    muted = state.get("muted", False)
    vol_icon = "🔇" if muted else "🔊"
    lines.append(f"{vol_icon} Volume: {vol}%{' (muted)' if muted else ''}")

    buttons: list[list[Button]] = []
    if playing:
        buttons.append(
            [
                Button.inline("⏮ Prev", data=b"k:pv"),
                Button.inline("⏯ Play/Pause", data=b"k:pp"),
                Button.inline("⏭ Next", data=b"k:nx"),
            ]
        )
        buttons.append(
            [
                Button.inline("⏪ -30s", data=b"k:rw"),
                Button.inline("⏹ Stop", data=b"k:st"),
                Button.inline("⏩ +30s", data=b"k:ff"),
            ]
        )
    buttons.append(
        [
            Button.inline("🔉 Vol-", data=b"k:vd"),
            Button.inline("🔇 Mute", data=b"k:mu"),
            Button.inline("🔊 Vol+", data=b"k:vu"),
        ]
    )
    buttons.append(
        [
            Button.inline("🧭 Navigation", data=b"k:nv"),
        ]
    )

    return "\n".join(lines), buttons


def _render_navigation() -> tuple[str, list[list[Button]]]:
    """Build the navigation remote view."""
    lines = [
        "🎬 **Kodi Remote** — Navigation",
        "━━━━━━━━━━━━━━━━━━━━━",
    ]
    buttons: list[list[Button]] = [
        [Button.inline("⬆️ Up", data=b"k:up")],
        [
            Button.inline("⬅️ Left", data=b"k:lt"),
            Button.inline("🆗 OK", data=b"k:ok"),
            Button.inline("➡️ Right", data=b"k:rt"),
        ],
        [Button.inline("⬇️ Down", data=b"k:dn")],
        [
            Button.inline("🔙 Back", data=b"k:bk"),
            Button.inline("🏠 Home", data=b"k:hm"),
            Button.inline("🔍 Info", data=b"k:if"),
        ],
        [
            Button.inline("📋 Menu", data=b"k:cm"),
            Button.inline("🖥️ OSD", data=b"k:os"),
        ],
        [
            Button.inline("🎮 Playback", data=b"k:pb"),
        ],
    ]
    return "\n".join(lines), buttons


# ── Helpers ──


async def _refresh_playback(event) -> None:
    """Fetch state and update the message to playback view."""
    state = await _fetch_playback_state()
    text, buttons = _render_playback(state)
    await throttle.edit_message(event, text, buttons=buttons, parse_mode="md")


async def _player_action(event, action) -> None:
    """Run a player action then refresh the playback view."""
    pid = await kodi.get_active_player_id()
    if pid is None:
        await throttle.answer_callback(event, "Nothing playing", alert=False)
        await _refresh_playback(event)
        return
    await action(pid)
    await _refresh_playback(event)
    await throttle.answer_callback(event)


# ── Callback dispatch ──

_INPUT_MAP = {
    b"k:up": "Up",
    b"k:dn": "Down",
    b"k:lt": "Left",
    b"k:rt": "Right",
    b"k:ok": "Select",
    b"k:bk": "Back",
    b"k:hm": "Home",
    b"k:if": "Info",
    b"k:cm": "ContextMenu",
    b"k:os": "ShowOSD",
}


# ── Handler registration ──


def register_kodi_remote(client: TelegramClient) -> None:
    """Register /kodi command and all remote callback handlers."""
    _register_command(client)
    _register_callbacks(client)


def _register_command(client: TelegramClient) -> None:
    @client.on(
        events.NewMessage(
            func=lambda e: e.is_private and not e.document and (e.raw_text or "").strip().lower() == "/kodi"
        )
    )
    async def _kodi_cmd(event):
        sender = await event.get_sender()
        if not config.is_user_allowed(getattr(sender, "id", None), getattr(sender, "username", None)):
            await throttle.send_message(event, "🛑 Not authorized.")
            return
        state = await _fetch_playback_state()
        text, buttons = _render_playback(state)
        await throttle.send_message(event, text, buttons=buttons, parse_mode="md")


def _register_callbacks(client: TelegramClient) -> None:
    @client.on(events.CallbackQuery(pattern=rb"k:[a-z]{2}"))
    async def _kodi_cb(event):
        data = event.data
        # Playback actions
        if data == b"k:pp":
            await _player_action(event, kodi.play_pause)
        elif data == b"k:st":
            await _player_action(event, kodi.stop_player)
        elif data == b"k:nx":
            await _player_action(event, kodi.go_next)
        elif data == b"k:pv":
            await _player_action(event, kodi.go_previous)
        elif data == b"k:ff":
            await _player_action(event, lambda pid: kodi.seek_step(pid, "smallforward"))
        elif data == b"k:rw":
            await _player_action(event, lambda pid: kodi.seek_step(pid, "smallbackward"))
        # Volume
        elif data == b"k:vu":
            vol, _ = await kodi.get_volume()
            await kodi.set_volume(min(100, vol + _VOL_STEP))
            await _refresh_playback(event)
            await throttle.answer_callback(event)
        elif data == b"k:vd":
            vol, _ = await kodi.get_volume()
            await kodi.set_volume(max(0, vol - _VOL_STEP))
            await _refresh_playback(event)
            await throttle.answer_callback(event)
        elif data == b"k:mu":
            await kodi.toggle_mute()
            await _refresh_playback(event)
            await throttle.answer_callback(event)
        # View switching
        elif data == b"k:nv":
            text, buttons = _render_navigation()
            await throttle.edit_message(event, text, buttons=buttons, parse_mode="md")
            await throttle.answer_callback(event)
        elif data == b"k:pb":
            await _refresh_playback(event)
            await throttle.answer_callback(event)
        # Navigation inputs
        elif data in _INPUT_MAP:
            await kodi.input_command(_INPUT_MAP[data])
            await throttle.answer_callback(event)
        else:
            log.debug("Unknown kodi remote callback: %s", data)
            await throttle.answer_callback(event)


__all__ = ["register_kodi_remote"]
