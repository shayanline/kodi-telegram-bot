from __future__ import annotations

import contextlib

from telethon import Button, TelegramClient, events

import config
import throttle
import utils

from .buttons import build_buttons
from .ids import get_file_id
from .queue import queue
from .state import MessageType, find_pending_deletion, message_tracker, resolve_file_id, states


def register_list_handlers(client: TelegramClient):
    """Register handlers for list commands."""
    _register_downloads_handler(client)
    _register_queue_handler(client)
    _register_list_callbacks(client)
    _register_noop_handler(client)


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

        message_tracker.register_message("__downloads_list__", msg, MessageType.DOWNLOAD_LIST, user_id)
        message_tracker.trim_list_messages("__downloads_list__")
        for filename in states:
            message_tracker.register_message(filename, msg, MessageType.DOWNLOAD_LIST, user_id)


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

        message_tracker.register_message("__queue_list__", msg, MessageType.QUEUE_LIST, user_id)
        message_tracker.trim_list_messages("__queue_list__")
        for filename in queue.items:
            message_tracker.register_message(filename, msg, MessageType.QUEUE_LIST, user_id)


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

    @client.on(events.CallbackQuery(pattern=b"info:"))
    @throttle.serialized
    async def _info(event):
        await _handle_info_callback(event)


async def _handle_info_callback(event):
    """Handle info button callback."""
    file_id = event.data.decode().split(":", 1)[1]
    filename = resolve_file_id(file_id)
    if not filename:
        await throttle.answer_callback(event, "Download completed or no longer active", alert=False)
        return

    state = states.get(filename)
    if not state:
        await throttle.answer_callback(event, "Download completed or no longer active", alert=False)
        return

    await _create_info_message(event, filename, state)


async def _create_info_message(event, filename, state):
    """Create a new info/progress message for the user.

    When the download is waiting for disk space, restores the interactive
    deletion prompt so the user can respond even if the original message
    was lost.
    """
    sender = await event.get_sender()
    user_id = getattr(sender, "id", None)

    if state.waiting_for_space:
        result = find_pending_deletion(filename)
        if result:
            pid, pending = result
            text = f"⏳ Waiting for space: {filename}\nDelete oldest candidate: {pending.candidate}?"
            buttons = [
                [
                    Button.inline("✅ Yes", data=f"delok:{pid}"),
                    Button.inline("❌ No", data=f"delnx:{pid}"),
                ]
            ]
        else:
            text = f"⏳ Checking disk space for {filename}..."
            buttons = None
        msg = await throttle.send_message(event, text, buttons=buttons)
        if msg:
            if result:
                # Redirect deletion handler edits to this message so the user
                # sees feedback here and _ensure_disk_space continues on it.
                result[1].message = msg
            await throttle.answer_callback(event, "Space prompt restored")
        else:
            await throttle.answer_callback(event, "Failed to create view", alert=True)
        return

    status = get_status_text(state)
    text = f"{status}: {filename}"
    buttons = build_buttons(state)

    msg = await throttle.send_message(event, text, buttons=buttons)
    if msg:
        message_tracker.register_message(filename, msg, MessageType.PROGRESS, user_id)
        await throttle.answer_callback(event, "Created progress view")
    else:
        await throttle.answer_callback(event, "Failed to create progress view", alert=True)


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
        row_buttons = [Button.inline("📊 Info", data=f"info:{file_id}")]
        if state.waiting_for_space:
            row_buttons.append(Button.inline("🛑 Cancel", data=f"lcancel:{file_id}"))
        else:
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
        if qi.file_id:
            buttons.append([Button.inline("🛑 Cancel", data=f"lqcancel:{qi.file_id}")])
        else:
            buttons.append([Button.inline("❌ No Action", data="no_action")])

    buttons.append([Button.inline("🔄 Refresh", data="refresh_queue")])
    return "\n".join(lines), buttons


def handle_existing_lists_for_new_download(filename: str):
    """Register a new download with existing list messages."""
    for tracked_msg in message_tracker.get_all_list_messages():
        with contextlib.suppress(Exception):
            message_tracker.register_message(
                filename, tracked_msg.message, tracked_msg.message_type, tracked_msg.user_id
            )


async def update_all_download_lists():
    """Edit every tracked /downloads list message with the current state."""
    for tracked in message_tracker.get_messages("__downloads_list__", MessageType.DOWNLOAD_LIST):
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


def _register_noop_handler(client: TelegramClient):
    @client.on(events.CallbackQuery(pattern=b"no_action"))
    @throttle.serialized
    async def _noop(event):
        await throttle.answer_callback(event)


__all__ = [
    "build_downloads_list",
    "build_queue_list",
    "get_status_text",
    "handle_existing_lists_for_new_download",
    "register_list_handlers",
    "update_all_download_lists",
]
