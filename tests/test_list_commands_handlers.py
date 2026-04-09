"""Tests for downloader.list_commands handlers and helpers."""

from __future__ import annotations

import asyncio

import config
import downloader.list_commands as lc
import throttle
from downloader.list_commands import (
    build_downloads_list,
    get_status_text,
    handle_existing_lists_for_new_download,
    register_list_handlers,
)
from downloader.queue import QueuedItem
from downloader.state import DownloadState, MessageTracker, MessageType

# ── Fakes ──


class FakeClient:
    """Captures handler functions registered via client.on(...)."""

    def __init__(self):
        self.handlers = []

    def on(self, event_type):
        def decorator(fn):
            self.handlers.append(fn)
            return fn

        return decorator


class FakeMsg:
    """Stand-in for a Telegram message."""

    def __init__(self, msg_id=1):
        self.id = msg_id

    async def edit(self, text, **kw):
        pass


class FakeEvent:
    """Minimal event for handler tests."""

    def __init__(self, data=None):
        self.data = data or b""

    async def get_sender(self):
        return type("S", (), {"id": 1, "username": "testuser"})()

    async def respond(self, text, **kw):
        return FakeMsg()

    async def edit(self, text, **kw):
        return self

    async def answer(self, text=None, **kw):
        pass


class FakeQueue:
    """Minimal stand-in for DownloadQueue (only .items is accessed by handlers)."""

    def __init__(self, items=None):
        self.items = items if items is not None else {}


# ── Setup helper ──


def _setup(monkeypatch, *, allowed=True):
    """Patch throttle/config/utils; return (sent, edited, answered, tracker)."""
    sent, edited, answered = [], [], []
    tracker = MessageTracker()

    monkeypatch.setattr(config, "is_user_allowed", lambda uid, uname: allowed)

    msg = FakeMsg()

    async def fake_send(event, text, **kw):
        sent.append({"text": text, **kw})
        return msg

    async def fake_edit(event, text, **kw):
        edited.append({"text": text, **kw})
        return event

    async def fake_answer(event, text=None, **kw):
        answered.append({"text": text, **kw})

    monkeypatch.setattr(throttle, "send_message", fake_send)
    monkeypatch.setattr(throttle, "edit_message", fake_edit)
    monkeypatch.setattr(throttle, "answer_callback", fake_answer)
    monkeypatch.setattr(lc, "message_tracker", tracker)

    return sent, edited, answered, tracker


def _handlers():
    """Register list handlers on a FakeClient and return the handler list.

    Order: [0] downloads, [1] queue, [2] refresh_downloads,
           [3] refresh_queue
    """
    client = FakeClient()
    register_list_handlers(client)
    return client.handlers


# ── register_list_handlers (lines 19-22) ──


def test_register_list_handlers_registers_six_handlers():
    client = FakeClient()
    register_list_handlers(client)
    assert len(client.handlers) == 4


# ── _downloads handler (lines 26-56) ──


def test_downloads_unauthorized(monkeypatch):
    sent, _, _, _ = _setup(monkeypatch, allowed=False)
    monkeypatch.setattr(lc, "states", {})
    h = _handlers()
    asyncio.run(h[0](FakeEvent()))
    assert len(sent) == 1
    assert "Not authorized" in sent[0]["text"]


def test_downloads_empty_no_warning(monkeypatch):
    sent, _, _, tracker = _setup(monkeypatch)
    monkeypatch.setattr(lc, "states", {})
    h = _handlers()
    asyncio.run(h[0](FakeEvent()))
    assert any("No active downloads" in s["text"] for s in sent)
    assert len(tracker.get_messages("__downloads_list__")) == 1


def test_downloads_with_active_states(monkeypatch):
    sent, _, _, tracker = _setup(monkeypatch)
    st = DownloadState("dl.mp4", "/tmp/dl.mp4", 1000)
    st.update_progress(500, 50, "1 MB/s")
    monkeypatch.setattr(lc, "states", {"dl.mp4": st})
    h = _handlers()
    asyncio.run(h[0](FakeEvent()))
    assert any("Active Downloads" in s["text"] for s in sent)
    assert len(tracker.get_messages("__downloads_list__")) == 1
    assert len(tracker.get_messages("dl.mp4")) == 1


