"""Tests for downloader.list_commands handlers and helpers."""

from __future__ import annotations

import asyncio

import config
import downloader.list_commands as lc
import throttle
from downloader.list_commands import (
    PAGE_SIZE,
    build_unified_list,
    register_list_handlers,
    update_all_lists,
)
from downloader.queue import QueuedItem
from downloader.state import ChatDownloadList, DownloadState, chat_lists, states

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

    async def delete(self):
        pass


class FakeEvent:
    """Minimal event for handler tests."""

    def __init__(self, data=None, chat_id=100, sender_id=1):
        self.data = data or b""
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.is_private = True
        self.document = None
        self.raw_text = "/downloads"

    async def get_sender(self):
        return type("S", (), {"id": self.sender_id, "username": "testuser"})()

    async def respond(self, text, **kw):
        return FakeMsg()

    async def edit(self, text, **kw):
        return self

    async def answer(self, text=None, **kw):
        pass


class FakeQueue:
    """Minimal stand-in for DownloadQueue (only .items is accessed by helpers)."""

    def __init__(self, items=None):
        self.items = items if items is not None else {}

    def cancel(self, filename):
        qi = self.items.get(filename)
        if not qi or qi.cancelled:
            return False
        qi.cancelled = True
        self.items.pop(filename, None)
        return True


# ── Setup helper ──


def _setup(monkeypatch, *, allowed=True):
    """Patch throttle/config; return (sent, edited, answered)."""
    sent, edited, answered = [], [], []

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

    return sent, edited, answered


def _cleanup():
    """Clean up global mutable state after each test."""
    states.clear()
    chat_lists.clear()


# ── build_unified_list: empty state ──


def test_build_unified_list_empty(monkeypatch):
    """No active downloads and no queued items produces a 'no items' message."""
    monkeypatch.setattr(lc, "states", {})
    monkeypatch.setattr(lc, "queue", FakeQueue())
    try:
        text, buttons = build_unified_list()
        assert "No active downloads or queued items" in text
        assert buttons == []
    finally:
        _cleanup()


# ── build_unified_list: active downloads only ──


def test_build_unified_list_active_downloading(monkeypatch):
    """Active download with progress shows percentage and speed."""
    st = DownloadState("movie.mp4", "/tmp/movie.mp4", 1_000_000)
    st.update_progress(500_000, 50, "1 MB/s")
    monkeypatch.setattr(lc, "states", {"movie.mp4": st})
    monkeypatch.setattr(lc, "queue", FakeQueue())
    try:
        text, _buttons = build_unified_list()
        assert "Downloads & Queue" in text
        assert "50%" in text
        assert "1 MB/s" in text
        assert "movie.mp4" in text
    finally:
        _cleanup()


def test_build_unified_list_active_paused(monkeypatch):
    """Paused download shows paused icon and details."""
    st = DownloadState("paused.mp4", "/tmp/paused.mp4", 1_000_000)
    st.update_progress(300_000, 30, "0 B/s")
    st.mark_paused()
    monkeypatch.setattr(lc, "states", {"paused.mp4": st})
    monkeypatch.setattr(lc, "queue", FakeQueue())
    try:
        text, _buttons = build_unified_list()
        assert "Paused" in text
        assert "paused.mp4" in text
    finally:
        _cleanup()


def test_build_unified_list_active_waiting_for_space(monkeypatch):
    """Waiting for space download shows appropriate text."""
    st = DownloadState("space.mp4", "/tmp/space.mp4", 1_000_000)
    st.waiting_for_space = True
    monkeypatch.setattr(lc, "states", {"space.mp4": st})
    monkeypatch.setattr(lc, "queue", FakeQueue())
    try:
        text, _buttons = build_unified_list()
        assert "Waiting for space" in text
        assert "space.mp4" in text
    finally:
        _cleanup()


def test_build_unified_list_active_starting(monkeypatch):
    """Download with no progress shows 'Starting...'."""
    st = DownloadState("start.mp4", "/tmp/start.mp4", 1_000_000)
    monkeypatch.setattr(lc, "states", {"start.mp4": st})
    monkeypatch.setattr(lc, "queue", FakeQueue())
    try:
        text, _buttons = build_unified_list()
        assert "Starting..." in text
        assert "start.mp4" in text
    finally:
        _cleanup()


