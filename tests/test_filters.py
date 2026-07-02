"""Filter port + adapters — R10 unit checks for AroundKeywordFilter,
RedactFilter, CallTargetFilter, plus the build path that parses
`filters: {name: {type, ...args}}` from pipeline JSON onto an agent.

Run: cd yaah && PYTHONPATH=src python3 tests/test_filters.py
"""
from __future__ import annotations

import asyncio
import sys
import types

from yaah.adapters.filters import AroundKeywordFilter, CallTargetFilter, RedactFilter
from yaah.agents import FakeProvider, make_envelope_get_tool
from yaah.core import Envelope, Kind
from yaah.filter_factories import build_filter
from yaah.filters import Filter


async def scenario_around_keyword_default_single_hit() -> None:
    text = "L1\nL2\nL3 X\nL4\nL5\nL6\nL7 X\nL8"
    f = AroundKeywordFilter()
    out = await f.apply(text, keyword="X", n=1)
    assert out == "L2\nL3 X\nL4", out  # only the first hit, ±1 line


async def scenario_around_keyword_all_merges_adjacent() -> None:
    text = "L1\nL2\nL3 X\nL4\nL5 X\nL6\nL7"
    f = AroundKeywordFilter()
    out = await f.apply(text, keyword="X", n=2, all=True)
    # two hits at L3 and L5 with ±2 windows → ranges overlap → one merged block
    # NOT two windows separated by "..."
    assert out == "L1\nL2\nL3 X\nL4\nL5 X\nL6\nL7", out


async def scenario_around_keyword_all_separate_with_ellipsis() -> None:
    text = "L1\nL2 X\nL3\nL4\nL5\nL6\nL7 X\nL8"
    f = AroundKeywordFilter()
    out = await f.apply(text, keyword="X", n=1, all=True)
    assert out == "L1\nL2 X\nL3\n...\nL6\nL7 X\nL8", out


async def scenario_around_keyword_no_hit_returns_empty() -> None:
    f = AroundKeywordFilter()
    assert await f.apply("a\nb\nc", keyword="z") == ""


async def scenario_around_keyword_passthrough_non_string() -> None:
    f = AroundKeywordFilter()
    assert await f.apply(42, keyword="x") == 42


async def scenario_redact_replaces_configured_patterns() -> None:
    f = RedactFilter(default_patterns=[r"api_key=\S+", r"\b\d{16}\b"],
                     replacement="[X]")
    out = await f.apply("api_key=secret123 and card 1234567890123456 last")
    assert out == "[X] and card [X] last", out


async def scenario_redact_model_args_cannot_widen_policy() -> None:
    # the model invokes the filter with extra params (here "patterns") trying to
    # WIDEN the redaction policy beyond what the AUTHOR pinned — the filter must
    # ignore them. This is the allow-list rule applied to filter params.
    f = RedactFilter(default_patterns=[r"secret"])
    out = await f.apply("secret and other text",
                        patterns=[r".*"], replacement="[FAIL]")
    assert out == "[REDACTED] and other text", out  # author's pattern + author's replacement


async def scenario_redact_bad_regex_is_skipped_not_raised() -> None:
    # a config typo shouldn't crash the agent; the bad pattern is just inert
    f = RedactFilter(default_patterns=[r"[unterminated", r"secret"])
    out = await f.apply("the secret thing")
    assert out == "the [REDACTED] thing", out


async def scenario_call_target_fn_bridge() -> None:
    # fn:filter_test_helpers:_upper used as a Filter — bridges to call_target
    f = CallTargetFilter("fn:filter_test_helpers:_upper")
    out = await f.apply("hello world")
    assert out == "HELLO WORLD", out


def _upper(args):
    return args["value"].upper()


_helpers = types.ModuleType("filter_test_helpers")
_helpers._upper = _upper
sys.modules["filter_test_helpers"] = _helpers


async def scenario_filter_factory_builds_each_type() -> None:
    ak = build_filter({"type": "around_keyword"})
    assert isinstance(ak, AroundKeywordFilter) and isinstance(ak, Filter)
    rd = build_filter({"type": "redact", "patterns": [r"\d+"], "replacement": "#"})
    assert isinstance(rd, RedactFilter)
    assert await rd.apply("a1 b22 c") == "a# b# c"
    ct = build_filter({"type": "call_target", "target": "fn:filter_test_helpers:_upper"})
    assert isinstance(ct, CallTargetFilter)


async def scenario_filter_factory_rejects_unknown() -> None:
    try:
        build_filter({"type": "nope"})
        raise AssertionError("should have raised")
    except ValueError as e:
        assert "nope" in str(e)


async def scenario_filter_factory_rejects_missing_type() -> None:
    try:
        build_filter({})
        raise AssertionError("should have raised")
    except ValueError as e:
        assert "type" in str(e)


async def scenario_envelope_get_dispatches_to_filter_port() -> None:
    env = Envelope(Kind.TASK, {"text": "L1\nL2 hit\nL3\nL4 hit\nL5"},
                   {"correlation_id": "R"})
    tool = make_envelope_get_tool(
        env, expose={"payload": ["text"], "header": []},
        filters={"around": AroundKeywordFilter()}, max_chars=200)
    r = await tool.impl({"key": "text", "filter": {
        "name": "around", "keyword": "hit", "n": 0, "all": True}})
    assert r["value"] == "L2 hit\n...\nL4 hit", r


async def scenario_envelope_get_filter_unknown_lists_available() -> None:
    env = Envelope(Kind.TASK, {"text": "x"}, {"correlation_id": "R"})
    tool = make_envelope_get_tool(
        env, expose={"payload": ["text"], "header": []},
        filters={"around": AroundKeywordFilter(), "redact": RedactFilter([r"\d"])},
        max_chars=200)
    r = await tool.impl({"key": "text", "filter": {"name": "nope"}})
    assert "error" in r and r["available"] == ["around", "redact"], r


async def scenario_build_parses_filters_spec() -> None:
    from yaah.build import build
    cfg = {
        "nodes": {"role:r": {"type": "agent", "template": "t", "model": "fake:x",
                             "expose": {"payload": ["text"]},
                             "filters": {
                                 "around":  {"type": "around_keyword"},
                                 "scrub":   {"type": "redact", "patterns": [r"\d+"]}}}},
        "graph": {"start": "s", "stages": {"s": {"node": "role:r"}}},
    }
    h = build(cfg, backend=FakeProvider(default="{}"))
    assert h.graph.stages["s"].node == "role:r"  # parsed without error


async def main() -> None:
    await scenario_around_keyword_default_single_hit()
    await scenario_around_keyword_all_merges_adjacent()
    await scenario_around_keyword_all_separate_with_ellipsis()
    await scenario_around_keyword_no_hit_returns_empty()
    await scenario_around_keyword_passthrough_non_string()
    await scenario_redact_replaces_configured_patterns()
    await scenario_redact_model_args_cannot_widen_policy()
    await scenario_redact_bad_regex_is_skipped_not_raised()
    await scenario_call_target_fn_bridge()
    await scenario_filter_factory_builds_each_type()
    await scenario_filter_factory_rejects_unknown()
    await scenario_filter_factory_rejects_missing_type()
    await scenario_envelope_get_dispatches_to_filter_port()
    await scenario_envelope_get_filter_unknown_lists_available()
    await scenario_build_parses_filters_spec()
    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
