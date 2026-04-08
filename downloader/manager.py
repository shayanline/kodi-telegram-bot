from __future__ import annotations

import asyncio
import contextlib
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from telethon import Button, TelegramClient, events
from telethon.tl.types import Document

import config
import kodi
import throttle
import utils
from logger import log
from organizer import build_final_path, parse_filename
from utils import remove_empty_parents

from .buttons import build_buttons
from .ids import get_file_id
from .list_commands import (
    build_downloads_list,
    build_queue_list,
    get_status_text,
    handle_existing_lists_for_new_download,
    register_list_handlers,
)
from .progress import RateLimiter, create_progress_callback, wait_if_paused
from .queue import QueuedItem, queue
from .state import (
    CancelledDownload,
    DownloadState,
    MessageType,
    PendingDeletion,
    file_id_map,
    message_tracker,
    pending_deletions,
    register_file_id,
    resolve_file_id,
    states,
)

# _queue_started gates one-time registration of queue worker & handlers
_queue_started = False

_NOT_FOUND = "Download completed or no longer active"

# Test hook: auto-accept deletions (bypasses interactive prompt during tests)
TEST_AUTO_ACCEPT = False

# Pending category selections: file_id -> (document, event, file_size, timestamp)
_pending_categories: dict[str, tuple[Any, Any, int, float]] = {}
_CATEGORY_TTL_SECONDS = 600  # 10 minutes

# Serializes the check-then-act section in _download to prevent duplicate race conditions
_download_lock = asyncio.Lock()


def _prune_stale_categories():
    """Remove pending category entries older than TTL."""
    cutoff = time.time() - _CATEGORY_TTL_SECONDS
    stale = [k for k, v in _pending_categories.items() if v[3] < cutoff]
    for k in stale:
        _pending_categories.pop(k, None)


async def _safe_edit(msg, text: str, buttons=None, state: DownloadState | None = None):
    """Edit a message, falling back to a new response if editing fails."""
    result = await throttle.edit_message(msg, text, buttons=buttons)
    if result is not None:
        return result
    log.debug("Edit failed, sending fallback message")
    new_msg = await throttle.send_message(msg, text, buttons=buttons)
    if state and new_msg:
        state.message = new_msg
    return new_msg


async def _update_tracked_messages(filename: str, state: DownloadState):
    """Best-effort update of mirror progress + list messages after state change."""
    for tracked in message_tracker.get_messages(filename):
        try:
            if tracked.message_type == MessageType.PROGRESS:
                # Skip the primary message (already updated by caller)
                if state.message and tracked.message.id == state.message.id:
                    continue
                status = get_status_text(state)
                text = f"{status}: {state.filename}"
                buttons = build_buttons(state)
                new_msg = await _safe_edit(tracked.message, text, buttons=buttons)
                if new_msg and new_msg is not tracked.message:
                    tracked.message = new_msg
            elif tracked.message_type == MessageType.DOWNLOAD_LIST:
                text, buttons = build_downloads_list(states)
                new_msg = await _safe_edit(tracked.message, text, buttons=buttons)
                if new_msg and new_msg is not tracked.message:
                    tracked.message = new_msg
            elif tracked.message_type == MessageType.QUEUE_LIST:
                if queue.items:
                    text, buttons = build_queue_list(queue.items)
                else:
                    text = "📝 No queued downloads"
                    buttons = [[Button.inline("🔄 Refresh", data="refresh_queue")]]
                new_msg = await _safe_edit(tracked.message, text, buttons=buttons)
                if new_msg and new_msg is not tracked.message:
                    tracked.message = new_msg
        except Exception:
            pass


def filename_for_document(document: Document) -> str:
    import mimetypes

    from telethon.tl.types import DocumentAttributeFilename

    for attr in document.attributes:
        if isinstance(attr, DocumentAttributeFilename):
            return attr.file_name
    ext = mimetypes.guess_extension(getattr(document, "mime_type", "")) or ""
    return f"media_{int(time.time())}{ext}"


def validate_size(expected_size: int, path: str) -> bool:
    return os.path.exists(path) and os.path.getsize(path) >= expected_size * 0.98