def test_build_unified_list_skips_cancelled_and_completed(monkeypatch):
    """Cancelled and completed downloads are excluded from the list."""
    st1 = DownloadState("done.mp4", "/tmp/done.mp4", 100)
    st1.mark_completed()
    st2 = DownloadState("gone.mp4", "/tmp/gone.mp4", 100)
    st2.mark_cancelled()
    st3 = DownloadState("active.mp4", "/tmp/active.mp4", 1000)
    st3.update_progress(500, 50, "1 MB/s")
    monkeypatch.setattr(lc, "states", {"done.mp4": st1, "gone.mp4": st2, "active.mp4": st3})
    monkeypatch.setattr(lc, "queue", FakeQueue())
    try:
        text, _buttons = build_unified_list()
        assert "done.mp4" not in text
        assert "gone.mp4" not in text
        assert "active.mp4" in text
    finally:
        _cleanup()


# ── build_unified_list: queued items only ──


def test_build_unified_list_queued_only(monkeypatch):
    """Queued items appear with 'Queued #N' position."""
    qi = QueuedItem("queued.mp4", object(), 10, "/tmp/queued.mp4", FakeEvent(), file_id="abc123")
    monkeypatch.setattr(lc, "states", {})
    monkeypatch.setattr(lc, "queue", FakeQueue({"queued.mp4": qi}))
    try:
        text, _buttons = build_unified_list()
        assert "queued.mp4" in text
        assert "Queued #1" in text
    finally:
        _cleanup()


def test_build_unified_list_multiple_queued(monkeypatch):
    """Multiple queued items show correct positions."""
    from collections import OrderedDict

    items = OrderedDict()
    qi1 = QueuedItem("q1.mp4", object(), 10, "/tmp/q1.mp4", FakeEvent(), file_id="aaa")
    qi2 = QueuedItem("q2.mp4", object(), 10, "/tmp/q2.mp4", FakeEvent(), file_id="bbb")
    items["q1.mp4"] = qi1
    items["q2.mp4"] = qi2
    fq = FakeQueue(items)
    monkeypatch.setattr(lc, "states", {})
    monkeypatch.setattr(lc, "queue", fq)
    try:
        text, _buttons = build_unified_list()
        assert "Queued #1" in text
        assert "Queued #2" in text
    finally:
        _cleanup()


# ── build_unified_list: both active and queued ──


def test_build_unified_list_active_and_queued(monkeypatch):
    """Active downloads appear before queued items."""
    st = DownloadState("dl.mp4", "/tmp/dl.mp4", 1000)
    st.update_progress(500, 50, "1 MB/s")
    qi = QueuedItem("q.mp4", object(), 10, "/tmp/q.mp4", FakeEvent(), file_id="abc")
    monkeypatch.setattr(lc, "states", {"dl.mp4": st})
    monkeypatch.setattr(lc, "queue", FakeQueue({"q.mp4": qi}))
    try:
        text, _buttons = build_unified_list()
        # Active appears first (lower index)
        dl_pos = text.index("dl.mp4")
        q_pos = text.index("q.mp4")
        assert dl_pos < q_pos
        assert "Queued" in text
        assert "50%" in text
    finally:
        _cleanup()


# ── Pagination ──


def test_build_unified_list_pagination_more_than_page_size(monkeypatch):
    """More than PAGE_SIZE items produces multiple pages."""
    active_states = {}
    for i in range(PAGE_SIZE + 2):
        fn = f"file{i}.mp4"
        st = DownloadState(fn, f"/tmp/{fn}", 1000)
        st.update_progress(500, 50, "1 MB/s")
        active_states[fn] = st
    monkeypatch.setattr(lc, "states", active_states)
    monkeypatch.setattr(lc, "queue", FakeQueue())
    try:
        text_p0, buttons_p0 = build_unified_list(page=0)
        assert "(1/2)" in text_p0
        # Should have Next button on page 0, no Prev
        nav_row = buttons_p0[-1]
        nav_labels = [str(b) for b in nav_row]
        assert any("Next" in lbl for lbl in nav_labels)
        assert not any("Prev" in lbl for lbl in nav_labels)

        text_p1, buttons_p1 = build_unified_list(page=1)
        assert "(2/2)" in text_p1
        # Should have Prev button on page 1, no Next
        nav_row = buttons_p1[-1]
        nav_labels = [str(b) for b in nav_row]
        assert any("Prev" in lbl for lbl in nav_labels)
        assert not any("Next" in lbl for lbl in nav_labels)
    finally:
        _cleanup()


