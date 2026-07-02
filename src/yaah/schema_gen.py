"""schema_gen — derive JSON Schemas for root + pipeline configs from the engine's
own validation tables. The "code is truth, schema derived" generator, living IN
the package (not just `scripts/`) so it can run from an installed wheel.

Two consumers, one source:
  - `scripts/build_schemas.py` re-exports `build_root_schema` / `build_pipeline_schema`
    and writes the committed `schemas/*.schema.json` (drift-tested by
    `tests/test_schemas_drift.py`).
  - `init_template.scaffold` calls them at `yaah init` time to write a schema next
    to the scaffolded config — GENERATED from the INSTALLED engine, so the
    `$schema` autocomplete a user gets always matches the engine that will run it
    (no version skew between a shipped snapshot and the runtime).

Why a SUBSET of JSON Schema and `additionalProperties: true` on typed blocks: the
runtime's `_check_typed_entry` already rejects unknown per-type keys, so the schema
only needs to pin the shape (required `type`, the type enum) and let the editor
surface the rest. The schema is for autocomplete, NOT the correctness gate — that's
`validate_pipeline` + `lint_pipeline`.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, Dict, List

from . import runtime_factories as rf
from . import validate as v

SCHEMA_DRAFT = "http://json-schema.org/draft-07/schema#"

# Each top-level root key falls into one of these categories. Mirrors the tables
# in validate.py — the import there stays the truth.
_TYPED_BLOCK_FACTORY = {
    "transport": rf._TRANSPORT_TYPES,
    "state":     rf._STATE_TYPES,
}
_NAMED_MAP_FACTORY = {
    "providers":      rf._PROVIDER_TYPES,
    "prompt_sources": rf._PROMPT_TYPES,
    "data_sources":   rf._DATA_SOURCE_TYPES,
    "data_sinks":     rf._DATA_SINK_TYPES,
    "mcp_sources":    rf._MCP_TYPES,
}


def _typed_block_schema(types_map: Dict[str, Any]) -> Dict[str, Any]:
    """A schema for the `{type: <one of>, ...}` shape. `additionalProperties`
    stays true so per-type spec keys (which differ by type and are auto-known to
    the factory) don't have to be enumerated here — the runtime's
    `_check_typed_entry` rejects unknown keys per type."""
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


def _capture_names() -> List[str]:
    """Trace capture (contributor) names — read from the same place validate.py
    reads them, so a new contributor lands in the schema automatically."""
    _, capture_names = v._factory_tables()
    return list(capture_names)


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

    # the editor-side schema pointer the scaffold writes; allowed so a config
    # carrying it still validates against this very schema (additionalProperties
    # is false below).
    props["$schema"] = {"type": "string"}

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
    # The set of node types ships from the same place the runtime uses to build
    # them — no parallel list to drift. Imported lazily (pulls the builder
    # registry) so importing this module stays cheap for the scaffold path.
    from .build.builders import default_registry
    node_type_enum = sorted(default_registry()._builders.keys())  # noqa: SLF001

    # A node spec: requires `type` from the known set; allows the common spec
    # keys + per-type extras (additionalProperties true; the runtime rejects
    # unknowns).
    node_spec_schema = {
        "type": "object",
        "required": ["type"],
        "properties": {
            "type": {"type": "string", "enum": node_type_enum},
            # ADR-0005: the payload keys this node guarantees (the requires<->provides
            # contract foothold; required to lint across an envelope-transform).
            "provides": {"type": "array", "items": {"type": "string", "minLength": 1}},
        },
        "additionalProperties": True,
    }

    # Stage keys with a pinned schema (autocomplete for enum-valued knobs; the
    # matching hard check lives in validate.py — _check_on_error).
    typed_stage_keys: Dict[str, Any] = {
        "on_error": {"oneOf": [
            {"type": "null"},
            {"const": "clear"},
            {"type": "object",
             "required": ["compensate"],
             "properties": {
                 "compensate": {"type": "string", "minLength": 1},
                 "on_compensate_fail": {"enum": ["error", "warn"]},
             },
             "additionalProperties": False},
        ]},
    }
    stage_props: Dict[str, Any] = {}
    for k in v._STAGE_KEYS:
        # leave most stage keys un-typed — they vary (string / int / bool /
        # array / object). The runtime checks shapes per key.
        stage_props[k] = typed_stage_keys.get(k, {})
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
            # editor-side schema pointer (see build_root_schema).
            "$schema": {"type": "string"},
        },
        "patternProperties": {"^_": {}},
        "additionalProperties": False,
    }
