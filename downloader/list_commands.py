"""Unified download list: single paginated message per chat.

Combines active downloads and queued items into one interactive view.
Handles all UI callbacks (pause, resume, cancel, cancel-all, pagination).
"""

from __future__ import annotations

import contextlib

from telethon import Button, TelegramClient, events

import config
import throttle
import utils
from logger import log

from .ids import get_file_id
from .queue import QueuedItem, queue
from .state import (
    ChatDownloadList,
    DownloadState,
    chat_lists,
    file_id_map,
    find_pending_deletion,
    resolve_file_id,
    states,
)

PAGE_SIZE = 8
_NOT_FOUND = "Download completed or no longer active"


# ── List rendering ──


def _active_items() -> list[tuple[str, DownloadState]]:
    """Return active (non-completed, non-cancelled) downloads."""
    return [(fn, st) for fn, st in states.items() if not st.cancelled and not st.completed]


def _queued_items() -> list[tuple[str, QueuedItem]]:
    """Return queued items in order."""
    return list(queue.items.items())


def _total_items() -> int:
    return len(_active_items()) + len(_queued_items())


def _total_pages() -> int:
    total = _total_items()
    if total == 0:
        return 1
    return (total + PAGE_SIZE - 1) // PAGE_SIZE


def build_unified_list(page: int = 0) -> tuple[str, list]:
    """Build the unified download + queue list for the given page."""
    active = _active_items()
    queued = _queued_items()
    all_items: list[tuple[str, str, DownloadState | QueuedItem]] = []

    for fn, st in active:
        all_items.append((fn, "active", st))
    for fn, qi in queued:
        all_items.append((fn, "queued", qi))

    total_pages = _total_pages()
    page = max(0, min(page, total_pages - 1))

    if not all_items:
        return "📥 No active downloads or queued items.", []

    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    page_items = all_items[start:end]

    lines = [f"📥 Downloads & Queue ({page + 1}/{total_pages})", ""]
    buttons: list[list] = []

    for idx, (fn, kind, item) in enumerate(page_items, start=start + 1):
        if kind == "active" and isinstance(item, DownloadState):
            lines.append(_format_active_line(idx, fn, item))
            buttons.append(_active_buttons(fn, item, idx))
        elif isinstance(item, QueuedItem):
            qi_pos = _queue_position(fn)
            lines.append(f"{idx}. 🕒 {fn} — Queued #{qi_pos}")
            buttons.append(_queued_buttons(fn, item, idx))

    # Navigation row
    nav_row: list = []
    if total_pages > 1 and page > 0:
        nav_row.append(Button.inline("◀ Prev", data=f"dl_page:{page - 1}"))
    nav_row.append(Button.inline("🛑 Cancel All", data="dl_cancelall"))
    if total_pages > 1 and page < total_pages - 1:
        nav_row.append(Button.inline("▶ Next", data=f"dl_page:{page + 1}"))
    buttons.append(nav_row)

    return "\n".join(lines), buttons


def _format_active_line(idx: int, fn: str, st: DownloadState) -> str:
    if st.waiting_for_space:
        return f"{idx}. ⏳ {fn} — Waiting for space"
    if st.paused:
        pct = f"{st.progress_percent}%" if st.progress_percent > 0 else ""
        dl = utils.humanize_size(st.downloaded_bytes) if st.downloaded_bytes > 0 else ""
        parts = [x for x in [pct, dl] if x]
        detail = f" ({', '.join(parts)})" if parts else ""
        return f"{idx}. ⏸️ {fn} — Paused{detail}"
    if st.progress_percent > 0 and st.downloaded_bytes > 0:
        dl = utils.humanize_size(st.downloaded_bytes)
        total = utils.humanize_size(st.size)
        return f"{idx}. ⏬ {fn} — {st.progress_percent}% ({dl}/{total}) @ {st.speed}/s"
    return f"{idx}. ⏬ {fn} — Starting..."


def _status_icon(st: DownloadState) -> str:
    """Return a status emoji for a DownloadState."""
    if st.waiting_for_space:
        return "⏳"
    if st.paused:
        return "⏸"
    return "⏬"