def test_build_unified_list_pagination_page_count(monkeypatch):
    """Verify page count with 16 items is exactly 2 pages."""
    active_states = {}
    for i in range(16):
        fn = f"item{i}.mp4"
        st = DownloadState(fn, f"/tmp/{fn}", 1000)
        active_states[fn] = st
    monkeypatch.setattr(lc, "states", active_states)
    monkeypatch.setattr(lc, "queue", FakeQueue())
    try:
        text, _ = build_unified_list(page=0)
        assert "(1/2)" in text
    finally:
        _cleanup()


def test_build_unified_list_page_clamped(monkeypatch):
    """Page number is clamped to valid range."""
    st = DownloadState("single.mp4", "/tmp/single.mp4", 1000)
    monkeypatch.setattr(lc, "states", {"single.mp4": st})
    monkeypatch.setattr(lc, "queue", FakeQueue())
    try:
        # Requesting page 999 should clamp to page 0 (only 1 page)
        text, _ = build_unified_list(page=999)
        assert "(1/1)" in text
        assert "single.mp4" in text

        # Requesting negative page should clamp to 0
        text2, _ = build_unified_list(page=-5)
        assert "(1/1)" in text2
    finally:
        _cleanup()


def test_build_unified_list_nav_has_cancel_all(monkeypatch):
    """Navigation row always includes a Cancel All button."""
    st = DownloadState("r.mp4", "/tmp/r.mp4", 1000)
    monkeypatch.setattr(lc, "states", {"r.mp4": st})
    monkeypatch.setattr(lc, "queue", FakeQueue())
    try:
        _, buttons = build_unified_list()
        nav_row = buttons[-1]
        nav_data = [getattr(b, "data", b"").decode() if hasattr(b, "data") else str(b) for b in nav_row]
        assert any("dl_cancelall" in d for d in nav_data)
    finally:
        _cleanup()


# ── _format_active_line ──


def test_format_active_line_downloading():
    st = DownloadState("dl.mp4", "/tmp/dl.mp4", 1_000_000)
    st.update_progress(500_000, 50, "1 MB/s")
    line = lc._format_active_line(1, "dl.mp4", st)
    assert "50%" in line
    assert "1 MB/s" in line
    assert "\u23ec" in line  # ⏬


def test_format_active_line_paused_with_progress():
    st = DownloadState("p.mp4", "/tmp/p.mp4", 1_000_000)
    st.update_progress(300_000, 30, "0 B/s")
    st.mark_paused()
    line = lc._format_active_line(1, "p.mp4", st)
    assert "Paused" in line
    assert "30%" in line
    assert "\u23f8" in line  # ⏸


def test_format_active_line_paused_no_progress():
    st = DownloadState("p0.mp4", "/tmp/p0.mp4", 1_000_000)
    st.mark_paused()
    line = lc._format_active_line(1, "p0.mp4", st)
    assert "Paused" in line
    # No percentage or size detail when nothing downloaded
    assert "0%" not in line


def test_format_active_line_waiting_for_space():
    st = DownloadState("ws.mp4", "/tmp/ws.mp4", 1_000_000)
    st.waiting_for_space = True
    line = lc._format_active_line(1, "ws.mp4", st)
    assert "Waiting for space" in line
    assert "\u23f3" in line  # ⏳


def test_format_active_line_starting():
    st = DownloadState("s.mp4", "/tmp/s.mp4", 1_000_000)
    line = lc._format_active_line(1, "s.mp4", st)
    assert "Starting..." in line
    assert "\u23ec" in line  # ⏬


# ── _active_buttons ──


