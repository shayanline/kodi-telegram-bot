from __future__ import annotations

import asyncio
import contextlib
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from telethon import Button, TelegramClient, events
from telethon.tl.types import Document, ReplyInlineMarkup

import config
import kodi
import throttle
import utils
from logger import log
from organizer import build_final_path, parse_filename
from utils import remove_empty_parents

from .ids import get_file_id
from .list_commands import register_list_handlers, update_all_lists
from .progress import RateLimiter, create_progress_callback, wait_if_paused
from .queue import QueuedItem, queue
from .state import (
    CancelledDownload,
    DownloadState,
    PendingDeletion,
    file_id_map,
    find_pending_deletion,
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

# Prevent GC of fire-and-forget download tasks (see RUF006)
_bg_tasks: set[asyncio.Task[None]] = set()

# Periodic download-list update interval (seconds)
_LIST_UPDATE_INTERVAL = 5.0


def _prune_stale_categories():
    """Remove pending category entries older than TTL."""
    cutoff = time.time() - _CATEGORY_TTL_SECONDS
    stale = [k for k, v in _pending_categories.items() if v[3] < cutoff]
    for k in stale:
        _pending_categories.pop(k, None)


def _unblock_pending_deletion(filename: str) -> None:
    """Resolve any active pending deletion future for *filename* so _ensure_disk_space unblocks."""
    result = find_pending_deletion(filename)
    if result:
        _pid, pending = result
        if not pending.future.done():
            pending.choice = "no"
            with contextlib.suppress(Exception):
                pending.future.set_result(True)


def _spawn(coro: Any) -> None:
    """Launch a background task and prevent its garbage collection."""
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def _periodic_list_updater():
    """Periodically update all tracked download list messages."""
    while True:
        await asyncio.sleep(_LIST_UPDATE_INTERVAL)
        if not any(not s.cancelled and not s.completed for s in states.values()):
            continue
        async with throttle.handler_lock:
            with contextlib.suppress(Exception):
                await update_all_lists()


async def _safe_edit(msg, text: str, buttons=None):
    """Edit a message, falling back to a new response if editing fails."""
    result = await throttle.edit_message(msg, text, buttons=buttons)
    if result is not None:
        return result
    log.debug("Edit failed, sending fallback message")
    return await throttle.send_message(msg, text, buttons=buttons)


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


async def _ensure_disk_space(
    event, filename: str, file_size: int, path: str | None = None, existing_message: Any = None
) -> tuple[bool, Any]:
    """Interactive disk space assurance with recursive candidate deletions."""
    target_path = path or os.path.join(config.DOWNLOAD_DIR, filename)
    prompt_msg = existing_message
    while True:
        cumulative = _current_reserved_bytes(exclude=filename) + file_size
        projected = _projected_free_mb(cumulative)
        if projected >= config.MIN_FREE_DISK_MB:
            return True, prompt_msg
        exclude = {st.path for st in states.values()}
        exclude.add(target_path)
        candidate = _select_deletion_candidate(target_path, exclude)
        if not candidate:
            no_space = f"🛑 Storage not enough for {filename} and no deletable files found. Cancelling."
            if prompt_msg:
                await _safe_edit(prompt_msg, no_space)
            else:
                await throttle.send_message(event, no_space, reply_to=getattr(event, "id", None))
            log.error("No candidate for deletion; cancelling %s", filename)
            return False, None
        cand_name = os.path.basename(candidate)

        if TEST_AUTO_ACCEPT:
            with contextlib.suppress(OSError):
                os.remove(candidate)
            log.debug("[TEST] Auto-deleted %s", candidate)
            continue

        # Interactive prompt
        pid = uuid.uuid4().hex[:8]
        pending = PendingDeletion(filename=filename, candidate=cand_name)
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
        if prompt_msg:
            pending.message = await _safe_edit(prompt_msg, text, buttons=buttons)
        else:
            pending.message = await throttle.send_message(
                event, text, buttons=buttons, reply_to=getattr(event, "id", None)
            )
        if not pending.message:
            pending_deletions.pop(pid, None)
            return False, None
        prompt_msg = pending.message
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
            return False, None
        choice = pending.choice
        pending_deletions.pop(pid, None)
        st = states.get(filename)
        if st and st.cancelled:
            return False, None
        if choice != "yes":
            if pending.message:
                await _safe_edit(pending.message, f"🛑 Cancelled: insufficient space for {filename}")
            log.info("User declined deletion for %s", filename)
            return False, None
        with contextlib.suppress(OSError):
            os.remove(candidate)
        if pending.message:
            prompt_msg = await _safe_edit(pending.message, f"Deleted {cand_name}. Re-checking space...") or prompt_msg


async def download_with_retries(
    client: TelegramClient,
    document: Document,
    path: str,
    progress_cb: Callable[[int, int], Awaitable[None]],
    state: DownloadState,
    *,
    source_message: Any | None = None,
) -> bool:
    """Download with retries. Updates state only, no per-file messages."""
    media: Any = source_message or document
    retry = 0
    while retry <= config.MAX_RETRY_ATTEMPTS:
        try:
            if state.cancelled:
                raise CancelledDownload
            await wait_if_paused(state)
            result = await client.download_media(media, file=path, progress_callback=progress_cb)
            if result is None:
                log.warning("download_media returned None for %s", state.filename)
                return False
            return True
        except TimeoutError:
            retry += 1
            if retry > config.MAX_RETRY_ATTEMPTS:
                return False
            log.info("Download stalled for %s, retrying (%d/%d)", state.filename, retry, config.MAX_RETRY_ATTEMPTS)
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
) -> None:
    """Run a download. Progress is tracked in-memory; list messages are updated periodically."""
    state = _init_state(filename, path, file_size, event)

    await kodi.notify("Download Started", filename)
    log.info("Start download %s (%s)", filename, utils.humanize_size(file_size))
    async with throttle.handler_lock:
        await update_all_lists()

    progress_cb = create_progress_callback(filename, time.time(), RateLimiter(), state)
    source_msg = getattr(event, "message", None)

    try:
        success = await download_with_retries(client, document, path, progress_cb, state, source_message=source_msg)
        if not await _post_download_check(success, file_size, path, state, filename, event):
            return
        await _handle_success(filename, path, state, event)
    except Exception as e:
        await _handle_error(e, state, filename, path, event)
    finally:
        _final_cleanup(filename)
        async with throttle.handler_lock:
            await update_all_lists()


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