# ── _queue_list handler (lines 60-90) ──


def test_queue_list_unauthorized(monkeypatch):
    sent, _, _, _ = _setup(monkeypatch, allowed=False)
    monkeypatch.setattr(lc, "queue", FakeQueue())
    h = _handlers()
    asyncio.run(h[1](FakeEvent()))
    assert len(sent) == 1
    assert "Not authorized" in sent[0]["text"]


def test_queue_list_empty(monkeypatch):
    sent, _, _, tracker = _setup(monkeypatch)
    monkeypatch.setattr(lc, "queue", FakeQueue())
    h = _handlers()
    asyncio.run(h[1](FakeEvent()))
    assert any("No queued downloads" in s["text"] for s in sent)
    assert len(tracker.get_messages("__queue_list__")) == 1


def test_queue_list_with_items(monkeypatch):
    sent, _, _, tracker = _setup(monkeypatch)
    qi = QueuedItem("q.mp4", object(), 10, "/tmp/q.mp4", FakeEvent(), file_id="abc123")
    monkeypatch.setattr(lc, "queue", FakeQueue({"q.mp4": qi}))
    h = _handlers()
    asyncio.run(h[1](FakeEvent()))
    assert any("Queued Downloads" in s["text"] for s in sent)
    assert len(tracker.get_messages("__queue_list__")) == 1
    assert len(tracker.get_messages("q.mp4")) == 1


# ── _refresh_downloads callback (lines 94-105) ──


def test_refresh_downloads_empty(monkeypatch):
    _, edited, answered, _ = _setup(monkeypatch)
    monkeypatch.setattr(lc, "states", {})
    h = _handlers()
    asyncio.run(h[2](FakeEvent()))
    assert any("No active downloads" in e["text"] for e in edited)
    assert any(a["text"] == "Refreshed" for a in answered)


def test_refresh_downloads_with_states(monkeypatch):
    _, edited, answered, _ = _setup(monkeypatch)
    st = DownloadState("r.mp4", "/tmp/r.mp4", 500)
    st.update_progress(100, 20, "500 KB/s")
    monkeypatch.setattr(lc, "states", {"r.mp4": st})
    h = _handlers()
    asyncio.run(h[2](FakeEvent()))
    assert any("Active Downloads" in e["text"] for e in edited)
    assert any(a["text"] == "Refreshed" for a in answered)


# ── _refresh_queue callback (lines 107-118) ──


def test_refresh_queue_empty(monkeypatch):
    _, edited, answered, _ = _setup(monkeypatch)
    monkeypatch.setattr(lc, "queue", FakeQueue())
    h = _handlers()
    asyncio.run(h[3](FakeEvent()))
    assert any("No queued downloads" in e["text"] for e in edited)
    assert any(a["text"] == "Refreshed" for a in answered)


def test_refresh_queue_with_items(monkeypatch):
    _, edited, answered, _ = _setup(monkeypatch)
    qi = QueuedItem("rq.mp4", object(), 10, "/tmp/rq.mp4", FakeEvent(), file_id="xyz")
    monkeypatch.setattr(lc, "queue", FakeQueue({"rq.mp4": qi}))
    h = _handlers()
    asyncio.run(h[3](FakeEvent()))
    assert any("Queued Downloads" in e["text"] for e in edited)
    assert any(a["text"] == "Refreshed" for a in answered)


# ── get_status_text ──


def test_get_status_text_cancelled():
    st = DownloadState("f.mp4", "/tmp/f.mp4", 100)
    st.mark_cancelled()
    assert get_status_text(st) == "\U0001f6d1 Cancelled"


def test_get_status_text_completed():
    st = DownloadState("f.mp4", "/tmp/f.mp4", 100)
    st.mark_completed()
    assert get_status_text(st) == "\u2705 Completed"


def test_get_status_text_paused():
    st = DownloadState("f.mp4", "/tmp/f.mp4", 100)
    st.mark_paused()
    assert get_status_text(st) == "\u23f8\ufe0f Paused"


def test_get_status_text_downloading():
    st = DownloadState("f.mp4", "/tmp/f.mp4", 100)
    assert get_status_text(st) == "\u23ec Downloading"


# ── build_downloads_list (lines 170-205) ──