def _active_buttons(fn: str, st: DownloadState, idx: int) -> list:
    file_id = get_file_id(fn)
    num_btn = Button.inline(f"{idx} {_status_icon(st)}", data=f"dl_info:{file_id}")
    cancel_btn = Button.inline("🛑 Cancel", data=f"dl_cancel:{file_id}")
    if st.waiting_for_space:
        spacer = Button.inline("⏳", data=f"dl_info:{file_id}")
        return [num_btn, spacer, cancel_btn]
    if st.paused:
        return [num_btn, Button.inline("▶️ Resume", data=f"dl_resume:{file_id}"), cancel_btn]
    return [num_btn, Button.inline("⏸️ Pause", data=f"dl_pause:{file_id}"), cancel_btn]


def _queued_buttons(fn: str, qi: QueuedItem, idx: int) -> list:
    file_id = qi.file_id or get_file_id(fn)
    num_btn = Button.inline(f"{idx} 🕒", data=f"dl_info:{file_id}")
    return [num_btn, Button.inline("🛑 Cancel", data=f"dl_qcancel:{file_id}")]


def _queue_position(filename: str) -> int:
    """Return 1-based queue position for a filename."""
    for i, fn in enumerate(queue.items, start=1):
        if fn == filename:
            return i
    return 0


# ── Per-chat list management ──


async def update_all_lists() -> None:
    """Update all tracked chat list messages with current state."""
    for chat_id, cl in list(chat_lists.items()):
        if cl.confirming or cl.message is None:
            continue
        try:
            text, buttons = build_unified_list(cl.page)
            result = await throttle.edit_message(cl.message, text, buttons=buttons)
            if result is None:
                # Edit failed (message too old or deleted) — send replacement
                new_msg = await throttle.send_message(cl.message, text, buttons=buttons)
                if new_msg:
                    cl.message = new_msg
                else:
                    chat_lists.pop(chat_id, None)
        except Exception:
            pass


async def _send_list_message(event, chat_id: int) -> None:
    """Send a new download list message and track it for this chat."""
    cl = chat_lists.get(chat_id)
    if cl and cl.message:
        with contextlib.suppress(Exception):
            await cl.message.delete()

    text, buttons = build_unified_list(page=0)
    msg = await throttle.send_message(event, text, buttons=buttons)
    if msg:
        chat_lists[chat_id] = ChatDownloadList(chat_id=chat_id, message=msg, page=0)


# ── Handler registration ──


def register_list_handlers(client: TelegramClient):
    """Register the /downloads command and all list-related callbacks."""
    _register_downloads_handler(client)
    _register_list_callbacks(client)
    _register_control_callbacks(client)


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
        chat_id = event.chat_id or user_id or 0
        await _send_list_message(event, chat_id)


def _register_list_callbacks(client: TelegramClient):
    @client.on(events.CallbackQuery(pattern=b"dl_page:"))
    @throttle.serialized
    async def _page(event):
        data = event.data.decode()
        page = int(data.split(":", 1)[1])
        chat_id = event.chat_id or event.sender_id or 0
        cl = chat_lists.get(chat_id)
        if cl:
            cl.page = page
            cl.confirming = None
        text, buttons = build_unified_list(page)
        await throttle.edit_message(event, text, buttons=buttons)
        await throttle.answer_callback(event)


def _register_control_callbacks(client: TelegramClient):
    _register_info_noop(client)
    _register_pause_resume(client)
    _register_cancel(client)
    _register_cancel_confirm(client)
    _register_qcancel(client)
    _register_qcancel_confirm(client)
    _register_cancel_all(client)
    _register_cancel_all_confirm(client)


def _register_info_noop(client: TelegramClient):
    @client.on(events.CallbackQuery(pattern=b"dl_info:"))
    async def _info(event):
        await throttle.answer_callback(event, alert=False)


def _register_pause_resume(client: TelegramClient):
    @client.on(events.CallbackQuery(pattern=b"dl_(pause|resume):"))
    @throttle.serialized
    async def _prc(event):
        data = event.data.decode()
        action, file_id = data.split(":", 1)
        filename = resolve_file_id(file_id)
        if not filename:
            await throttle.answer_callback(event, _NOT_FOUND, alert=False)
            return
        st = states.get(filename)
        if not st or st.cancelled:
            await throttle.answer_callback(event, _NOT_FOUND, alert=False)
            return
        if action == "dl_pause":
            if st.paused:
                await throttle.answer_callback(event, "Already paused", alert=False)
                return
            st.mark_paused()
            await throttle.answer_callback(event, "Paused")
        else:
            if not st.paused:
                await throttle.answer_callback(event, "Not paused", alert=False)
                return
            st.mark_resumed()
            await throttle.answer_callback(event, "Resuming")
        await _refresh_caller_list(event)


