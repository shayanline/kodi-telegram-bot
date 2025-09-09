"""Kodi restart command with confirmation prompt.

Provides ``/restart_kodi`` which quits Kodi via JSON-RPC and then launches it
again using a user-configured shell command (``KODI_START_CMD``).  The feature
is disabled when the env variable is not set.
"""

from __future__ import annotations

import asyncio

from telethon import Button, TelegramClient, events

import config
import kodi
import throttle
from logger import log

_SETUP_MSG = (
    "⚙️ **Kodi restart is not configured.**\n\n"
    "Set `KODI_START_CMD` in your `.env` file to the command that starts Kodi.\n"
    "Example: `KODI_START_CMD=systemctl start cec-kodi-launcher.service`"
)

_EXIT_POLL_INTERVAL = 1
_EXIT_TIMEOUT = 30
_START_DELAY = 3


# ── Registration ──


def register_kodi_restart(client: TelegramClient) -> None:
    """Register /restart_kodi command and confirmation callbacks."""
    _register_command(client)
    _register_callbacks(client)


def _register_command(client: TelegramClient) -> None:
    @client.on(
        events.NewMessage(
            func=lambda e: e.is_private and not e.document and (e.raw_text or "").strip().lower() == "/restart_kodi"
        )
    )
    async def _restart_cmd(event):
        sender = await event.get_sender()
        if not config.is_user_allowed(getattr(sender, "id", None), getattr(sender, "username", None)):
            await throttle.send_message(event, "🛑 Not authorized.")
            return
        if not config.KODI_START_CMD:
            await throttle.send_message(event, _SETUP_MSG, parse_mode="md")
            return
        text = "⚠️ **Restart Kodi?**\n\nThis will quit Kodi and start it again."
        buttons = [
            [
                Button.inline("✅ Yes, Restart", data=b"kr:y"),
                Button.inline("❌ Cancel", data=b"kr:n"),
            ]
        ]
        await throttle.send_message(event, text, buttons=buttons, parse_mode="md")


def _register_callbacks(client: TelegramClient) -> None:
    @client.on(events.CallbackQuery(pattern=rb"kr:[yn]"))
    async def _restart_cb(event):
        if event.data == b"kr:n":
            await throttle.edit_message(event, "🛑 Restart cancelled.", buttons=None)
            await throttle.answer_callback(event)
            return
        await throttle.edit_message(event, "🔄 Restarting Kodi…", buttons=None)
        await throttle.answer_callback(event)
        await _do_restart(event)


async def _wait_for_exit() -> bool:
    """Poll until Kodi stops responding, or timeout."""
    deadline = asyncio.get_running_loop().time() + _EXIT_TIMEOUT
    while asyncio.get_running_loop().time() < deadline:
        if not await kodi.is_alive():
            return True
        await asyncio.sleep(_EXIT_POLL_INTERVAL)
    return False


async def _do_restart(event) -> None:
    """Quit Kodi, wait for it to exit, then run the start command."""
    await kodi.quit_kodi()
    if not await _wait_for_exit():
        await throttle.edit_message(event, "❌ Kodi did not exit in time.")
        log.error("Kodi did not exit within %ss", _EXIT_TIMEOUT)
        return
    # Let the OS fully release Kodi's process, ports and locks
    await asyncio.sleep(_START_DELAY)
    try:
        proc = await asyncio.create_subprocess_shell(
            config.KODI_START_CMD,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode == 0:
            await throttle.edit_message(event, "✅ Kodi restarted successfully.")
            log.info("Kodi restarted via: %s", config.KODI_START_CMD)
        else:
            err = stderr.decode(errors="replace").strip() if stderr else "unknown error"
            await throttle.edit_message(
                event, f"❌ Kodi start failed (exit {proc.returncode}):\n`{err}`", parse_mode="md"
            )
            log.error("Kodi start failed (exit %s): %s", proc.returncode, err)
    except TimeoutError:
        await throttle.edit_message(event, "❌ Kodi start command timed out.")
        log.error("Kodi start command timed out")
    except Exception as e:
        await throttle.edit_message(event, f"❌ Failed to start Kodi: {e}")
        log.error("Kodi start error: %s", e)


__all__ = ["register_kodi_restart"]