def test_build_downloads_list_paused_state():
    st = DownloadState("p.mp4", "/tmp/p.mp4", 1000)
    st.mark_paused()
    text, buttons = build_downloads_list({"p.mp4": st})
    assert "Paused" in text
    resume_found = any(hasattr(b, "data") and b.data and b"resume:" in b.data for row in buttons for b in row)
    assert resume_found


def test_build_downloads_list_starting_state():
    st = DownloadState("s.mp4", "/tmp/s.mp4", 1000)
    text, _buttons = build_downloads_list({"s.mp4": st})
    assert "Starting..." in text


def test_build_downloads_list_all_cancelled_and_completed():
    """When every state is cancelled or completed, show 'No active downloads'."""
    st1 = DownloadState("a.mp4", "/tmp/a.mp4", 100)
    st1.mark_cancelled()
    st2 = DownloadState("b.mp4", "/tmp/b.mp4", 100)
    st2.mark_completed()
    text, _buttons = build_downloads_list({"a.mp4": st1, "b.mp4": st2})
    assert "No active downloads" in text


# ── handle_existing_lists_for_new_download (lines 224-230) ──


def test_handle_existing_lists_registers_with_tracked_messages(monkeypatch):
    tracker = MessageTracker()
    monkeypatch.setattr(lc, "message_tracker", tracker)
    msg = FakeMsg()
    tracker.register_message("__downloads_list__", msg, MessageType.DOWNLOAD_LIST)

    handle_existing_lists_for_new_download("new.mp4")

    msgs = tracker.get_messages("new.mp4")
    assert len(msgs) == 1
    assert msgs[0].message_type == MessageType.DOWNLOAD_LIST


def test_handle_existing_lists_with_multiple_list_types(monkeypatch):
    tracker = MessageTracker()
    monkeypatch.setattr(lc, "message_tracker", tracker)
    tracker.register_message("__downloads_list__", FakeMsg(1), MessageType.DOWNLOAD_LIST)
    tracker.register_message("__queue_list__", FakeMsg(2), MessageType.QUEUE_LIST)

    handle_existing_lists_for_new_download("multi.mp4")

    msgs = tracker.get_messages("multi.mp4")
    assert len(msgs) == 2
    types = {m.message_type for m in msgs}
    assert MessageType.DOWNLOAD_LIST in types
    assert MessageType.QUEUE_LIST in types


def test_handle_existing_lists_empty_tracker(monkeypatch):
    tracker = MessageTracker()
    monkeypatch.setattr(lc, "message_tracker", tracker)
    handle_existing_lists_for_new_download("new.mp4")
    assert len(tracker.get_messages("new.mp4")) == 0


# ── waiting_for_space support ──


def test_get_status_text_waiting_for_space():
    st = DownloadState("f.mp4", "/tmp/f.mp4", 100)
    st.waiting_for_space = True
    assert "Waiting for space" in get_status_text(st)


def test_build_downloads_list_waiting_for_space():
    st = DownloadState("space.mp4", "/tmp/space.mp4", 1000)
    st.waiting_for_space = True
    text, buttons = build_downloads_list({"space.mp4": st})
    assert "Waiting for space..." in text
    # Only Cancel button (no Pause/Resume)
    row = buttons[0]
    labels = [b.text for b in row]
    assert any("Cancel" in label for label in labels)
    assert not any("Pause" in label for label in labels)
    assert not any("Resume" in label for label in labels)


# ── update_all_download_lists ──


def test_update_all_download_lists_edits_sentinel_messages(monkeypatch):
    """update_all_download_lists edits all messages tracked under __downloads_list__."""
    from downloader.list_commands import update_all_download_lists

    edited_calls = []
    tracker = MessageTracker()
    monkeypatch.setattr(lc, "message_tracker", tracker)

    msg1 = FakeMsg(10)
    msg2 = FakeMsg(20)
    tracker.register_message("__downloads_list__", msg1, MessageType.DOWNLOAD_LIST)
    tracker.register_message("__downloads_list__", msg2, MessageType.DOWNLOAD_LIST)

    st = DownloadState("dl.mp4", "/tmp/dl.mp4", 1000)
    st.update_progress(500, 50, "1 MB/s")
    monkeypatch.setattr(lc, "states", {"dl.mp4": st})

    async def fake_edit(target, text, **kw):
        edited_calls.append({"target_id": target.id, "text": text})
        return target

    monkeypatch.setattr(throttle, "edit_message", fake_edit)

    asyncio.run(update_all_download_lists())
    assert len(edited_calls) == 2
    assert all("Active Downloads" in c["text"] for c in edited_calls)
    assert {c["target_id"] for c in edited_calls} == {10, 20}