async def _post_download_check(
    success: bool,
    expected_size: int,
    path: str,
    state: DownloadState,
    filename: str,
    event: Any,
) -> bool:
    if success and validate_size(expected_size, path):
        return True
    if state.cancelled:
        if os.path.exists(path):
            try:
                os.remove(path)
                remove_empty_parents(path, [config.DOWNLOAD_DIR])
            except OSError:
                pass
        log.info("Cancelled %s", filename)
    else:
        await throttle.send_message(
            event,
            f"❌ Download incomplete: {filename}. Expected {utils.humanize_size(expected_size)}",
            reply_to=getattr(event, "id", None),
        )
        await kodi.notify("Download Failed", f"Incomplete: {filename}")
        log.error("Incomplete download %s", filename)
    return False


async def _handle_success(filename: str, path: str, state: DownloadState, event: Any) -> None:
    state.mark_completed()
    playing = await kodi.is_playing()
    text = (
        f"✅ Download complete: {filename}\nKodi playing something else. File ready."
        if playing
        else f"✅ Download complete: {filename}\nPlaying on Kodi..."
    )
    await throttle.send_message(event, text, reply_to=getattr(event, "id", None))
    if not playing:
        await kodi.play(path)
    await kodi.notify("Download Complete", filename)
    log.info("Completed %s", filename)


async def _handle_error(
    exc: Exception,
    state: DownloadState,
    filename: str,
    path: str,
    event: Any,
) -> None:
    if state.cancelled:
        if os.path.exists(path):
            try:
                os.remove(path)
                remove_empty_parents(path, [config.DOWNLOAD_DIR])
            except OSError:
                pass
        return
    err = str(exc)
    await throttle.send_message(
        event,
        f"❌ Error downloading {filename}: {err[:200]}",
        reply_to=getattr(event, "id", None),
    )
    await kodi.notify("Download Failed", err[:50])
    log.error("Download error %s: %s", filename, err)


async def _queued_runner(client: TelegramClient, qi: QueuedItem) -> None:
    """Runner for queued downloads: registers state for visibility, checks space, then downloads."""
    state = _init_state(qi.filename, qi.path, qi.size, qi.event)
    state.waiting_for_space = True
    async with throttle.handler_lock:
        await update_all_lists()
    try:
        ok, _space_msg = await _ensure_disk_space(qi.event, qi.filename, qi.size, qi.path)
        if not ok or state.cancelled:
            _final_cleanup(qi.filename)
            return
        state.waiting_for_space = False
        await run_download(client, qi.event, qi.document, qi.filename, qi.size, qi.path)
    except Exception:
        if qi.filename in states:
            _final_cleanup(qi.filename)
        raise


# ── Handler registration ──


def register_handlers(client: TelegramClient):
    """Register Telegram handlers and start queue worker."""
    global _queue_started
    if not _queue_started:
        queue.set_runner(_queued_runner)
        queue.ensure_worker(client.loop, client)
        _spawn(_periodic_list_updater())
        _queue_started = True
    log.debug("Queue worker started")

    _register_download_handler(client)
    _register_start_handler(client)
    _register_deletion_callbacks(client)
    _register_category_selection(client)
    register_list_handlers(client)