async def pre_checks(event: events.NewMessage.Event, text: str | None = None):
    document = event.document
    original_filename = filename_for_document(document)
    file_size = document.size or 0
    path, filename = build_final_path(original_filename, text=text)
    try:
        if utils.free_disk_mb(config.DOWNLOAD_DIR) < config.DISK_WARNING_MB:
            await throttle.send_message(
                event, f"⚠️ Low disk space (< {config.DISK_WARNING_MB}MB free). Consider cleaning up soon."
            )
    except Exception:
        pass
    if os.path.exists(path):
        try:
            actual = os.path.getsize(path)
        except OSError:
            actual = 0
        if file_size == 0 or actual >= file_size * 0.98:
            await throttle.send_message(
                event,
                f"ℹ️ File already exists: {filename} (size: {utils.humanize_size(actual)})",  # noqa: RUF001
                reply_to=getattr(event, "id", None),
            )
            log.info("Skip existing file %s", filename)
            return None
        await throttle.send_message(
            event,
            f"⚠️ Found incomplete existing file ({utils.humanize_size(actual)}/{utils.humanize_size(file_size)}); re-downloading...",
            reply_to=getattr(event, "id", None),
        )
        with contextlib.suppress(OSError):
            os.remove(path)
        log.info("Re-downloading incomplete file %s", filename)
    return document, filename, file_size, path


def _projected_free_mb(after_adding_bytes: int) -> int:
    free_now = utils.free_disk_mb(config.DOWNLOAD_DIR)
    return free_now - int(after_adding_bytes / (1024 * 1024))


def _current_reserved_bytes(exclude: str | None = None) -> int:
    return sum(max(0, st.size - st.downloaded_bytes) for name, st in states.items() if name != exclude)


def _list_files_under(root: str, exclude: set[str]) -> list[tuple[float, str]]:
    entries: list[tuple[float, str]] = []
    for r, _d, files in os.walk(root):
        for f in files:
            full = os.path.join(r, f)
            if full in exclude:
                continue
            try:
                m = os.path.getmtime(full)
            except OSError:
                continue
            entries.append((m, full))
    entries.sort(key=lambda x: x[0])  # oldest first
    return entries


def _infer_category_root(path: str) -> str | None:
    if not config.ORGANIZE_MEDIA:
        return None
    for name in (config.MOVIES_DIR_NAME, config.SERIES_DIR_NAME, config.OTHER_DIR_NAME):
        root = os.path.join(config.DOWNLOAD_DIR, name)
        if path.startswith(root + os.sep):
            return root
    return None


def _select_deletion_candidate(target_path: str, exclude: set[str]) -> str | None:
    """Return full path of candidate file to delete following selection rules."""
    if config.ORGANIZE_MEDIA:
        cat_root = _infer_category_root(target_path)
        if cat_root:
            cat_files = _list_files_under(cat_root, exclude)
            if cat_files:
                return cat_files[0][1]
    all_files = _list_files_under(config.DOWNLOAD_DIR, exclude)
    return all_files[0][1] if all_files else None


async def _ensure_disk_space(event, filename: str, file_size: int, path: str | None = None) -> bool:
    """Interactive disk space assurance with recursive candidate deletions.

    Holds the concurrency slot (caller acquires it) while waiting for user decision.
    Timeout 120s -> cancellation. TEST_AUTO_ACCEPT path auto-deletes oldest files.
    """
    target_path = path or os.path.join(config.DOWNLOAD_DIR, filename)
    while True:
        cumulative = _current_reserved_bytes(exclude=filename) + file_size
        projected = _projected_free_mb(cumulative)
        if projected >= config.MIN_FREE_DISK_MB:
            return True
        exclude = {st.path for st in states.values()}
        exclude.add(target_path)
        candidate = _select_deletion_candidate(target_path, exclude)
        if not candidate:
            await throttle.send_message(
                event,
                f"🛑 Storage not enough for {filename} and no deletable files found. Cancelling.",
                reply_to=getattr(event, "id", None),
            )
            log.error("No candidate for deletion; cancelling %s", filename)
            return False
        cand_name = os.path.basename(candidate)

        if TEST_AUTO_ACCEPT:
            with contextlib.suppress(OSError):
                os.remove(candidate)
            log.debug("[TEST] Auto-deleted %s", candidate)
            continue

        # Interactive prompt
        pid = uuid.uuid4().hex[:8]
        pending = PendingDeletion()
        pending_deletions[pid] = pending
        free_now = utils.free_disk_mb(config.DOWNLOAD_DIR)
        needed_after = config.MIN_FREE_DISK_MB
        size_h = utils.humanize_size(file_size)
        text = (
            f"Storage is not enough to download {filename} (need to reserve {size_h}).\n"
            f"Free now: {free_now}MB. Need >= {needed_after}MB free AFTER reserving active downloads.\n"
            f"Delete oldest candidate: {cand_name}?"
        )
        buttons = [
            [
                Button.inline("✅ Yes", data=f"delok:{pid}"),
                Button.inline("❌ No", data=f"delnx:{pid}"),
            ]
        ]
        pending.message = await throttle.send_message(event, text, buttons=buttons, reply_to=getattr(event, "id", None))
        if not pending.message:
            pending_deletions.pop(pid, None)
            return False
        try:
            await asyncio.wait_for(pending.future, timeout=120)
        except TimeoutError:
            try:
                if pending.message:
                    await _safe_edit(pending.message, f"🛑 Timed out waiting for confirmation. Cancelled: {filename}")
            except Exception:
                pass
            pending_deletions.pop(pid, None)
            log.warning("Deletion prompt timeout for %s", filename)
            return False
        choice = pending.choice
        pending_deletions.pop(pid, None)
        if choice != "yes":
            if pending.message:
                await _safe_edit(pending.message, f"🛑 Cancelled: insufficient space for {filename}")
            log.info("User declined deletion for %s", filename)
            return False
        with contextlib.suppress(OSError):
            os.remove(candidate)
        if pending.message:
            await _safe_edit(pending.message, f"Deleted {cand_name}. Re-checking space...")
        # loop continues to re-evaluate space