def test_update_all_download_lists_empty_states(monkeypatch):
    """When no active downloads, list messages show 'No active downloads'."""
    from downloader.list_commands import update_all_download_lists

    edited_calls = []
    tracker = MessageTracker()
    monkeypatch.setattr(lc, "message_tracker", tracker)
    monkeypatch.setattr(lc, "states", {})

    msg = FakeMsg(30)
    tracker.register_message("__downloads_list__", msg, MessageType.DOWNLOAD_LIST)

    async def fake_edit(target, text, **kw):
        edited_calls.append({"text": text})
        return target

    monkeypatch.setattr(throttle, "edit_message", fake_edit)

    asyncio.run(update_all_download_lists())
    assert len(edited_calls) == 1
    assert "No active downloads" in edited_calls[0]["text"]


def test_update_all_download_lists_tolerates_edit_failure(monkeypatch):
    """Edit failure is silently suppressed."""
    from downloader.list_commands import update_all_download_lists

    tracker = MessageTracker()
    monkeypatch.setattr(lc, "message_tracker", tracker)
    monkeypatch.setattr(lc, "states", {})

    msg = FakeMsg(40)
    tracker.register_message("__downloads_list__", msg, MessageType.DOWNLOAD_LIST)

    async def failing_edit(target, text, **kw):
        raise RuntimeError("edit boom")

    monkeypatch.setattr(throttle, "edit_message", failing_edit)

    # Should not raise
    asyncio.run(update_all_download_lists())


def test_update_all_download_lists_no_tracked_messages(monkeypatch):
    """No sentinel messages means no edits (no-op)."""
    from downloader.list_commands import update_all_download_lists

    tracker = MessageTracker()
    monkeypatch.setattr(lc, "message_tracker", tracker)
    monkeypatch.setattr(lc, "states", {})

    # Should not raise with empty tracker
    asyncio.run(update_all_download_lists())


def test_update_all_download_lists_skips_frozen_message(monkeypatch):
    """Frozen list messages (showing cancel confirmation) are skipped during update."""
    from downloader.list_commands import update_all_download_lists
    from downloader.state import frozen_list_msg_ids

    edited_calls = []
    tracker = MessageTracker()
    monkeypatch.setattr(lc, "message_tracker", tracker)

    frozen_msg = FakeMsg(50)
    ok_msg = FakeMsg(51)
    tracker.register_message("__downloads_list__", frozen_msg, MessageType.DOWNLOAD_LIST)
    tracker.register_message("__downloads_list__", ok_msg, MessageType.DOWNLOAD_LIST)

    st = DownloadState("dl.mp4", "/tmp/dl.mp4", 1000)
    st.update_progress(500, 50, "1 MB/s")
    monkeypatch.setattr(lc, "states", {"dl.mp4": st})

    frozen_list_msg_ids.add(50)

    async def fake_edit(target, text, **kw):
        edited_calls.append({"target_id": target.id, "text": text})
        return target

    monkeypatch.setattr(throttle, "edit_message", fake_edit)

    try:
        asyncio.run(update_all_download_lists())
        # Only the non-frozen message should be edited
        assert len(edited_calls) == 1
        assert edited_calls[0]["target_id"] == 51
    finally:
        frozen_list_msg_ids.discard(50)


# ── Duplicate Cancel button fix ──


def test_build_downloads_list_waiting_for_space_single_cancel():
    """waiting_for_space row has exactly one Cancel button (not two)."""
    st = DownloadState("space2.mp4", "/tmp/space2.mp4", 1000)
    st.waiting_for_space = True
    _text, buttons = build_downloads_list({"space2.mp4": st})
    row = buttons[0]
    cancel_count = sum(1 for b in row if hasattr(b, "text") and "Cancel" in b.text)
    assert cancel_count == 1