def _same_user(ev1, ev2):
    return getattr(ev1, "sender_id", None) == getattr(ev2, "sender_id", None)


async def _handle_active_duplicate(event, filename: str):
    """Send brief acknowledgement for duplicate active download request."""
    await throttle.send_message(
        event,
        f"⏳ Already downloading: {filename}",
        reply_to=getattr(event, "id", None),
    )


async def _handle_queued_duplicate(event, filename: str):
    """Send brief acknowledgement for duplicate queued download request."""
    await throttle.send_message(
        event,
        f"🕒 Already queued: {filename}",
        reply_to=getattr(event, "id", None),
    )


async def _do_enqueue(client: TelegramClient, document, filename, size, path, event):
    """Enqueue a download and send acknowledgement."""
    file_id = register_file_id(filename)
    qi = QueuedItem(filename, document, size, path, event, file_id=file_id)
    position = await queue.enqueue(qi)
    await throttle.send_message(
        event,
        f"📥 Queued (#{position}): {filename}\nUse /downloads to see the queue.",
        reply_to=getattr(event, "id", None),
    )
    await update_all_lists()


async def _start_direct_download(client: TelegramClient, event, document, filename, size, path):
    """Run a direct download. Called OUTSIDE _download_lock after pre-registering state."""
    try:
        async with queue.slot():
            st = states.get(filename)
            if not st or st.cancelled:
                _final_cleanup(filename)
                return
            st.waiting_for_space = True
            await update_all_lists()
            ok, _space_msg = await _ensure_disk_space(event, filename, size, path)
            if not ok or st.cancelled:
                _final_cleanup(filename)
                return
            st.waiting_for_space = False
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
        parsed = parse_filename(original_filename, text=(event.raw_text or None))
        ambiguous = parsed.category == "other" and parsed.year is not None
        if not ambiguous or not config.ORGANIZE_MEDIA:
            _path_tmp, normalized_name = build_final_path(original_filename, text=(event.raw_text or None))
            lookup_name = normalized_name
        else:
            lookup_name = original_filename

        direct_download = None
        async with _download_lock:
            active_state = states.get(lookup_name)
            if active_state:
                await _handle_active_duplicate(event, lookup_name)
                return
            queued_item = queue.items.get(lookup_name)
            if queued_item:
                await _handle_queued_duplicate(event, lookup_name)
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
            if len(states) >= config.MAX_CONCURRENT_DOWNLOADS or queue.items:
                await _do_enqueue(client, document, filename, size, path, event)
                log.debug("Enqueued %s", filename)
                return
            # Pre-register state so duplicates are detected after lock release
            register_file_id(filename)
            states[filename] = DownloadState(filename, path, size, original_event=event)
            direct_download = (document, filename, size, path)

        if direct_download:
            document, filename, size, path = direct_download
            await throttle.send_message(
                event,
                f"📥 Added: {filename}\nUse /downloads to see progress.",
                reply_to=getattr(event, "id", None),
            )
            _spawn(_start_direct_download(client, event, document, filename, size, path))
            log.debug("Started %s", filename)


def _register_start_handler(client: TelegramClient):
    HELP_TEXT = (
        "Send me a video or audio file — I'll download it and play it on Kodi.\n\n"
        "Commands:\n"
        "/downloads - show downloads & queue list\n"
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
        await throttle.send_message(event, HELP_TEXT)


def _register_deletion_callbacks(client: TelegramClient):
    pattern = b"del(ok|nx):"
    no_buttons = ReplyInlineMarkup(rows=[])

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
            if pending.message:
                await _safe_edit(
                    pending.message,
                    f"✅ Deleting {pending.candidate}...",
                    buttons=no_buttons,
                )
            await throttle.answer_callback(event, "Deleting", alert=False)
        else:
            pending.choice = "no"
            if pending.message:
                await _safe_edit(
                    pending.message,
                    f"🛑 Cancelled: insufficient space for {pending.filename}",
                    buttons=no_buttons,
                )
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
            if len(states) >= config.MAX_CONCURRENT_DOWNLOADS or queue.items:
                await _do_enqueue(client, document, final_name, size, path, orig_event)
                await throttle.answer_callback(event, "Queued", alert=False)
                return
            register_file_id(final_name)
            states[final_name] = DownloadState(final_name, path, size, original_event=orig_event)
            direct_download = (document, final_name, size, path)

        if direct_download:
            document, final_name, size, path = direct_download
            _spawn(_start_direct_download(client, orig_event, document, final_name, size, path))
        await throttle.answer_callback(event, "Started", alert=False)


__all__ = [
    "register_handlers",
    "run_download",
]