async def download_with_retries(
    client: TelegramClient,
    document: Document,
    path: str,
    progress_cb: Callable[[int, int], Awaitable[None]],
    msg: Any,
    state: DownloadState,
) -> bool:
    retry = 0
    while retry <= config.MAX_RETRY_ATTEMPTS:
        try:
            if state.cancelled:
                raise CancelledDownload
            await wait_if_paused(state)
            await client.download_media(document, file=path, progress_callback=progress_cb)
            return True
        except TimeoutError:
            retry += 1
            if retry > config.MAX_RETRY_ATTEMPTS:
                return False
            await throttle.edit_message(msg, f"Download stalled. Retrying ({retry}/{config.MAX_RETRY_ATTEMPTS})...")
            await asyncio.sleep(2)
        except CancelledDownload:
            return False
        except Exception as e:
            log.warning("Download error attempt %d for %s: %s", retry, state.filename, e)
            retry += 1
            await asyncio.sleep(1)
    return False


def _final_cleanup(filename: str):
    """Remove state after download finishes (success, error, or cancellation)."""
    message_tracker.cleanup_file(filename)
    states.pop(filename, None)
    file_id = get_file_id(filename)
    file_id_map.pop(file_id, None)


async def run_download(
    client: TelegramClient,
    event: events.NewMessage.Event,
    document: Document,
    filename: str,
    file_size: int,
    path: str,
    watcher_events: list[Any] | None = None,
    existing_message: Any | None = None,
) -> None:
    """Run a download and mirror progress to any duplicate requester chats."""
    state = _init_state(filename, path, file_size, event)
    if existing_message is not None:
        state.message = existing_message
        handle_existing_lists_for_new_download(filename)
        msg = existing_message
    else:
        msg = await _send_start_message(event, state)

    # Send initial messages to watchers, register as mirror progress messages
    if watcher_events:
        for wev in watcher_events:
            try:
                mirror_msg = await throttle.send_message(
                    wev,
                    f"Starting download of {state.filename}...",
                    reply_to=getattr(wev, "id", None),
                    buttons=build_buttons(state),
                )
                if mirror_msg:
                    sender = await wev.get_sender()
                    user_id = getattr(sender, "id", None)
                    message_tracker.register_message(filename, mirror_msg, MessageType.PROGRESS, user_id)
            except Exception:
                pass

    # Monkey-patch msg.edit to fan out updates to mirror messages with fallback
    _orig_edit = msg.edit

    async def _patched_edit(text: str, **kwargs):  # pragma: no cover simple wrapper
        nonlocal _orig_edit
        r = None
        try:
            r = await _orig_edit(text, **kwargs)
        except Exception as exc:
            if type(exc).__name__ != "MessageNotModifiedError":
                # Primary message uneditable — send a replacement
                new_msg = await throttle.send_message(msg, text, **kwargs)
                if new_msg:
                    state.message = new_msg
                    _orig_edit = new_msg.edit
                    r = new_msg
        for tracked in message_tracker.get_messages(state.filename, MessageType.PROGRESS):
            if state.message and tracked.message.id == state.message.id:
                continue
            result = await throttle.edit_message(tracked.message, text, **kwargs)
            if result is None:
                # Mirror message uneditable — send a replacement in the same chat
                new_mirror = await throttle.send_message(tracked.message, text, **kwargs)
                if new_mirror:
                    tracked.message = new_mirror
        return r

    with contextlib.suppress(Exception):
        msg.edit = _patched_edit

    progress_cb = create_progress_callback(filename, time.time(), RateLimiter(), msg, state)

    try:
        success = await download_with_retries(client, document, path, progress_cb, msg, state)
        if not await _post_download_check(success, file_size, path, state, msg, filename):
            return
        await _handle_success(msg, filename, path, state)
    except Exception as e:
        await _handle_error(e, state, msg, filename, path)
    finally:
        _final_cleanup(filename)


