from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from telethon.errors import FloodWaitError, MessageNotModifiedError

import throttle

# ── serialized decorator ──


def test_serialized_runs_function():
    @throttle.serialized
    async def handler(x):
        return x * 2

    assert asyncio.run(handler(5)) == 10


def test_serialized_preserves_order():
    results: list[int] = []

    @throttle.serialized
    async def handler(x):
        results.append(x)

    async def _run():
        await asyncio.gather(handler(1), handler(2), handler(3))

    asyncio.run(_run())
    assert results == [1, 2, 3]


# ── edit_message ──


def test_edit_message_success():
    class Msg:
        async def edit(self, text, **kw):
            self.text = text

    msg = Msg()
    result = asyncio.run(throttle.edit_message(msg, "hello"))
    assert result is msg


def test_edit_message_not_modified():
    class Msg:
        async def edit(self, text, **kw):
            raise MessageNotModifiedError(None)

    msg = Msg()
    result = asyncio.run(throttle.edit_message(msg, "hello"))
    assert result is msg


def test_edit_message_other_error():
    class Msg:
        async def edit(self, text, **kw):
            raise RuntimeError("gone")

    result = asyncio.run(throttle.edit_message(Msg(), "hello"))
    assert result is None


# ── send_message ──


def test_send_message_success():
    class Target:
        async def respond(self, text, **kw):
            return "new_msg"

    result = asyncio.run(throttle.send_message(Target(), "hi"))
    assert result == "new_msg"


def test_send_message_error():
    class Target:
        async def respond(self, text, **kw):
            raise RuntimeError("fail")

    result = asyncio.run(throttle.send_message(Target(), "hi"))
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


# ── FloodWaitError retry ──


def test_tg_call_retries_on_flood_wait(monkeypatch):
    monkeypatch.setattr(throttle, "_TG_MIN_INTERVAL", 0)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    attempt = {"n": 0}

    async def flaky(*a, **k):
        attempt["n"] += 1
        if attempt["n"] == 1:
            raise FloodWaitError(request=None, capture=0)
        return "ok"

    async def _run():
        result = await throttle._tg_call(flaky)
        assert result == "ok"
        assert attempt["n"] == 2

    asyncio.run(_run())
