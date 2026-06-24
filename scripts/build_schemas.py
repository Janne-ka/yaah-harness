"""build_schemas — generate JSON Schemas for root + pipeline configs from
the engine's own validation tables.

Used by: contributors regenerating `schemas/*.schema.json` after touching
`src/yaah/validate.py` or the factory tables in `src/yaah/runtime_factories.py`.
Wired into the test suite via `tests/test_schemas_drift.py` (running this
script must produce output matching the committed schemas — catches drift
at suite time).

Where: a small build helper, sibling of `scripts/build_catalog.py` which
auto-generates `docs/module-catalog.md` from the same engine tables. Both
follow the "the code is the truth; the doc/schema is derived" pattern.

Why: end-users (and AI coding agents) authoring YAAH configs in VS Code /
JetBrains / Cursor get autocomplete + error highlighting for free when the
config file references the schema via `$schema:`. Removes the why-not §1.3
"stringly-typed magic strings with no IDE help" complaint without forcing
the user to memorize the surface.

Run: `python3 scripts/build_schemas.py`
Writes: `schemas/root.schema.json`, `schemas/pipeline.schema.json`
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Tuple

# Make the engine importable when running from the repo root.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from yaah import validate as v                             # noqa: E402
from yaah import runtime_factories as rf                   # noqa: E402
from yaah.build.builders import default_registry           # noqa: E402

# The set of node types ships from the same place the runtime uses to build
# them — no parallel list to drift.
_NODE_TYPES = default_registry()._builders                 # noqa: SLF001


SCHEMA_DRAFT = "http://json-schema.org/draft-07/schema#"

# Each top-level root key falls into one of these categories. Mirrors the
# tables in validate.py — keep the import there as the truth.
_TYPED_BLOCK_FACTORY = {
    "transport": rf._TRANSPORT_TYPES,
    "state":     rf._STATE_TYPES,
}
_NAMED_MAP_FACTORY = {
    "providers":      rf._BACKEND_TYPES,
    "prompt_sources": rf._PROMPT_TYPES,
    "data_sources":   rf._DATA_SOURCE_TYPES,
    "data_sinks":     rf._DATA_SINK_TYPES,
    "mcp_sources":    rf._MCP_TYPES,
}


def _typed_block_schema(types_map: Dict[str, Any]) -> Dict[str, Any]:
    """A schema for `{type: <one of>, ...}` shape. `additionalProperties`
    stays true so per-type spec keys (which differ by type and are
    auto-known to the factory) don't have to be enumerated here — the
    runtime's `_check_typed_entry` rejects unknown keys per type."""
    return {
        "type": "object",
        "required": ["type"],
        "properties": {
            "type": {"type": "string", "enum": sorted(types_map.keys())},
        },
        "additionalProperties": True,
    }


def _named_map_schema(types_map: Dict[str, Any]) -> Dict[str, Any]:
    """A schema for `{<name>: {type: ..., ...}, ...}` — the providers /
    prompt_sources / data_sources shape."""
    return {
        "type": "object",
        "additionalProperties": _typed_block_schema(types_map),
    }


