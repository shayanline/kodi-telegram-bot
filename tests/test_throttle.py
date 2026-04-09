from __future__ import annotations

import asyncio

from telethon.errors import FloodWaitError, MessageNotModifiedError

import throttle

# Save original _enqueue before conftest patches it
_real_enqueue = throttle._enqueue


def _run_with_publisher(coro):
    """Run a coroutine with a fresh publisher, cleaning up afterward."""

    async def _wrapper():
        # Restore real _enqueue so the publisher queue is exercised
        throttle._enqueue = _real_enqueue
        throttle._last_call = 0.0
        throttle._seq = 0
        throttle._TG_MIN_INTERVAL = 0
        throttle.start_publisher()
        try:
            return await coro
        finally:
            throttle.stop_publisher()

    return asyncio.run(_wrapper())


# ── _Item ordering ──


def test_item_ordering():
    """Lower priority number is higher priority; ties broken by sequence."""

    async def _run():
        loop = asyncio.get_running_loop()
        items = [
            throttle._Item(2, 3, loop.create_future(), None, (), {}),
            throttle._Item(0, 2, loop.create_future(), None, (), {}),
            throttle._Item(1, 1, loop.create_future(), None, (), {}),
        ]
        assert sorted(items) == [items[1], items[2], items[0]]

    asyncio.run(_run())


# ── edit_message ──


def test_edit_message_success():
    class Msg:
        async def edit(self, text, **kw):
            self.text = text

    msg = Msg()

    async def go():
        return await throttle.edit_message(msg, "hello")

    result = _run_with_publisher(go())
    assert result is msg


def test_edit_message_not_modified():
    class Msg:
        async def edit(self, text, **kw):
            raise MessageNotModifiedError(None)

    msg = Msg()
    result = _run_with_publisher(throttle.edit_message(msg, "hello"))
    assert result is msg


def test_edit_message_other_error():
    class Msg:
        async def edit(self, text, **kw):
            raise RuntimeError("gone")

    result = _run_with_publisher(throttle.edit_message(Msg(), "hello"))
    assert result is None


# ── send_message ──


def test_send_message_success():
    class Target:
        async def respond(self, text, **kw):
            return "new_msg"

    result = _run_with_publisher(throttle.send_message(Target(), "hi"))
    assert result == "new_msg"


def test_send_message_error():
    class Target:
        async def respond(self, text, **kw):
            raise RuntimeError("fail")

    result = _run_with_publisher(throttle.send_message(Target(), "hi"))
    assert result is None


# ── answer_callback ──


def test_answer_callback_success():
    class Ev:
        answered = False

        async def answer(self, text=None, **kw):
            self.answered = True

    ev = Ev()
    asyncio.run(throttle.answer_callback(ev, "ok"))
    assert ev.answered


def test_answer_callback_suppresses_error():
    class Ev:
        async def answer(self, text=None, **kw):
            raise RuntimeError("fail")

    asyncio.run(throttle.answer_callback(Ev(), "ok"))


# ── FloodWait retry ──


def test_flood_wait_retries():
    """Publisher retries the call after a FloodWaitError."""
    attempt = {"n": 0}

    class Msg:
        async def edit(self, text, **kw):
            attempt["n"] += 1
            if attempt["n"] == 1:
                raise FloodWaitError(request=None, capture=0)

    msg = Msg()
    result = _run_with_publisher(throttle.edit_message(msg, "hello"))
    assert result is msg
    assert attempt["n"] == 2


# ── Priority integration ──


def test_priority_constants():
    assert throttle.PRIORITY_USER < throttle.PRIORITY_EVENT < throttle.PRIORITY_PROGRESS


def test_edit_message_accepts_priority():
    class Msg:
        async def edit(self, text, **kw):
            pass

    result = _run_with_publisher(throttle.edit_message(Msg(), "hi", priority=throttle.PRIORITY_PROGRESS))
    assert result is not None


def test_send_message_accepts_priority():
    class Target:
        async def respond(self, text, **kw):
            return "msg"

    result = _run_with_publisher(throttle.send_message(Target(), "hi", priority=throttle.PRIORITY_EVENT))
    assert result == "msg"


def test_start_publisher_creates_queue_and_task():
    async def _run():
        throttle._queue = None
        throttle._publisher_task = None
        throttle.start_publisher()
        assert throttle._queue is not None
        assert throttle._publisher_task is not None
        throttle.stop_publisher()

    asyncio.run(_run())