def test_active_buttons_downloading(monkeypatch):
    """Downloading state produces number+icon, Pause, and Cancel buttons."""
    st = DownloadState("dl.mp4", "/tmp/dl.mp4", 1000)
    st.update_progress(500, 50, "1 MB/s")
    btns = lc._active_buttons("dl.mp4", st, 1)
    texts = [b.text for b in btns]
    assert any("Pause" in t for t in texts)
    assert any("Cancel" in t for t in texts)
    assert not any("Resume" in t for t in texts)
    assert len(btns) == 3
    # Cancel is always last
    assert "Cancel" in btns[-1].text
    # Number button is first
    assert "1" in btns[0].text


def test_active_buttons_paused(monkeypatch):
    """Paused state produces number+icon, Resume, and Cancel buttons."""
    st = DownloadState("p.mp4", "/tmp/p.mp4", 1000)
    st.mark_paused()
    btns = lc._active_buttons("p.mp4", st, 2)
    texts = [b.text for b in btns]
    assert any("Resume" in t for t in texts)
    assert any("Cancel" in t for t in texts)
    assert not any("Pause" in t for t in texts)
    assert len(btns) == 3
    assert "Cancel" in btns[-1].text


def test_active_buttons_waiting_for_space(monkeypatch):
    """Waiting-for-space state shows number, spacer, and Cancel (3 buttons for alignment)."""
    st = DownloadState("ws.mp4", "/tmp/ws.mp4", 1000)
    st.waiting_for_space = True
    btns = lc._active_buttons("ws.mp4", st, 3)
    texts = [b.text for b in btns]
    assert any("Cancel" in t for t in texts)
    assert not any("Pause" in t for t in texts)
    assert not any("Resume" in t for t in texts)
    assert len(btns) == 3
    assert "Cancel" in btns[-1].text


# ── _queued_buttons ──


def test_queued_buttons_returns_number_and_cancel():
    """Queued item buttons include number+icon and Cancel."""
    qi = QueuedItem("q.mp4", object(), 10, "/tmp/q.mp4", FakeEvent(), file_id="abc123")
    btns = lc._queued_buttons("q.mp4", qi, 1)
    texts = [b.text for b in btns]
    assert any("Cancel" in t for t in texts)
    assert len(btns) == 2
    assert "Cancel" in btns[-1].text


def test_queued_buttons_data_contains_file_id():
    """Button callback data contains the correct file_id."""
    qi = QueuedItem("q.mp4", object(), 10, "/tmp/q.mp4", FakeEvent(), file_id="abc123")
    btns = lc._queued_buttons("q.mp4", qi, 1)
    data_values = [b.data.decode() for b in btns]
    assert any("abc123" in d for d in data_values)


# ── _queue_position ──


def test_queue_position_returns_correct_position(monkeypatch):
    """_queue_position returns 1-based position."""
    from collections import OrderedDict

    items = OrderedDict()
    items["first.mp4"] = QueuedItem("first.mp4", object(), 10, "/tmp", FakeEvent())
    items["second.mp4"] = QueuedItem("second.mp4", object(), 10, "/tmp", FakeEvent())
    items["third.mp4"] = QueuedItem("third.mp4", object(), 10, "/tmp", FakeEvent())
    monkeypatch.setattr(lc, "queue", FakeQueue(items))
    assert lc._queue_position("first.mp4") == 1
    assert lc._queue_position("second.mp4") == 2
    assert lc._queue_position("third.mp4") == 3


def test_queue_position_returns_zero_for_missing(monkeypatch):
    """_queue_position returns 0 for non-existent filename."""
    monkeypatch.setattr(lc, "queue", FakeQueue())
    assert lc._queue_position("nope.mp4") == 0


# ── Cancel All ──


def test_cancel_all_button_in_nav_row(monkeypatch):
    """Cancel All button appears in the navigation row."""
    st = DownloadState("x.mp4", "/tmp/x.mp4", 1000)
    monkeypatch.setattr(lc, "states", {"x.mp4": st})
    monkeypatch.setattr(lc, "queue", FakeQueue())
    try:
        _, buttons = build_unified_list()
        nav_row = buttons[-1]
        assert any(getattr(b, "data", b"") == b"dl_cancelall" for b in nav_row)
    finally:
        _cleanup()