def _register_cancel(client: TelegramClient):
    @client.on(events.CallbackQuery(pattern=b"dl_cancel:"))
    @throttle.serialized
    async def _cancel(event):
        file_id = event.data.decode().split(":", 1)[1]
        filename = resolve_file_id(file_id)
        if not filename:
            await throttle.answer_callback(event, _NOT_FOUND, alert=False)
            return
        st = states.get(filename)
        if not st or st.cancelled:
            await throttle.answer_callback(event, _NOT_FOUND, alert=False)
            return
        # Show confirmation
        chat_id = event.chat_id or event.sender_id or 0
        cl = chat_lists.get(chat_id)
        if cl:
            cl.confirming = file_id
        text = f"⚠️ **Cancel this download?**\n\n{filename}"
        buttons = [
            [
                Button.inline("✅ Yes, Cancel", data=f"dl_cy:{file_id}"),
                Button.inline("❌ No, Go Back", data=f"dl_cn:{file_id}"),
            ]
        ]
        await throttle.edit_message(event, text, buttons=buttons)
        await throttle.answer_callback(event)


def _register_cancel_confirm(client: TelegramClient):
    @client.on(events.CallbackQuery(pattern=b"dl_c(y|n):"))
    @throttle.serialized
    async def _cancel_confirm(event):
        data = event.data.decode()
        colon_idx = data.index(":")
        prefix = data[:colon_idx]
        file_id = data[colon_idx + 1 :]
        confirmed = prefix == "dl_cy"

        chat_id = event.chat_id or event.sender_id or 0
        cl = chat_lists.get(chat_id)
        if cl:
            cl.confirming = None

        if confirmed:
            filename = resolve_file_id(file_id)
            if filename:
                st = states.get(filename)
                if st and not st.cancelled:
                    st.mark_cancelled()
                    _unblock_pending_deletion(filename)
                    await throttle.answer_callback(event, "Cancelling")
                    await _refresh_caller_list(event)
                    return
            await throttle.answer_callback(event, _NOT_FOUND, alert=False)
        else:
            await throttle.answer_callback(event)

        await _refresh_caller_list(event)


def _register_qcancel(client: TelegramClient):
    @client.on(events.CallbackQuery(pattern=b"dl_qcancel:"))
    @throttle.serialized
    async def _qcancel(event):
        file_id = event.data.decode().split(":", 1)[1]
        filename = resolve_file_id(file_id)
        if not filename:
            await throttle.answer_callback(event, _NOT_FOUND, alert=False)
            return

        # Item may have moved to active downloads by now
        qi = queue.items.get(filename)
        if not qi or qi.cancelled:
            st = states.get(filename)
            if st and not st.cancelled:
                # Redirect to active cancel confirmation
                chat_id = event.chat_id or event.sender_id or 0
                cl = chat_lists.get(chat_id)
                if cl:
                    cl.confirming = file_id
                text = f"⚠️ **Cancel this download?**\n\n{filename}"
                buttons = [
                    [
                        Button.inline("✅ Yes, Cancel", data=f"dl_cy:{file_id}"),
                        Button.inline("❌ No, Go Back", data=f"dl_cn:{file_id}"),
                    ]
                ]
                await throttle.edit_message(event, text, buttons=buttons)
                await throttle.answer_callback(event)
                return
            await throttle.answer_callback(event, _NOT_FOUND, alert=False)
            return

        # Show confirmation for queued item
        chat_id = event.chat_id or event.sender_id or 0
        cl = chat_lists.get(chat_id)
        if cl:
            cl.confirming = file_id
        text = f"⚠️ **Cancel this queued download?**\n\n{filename}"
        buttons = [
            [
                Button.inline("✅ Yes, Cancel", data=f"dl_qcy:{file_id}"),
                Button.inline("❌ No, Go Back", data=f"dl_qcn:{file_id}"),
            ]
        ]
        await throttle.edit_message(event, text, buttons=buttons)
        await throttle.answer_callback(event)