def _init_state(filename: str, path: str, size: int, event: events.NewMessage.Event) -> DownloadState:
    existing = states.get(filename)
    if existing:
        existing.path = path
        existing.size = size
        existing.original_event = event
        return existing
    st = DownloadState(filename, path, size, original_event=event)
    states[filename] = st
    register_file_id(filename)
    return st


async def _send_start_message(event: events.NewMessage.Event, state: DownloadState):
    start_text = f"Starting download of {state.filename}..."
    msg = await throttle.send_message(
        event,
        start_text,
        buttons=build_buttons(state),
        reply_to=getattr(event, "id", None),
    )
    state.message = msg
    state.last_text = start_text

    sender = await event.get_sender()
    user_id = getattr(sender, "id", None)
    message_tracker.register_message(state.filename, msg, MessageType.PROGRESS, user_id)

    await kodi.notify("Download Started", state.filename)
    log.info("Start download %s (%s)", state.filename, utils.humanize_size(state.size))
    return msg


async def _post_download_check(
    success: bool,
    expected_size: int,
    path: str,
    state: DownloadState,
    msg,
    filename: str,
) -> bool:
    if success and validate_size(expected_size, path):
        return True
    if state.cancelled:
        await _safe_edit(msg, f"🛑 Download cancelled: {filename}", state=state)
        if os.path.exists(path):
            try:
                os.remove(path)
                remove_empty_parents(path, [config.DOWNLOAD_DIR])
            except OSError:
                pass
        log.info("Cancelled %s", filename)
    else:
        await _safe_edit(
            msg,
            f"❌ Download incomplete. Expected {utils.humanize_size(expected_size)}",
            state=state,
        )
        await kodi.notify("Download Failed", f"Incomplete: {filename}")
        log.error("Incomplete download %s", filename)
    return False


async def _handle_success(msg, filename: str, path: str, state: DownloadState) -> None:
    state.mark_completed()
    playing = await kodi.is_playing()
    text = (
        f"✅ Download complete: {filename}\nKodi playing something else. File ready."
        if playing
        else f"✅ Download complete: {filename}\nPlaying on Kodi..."
    )
    await _safe_edit(msg, text, state=state)
    await _update_tracked_messages(filename, state)
    if not playing:
        await kodi.play(path)
    await kodi.notify("Download Complete", filename)
    log.info("Completed %s", filename)


async def _handle_error(
    exc: Exception,
    state: DownloadState,
    msg,
    filename: str,
    path: str,
) -> None:
    if state.cancelled:
        await _safe_edit(msg, f"🛑 Download cancelled: {filename}", state=state)
        await _update_tracked_messages(filename, state)
        if os.path.exists(path):
            try:
                os.remove(path)
                remove_empty_parents(path, [config.DOWNLOAD_DIR])
            except OSError:
                pass
        return
    err = str(exc)
    await _safe_edit(msg, f"❌ Error: {err[:200]}", state=state)
    await kodi.notify("Download Failed", err[:50])
    log.error("Download error %s: %s", filename, err)


# ── Handler registration ──


def register_handlers(client: TelegramClient):
    """Register Telegram handlers and start queue worker."""
    global _queue_started
    if not _queue_started:
        queue.set_runner(
            lambda c, qi: run_download(
                c,
                qi.event,
                qi.document,
                qi.filename,
                qi.size,
                qi.path,
                watcher_events=qi.watcher_events or [],
                existing_message=qi.message,
            )
        )
        queue.ensure_worker(client.loop, client)
        _queue_started = True
    log.debug("Queue worker started")

    _register_download_handler(client)
    _register_status_handler(client)
    _register_start_handler(client)
    _register_control_callbacks(client)
    register_list_handlers(client)


def _same_user(ev1, ev2):
    return getattr(ev1, "sender_id", None) == getattr(ev2, "sender_id", None)


async def _handle_active_duplicate(event, active_state: DownloadState, filename: str):
    # Resume if paused
    if active_state.paused and not active_state.cancelled:
        active_state.mark_resumed()
        await _update_tracked_messages(filename, active_state)

    progress_msg = getattr(active_state, "message", None)
    reply_target = getattr(progress_msg, "id", None)

    if reply_target:
        try:
            base = (
                "⏳ Already in progress"
                if active_state.original_event and _same_user(event, active_state.original_event)
                else "⏳ Already being downloaded"
            )
            msg = await throttle.send_message(event, f"{base}: {filename}", reply_to=reply_target)
            if msg:
                sender = await event.get_sender()
                user_id = getattr(sender, "id", None)
                message_tracker.register_message(filename, msg, MessageType.ALREADY_DOWNLOADING, user_id)
                return
        except Exception:
            pass  # fall through to creating mirror message

    # Progress message missing (deleted?) — create a new mirror with progress mirroring
    try:
        mirror_msg = await throttle.send_message(
            event,
            f"⏳ Already being downloaded: {filename}. You'll receive progress here.",
            reply_to=getattr(event, "id", None),
        )
        if mirror_msg:
            sender = await event.get_sender()
            user_id = getattr(sender, "id", None)
            message_tracker.register_message(filename, mirror_msg, MessageType.PROGRESS, user_id)
    except Exception:
        pass


