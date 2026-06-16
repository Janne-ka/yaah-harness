"""ScriptedBackend — cursor durability + exhaustion behavior.

Assessment #7 / cluster 3 B2: the in-memory cursor was process-state, so each
--resume rebuilt the backend and returned seq[0] forever (the "grill: one
question then hangs" offline bug). Now MAX(in-memory, content-derived). And
exhaustion is consistent with FakeBackend (returns default, not seq[-1]).

Run: cd yaah && PYTHONPATH=src python3 tests/test_scripted_backend.py
"""
from __future__ import annotations

import asyncio

from yaah.agents import ScriptedBackend


async def scenario_in_memory_cursor_advances_per_call() -> None:
    # base case: same-process, two calls return seq[0] then seq[1]
    be = ScriptedBackend({"m": ["A", "B", "C"]})
    assert await be.complete("any", model="m") == "A"
    assert await be.complete("any", model="m") == "B"
    assert await be.complete("any", model="m") == "C"


async def scenario_content_cursor_survives_resume() -> None:
    # the durability fix: a FRESH backend (rebuilt by --resume) reading turn 2's
    # prompt (which embeds turn 1's response in transcript) returns seq[1] not seq[0].
    be1 = ScriptedBackend({"m": ["RESP-ONE", "RESP-TWO", "RESP-THREE"]})
    assert await be1.complete("Q1: hello?", model="m") == "RESP-ONE"
    # simulate cross-process: rebuild backend, fresh in-memory cursor
    be2 = ScriptedBackend({"m": ["RESP-ONE", "RESP-TWO", "RESP-THREE"]})
    next_prompt = "Q1: hello?\nA1: RESP-ONE\nQ2: now what?"
    assert await be2.complete(next_prompt, model="m") == "RESP-TWO"


async def scenario_exhaustion_returns_default_by_default() -> None:
    # consistent with FakeBackend: exhaustion -> default (was: silently
    # repeating seq[-1], which masked over-invocation bugs).
    be = ScriptedBackend({"m": ["A"]}, default="DONE")
    assert await be.complete("p1", model="m") == "A"
    assert await be.complete("p2", model="m") == "DONE"
    assert await be.complete("p3", model="m") == "DONE"


async def scenario_exhaustion_can_raise_when_loud_is_wanted() -> None:
    be = ScriptedBackend({"m": ["A"]}, on_exhaustion="raise")
    assert await be.complete("p1", model="m") == "A"
    try:
        await be.complete("p2", model="m")
    except IndexError:
        return
    raise AssertionError("expected IndexError on exhaustion")


async def scenario_exhaustion_repeat_last_for_legacy_callers() -> None:
    be = ScriptedBackend({"m": ["A", "B"]}, on_exhaustion="repeat_last")
    await be.complete("p1", model="m")
    await be.complete("p2", model="m")
    assert await be.complete("p3", model="m") == "B"


async def scenario_unknown_model_returns_default() -> None:
    be = ScriptedBackend({"a": ["X"]}, default="FALLBACK")
    assert await be.complete("p", model="b") == "FALLBACK"


def scenario_invalid_on_exhaustion_raises() -> None:
    try:
        ScriptedBackend({"m": ["A"]}, on_exhaustion="bogus")
    except ValueError:
        return
    raise AssertionError("expected ValueError on bad on_exhaustion")


async def scenario_bare_string_value_is_one_reply_not_chars() -> None:
    # first-run trap (DX verify 2026-06-10): list("{...}") explodes a bare
    # string into characters — each attempt answered `{`. A string value now
    # means a one-reply script.
    be = ScriptedBackend({"m": '{"summary":"hello"}'})
    assert await be.complete("any", model="m") == '{"summary":"hello"}'
    assert await be.complete("any", model="m") == ""  # exhausted → default


async def main() -> None:
    await scenario_in_memory_cursor_advances_per_call()
    await scenario_content_cursor_survives_resume()
    await scenario_exhaustion_returns_default_by_default()
    await scenario_exhaustion_can_raise_when_loud_is_wanted()
    await scenario_exhaustion_repeat_last_for_legacy_callers()
    await scenario_unknown_model_returns_default()
    await scenario_bare_string_value_is_one_reply_not_chars()
    scenario_invalid_on_exhaustion_raises()
    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