def _register_qcancel_confirm(client: TelegramClient):
    @client.on(events.CallbackQuery(pattern=b"dl_qc(y|n):"))
    @throttle.serialized
    async def _qcancel_confirm(event):
        data = event.data.decode()
        colon_idx = data.index(":")
        prefix = data[:colon_idx]
        file_id = data[colon_idx + 1 :]
        confirmed = prefix == "dl_qcy"

        chat_id = event.chat_id or event.sender_id or 0
        cl = chat_lists.get(chat_id)
        if cl:
            cl.confirming = None

        if confirmed:
            filename = resolve_file_id(file_id)
            if filename:
                # Try queue cancel first
                if queue.cancel(filename):
                    file_id_map.pop(file_id, None)
                    await throttle.answer_callback(event, "Cancelled")
                    await _refresh_caller_list(event)
                    await update_all_lists()
                    return
                # May have started — cancel active
                st = states.get(filename)
                if st and not st.cancelled:
                    st.mark_cancelled()
                    _unblock_pending_deletion(filename)
                    await throttle.answer_callback(event, "Cancelling")
                    await _refresh_caller_list(event)
                    return
            await throttle.answer_callback(event, _NOT_FOUND, alert=False)
        else:
            await throttle.answer_callback(event)

        await _refresh_caller_list(event)


def _register_cancel_all(client: TelegramClient):
    @client.on(events.CallbackQuery(pattern=b"dl_cancelall"))
    @throttle.serialized
    async def _cancel_all(event):
        active = [fn for fn, st in states.items() if not st.cancelled and not st.completed]
        queued = list(queue.items)
        total = len(active) + len(queued)
        if total == 0:
            await throttle.answer_callback(event, "Nothing to cancel", alert=False)
            return
        chat_id = event.chat_id or event.sender_id or 0
        cl = chat_lists.get(chat_id)
        if cl:
            cl.confirming = "all"
        text = f"⚠️ **Cancel all {total} downloads?**"
        buttons = [
            [
                Button.inline("✅ Yes, Cancel All", data="dl_cay"),
                Button.inline("❌ No, Go Back", data="dl_can"),
            ]
        ]
        await throttle.edit_message(event, text, buttons=buttons)
        await throttle.answer_callback(event)


def _register_cancel_all_confirm(client: TelegramClient):
    @client.on(events.CallbackQuery(pattern=rb"dl_ca(y|n)"))
    @throttle.serialized
    async def _cancel_all_confirm(event):
        choice = event.data.decode()
        confirmed = choice == "dl_cay"
        chat_id = event.chat_id or event.sender_id or 0
        cl = chat_lists.get(chat_id)
        if cl:
            cl.confirming = None
        if confirmed:
            cancelled = 0
            for fn, st in list(states.items()):
                if not st.cancelled and not st.completed:
                    st.mark_cancelled()
                    _unblock_pending_deletion(fn)
                    cancelled += 1
            for fn in list(queue.items):
                if queue.cancel(fn):
                    fid = get_file_id(fn)
                    file_id_map.pop(fid, None)
                    cancelled += 1
            await throttle.answer_callback(event, f"Cancelled {cancelled} downloads")
            log.info("Cancel-all: cancelled %d downloads", cancelled)
        else:
            await throttle.answer_callback(event)
        await _refresh_caller_list(event)


def _unblock_pending_deletion(filename: str) -> None:
    """Resolve any active pending deletion future for *filename*."""
    result = find_pending_deletion(filename)
    if result:
        _pid, pending = result
        if not pending.future.done():
            pending.choice = "no"
            with contextlib.suppress(Exception):
                pending.future.set_result(True)


async def _refresh_caller_list(event) -> None:
    """Refresh the list message for the chat that triggered the callback."""
    chat_id = event.chat_id or event.sender_id or 0
    cl = chat_lists.get(chat_id)
    page = cl.page if cl else 0
    text, buttons = build_unified_list(page)
    await throttle.edit_message(event, text, buttons=buttons)


__all__ = [
    "build_unified_list",
    "register_list_handlers",
    "update_all_lists",
]