def test_cancel_all_confirm_cancels_active_and_queued(monkeypatch):
    """Confirming cancel-all cancels all active and queued downloads."""
    _sent, _edited, answered = _setup(monkeypatch)

    st1 = DownloadState("a.mp4", "/tmp/a.mp4", 100)
    st2 = DownloadState("b.mp4", "/tmp/b.mp4", 100)
    qi = QueuedItem("c.mp4", object(), 100, "/tmp/c.mp4", FakeEvent(), file_id="ccc")
    fq = FakeQueue({"c.mp4": qi})
    monkeypatch.setattr(lc, "states", {"a.mp4": st1, "b.mp4": st2})
    monkeypatch.setattr(lc, "queue", fq)

    ev = FakeEvent(data=b"dl_cay", chat_id=100)
    chat_lists[100] = ChatDownloadList(chat_id=100, message=FakeMsg(), page=0, confirming="all")

    client = FakeClient()
    register_list_handlers(client)
    handler = [h for h in client.handlers if getattr(h, "__wrapped__", h).__name__ == "_cancel_all_confirm"]
    assert handler, "cancel_all_confirm handler not found"

    try:
        asyncio.run(handler[0](ev))
        assert st1.cancelled
        assert st2.cancelled
        assert "c.mp4" not in fq.items
        assert any("3" in str(a.get("text", "")) for a in answered)
    finally:
        _cleanup()


def test_cancel_all_deny_does_nothing(monkeypatch):
    """Denying cancel-all leaves downloads intact."""
    _setup(monkeypatch)

    st = DownloadState("keep.mp4", "/tmp/keep.mp4", 100)
    monkeypatch.setattr(lc, "states", {"keep.mp4": st})
    monkeypatch.setattr(lc, "queue", FakeQueue())

    ev = FakeEvent(data=b"dl_can", chat_id=100)
    chat_lists[100] = ChatDownloadList(chat_id=100, message=FakeMsg(), page=0, confirming="all")

    client = FakeClient()
    register_list_handlers(client)
    handler = [h for h in client.handlers if getattr(h, "__wrapped__", h).__name__ == "_cancel_all_confirm"]

    try:
        asyncio.run(handler[0](ev))
        assert not st.cancelled
    finally:
        _cleanup()


# ── register_list_handlers ──


def test_register_list_handlers_registers_correct_count():
    """register_list_handlers registers the expected number of handlers."""
    client = FakeClient()
    register_list_handlers(client)
    # _register_downloads_handler: 1
    # _register_list_callbacks: 1 (page)
    # _register_control_callbacks: 8 (info, pause/resume, cancel, cancel confirm,
    #                                  qcancel, qcancel confirm, cancel all, cancel all confirm)
    assert len(client.handlers) == 10


# ── Callback pattern collision guard ──


def test_cancelall_pattern_does_not_match_confirm():
    """dl_cancelall must not match the cancel-all confirm pattern (dl_ca(y|n)$)."""
    import re

    p_confirm = re.compile(rb"dl_ca(y|n)$")
    assert p_confirm.match(b"dl_cancelall") is None, "dl_cancelall must not trigger _cancel_all_confirm"
    assert p_confirm.match(b"dl_cay") is not None
    assert p_confirm.match(b"dl_can") is not None


# ── update_all_lists ──


def test_update_all_lists_edits_tracked_messages(monkeypatch):
    """update_all_lists edits messages for all tracked chats."""
    edited_calls = []

    msg = FakeMsg(10)
    chat_lists[100] = ChatDownloadList(chat_id=100, message=msg, page=0)

    st = DownloadState("dl.mp4", "/tmp/dl.mp4", 1000)
    st.update_progress(500, 50, "1 MB/s")
    monkeypatch.setattr(lc, "states", {"dl.mp4": st})
    monkeypatch.setattr(lc, "queue", FakeQueue())

    async def fake_edit(target, text, **kw):
        edited_calls.append({"target_id": target.id, "text": text})
        return target

    monkeypatch.setattr(throttle, "edit_message", fake_edit)

    try:
        asyncio.run(update_all_lists())
        assert len(edited_calls) == 1
        assert edited_calls[0]["target_id"] == 10
        assert "dl.mp4" in edited_calls[0]["text"]
    finally:
        _cleanup()