async def _handle_queued_duplicate(event, queued_item: QueuedItem, filename: str):
    queued_msg = getattr(queued_item, "message", None)
    reply_target = getattr(queued_msg, "id", None)
    same = queued_item.event and _same_user(event, queued_item.event)

    if reply_target:
        try:
            msg = await throttle.send_message(
                event,
                f"🕒 Already queued: {filename}",
                reply_to=reply_target,
            )
            if msg:
                sender = await event.get_sender()
                user_id = getattr(sender, "id", None)
                message_tracker.register_message(filename, msg, MessageType.ALREADY_QUEUED, user_id)
                if not same:
                    queued_item.add_watcher(event)
                return
        except Exception:
            pass  # recreate below

    if not queued_item.file_id:
        queued_item.file_id = get_file_id(filename)

    try:
        msg = await throttle.send_message(
            event,
            f"🕒 Queued: {filename}\nWaiting for free slot (limit {config.MAX_CONCURRENT_DOWNLOADS})",
            buttons=[[Button.inline("🛑 Cancel", data=f"qcancel:{queued_item.file_id}")]],
            reply_to=getattr(event, "id", None),
        )
        if msg:
            if not queued_item.message:
                queued_item.message = msg
            sender = await event.get_sender()
            user_id = getattr(sender, "id", None)
            message_tracker.register_message(filename, msg, MessageType.QUEUED, user_id)
            if not same:
                queued_item.add_watcher(event)
    except Exception:
        pass


async def _do_enqueue(client: TelegramClient, document, filename, size, path, event):
    """Enqueue a download and send queue position message."""
    file_id = register_file_id(filename)
    qi = QueuedItem(filename, document, size, path, event, file_id=file_id)
    position = await queue.enqueue(qi)
    try:
        msg = await throttle.send_message(
            event,
            f"🕒 Queued #{position}: {filename}\nWaiting for free slot (limit {config.MAX_CONCURRENT_DOWNLOADS})",
            buttons=[[Button.inline("🛑 Cancel", data=f"qcancel:{file_id}")]],
            reply_to=getattr(event, "id", None),
        )
        if msg:
            qi.message = msg
            sender = await event.get_sender()
            user_id = getattr(sender, "id", None)
            message_tracker.register_message(filename, msg, MessageType.QUEUED, user_id)
    except Exception:
        pass


async def _start_direct_download(client: TelegramClient, event, document, filename, size, path):
    """Run a direct download. Called OUTSIDE _download_lock after pre-registering state."""
    try:
        async with queue.slot():
            st = states.get(filename)
            if st and st.cancelled:
                _final_cleanup(filename)
                return
            if not await _ensure_disk_space(event, filename, size, path):
                _final_cleanup(filename)
                return
            await run_download(client, event, document, filename, size, path)
    except Exception:
        if filename in states:
            _final_cleanup(filename)


def _register_download_handler(client: TelegramClient):
    @client.on(events.NewMessage(func=lambda e: e.is_private and e.document))
    @throttle.serialized
    async def _download(event):
        sender = await event.get_sender()
        uid = getattr(sender, "id", None)
        uname = getattr(sender, "username", None)
        if not config.is_user_allowed(uid, uname):
            await throttle.send_message(event, "🛑 You are not authorized to use this bot.")
            return
        document = event.document
        if not utils.is_media_file(document):
            await throttle.send_message(event, "⚠️ Only video and audio files are supported")
            return
        _prune_stale_categories()
        original_filename = filename_for_document(document)
        # Provide message text (caption) to parser for richer extraction
        parsed = parse_filename(original_filename, text=(event.raw_text or None))
        ambiguous = parsed.category == "other" and parsed.year is not None
        if not ambiguous or not config.ORGANIZE_MEDIA:
            _path_tmp, normalized_name = build_final_path(original_filename, text=(event.raw_text or None))
            lookup_name = normalized_name
        else:
            lookup_name = original_filename

        # Lock prevents race: two events for the same file could both pass the
        # duplicate check before either registers state, causing double downloads.
        # Lock is held only during check+register, NOT during the actual download.
        direct_download = None
        async with _download_lock:
            active_state = states.get(lookup_name)
            if active_state:
                await _handle_active_duplicate(event, active_state, lookup_name)
                return
            queued_item = queue.items.get(lookup_name)
            if queued_item:
                await _handle_queued_duplicate(event, queued_item, lookup_name)
                return
            if ambiguous and config.ORGANIZE_MEDIA:
                file_id = register_file_id(original_filename)
                _pending_categories[file_id] = (document, event, document.size or 0, time.time())
                buttons = [
                    [
                        Button.inline("🎬 Movie", data=f"catm:{file_id}"),
                        Button.inline("📺 Series", data=f"cats:{file_id}"),
                        Button.inline("📁 Other", data=f"cato:{file_id}"),
                    ]
                ]
                await throttle.send_message(event, f"Select category for: {original_filename}", buttons=buttons)
                return
            pre = await pre_checks(event, text=(event.raw_text or None))
            if not pre:
                return
            document, filename, size, path = pre
            if queue.is_saturated():
                await _do_enqueue(client, document, filename, size, path, event)
                log.debug("Enqueued %s", filename)
                return
            # Pre-register state so duplicates are detected after lock release
            register_file_id(filename)
            states[filename] = DownloadState(filename, path, size, original_event=event)
            direct_download = (document, filename, size, path)

        if direct_download:
            document, filename, size, path = direct_download
            await _start_direct_download(client, event, document, filename, size, path)
            log.debug("Started %s", filename)


