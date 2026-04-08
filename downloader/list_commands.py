from __future__ import annotations

import contextlib

from telethon import Button, TelegramClient, events
from telethon.errors import MessageNotModifiedError

import config
import utils
from logger import log

from .buttons import build_buttons
from .ids import get_file_id
from .queue import queue
from .state import MessageType, message_tracker, resolve_file_id, states


def register_list_handlers(client: TelegramClient):
    """Register handlers for list commands."""
    _register_downloads_handler(client)
    _register_queue_handler(client)
    _register_list_callbacks(client)


def _register_downloads_handler(client: TelegramClient):
    @client.on(
        events.NewMessage(
            func=lambda e: e.is_private and not e.document and (e.raw_text or "").strip().lower() == "/downloads"
        )
    )
    async def _downloads(event):
        sender = await event.get_sender()
        user_id = getattr(sender, "id", None)
        username = getattr(sender, "username", None)

        if not config.is_user_allowed(user_id, username):
            await event.respond("🛑 Not authorized.")
            return

        if not states:
            text = "📁 No active downloads"
            buttons = [[Button.inline("🔄 Refresh", data="refresh_downloads")]]
            msg = await event.respond(text, buttons=buttons)
        else:
            text, buttons = _build_downloads_list(states)
            msg = await event.respond(text, buttons=buttons)

        message_tracker.register_message("__downloads_list__", msg, MessageType.DOWNLOAD_LIST, user_id)
        for filename in states:
            message_tracker.register_message(filename, msg, MessageType.DOWNLOAD_LIST, user_id)


def _register_queue_handler(client: TelegramClient):
    @client.on(
        events.NewMessage(
            func=lambda e: e.is_private and not e.document and (e.raw_text or "").strip().lower() == "/queue"
        )
    )
    async def _queue_list(event):
        sender = await event.get_sender()
        user_id = getattr(sender, "id", None)
        username = getattr(sender, "username", None)

        if not config.is_user_allowed(user_id, username):
            await event.respond("🛑 Not authorized.")
            return

        if not queue.items:
            text = "📝 No queued downloads"
            buttons = [[Button.inline("🔄 Refresh", data="refresh_queue")]]
            msg = await event.respond(text, buttons=buttons)
        else:
            text, buttons = _build_queue_list(queue.items)
            msg = await event.respond(text, buttons=buttons)

        message_tracker.register_message("__queue_list__", msg, MessageType.QUEUE_LIST, user_id)
        for filename in queue.items:
            message_tracker.register_message(filename, msg, MessageType.QUEUE_LIST, user_id)


def _register_list_callbacks(client: TelegramClient):
    @client.on(events.CallbackQuery(pattern=b"refresh_downloads"))
    async def _refresh_downloads(event):
        try:
            if not states:
                text = "📁 No active downloads"
                buttons = [[Button.inline("🔄 Refresh", data="refresh_downloads")]]
                await event.edit(text, buttons=buttons)
            else:
                text, buttons = _build_downloads_list(states)
                await event.edit(text, buttons=buttons)
        except MessageNotModifiedError:
            pass
        except Exception as e:
            log.debug("Failed to refresh downloads list: %s", e)

        await event.answer("Refreshed")

    @client.on(events.CallbackQuery(pattern=b"refresh_queue"))
    async def _refresh_queue(event):
        try:
            if not queue.items:
                text = "📝 No queued downloads"
                buttons = [[Button.inline("🔄 Refresh", data="refresh_queue")]]
                await event.edit(text, buttons=buttons)
            else:
                text, buttons = _build_queue_list(queue.items)
                await event.edit(text, buttons=buttons)
        except MessageNotModifiedError:
            pass
        except Exception as e:
            log.debug("Failed to refresh queue list: %s", e)

        await event.answer("Refreshed")

    @client.on(events.CallbackQuery(pattern=b"info:"))
    async def _info(event):
        await _handle_info_callback(event)


async def _handle_info_callback(event):
    """Handle info button callback."""
    file_id = event.data.decode().split(":", 1)[1]
    filename = resolve_file_id(file_id)
    if not filename:
        await event.answer("File not found", alert=False)
        return

    state = states.get(filename)
    if not state:
        await event.answer("Download not active", alert=False)
        return

    await _create_info_message(event, filename, state)


async def _create_info_message(event, filename, state):
    """Create a new info/progress message for the user."""
    sender = await event.get_sender()
    user_id = getattr(sender, "id", None)

    status = _get_status_text(state)
    text = f"{status}: {filename}"
    buttons = build_buttons(state)

    try:
        msg = await event.respond(text, buttons=buttons)
        message_tracker.register_message(filename, msg, MessageType.PROGRESS, user_id)
        await event.answer("Created progress view")
    except Exception as e:
        log.debug("Failed to create info message: %s", e)
        await event.answer("Failed to create progress view", alert=True)


def _get_status_text(state):
    """Get status text for download state."""
    if state.cancelled:
        return "🛑 Cancelled"
    if state.completed:
        return "✅ Completed"
    if state.paused:
        return "⏸️ Paused"
    return "⏬ Downloading"


def _build_downloads_list(active_states):
    """Build downloads list content."""
    lines = ["📁 Active Downloads:"]
    buttons = []

    for i, (filename, state) in enumerate(active_states.items(), 1):
        if state.cancelled or state.completed:
            continue

        if state.paused:
            line = f"{i}. {filename} (Paused)"
        elif state.progress_percent > 0 and state.downloaded_bytes > 0:
            downloaded_size = utils.humanize_size(state.downloaded_bytes)
            total_size = utils.humanize_size(state.size)
            line = f"{i}. {filename} ({state.progress_percent}% - {downloaded_size}/{total_size})"
        else:
            line = f"{i}. {filename} (Starting...)"

        lines.append(line)

        file_id = get_file_id(filename)
        row_buttons = [Button.inline("📊 Info", data=f"info:{file_id}")]
        if state.paused:
            row_buttons.append(Button.inline("▶️ Resume", data=f"resume:{file_id}"))
        else:
            row_buttons.append(Button.inline("⏸️ Pause", data=f"pause:{file_id}"))
        row_buttons.append(Button.inline("🛑 Cancel", data=f"cancel:{file_id}"))
        buttons.append(row_buttons)

    if len(lines) == 1:
        lines = ["📁 No active downloads"]

    buttons.append([Button.inline("🔄 Refresh", data="refresh_downloads")])
    return "\n".join(lines), buttons


def _build_queue_list(queue_items):
    """Build queue list content."""
    lines = ["📝 Queued Downloads:"]
    buttons = []

    for i, (filename, qi) in enumerate(queue_items.items(), 1):
        lines.append(f"**{i}.** 🕒 {filename}")
        if qi.file_id:
            buttons.append([Button.inline("🛑 Cancel", data=f"qcancel:{qi.file_id}")])
        else:
            buttons.append([Button.inline("❌ No Action", data="no_action")])

    buttons.append([Button.inline("🔄 Refresh", data="refresh_queue")])
    return "\n".join(lines), buttons


def _handle_existing_lists_for_new_download(filename: str):
    """Register a new download with existing list messages."""
    for tracked_msg in message_tracker.get_all_list_messages():
        with contextlib.suppress(Exception):
            message_tracker.register_message(
                filename, tracked_msg.message, tracked_msg.message_type, tracked_msg.user_id
            )


__all__ = ["register_list_handlers"]