def build_root_schema() -> Dict[str, Any]:
    props: Dict[str, Any] = {}

    for k, types_map in _TYPED_BLOCK_FACTORY.items():
        props[k] = _typed_block_schema(types_map)

    for k, types_map in _NAMED_MAP_FACTORY.items():
        props[k] = _named_map_schema(types_map)

    # the matching default_* string keys — must match one of the named-map
    # entries; we don't constrain to that here (the runtime checks), just
    # require a string.
    for k in v._STRING_KEYS:
        props[k] = {"type": "string"}

    for k in v._BOOL_KEYS:
        props[k] = {"type": "boolean"}

    # trace block — its own typed shape (mode enum + capture list + sinks list)
    props["trace"] = {
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "mode": {"type": "string", "enum": list(rf._TRACE_MODES)},
            "capture": {
                "type": "array",
                "items": {"type": "string", "enum": sorted(_capture_names())},
            },
            "sinks": {
                "type": "array",
                "items": _typed_block_schema(rf._TRACE_SINK_TYPES),
            },
            "topic": {"type": "string"},
            "buffer_max": {"type": "integer", "minimum": 1},
        },
    }

    # input + pipeline accept a path OR an inline object
    props["pipeline"] = {"oneOf": [{"type": "string"}, {"type": "object"}]}
    props["input"]    = {"oneOf": [{"type": "string"}, {"type": "object"}]}

    # serve: "all" | list[role] | {placement: "local"|"cloud"|"either"}
    props["serve"] = {
        "oneOf": [
            {"type": "string", "enum": ["all"]},
            {"type": "array", "items": {"type": "string"}},
            {
                "type": "object",
                "properties": {"placement": {"type": "string",
                                              "enum": ["local", "cloud", "either"]}},
                "additionalProperties": False,
            },
        ],
    }

    # baton_ttl: minutes (positive number)
    props["baton_ttl"] = {"type": "number", "minimum": 0}

    # decisions: map of <gate-stage-name> → {auto: "approve"|"revise"|...}
    props["decisions"] = {
        "type": "object",
        "additionalProperties": {"type": "object", "additionalProperties": True},
    }

    # _extends: a path string or a list of paths
    props["_extends"] = {
        "oneOf": [{"type": "string"},
                  {"type": "array", "items": {"type": "string"}}],
    }

    return {
        "$schema": SCHEMA_DRAFT,
        "title": "YAAH root config",
        "description": (
            "Generated from src/yaah/validate.py + src/yaah/runtime_factories.py "
            "by scripts/build_schemas.py. Do not edit by hand — re-run the "
            "generator after changing the engine's key tables."),
        "type": "object",
        "properties": props,
        # _-prefixed keys are config comments; allow them.
        "patternProperties": {"^_": {}},
        "additionalProperties": False,
    }


def build_pipeline_schema() -> Dict[str, Any]:
    node_type_enum = sorted(_NODE_TYPES.keys())

    # A node spec: requires `type` from the known set; allows the common
    # spec keys + per-type extras (additionalProperties true; the runtime
    # rejects unknowns).
    node_spec_schema = {
        "type": "object",
        "required": ["type"],
        "properties": {"type": {"type": "string", "enum": node_type_enum}},
        "additionalProperties": True,
    }

    stage_props: Dict[str, Any] = {}
    for k in v._STAGE_KEYS:
        # leave most stage keys un-typed — they vary (string / int / bool /
        # array / object). The runtime checks shapes per key.
        stage_props[k] = {}
    stage_schema = {
        "type": "object",
        "properties": stage_props,
        "patternProperties": {"^_": {}},
        "additionalProperties": False,
    }

    graph_props: Dict[str, Any] = {
        "start": {"type": "string"},
        "stages": {
            "type": "object",
            "additionalProperties": stage_schema,
        },
        "sticky": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "constraints": {},
        "note": {},
    }
    graph_schema = {
        "type": "object",
        "required": ["start", "stages"],
        "properties": graph_props,
        "patternProperties": {"^_": {}},
        "additionalProperties": False,
    }

    return {
        "$schema": SCHEMA_DRAFT,
        "title": "YAAH pipeline config",
        "description": (
            "Generated from src/yaah/validate.py + src/yaah/build/registry.py "
            "by scripts/build_schemas.py. Do not edit by hand — re-run the "
            "generator after touching the engine."),
        "type": "object",
        "required": ["nodes", "graph"],
        "properties": {
            "nodes": {
                "type": "object",
                "additionalProperties": node_spec_schema,
            },
            "graph": graph_schema,
        },
        "patternProperties": {"^_": {}},
        "additionalProperties": False,
    }


def _capture_names() -> List[str]:
    """Trace capture (contributor) names — read from the same place validate.py
    reads them, so a new contributor lands in the schema automatically."""
    _, capture_names = v._factory_tables()
    return list(capture_names)


def write_schemas() -> List[Tuple[str, str]]:
    out_dir = os.path.join(ROOT, "schemas")
    os.makedirs(out_dir, exist_ok=True)
    written = []
    for name, schema in (
        ("root.schema.json",     build_root_schema()),
        ("pipeline.schema.json", build_pipeline_schema()),
    ):
        path = os.path.join(out_dir, name)
        text = json.dumps(schema, indent=2, sort_keys=True) + "\n"
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        written.append((name, path))
    return written


def main() -> None:
    for name, path in write_schemas():
        print("wrote {} ({} bytes)".format(name, os.path.getsize(path)))


if __name__ == "__main__":
    main()