def _register_status_handler(client: TelegramClient):
    @client.on(
        events.NewMessage(
            func=lambda e: e.is_private and not e.document and (e.raw_text or "").strip().lower() == "/status"
        )
    )
    async def _status(event):
        sender = await event.get_sender()
        if not config.is_user_allowed(getattr(sender, "id", None), getattr(sender, "username", None)):
            await throttle.send_message(event, "🛑 Not authorized.")
            return
        q = list(queue.items.keys())
        active = list(states.keys())
        parts = [
            f"Active: {len(active)}/{config.MAX_CONCURRENT_DOWNLOADS}",
            f"Queued: {len(q)}",
        ]
        if active:
            parts.append("\nCurrent downloads:")
            parts.extend(f" • {fn}" for fn in active[:10])
        if q:
            parts.append("\nQueue:")
            parts.extend(f" {i + 1}. {fn}" for i, fn in enumerate(q[:15]))
        await throttle.send_message(event, "\n".join(parts))


def _register_start_handler(client: TelegramClient):
    HELP_TEXT = (
        "Send me a video or audio file — I'll download it and play it on Kodi.\n\n"
        "Commands:\n"
        "/status - show active + queued downloads summary\n"
        "/downloads - show detailed active downloads list\n"
        "/queue - show detailed queued downloads list\n"
        "/files - browse and manage downloaded files\n"
        "/kodi - Kodi remote control\n"
        "/restart_kodi - quit and restart Kodi\n"
        "/start - this help"
    )

    @client.on(events.NewMessage(func=lambda e: e.is_private and (e.raw_text or "").strip().lower() == "/start"))
    @throttle.serialized
    async def _start(event):
        sender = await event.get_sender()
        if not config.is_user_allowed(getattr(sender, "id", None), getattr(sender, "username", None)):
            await throttle.send_message(event, "🛑 Not authorized.")
            return
        warning = utils.memory_warning_message(config.MEMORY_WARNING_PERCENT)
        if warning:
            await throttle.send_message(event, warning)
        await throttle.send_message(event, HELP_TEXT)


def _register_control_callbacks(client: TelegramClient):
    _register_pause_resume_cancel(client)
    _register_cancel_confirm(client)
    _register_qcancel(client)
    _register_qcancel_confirm(client)
    _register_category_selection(client)
    _register_deletion_callbacks(client)


