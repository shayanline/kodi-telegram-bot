from __future__ import annotations

import asyncio
import contextlib
import os
import signal

from telethon import TelegramClient

import config
import kodi
from downloader.manager import register_handlers, validate_size
from downloader.queue import queue
from downloader.state import states
from filemanager import register_filemanager
from kodiremote import register_kodi_remote
from kodirestart import register_kodi_restart
from logger import log
from utils import remove_empty_parents


async def startup_message() -> None:
    try:
        await kodi.notify("Telegram Bot", "Ready for private media uploads")
    except Exception as e:
        log.warning("Startup notification failed: %s", e)


def main() -> None:
    asyncio.run(_main())


async def _main():
    client, shutdown_event = await _setup_client()
    loop = asyncio.get_running_loop()

    async def shutdown():
        await _graceful_shutdown(client, shutdown_event)

    _install_signal_handlers(loop, shutdown)
    try:
        await client.run_until_disconnected()
    finally:
        if not shutdown_event.is_set():
            await shutdown()


async def _setup_client():
    config.validate()
    client = TelegramClient("bot", config.API_ID, config.API_HASH, catch_up=True)
    register_handlers(client)
    register_filemanager(client)
    register_kodi_remote(client)
    register_kodi_restart(client)
    await client.start(bot_token=config.BOT_TOKEN)
    try:  # Explicit catch-up so we know backlog is processed before announcing ready.
        await client.catch_up()
        log.debug("Initial catch_up completed")
    except Exception as e:
        log.warning("catch_up error: %s", e)
    await startup_message()
    log.info("Bridge running - send a video or audio file to this bot in a private chat.")
    return client, asyncio.Event()


async def _graceful_shutdown(client, shutdown_event: asyncio.Event):
    if shutdown_event.is_set():
        return
    log.info("Shutting down gracefully...")
    shutdown_event.set()
    # Snapshot to avoid mutation during iteration
    snapshot = tuple(states.values())
    for st in snapshot:
        st.mark_cancelled()
        try:
            if st.message:
                # Remove buttons when signalling shutdown cancellation
                await st.message.edit(f"🛑 Cancelling (shutdown): {st.filename}", buttons=[])
        except Exception:
            pass
    with contextlib.suppress(Exception):
        await asyncio.wait_for(queue.stop(), timeout=6)
    removed = _cleanup_partials(snapshot)
    if removed:
        log.info("Removed %d partial file(s)", removed)
    await client.disconnect()


def _cleanup_partials(active_snapshot):
    """Delete incomplete files for cancelled active or queued items (best-effort)."""

    def _maybe_remove(path: str, expected: int) -> int:
        try:
            if os.path.exists(path) and (expected == 0 or not validate_size(expected, path)):
                os.remove(path)
                remove_empty_parents(path, [config.DOWNLOAD_DIR])
                return 1
        except Exception:
            return 0
        return 0

    removed = 0
    for st in active_snapshot:
        removed += _maybe_remove(st.path, st.size)
    for qi in queue.items.values():
        removed += _maybe_remove(qi.path, qi.size)
    return removed


def _install_signal_handlers(loop, shutdown_coro):
    def trigger():
        loop.create_task(shutdown_coro())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, trigger)
        except NotImplementedError:  # pragma: no cover
            signal.signal(sig, lambda *_: loop.create_task(shutdown_coro()))


if __name__ == "__main__":  # pragma: no cover
    main()
