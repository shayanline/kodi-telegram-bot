"""Interactive Telegram file manager for browsing and deleting downloaded files.

Provides a `/files` command that opens a single-message UI with inline buttons.
Users can navigate folders, view disk usage, and delete files/folders — all via
message edits and callback queries. Designed for large collections with pagination.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
from datetime import UTC, datetime

from telethon import Button, TelegramClient, events
from telethon.errors import MessageNotModifiedError

import config
import utils
from downloader.queue import queue
from downloader.state import states
from logger import log

_ITEMS_PER_PAGE = 10
_MAX_BTN_PER_ROW = 5
_DEFAULT_SORT = "S"
_MAX_NAME_LEN = 45
_SORT_LABELS = {"S": "📊 Size", "N": "🔤 Name", "D": "📅 Date"}
_SORT_NAMES = {"S": "size", "N": "name", "D": "date"}

# Maps 8-char hash → relative path (populated as the user browses).
_path_registry: dict[str, str] = {}

_EXPIRED = "Session expired — use /files again"


def _path_id(relpath: str) -> str:
    """Return 8-char hash for a relative path and register it."""
    pid = hashlib.md5(relpath.encode()).hexdigest()[:8]
    _path_registry[pid] = relpath
    return pid


def _resolve(pid: str) -> str | None:
    """Resolve a path hash to an absolute path within DOWNLOAD_DIR."""
    relpath = _path_registry.get(pid)
    if relpath is None:
        return None
    abspath = os.path.normpath(os.path.join(config.DOWNLOAD_DIR, relpath))
    base = os.path.abspath(config.DOWNLOAD_DIR)
    if not (abspath == base or abspath.startswith(base + os.sep)):
        return None
    return abspath


def _is_protected(abspath: str) -> bool:
    """Return True if the path is being actively downloaded or queued."""
    norm = os.path.abspath(abspath)
    if any(os.path.abspath(st.path) == norm for st in states.values()):
        return True
    return any(os.path.abspath(qi.path) == norm for qi in queue.items.values())


def _is_protected_recursive(abspath: str) -> bool:
    """Return True if any file under abspath is being downloaded or queued."""
    if os.path.isfile(abspath):
        return _is_protected(abspath)
    prefix = os.path.abspath(abspath) + os.sep
    if any(os.path.abspath(st.path).startswith(prefix) for st in states.values()):
        return True
    return any(os.path.abspath(qi.path).startswith(prefix) for qi in queue.items.values())


def _dir_summary(abspath: str) -> tuple[int, int]:
    """Return (file_count, total_bytes) for a directory, recursively."""
    count = 0
    total = 0
    try:
        for root, _dirs, files in os.walk(abspath):
            for f in files:
                count += 1
                with contextlib.suppress(OSError):
                    total += os.path.getsize(os.path.join(root, f))
    except OSError:
        pass
    return count, total


def _entry_size(abspath: str) -> int:
    """Return size of a file or total size of a directory."""
    if os.path.isfile(abspath):
        try:
            return os.path.getsize(abspath)
        except OSError:
            return 0
    _, total = _dir_summary(abspath)
    return total


def _entry_mtime(abspath: str) -> float:
    """Return modification time of a file or directory."""
    try:
        return os.stat(abspath).st_mtime
    except OSError:
        return 0.0


def _disk_bar(path: str) -> str:
    """Return a text-based disk usage bar."""
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return "💾 Disk info unavailable"
    used_pct = usage.used / usage.total * 100 if usage.total else 0
    filled = round(used_pct / 100 * 16)
    bar = "█" * filled + "░" * (16 - filled)
    return (
        f"💾 {bar} {used_pct:.0f}%\n"
        f"   {utils.humanize_size(usage.used)} / {utils.humanize_size(usage.total)} used"
        f" · {utils.humanize_size(usage.free)} free"
    )


def _sorted_entries(abspath: str, sort: str = _DEFAULT_SORT) -> list[str]:
    """Return directory entries sorted by the given mode."""
    try:
        entries = list(os.scandir(abspath))
    except OSError:
        return []
    if sort == "N":
        entries.sort(key=lambda e: e.name.lower())
    elif sort == "D":
        entries.sort(key=lambda e: _entry_mtime(e.path), reverse=True)
    else:
        entries.sort(key=lambda e: _entry_size(e.path), reverse=True)
    return [e.name for e in entries]


def _truncate(name: str) -> str:
    """Truncate a filename for display if it exceeds _MAX_NAME_LEN."""
    return name if len(name) <= _MAX_NAME_LEN else name[: _MAX_NAME_LEN - 1] + "\u2026"


def _chunk_rows(items: list[Button]) -> list[list[Button]]:
    """Split a flat list of buttons into rows of _MAX_BTN_PER_ROW."""
    return [items[i : i + _MAX_BTN_PER_ROW] for i in range(0, len(items), _MAX_BTN_PER_ROW)]


def _sort_row(current: str, data_prefix: str) -> list[Button]:
    """Build inline sort toggle buttons for the current view."""
    return [
        Button.inline(f"{label} \u2713" if mode == current else label, data=f"{data_prefix}{mode}")
        for mode, label in _SORT_LABELS.items()
    ]


def _render_root(page: int = 1, sort: str = _DEFAULT_SORT) -> tuple[str, list[list[Button]]]:
    """Build the root dashboard view with pagination."""
    lines = ["📂 **File Manager**", "━━━━━━━━━━━━━━━━━━━━━", _disk_bar(config.DOWNLOAD_DIR), ""]
    buttons: list[list[Button]] = []

    entries = _sorted_entries(config.DOWNLOAD_DIR, sort)
    if not entries:
        lines.append("📭 Download folder is empty")
        buttons.append([Button.inline("🔄 Refresh", data=f"f:r:1:{sort}")])
        return "\n".join(lines), buttons

    total_items = len(entries)
    total_pages = max(1, (total_items + _ITEMS_PER_PAGE - 1) // _ITEMS_PER_PAGE)
    page = max(1, min(page, total_pages))

    page_info = f"  (page {page}/{total_pages})" if total_pages > 1 else ""
    lines.append(f"{total_items} item{'s' if total_items != 1 else ''} · by {_SORT_NAMES[sort]}{page_info}")
    lines.append("")

    start = (page - 1) * _ITEMS_PER_PAGE
    page_entries = entries[start : start + _ITEMS_PER_PAGE]

    for i, name in enumerate(page_entries, start=start + 1):
        full = os.path.join(config.DOWNLOAD_DIR, name)
        if os.path.isdir(full):
            count, total = _dir_summary(full)
            lines.append(
                f"{i}. 📁 {_truncate(name)} — {count} item{'s' if count != 1 else ''}, {utils.humanize_size(total)}"
            )
        else:
            try:
                sz = os.path.getsize(full)
            except OSError:
                sz = 0
            lines.append(f"{i}. 📄 {_truncate(name)} — {utils.humanize_size(sz)}")

    nav_btns: list[Button] = []
    for i, name in enumerate(page_entries, start=start + 1):
        pid = _path_id(name)
        full = os.path.join(config.DOWNLOAD_DIR, name)
        is_dir = os.path.isdir(full)
        label = f"{i} 📁" if is_dir else f"{i} 📄"
        data = f"f:n:{pid}:1:{sort}" if is_dir else f"f:i:{pid}:{sort}"
        nav_btns.append(Button.inline(label, data=data))
    buttons.extend(_chunk_rows(nav_btns))

    buttons.append(_sort_row(sort, "f:r:1:"))

    if total_pages > 1:
        pag_row: list[Button] = []
        if page > 1:
            pag_row.append(Button.inline(f"◀️ {page - 1}", data=f"f:r:{page - 1}:{sort}"))
        pag_row.append(Button.inline(f"{page}/{total_pages}", data="f:noop"))
        if page < total_pages:
            pag_row.append(Button.inline(f"{page + 1} ▶️", data=f"f:r:{page + 1}:{sort}"))
        buttons.append(pag_row)

    buttons.append([Button.inline("🔄 Refresh", data=f"f:r:{page}:{sort}")])
    return "\n".join(lines), buttons


def _render_dir(relpath: str, page: int, sort: str = _DEFAULT_SORT) -> tuple[str, list[list[Button]]]:
    """Build a paginated directory listing view."""
    abspath = os.path.normpath(os.path.join(config.DOWNLOAD_DIR, relpath))
    dirname = os.path.basename(abspath)
    entries = _sorted_entries(abspath, sort)

    total_items = len(entries)
    total_pages = max(1, (total_items + _ITEMS_PER_PAGE - 1) // _ITEMS_PER_PAGE)
    page = max(1, min(page, total_pages))

    _, total_bytes = _dir_summary(abspath)

    lines = [f"📂 **{_truncate(dirname)}**", "━━━━━━━━━━━━━━━━━━━━━"]
    page_info = f"  (page {page}/{total_pages})" if total_pages > 1 else ""
    lines.append(
        f"{total_items} item{'s' if total_items != 1 else ''} · {utils.humanize_size(total_bytes)} total"
        f" · by {_SORT_NAMES[sort]}{page_info}"
    )
    lines.append("")

    buttons: list[list[Button]] = []
    pid_self = _path_id(relpath)

    if not entries:
        lines.append("📭 Empty folder")
        parent_relpath = os.path.dirname(relpath)
        back_data = f"f:n:{_path_id(parent_relpath)}:1:{sort}" if parent_relpath else f"f:r:1:{sort}"
        buttons.append(
            [Button.inline("⬅️ Back", data=back_data), Button.inline("🗑 Delete Folder", data=f"f:d:{pid_self}:{sort}")]
        )
        return "\n".join(lines), buttons

    start = (page - 1) * _ITEMS_PER_PAGE
    page_entries = entries[start : start + _ITEMS_PER_PAGE]

    nav_btns: list[Button] = []
    del_btns: list[Button] = []
    for i, name in enumerate(page_entries, start=start + 1):
        full = os.path.join(abspath, name)
        entry_rel = os.path.join(relpath, name)
        pid = _path_id(entry_rel)

        if os.path.isdir(full):
            _, sz = _dir_summary(full)
            lines.append(f"{i}. 📁 {_truncate(name)} — {utils.humanize_size(sz)}")
            nav_btns.append(Button.inline(f"{i} 📂", data=f"f:n:{pid}:1:{sort}"))
        else:
            try:
                sz = os.path.getsize(full)
            except OSError:
                sz = 0
            lines.append(f"{i}. 📄 {_truncate(name)} — {utils.humanize_size(sz)}")
            nav_btns.append(Button.inline(f"{i} 📄", data=f"f:i:{pid}:{sort}"))

        if _is_protected_recursive(full):
            del_btns.append(Button.inline(f"{i} 🔒", data="f:noop"))
        else:
            del_btns.append(Button.inline(f"{i} 🗑", data=f"f:d:{pid}:{sort}"))

    buttons.extend(_chunk_rows(nav_btns))
    buttons.extend(_chunk_rows(del_btns))

    buttons.append(_sort_row(sort, f"f:n:{pid_self}:1:"))

    if total_pages > 1:
        pag_row: list[Button] = []
        if page > 1:
            pag_row.append(Button.inline(f"◀️ {page - 1}", data=f"f:n:{pid_self}:{page - 1}:{sort}"))
        pag_row.append(Button.inline(f"{page}/{total_pages}", data="f:noop"))
        if page < total_pages:
            pag_row.append(Button.inline(f"{page + 1} ▶️", data=f"f:n:{pid_self}:{page + 1}:{sort}"))
        buttons.append(pag_row)

    parent_relpath = os.path.dirname(relpath)
    back_data = f"f:n:{_path_id(parent_relpath)}:1:{sort}" if parent_relpath else f"f:r:1:{sort}"
    bottom: list[Button] = [Button.inline("⬅️ Back", data=back_data)]
    if not _is_protected_recursive(abspath):
        bottom.append(Button.inline("🗑 Delete All", data=f"f:d:{pid_self}:{sort}"))
    bottom.append(Button.inline("🔄 Refresh", data=f"f:n:{pid_self}:{page}:{sort}"))
    buttons.append(bottom)

    return "\n".join(lines), buttons


def _render_file(relpath: str, sort: str = _DEFAULT_SORT) -> tuple[str, list[list[Button]]]:
    """Build a file detail view."""
    abspath = os.path.normpath(os.path.join(config.DOWNLOAD_DIR, relpath))
    name = os.path.basename(abspath)

    try:
        stat = os.stat(abspath)
        sz = stat.st_size
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=UTC).strftime("%Y-%m-%d %H:%M")
    except OSError:
        return "❌ File not found", [[Button.inline("⬅️ Back", data=f"f:r:1:{sort}")]]

    protected = _is_protected(abspath)

    lines = [f"📄 **{name}**", "━━━━━━━━━━━━━━━━━━━━━", f"📊 Size: {utils.humanize_size(sz)}", f"📅 Modified: {mtime}"]
    if protected:
        lines.append("🔒 Currently downloading — cannot delete")

    pid = _path_id(relpath)
    parent_relpath = os.path.dirname(relpath)
    back_data = f"f:n:{_path_id(parent_relpath)}:1:{sort}" if parent_relpath else f"f:r:1:{sort}"

    buttons: list[list[Button]] = []
    if protected:
        buttons.append([Button.inline("⬅️ Back", data=back_data)])
    else:
        buttons.append([Button.inline("🗑 Delete", data=f"f:d:{pid}:{sort}"), Button.inline("⬅️ Back", data=back_data)])

    return "\n".join(lines), buttons


def _render_delete_confirm(relpath: str, sort: str = _DEFAULT_SORT) -> tuple[str, list[list[Button]]]:
    """Build a delete confirmation prompt."""
    abspath = os.path.normpath(os.path.join(config.DOWNLOAD_DIR, relpath))
    name = os.path.basename(abspath)
    pid = _path_id(relpath)

    if os.path.isdir(abspath):
        count, total = _dir_summary(abspath)
        lines = [
            "⚠️ **Delete this folder and ALL contents?**",
            "",
            f"📁 {name} — {count} item{'s' if count != 1 else ''}, {utils.humanize_size(total)}",
            "This action cannot be undone.",
        ]
    elif os.path.isfile(abspath):
        try:
            sz = os.path.getsize(abspath)
        except OSError:
            sz = 0
        lines = [
            "⚠️ **Delete this file?**",
            "",
            f"📄 {name} ({utils.humanize_size(sz)})",
            "This action cannot be undone.",
        ]
    else:
        return "❌ Path not found", [[Button.inline("⬅️ Back", data=f"f:r:1:{sort}")]]

    buttons = [
        [
            Button.inline("✅ Yes, Delete", data=f"f:y:{pid}:{sort}"),
            Button.inline("❌ No, Go Back", data=f"f:x:{pid}:{sort}"),
        ]
    ]
    return "\n".join(lines), buttons


def _do_delete(abspath: str) -> bool:
    """Delete a file or directory tree. Returns True on success."""
    try:
        if os.path.isfile(abspath):
            os.remove(abspath)
        elif os.path.isdir(abspath):
            shutil.rmtree(abspath)
        else:
            return False
        utils.remove_empty_parents(abspath, [config.DOWNLOAD_DIR])
        return True
    except OSError as e:
        log.warning("File manager delete failed for %s: %s", abspath, e)
        return False


async def _safe_edit(event, text: str, buttons) -> None:
    """Edit a callback query message, suppressing no-change errors."""
    with contextlib.suppress(MessageNotModifiedError):
        await event.edit(text, buttons=buttons, parse_mode="md")


# ── Handler registration ──


def register_filemanager(client: TelegramClient) -> None:
    """Register /files command and all file manager callback handlers."""
    _register_files_command(client)
    _register_callbacks(client)


def _register_files_command(client: TelegramClient) -> None:
    @client.on(
        events.NewMessage(
            func=lambda e: e.is_private and not e.document and (e.raw_text or "").strip().lower() == "/files"
        )
    )
    async def _files(event):
        sender = await event.get_sender()
        if not config.is_user_allowed(getattr(sender, "id", None), getattr(sender, "username", None)):
            await event.respond("🛑 Not authorized.")
            return
        warning = utils.memory_warning_message(config.MEMORY_WARNING_PERCENT)
        if warning:
            await event.respond(warning)
        text, buttons = _render_root()
        await event.respond(text, buttons=buttons, parse_mode="md")


def _register_callbacks(client: TelegramClient) -> None:
    @client.on(events.CallbackQuery(pattern=rb"f:r:(\d+):([SND])"))
    async def _root(event):
        page = int(event.pattern_match.group(1).decode())
        sort = event.pattern_match.group(2).decode()
        text, buttons = _render_root(page, sort)
        await _safe_edit(event, text, buttons)
        await event.answer()

    @client.on(events.CallbackQuery(pattern=rb"f:n:([a-f0-9]{8}):(\d+):([SND])"))
    async def _navigate(event):
        match = event.pattern_match
        pid = match.group(1).decode()
        page = int(match.group(2).decode())
        sort = match.group(3).decode()
        abspath = _resolve(pid)
        if not abspath or not os.path.isdir(abspath):
            await event.answer(_EXPIRED, alert=True)
            return
        relpath = _path_registry[pid]
        text, buttons = _render_dir(relpath, page, sort)
        await _safe_edit(event, text, buttons)
        await event.answer()

    @client.on(events.CallbackQuery(pattern=rb"f:i:([a-f0-9]{8}):([SND])"))
    async def _file_info(event):
        pid = event.pattern_match.group(1).decode()
        sort = event.pattern_match.group(2).decode()
        abspath = _resolve(pid)
        if not abspath or not os.path.exists(abspath):
            await event.answer(_EXPIRED, alert=True)
            return
        relpath = _path_registry[pid]
        if os.path.isdir(abspath):
            text, buttons = _render_dir(relpath, 1, sort)
        else:
            text, buttons = _render_file(relpath, sort)
        await _safe_edit(event, text, buttons)
        await event.answer()

    @client.on(events.CallbackQuery(pattern=rb"f:d:([a-f0-9]{8}):([SND])"))
    async def _delete_prompt(event):
        pid = event.pattern_match.group(1).decode()
        sort = event.pattern_match.group(2).decode()
        abspath = _resolve(pid)
        if not abspath or not os.path.exists(abspath):
            await event.answer(_EXPIRED, alert=True)
            return
        if _is_protected_recursive(abspath):
            await event.answer("🔒 Cannot delete — active download", alert=True)
            return
        relpath = _path_registry[pid]
        text, buttons = _render_delete_confirm(relpath, sort)
        await _safe_edit(event, text, buttons)
        await event.answer()

    @client.on(events.CallbackQuery(pattern=rb"f:y:([a-f0-9]{8}):([SND])"))
    async def _delete_confirm(event):
        pid = event.pattern_match.group(1).decode()
        sort = event.pattern_match.group(2).decode()
        abspath = _resolve(pid)
        if not abspath or not os.path.exists(abspath):
            await event.answer("Already deleted or not found", alert=True)
            text, buttons = _render_root(sort=sort)
            await _safe_edit(event, text, buttons)
            return
        if _is_protected_recursive(abspath):
            await event.answer("🔒 Cannot delete — active download", alert=True)
            return
        relpath = _path_registry[pid]
        name = os.path.basename(abspath)
        success = _do_delete(abspath)
        if success:
            await event.answer(f"Deleted: {name}")
            log.info("File manager deleted: %s", relpath)
        else:
            await event.answer(f"Failed to delete: {name}", alert=True)
        parent_rel = os.path.dirname(relpath)
        if parent_rel:
            text, buttons = _render_dir(parent_rel, 1, sort)
        else:
            text, buttons = _render_root(sort=sort)
        await _safe_edit(event, text, buttons)

    @client.on(events.CallbackQuery(pattern=rb"f:x:([a-f0-9]{8}):([SND])"))
    async def _delete_cancel(event):
        pid = event.pattern_match.group(1).decode()
        sort = event.pattern_match.group(2).decode()
        abspath = _resolve(pid)
        if not abspath:
            await event.answer(_EXPIRED, alert=True)
            text, buttons = _render_root(sort=sort)
            await _safe_edit(event, text, buttons)
            return
        relpath = _path_registry[pid]
        if os.path.isfile(abspath):
            text, buttons = _render_file(relpath, sort)
        elif os.path.isdir(abspath):
            text, buttons = _render_dir(relpath, 1, sort)
        else:
            parent_rel = os.path.dirname(relpath)
            if parent_rel:
                text, buttons = _render_dir(parent_rel, 1, sort)
            else:
                text, buttons = _render_root(sort=sort)
        await _safe_edit(event, text, buttons)
        await event.answer()

    @client.on(events.CallbackQuery(pattern=b"f:noop"))
    async def _noop(event):
        await event.answer()


__all__ = ["register_filemanager"]