def _register_pause_resume_cancel(client: TelegramClient):
    pattern = b"(pause|resume|cancel|lcancel):"

    async def _update_progress_message(state: DownloadState, status_text: str):
        """Update the primary progress message with new buttons and status."""
        if not state.message:
            return
        if state.paused:
            progress_text = state.get_progress_text() or "Paused"
            content = f"{status_text}\nFile: {state.filename}\nStatus: {progress_text}"
        else:
            progress_text = state.get_progress_text()
            content = (
                f"{status_text}\nFile: {state.filename}\n{progress_text}"
                if progress_text
                else f"{status_text}\nFile: {state.filename}"
            )
        buttons = build_buttons(state)
        await throttle.edit_message(state.message, content, buttons=buttons)

    async def _do_pause(st, event):
        if st.paused:
            await throttle.answer_callback(event, "Already paused", alert=False)
            return
        st.mark_paused()
        await _update_progress_message(st, "⏸️ Paused")
        await _update_tracked_messages(st.filename, st)
        await throttle.answer_callback(event, "Paused")

    async def _do_resume(st, event):
        if not st.paused:
            await throttle.answer_callback(event, "Not paused", alert=False)
            return
        st.mark_resumed()
        await _update_progress_message(st, "▶️ Resuming...")
        await _update_tracked_messages(st.filename, st)
        await throttle.answer_callback(event, "Resuming")

    async def _do_cancel(st, event, *, from_list: bool = False):
        file_id = get_file_id(st.filename)
        st.confirming_cancel = True
        text = f"⚠️ **Cancel this download?**\n\n{st.filename}"
        yes_data = f"cyl:{file_id}" if from_list else f"cy:{file_id}"
        no_data = f"cnl:{file_id}" if from_list else f"cn:{file_id}"
        buttons = [
            [
                Button.inline("✅ Yes, Cancel", data=yes_data),
                Button.inline("❌ No, Go Back", data=no_data),
            ]
        ]
        await throttle.edit_message(event, text, buttons=buttons)
        await throttle.answer_callback(event)

    @client.on(events.CallbackQuery(pattern=pattern))
    @throttle.serialized
    async def _prc(event):
        action, file_id = event.data.decode().split(":", 1)
        filename = resolve_file_id(file_id)
        if not filename:
            await throttle.answer_callback(event, _NOT_FOUND, alert=False)
            return
        st = states.get(filename)
        if not st or st.cancelled:
            await throttle.answer_callback(event, _NOT_FOUND, alert=False)
            return
        if action == "pause":
            await _do_pause(st, event)
        elif action == "resume":
            await _do_resume(st, event)
        else:
            await _do_cancel(st, event, from_list=(action == "lcancel"))


def _register_cancel_confirm(client: TelegramClient):
    @client.on(events.CallbackQuery(pattern=b"c(y|n)l?:"))
    @throttle.serialized
    async def _cancel_confirm(event):
        data = event.data.decode()
        colon_idx = data.index(":")
        prefix = data[:colon_idx]
        file_id = data[colon_idx + 1 :]
        from_list = prefix.endswith("l")
        confirmed = prefix.startswith("cy")
        filename = resolve_file_id(file_id)
        if not filename:
            await throttle.answer_callback(event, _NOT_FOUND, alert=False)
            return
        st = states.get(filename)
        if not st:
            await throttle.answer_callback(event, _NOT_FOUND, alert=False)
            return
        st.confirming_cancel = False
        if confirmed:
            st.mark_cancelled()
            await throttle.edit_message(event, f"🛑 Cancelling: {st.filename}", buttons=None)
            await _update_tracked_messages(filename, st)
            await throttle.answer_callback(event, "Cancelling")
        elif from_list:
            if states:
                text, buttons = build_downloads_list(states)
            else:
                text = "📁 No active downloads"
                buttons = [[Button.inline("🔄 Refresh", data="refresh_downloads")]]
            await throttle.edit_message(event, text, buttons=buttons)
            await throttle.answer_callback(event)
        else:
            status = get_status_text(st)
            progress_text = st.get_progress_text()
            text = f"{status}: {st.filename}"
            if progress_text:
                text += f"\n{progress_text}"
            buttons = build_buttons(st)
            await throttle.edit_message(event, text, buttons=buttons)
            await throttle.answer_callback(event)


def _register_qcancel(client: TelegramClient):
    @client.on(events.CallbackQuery(pattern=b"l?qcancel:"))
    @throttle.serialized
    async def _qcancel(event):
        data = event.data.decode()
        from_list = data.startswith("lqcancel:")
        file_id = data.split(":", 1)[1]
        filename = resolve_file_id(file_id)
        if not filename:
            await throttle.answer_callback(event, _NOT_FOUND, alert=False)
            return
        qi = queue.items.get(filename)
        if not (qi and not qi.cancelled):
            await throttle.answer_callback(event, _NOT_FOUND, alert=False)
            return

        text = f"⚠️ **Cancel this queued download?**\n\n{filename}"
        yes_data = f"qcyl:{file_id}" if from_list else f"qcy:{file_id}"
        no_data = f"qcnl:{file_id}" if from_list else f"qcn:{file_id}"
        buttons = [
            [
                Button.inline("✅ Yes, Cancel", data=yes_data),
                Button.inline("❌ No, Go Back", data=no_data),
            ]
        ]
        await throttle.edit_message(event, text, buttons=buttons)
        await throttle.answer_callback(event)


