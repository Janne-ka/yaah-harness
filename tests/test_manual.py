"""test_manual — falsification tests for yaah.manual.build_manual().

The manual is a PROJECTION of the enforcing tables (node registry, factory
maps, validate key tables), so the tests attack the projection: every live
enum value must appear, an injected fake entry must appear on regeneration
(proving generation, not hand-writing), the char budget must hold, and the
embedded example must round-trip through validate_root / validate_pipeline —
the manual's example can never rot.

Run: cd yaah && PYTHONPATH=src python3 tests/test_manual.py
"""
from __future__ import annotations

import json
import re
import types

import yaah.manual as manual
from yaah import runtime_factories as rf
from yaah import validate as v
from yaah.build.builders import default_registry
from yaah.validate import validate_pipeline, validate_root

CHAR_CEILING = 24000


def _tick(name: str) -> str:
    return "`{}`".format(name)


def every_node_type_appears() -> None:
    text = manual.build_manual()
    for t in default_registry()._builders:
        assert _tick(t) in text, "node type {!r} missing from manual".format(t)


def every_factory_enum_value_appears() -> None:
    text = manual.build_manual()
    maps = [rf._PROVIDER_TYPES, rf._PROMPT_TYPES, rf._DATA_SOURCE_TYPES,
            rf._DATA_SINK_TYPES, rf._MCP_TYPES, rf._STATE_TYPES,
            rf._TRANSPORT_TYPES, rf._TRACE_SINK_TYPES]
    for m in maps:
        for t in m:
            assert _tick(t) in text, "enum value {!r} missing from manual".format(t)
    assert "modes: " + ", ".join(rf._TRACE_MODES) in text


def every_root_and_stage_key_appears() -> None:
    text = manual.build_manual()
    for k in v._ROOT_KEYS:
        assert _tick(k) in text, "root key {!r} missing from manual".format(k)
    for k in v._STAGE_KEYS:
        assert _tick(k) in text, "stage key {!r} missing from manual".format(k)
    for k in v._GRAPH_KEYS:
        assert _tick(k) in text, "graph key {!r} missing from manual".format(k)


def injected_factory_entry_appears_and_leaves() -> None:
    fake = "zz_fake_provider_proof"
    assert _tick(fake) not in manual.build_manual()
    rf._PROVIDER_TYPES[fake] = (lambda spec, base: None, frozenset({"knob"}))
    try:
        text = manual.build_manual()
        assert _tick(fake) in text, "manual is not generated from _PROVIDER_TYPES"
        assert "knob" in text, "per-type spec keys are not projected"
    finally:
        del rf._PROVIDER_TYPES[fake]
    assert _tick(fake) not in manual.build_manual()


def injected_node_type_appears_and_leaves() -> None:
    fake = "zz_fake_node_proof"

    def _build_fake(spec, ctx):
        """A registry-injected probe node for the generation proof."""
        return None

    real = manual.default_registry
    reg = types.SimpleNamespace(_builders=dict(default_registry()._builders))
    reg._builders[fake] = _build_fake
    manual.default_registry = lambda: reg
    try:
        text = manual.build_manual()
        assert _tick(fake) in text, "manual is not generated from the registry"
        assert "registry-injected probe node" in text, \
            "builder docstring fallback not used for an unknown node type"
    finally:
        manual.default_registry = real
    assert _tick(fake) not in manual.build_manual()


def char_budget_holds() -> None:
    n = len(manual.build_manual())
    assert n < CHAR_CEILING, "manual is {} chars, budget is {}".format(n, CHAR_CEILING)
    assert n > 4000, "manual is suspiciously small ({} chars)".format(n)


def _fenced_json_blocks(text: str):
    return [json.loads(m) for m in re.findall(r"```json\n(.*?)\n```", text, re.S)]


def embedded_example_validates() -> None:
    blocks = _fenced_json_blocks(manual.build_manual())
    assert len(blocks) == 2, "expected exactly root + pipeline blocks, got {}".format(len(blocks))
    pipelines = [b for b in blocks if "graph" in b]
    roots = [b for b in blocks if "graph" not in b]
    assert len(pipelines) == 1 and len(roots) == 1, "cannot tell root from pipeline"
    validate_root(roots[0])
    validate_pipeline(pipelines[0])
    known = set(default_registry()._builders)
    for role, node in pipelines[0]["nodes"].items():
        assert node["type"] in known, "example node {!r} uses unknown type {!r}".format(
            role, node["type"])


def broken_example_would_be_caught() -> None:
    blocks = _fenced_json_blocks(manual.build_manual())
    pipeline = next(b for b in blocks if "graph" in b)
    bad = json.loads(json.dumps(pipeline))
    first = next(iter(bad["graph"]["stages"].values()))
    first["then"] = "no_such_stage"
    try:
        validate_pipeline(bad)
    except ValueError:
        pass
    else:
        raise AssertionError("validate_pipeline blessed a broken example — "
                             "the round-trip test would never catch rot")


def main() -> None:
    every_node_type_appears()
    every_factory_enum_value_appears()
    every_root_and_stage_key_appears()
    injected_factory_entry_appears_and_leaves()
    injected_node_type_appears_and_leaves()
    char_budget_holds()
    embedded_example_validates()
    broken_example_would_be_caught()
    print("ok")


if __name__ == "__main__":
    main()
