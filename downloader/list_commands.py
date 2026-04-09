from __future__ import annotations

import contextlib

from telethon import Button, TelegramClient, events

import config
import throttle
import utils

from .ids import get_file_id
from .queue import queue
from .state import MessageType, frozen_list_msg_ids, message_tracker, states


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
    @throttle.serialized
    async def _downloads(event):
        sender = await event.get_sender()
        user_id = getattr(sender, "id", None)
        username = getattr(sender, "username", None)

        if not config.is_user_allowed(user_id, username):
            await throttle.send_message(event, "🛑 Not authorized.")
            return

        if not states:
            text = "📁 No active downloads"
            buttons = [[Button.inline("🔄 Refresh", data="refresh_downloads")]]
            msg = await throttle.send_message(event, text, buttons=buttons)
        else:
            text, buttons = build_downloads_list(states)
            msg = await throttle.send_message(event, text, buttons=buttons)

        message_tracker.register_message("__downloads_list__", msg, MessageType.DOWNLOAD_LIST)
        message_tracker.trim_list_messages("__downloads_list__")
        for filename in states:
            message_tracker.register_message(filename, msg, MessageType.DOWNLOAD_LIST)


def _register_queue_handler(client: TelegramClient):
    @client.on(
        events.NewMessage(
            func=lambda e: e.is_private and not e.document and (e.raw_text or "").strip().lower() == "/queue"
        )
    )
    @throttle.serialized
    async def _queue_list(event):
        sender = await event.get_sender()
        user_id = getattr(sender, "id", None)
        username = getattr(sender, "username", None)

        if not config.is_user_allowed(user_id, username):
            await throttle.send_message(event, "🛑 Not authorized.")
            return

        if not queue.items:
            text = "📝 No queued downloads"
            buttons = [[Button.inline("🔄 Refresh", data="refresh_queue")]]
            msg = await throttle.send_message(event, text, buttons=buttons)
        else:
            text, buttons = build_queue_list(queue.items)
            msg = await throttle.send_message(event, text, buttons=buttons)

        message_tracker.register_message("__queue_list__", msg, MessageType.QUEUE_LIST)
        message_tracker.trim_list_messages("__queue_list__")
        for filename in queue.items:
            message_tracker.register_message(filename, msg, MessageType.QUEUE_LIST)


def _register_list_callbacks(client: TelegramClient):
    @client.on(events.CallbackQuery(pattern=b"refresh_downloads"))
    @throttle.serialized
    async def _refresh_downloads(event):
        if not states:
            text = "📁 No active downloads"
            buttons = [[Button.inline("🔄 Refresh", data="refresh_downloads")]]
            await throttle.edit_message(event, text, buttons=buttons)
        else:
            text, buttons = build_downloads_list(states)
            await throttle.edit_message(event, text, buttons=buttons)

        await throttle.answer_callback(event, "Refreshed")

    @client.on(events.CallbackQuery(pattern=b"refresh_queue"))
    @throttle.serialized
    async def _refresh_queue(event):
        if not queue.items:
            text = "📝 No queued downloads"
            buttons = [[Button.inline("🔄 Refresh", data="refresh_queue")]]
            await throttle.edit_message(event, text, buttons=buttons)
        else:
            text, buttons = build_queue_list(queue.items)
            await throttle.edit_message(event, text, buttons=buttons)

        await throttle.answer_callback(event, "Refreshed")


def get_status_text(state):
    """Get status text for download state."""
    if state.cancelled:
        return "🛑 Cancelled"
    if state.completed:
        return "✅ Completed"
    if state.waiting_for_space:
        return "⏳ Waiting for space"
    if state.paused:
        return "⏸️ Paused"
    return "⏬ Downloading"


def build_downloads_list(active_states):
    """Build downloads list content."""
    lines = ["📁 Active Downloads:"]
    buttons = []

    display_num = 0
    for filename, state in active_states.items():
        if state.cancelled or state.completed:
            continue

        display_num += 1
        if state.waiting_for_space:
            line = f"{display_num}. {filename} (Waiting for space...)"
        elif state.paused:
            line = f"{display_num}. {filename} (Paused)"
        elif state.progress_percent > 0 and state.downloaded_bytes > 0:
            downloaded_size = utils.humanize_size(state.downloaded_bytes)
            total_size = utils.humanize_size(state.size)
            line = f"{display_num}. {filename} ({state.progress_percent}% - {downloaded_size}/{total_size})"
        else:
            line = f"{display_num}. {filename} (Starting...)"

        lines.append(line)

        file_id = get_file_id(filename)
        row_buttons = []
        if not state.waiting_for_space:
            if state.paused:
                row_buttons.append(Button.inline("▶️ Resume", data=f"resume:{file_id}"))
            else:
                row_buttons.append(Button.inline("⏸️ Pause", data=f"pause:{file_id}"))
        row_buttons.append(Button.inline("🛑 Cancel", data=f"lcancel:{file_id}"))
        buttons.append(row_buttons)

    if len(lines) == 1:
        lines = ["📁 No active downloads"]

    buttons.append([Button.inline("🔄 Refresh", data="refresh_downloads")])
    return "\n".join(lines), buttons


def build_queue_list(queue_items):
    """Build queue list content."""
    lines = ["📝 Queued Downloads:"]
    buttons = []

    for i, (filename, qi) in enumerate(queue_items.items(), 1):
        lines.append(f"**{i}.** 🕒 {filename}")
        buttons.append([Button.inline("🛑 Cancel", data=f"lqcancel:{qi.file_id}")])

    buttons.append([Button.inline("🔄 Refresh", data="refresh_queue")])
    return "\n".join(lines), buttons


def handle_existing_lists_for_new_download(filename: str):
    """Register a new download with existing list messages."""
    for tracked_msg in message_tracker.get_all_list_messages():
        with contextlib.suppress(Exception):
            message_tracker.register_message(filename, tracked_msg.message, tracked_msg.message_type)


async def update_all_download_lists():
    """Edit every tracked /downloads list message with the current state."""
    for tracked in message_tracker.get_messages("__downloads_list__", MessageType.DOWNLOAD_LIST):
        if tracked.message.id in frozen_list_msg_ids:
            continue
        try:
            if states:
                text, buttons = build_downloads_list(states)
            else:
                text = "📁 No active downloads"
                buttons = [[Button.inline("🔄 Refresh", data="refresh_downloads")]]
            new_msg = await throttle.edit_message(tracked.message, text, buttons=buttons)
            if new_msg and new_msg is not tracked.message:
                tracked.message = new_msg
        except Exception:
            pass


__all__ = [
    "build_downloads_list",
    "build_queue_list",
    "get_status_text",
    "handle_existing_lists_for_new_download",
    "register_list_handlers",
    "update_all_download_lists",
]