def _register_qcancel_confirm(client: TelegramClient):
    @client.on(events.CallbackQuery(pattern=b"qc(y|n)l?:"))
    @throttle.serialized
    async def _qcancel_confirm(event):
        data = event.data.decode()
        colon_idx = data.index(":")
        prefix = data[:colon_idx]
        file_id = data[colon_idx + 1 :]
        from_list = prefix.endswith("l")
        confirmed = prefix.startswith("qcy")
        filename = resolve_file_id(file_id)
        if not filename:
            await throttle.answer_callback(event, _NOT_FOUND, alert=False)
            return

        if confirmed:
            qi = queue.items.get(filename)
            if not (qi and not qi.cancelled):
                await throttle.answer_callback(event, _NOT_FOUND, alert=False)
                return

            for tracked in message_tracker.get_messages(filename):
                if tracked.message_type in (MessageType.PROGRESS, MessageType.QUEUED):
                    await throttle.edit_message(
                        tracked.message,
                        f"🛑 Cancelled: {filename}\nThis download was cancelled from the queue.",
                        buttons=None,
                    )

            list_messages = message_tracker.get_all_list_messages()
            queue.cancel(filename)

            for tracked in list_messages:
                if tracked.message_type == MessageType.QUEUE_LIST:
                    if queue.items:
                        text, buttons = build_queue_list(queue.items)
                        await throttle.edit_message(tracked.message, text, buttons=buttons)
                    else:
                        await throttle.edit_message(
                            tracked.message,
                            "📝 No queued downloads",
                            buttons=[[Button.inline("🔄 Refresh", data="refresh_queue")]],
                        )

            message_tracker.cleanup_file(filename)
            file_id_map.pop(file_id, None)
            await throttle.answer_callback(event, "Cancelled")
        elif from_list:
            if queue.items:
                text, buttons = build_queue_list(queue.items)
            else:
                text = "📝 No queued downloads"
                buttons = [[Button.inline("🔄 Refresh", data="refresh_queue")]]
            await throttle.edit_message(event, text, buttons=buttons)
            await throttle.answer_callback(event)
        else:
            qi = queue.items.get(filename)
            if qi and qi.file_id:
                text = f"🕒 Queued: {filename}\nWaiting for free slot (limit {config.MAX_CONCURRENT_DOWNLOADS})"
                buttons = [[Button.inline("🛑 Cancel", data=f"qcancel:{qi.file_id}")]]
            else:
                text = _NOT_FOUND
                buttons = None
            await throttle.edit_message(event, text, buttons=buttons)
            await throttle.answer_callback(event)


def _register_deletion_callbacks(client: TelegramClient):
    pattern = b"del(ok|nx):"

    @client.on(events.CallbackQuery(pattern=pattern))
    @throttle.serialized
    async def _del(event):
        data = event.data.decode()
        action, pid = data.split(":", 1)
        pending = pending_deletions.get(pid)
        if not pending:
            await throttle.answer_callback(event, _NOT_FOUND, alert=False)
            return
        if pending.future.done():
            await throttle.answer_callback(event, "Already processed", alert=False)
            return
        if action == "delok":
            pending.choice = "yes"
            await throttle.answer_callback(event, "Deleting", alert=False)
        else:
            pending.choice = "no"
            await throttle.answer_callback(event, "Cancelled", alert=False)
        with contextlib.suppress(Exception):
            pending.future.set_result(True)


def _register_category_selection(client: TelegramClient):
    @client.on(events.CallbackQuery(pattern=b"cat[mso]:"))
    @throttle.serialized
    async def _cat(event):
        data = event.data.decode()
        prefix, file_id = data.split(":", 1)
        filename = resolve_file_id(file_id)
        if not filename:
            await throttle.answer_callback(event, _NOT_FOUND, alert=False)
            return
        pending = _pending_categories.pop(file_id, None)
        if not pending:
            await throttle.answer_callback(event, "Selection expired", alert=False)
            return
        document, orig_event, size, _ts = pending
        forced = {"catm": "movie", "cats": "series", "cato": "other"}.get(prefix)
        if not forced:
            await throttle.answer_callback(event, "Unknown", alert=False)
            return
        path, final_name = build_final_path(filename, forced_category=forced)
        direct_download = None
        async with _download_lock:
            if states.get(final_name) or queue.items.get(final_name):
                await throttle.answer_callback(event, "Already queued", alert=False)
                return
            if queue.is_saturated():
                await _do_enqueue(client, document, final_name, size, path, orig_event)
                await throttle.answer_callback(event, "Queued", alert=False)
                return
            # Pre-register state so duplicates are detected after lock release
            register_file_id(final_name)
            states[final_name] = DownloadState(final_name, path, size, original_event=orig_event)
            direct_download = (document, final_name, size, path)

        if direct_download:
            document, final_name, size, path = direct_download
            await _start_direct_download(client, orig_event, document, final_name, size, path)
        await throttle.answer_callback(event, "Started", alert=False)


__all__ = [
    "register_handlers",
    "run_download",
]
