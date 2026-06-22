"""Sibling-module dispatch fns for test_agent_loop.py.

Used by: tests/test_agent_loop.py — the loop's `fn:` dispatch resolves via
`import_callable`, so the dispatch fns must live in a module reachable by the
SAME canonical import name the test inspects. Putting them in
`test_agent_loop.py` itself would create two module instances (one as
`__main__`, one as `tests.test_agent_loop`); the call log would be split
between them and assertions would fail.
"""
from __future__ import annotations


calls_log = []   # cleared by each test that asserts against it


async def tool_ok(args):
    calls_log.append(("ok", dict(args)))
    return "ran with {!r}".format(args)


async def tool_boom(args):
    calls_log.append(("boom", dict(args)))
    raise RuntimeError("kaboom")