def test_update_all_lists_skips_confirming(monkeypatch):
    """Chats with confirming set are skipped during update."""
    edited_calls = []

    msg_confirm = FakeMsg(20)
    msg_ok = FakeMsg(21)
    chat_lists[200] = ChatDownloadList(chat_id=200, message=msg_confirm, page=0, confirming="some_id")
    chat_lists[201] = ChatDownloadList(chat_id=201, message=msg_ok, page=0)

    monkeypatch.setattr(lc, "states", {})
    monkeypatch.setattr(lc, "queue", FakeQueue())

    async def fake_edit(target, text, **kw):
        edited_calls.append({"target_id": target.id})
        return target

    monkeypatch.setattr(throttle, "edit_message", fake_edit)

    try:
        asyncio.run(update_all_lists())
        # Only the non-confirming chat should be edited
        assert len(edited_calls) == 1
        assert edited_calls[0]["target_id"] == 21
    finally:
        _cleanup()


def test_update_all_lists_skips_none_message(monkeypatch):
    """Chats with no message set are skipped."""
    edited_calls = []

    chat_lists[300] = ChatDownloadList(chat_id=300, message=None, page=0)

    monkeypatch.setattr(lc, "states", {})
    monkeypatch.setattr(lc, "queue", FakeQueue())

    async def fake_edit(target, text, **kw):
        edited_calls.append({})
        return target

    monkeypatch.setattr(throttle, "edit_message", fake_edit)

    try:
        asyncio.run(update_all_lists())
        assert len(edited_calls) == 0
    finally:
        _cleanup()


def test_update_all_lists_handles_edit_failure_sends_replacement(monkeypatch):
    """When edit returns None (failure), a replacement message is sent."""
    sent_calls = []
    msg = FakeMsg(30)
    replacement = FakeMsg(31)
    chat_lists[400] = ChatDownloadList(chat_id=400, message=msg, page=0)

    monkeypatch.setattr(lc, "states", {})
    monkeypatch.setattr(lc, "queue", FakeQueue())

    async def failing_edit(target, text, **kw):
        return None  # Simulate edit failure

    async def fake_send(target, text, **kw):
        sent_calls.append({"target_id": target.id})
        return replacement

    monkeypatch.setattr(throttle, "edit_message", failing_edit)
    monkeypatch.setattr(throttle, "send_message", fake_send)

    try:
        asyncio.run(update_all_lists())
        assert len(sent_calls) == 1
        assert sent_calls[0]["target_id"] == 30  # sent to original message
        # Replacement message should be tracked
        assert chat_lists[400].message is replacement
    finally:
        _cleanup()


def test_update_all_lists_edit_failure_send_also_fails(monkeypatch):
    """When both edit and send fail, the chat is removed from tracking."""
    msg = FakeMsg(40)
    chat_lists[500] = ChatDownloadList(chat_id=500, message=msg, page=0)

    monkeypatch.setattr(lc, "states", {})
    monkeypatch.setattr(lc, "queue", FakeQueue())

    async def failing_edit(target, text, **kw):
        return None

    async def failing_send(target, text, **kw):
        return None

    monkeypatch.setattr(throttle, "edit_message", failing_edit)
    monkeypatch.setattr(throttle, "send_message", failing_send)

    try:
        asyncio.run(update_all_lists())
        assert 500 not in chat_lists
    finally:
        _cleanup()


def test_update_all_lists_tolerates_exception(monkeypatch):
    """Exception during edit is silently suppressed."""
    msg = FakeMsg(50)
    chat_lists[600] = ChatDownloadList(chat_id=600, message=msg, page=0)

    monkeypatch.setattr(lc, "states", {})
    monkeypatch.setattr(lc, "queue", FakeQueue())

    async def exploding_edit(target, text, **kw):
        raise RuntimeError("edit boom")

    monkeypatch.setattr(throttle, "edit_message", exploding_edit)

    try:
        asyncio.run(update_all_lists())  # Should not raise
    finally:
        _cleanup()


def test_update_all_lists_no_tracked_chats(monkeypatch):
    """No tracked chats means no-op."""
    monkeypatch.setattr(lc, "states", {})
    monkeypatch.setattr(lc, "queue", FakeQueue())
    try:
        asyncio.run(update_all_lists())  # Should not raise
    finally:
        _cleanup()


def test_update_all_lists_preserves_page(monkeypatch):
    """Update uses the stored page number for each chat."""
    edited_calls = []
    msg = FakeMsg(60)
    # Put 10 items so there are 2 pages
    active_states = {}
    for i in range(10):
        fn = f"file{i}.mp4"
        st = DownloadState(fn, f"/tmp/{fn}", 1000)
        active_states[fn] = st

    chat_lists[700] = ChatDownloadList(chat_id=700, message=msg, page=1)

    monkeypatch.setattr(lc, "states", active_states)
    monkeypatch.setattr(lc, "queue", FakeQueue())

    async def fake_edit(target, text, **kw):
        edited_calls.append({"text": text})
        return target

    monkeypatch.setattr(throttle, "edit_message", fake_edit)

    try:
        asyncio.run(update_all_lists())
        assert len(edited_calls) == 1
        assert "(2/2)" in edited_calls[0]["text"]
    finally:
        _cleanup()


# ── _send_list_message ──


def test_send_list_message_deletes_old_message(monkeypatch):
    """Sending a new list message deletes the previous one for that chat."""
    deleted = []
    old_msg = FakeMsg(70)
    original_delete = old_msg.delete

    async def tracking_delete():
        deleted.append(True)
        return await original_delete()

    old_msg.delete = tracking_delete
    chat_lists[800] = ChatDownloadList(chat_id=800, message=old_msg, page=0)

    monkeypatch.setattr(lc, "states", {})
    monkeypatch.setattr(lc, "queue", FakeQueue())

    new_msg = FakeMsg(71)

    async def fake_send(event, text, **kw):
        return new_msg

    monkeypatch.setattr(throttle, "send_message", fake_send)

    try:
        asyncio.run(lc._send_list_message(FakeEvent(chat_id=800), 800))
        assert len(deleted) == 1
        assert chat_lists[800].message is new_msg
    finally:
        _cleanup()


# ── PAGE_SIZE constant ──


def test_page_size_is_eight():
    """PAGE_SIZE is 8."""
    assert PAGE_SIZE == 8


# ── _total_pages and _total_items ──


def test_total_pages_empty(monkeypatch):
    """Empty state returns 1 page."""
    monkeypatch.setattr(lc, "states", {})
    monkeypatch.setattr(lc, "queue", FakeQueue())
    assert lc._total_pages() == 1


def test_total_pages_exactly_page_size(monkeypatch):
    """Exactly PAGE_SIZE items is 1 page."""
    active = {}
    for i in range(PAGE_SIZE):
        fn = f"f{i}.mp4"
        active[fn] = DownloadState(fn, f"/tmp/{fn}", 100)
    monkeypatch.setattr(lc, "states", active)
    monkeypatch.setattr(lc, "queue", FakeQueue())
    assert lc._total_pages() == 1


def test_total_pages_one_over(monkeypatch):
    """PAGE_SIZE + 1 items is 2 pages."""
    active = {}
    for i in range(PAGE_SIZE + 1):
        fn = f"f{i}.mp4"
        active[fn] = DownloadState(fn, f"/tmp/{fn}", 100)
    monkeypatch.setattr(lc, "states", active)
    monkeypatch.setattr(lc, "queue", FakeQueue())
    assert lc._total_pages() == 2


def test_total_items_counts_active_and_queued(monkeypatch):
    """_total_items counts both active and queued."""
    st = DownloadState("a.mp4", "/tmp/a.mp4", 100)
    qi = QueuedItem("q.mp4", object(), 10, "/tmp", FakeEvent())
    monkeypatch.setattr(lc, "states", {"a.mp4": st})
    monkeypatch.setattr(lc, "queue", FakeQueue({"q.mp4": qi}))
    assert lc._total_items() == 2
