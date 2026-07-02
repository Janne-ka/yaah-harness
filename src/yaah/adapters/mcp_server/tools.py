"""The five MCP tools: validate / list_gates / baton_schema / resume / run.

Who: McpServer (server.py) dispatches `tools/call` to the handlers in TOOLS;
each handler mirrors the matching CLI action (`yaah validate --json`, `--list
--json`, `baton-schema`, `--resume`, run) minus the printing — it RETURNS the
JSON object the CLI prints, so the MCP client gets the same stable shapes the
driver skills already consume.
Where: yaah.adapters.mcp_server — an operator entry surface beside yaah.cli
(entry points may call runtime assembly; port adapters may not).
Why: one handler per CLI action keeps the two operator surfaces honest mirrors
of each other; the shapes (`_baton_json`, validate diagnostics, decision-form
lookup) stay single-sourced in runtime/validate/decision_forms.

Every handler loads the root the way the CLI does (`_load_root`): base dir =
dirname of the root file, base on sys.path for `fn:` resolution, plugins
imported BEFORE any validation so registered types are known enum values.

Targets Python 3.9+.
"""
from __future__ import annotations

import os
import re
import sys
from typing import Any, Dict, List, Tuple

from ...core import Envelope, Kind
from ...harness import BatonStore, Cleared, Done, Suspended
from ...harness.decision_forms import lookup
from ...plugins import load_plugins
from ...runtime_factories import _build_store, _read_json, _rel
from ...validate import lint_pipeline, validate_pipeline, validate_root


def _load_root(root_path: str) -> Tuple[Dict[str, Any], str]:
    """Load a root config the way cli._dispatch does: base = the root file's
    directory, base front-inserted on sys.path (so `fn:` targets and plugin
    modules beside the config import), plugins loaded before any validation."""
    root_path = os.path.abspath(root_path)
    root = _read_json(root_path)
    base = os.path.dirname(root_path)
    if base not in sys.path:
        sys.path.insert(0, base)
    load_plugins(root.get("plugins"), base)
    return root, base


# Kept in step with yaah.cli._diagnostics — small, sanctioned duplication (the
# CLI module is maintainer-owned; importing its privates would couple the two
# operator surfaces the wrong way round). If the CLI's diagnostic splitting
# grows a real taxonomy, hoist it into yaah.validate and import from there.
def _diagnostics(exc_text: str) -> List[Dict[str, Any]]:
    """Split a validate ValueError's bulleted message into per-item diagnostics;
    `stage` extracted where the message follows the "stage '<name>': ..." form."""
    header, sep, rest = exc_text.partition(":\n  - ")
    items = rest.split("\n  - ") if sep else [exc_text]
    out: List[Dict[str, Any]] = []
    for msg in items:
        d: Dict[str, Any] = {"message": msg}
        m = re.match(r"stage '([^']+)':", msg)
        if m:
            d["stage"] = m.group(1)
        out.append(d)
    return out


def _outcome_json(out: Any) -> Dict[str, Any]:
    """One JSON shape per Outcome type — what `run`/`resume` hand the client.
    StageFailed is an EXCEPTION, not an Outcome; it propagates to the server's
    tools/call wrapper and comes back as isError:true."""
    if isinstance(out, Done):
        return {"outcome": "done", "baton_id": out.baton_id,
                "payload": out.output.payload}
    if isinstance(out, Suspended):
        return {"outcome": "suspended", "baton_id": out.baton_id,
                "awaiting": out.awaiting,
                "concerns": [dict(c) for c in out.concerns],
                "ask": out.ask}
    if isinstance(out, Cleared):
        return {"outcome": "cleared", "baton_id": out.baton_id,
                "node": out.node, "payload": out.payload}
    return {"outcome": type(out).__name__.lower(), "detail": str(out)}


async def _tool_validate(args: Dict[str, Any]) -> Dict[str, Any]:
    """Same collection logic (and result shape) as `yaah validate --json`:
    {ok, root, errors: [{message, stage?}], warnings: [{id, message}]}. An
    INVALID config is a SUCCESSFUL validation (ok:false, not isError)."""
    root, base = _load_root(args["root_path"])
    errors: List[Dict[str, Any]] = []
    warnings: List[str] = []
    try:
        validate_root(root)
        pipeline_ref = root.get("pipeline")
        if isinstance(pipeline_ref, str):
            pipeline_cfg = _read_json(_rel(base, pipeline_ref))
            validate_pipeline(pipeline_cfg, base_path=base)
            # template_file paths resolve against the ROOT's dir, matching the
            # runtime's base_dir=base (see cli._dispatch_validate for the why).
            warnings = lint_pipeline(pipeline_cfg, base_path=base)
        elif isinstance(pipeline_ref, dict):
            validate_pipeline(pipeline_ref, base_path=base)
            warnings = lint_pipeline(pipeline_ref, base_path=base)
    except ValueError as e:
        errors = _diagnostics(str(e))
    warn_items = []
    for w in warnings:
        m = re.search(r"\s*\[lint: ([a-z0-9-]+)\]$", w)
        warn_items.append({"id": m.group(1) if m else None,
                           "message": w[:m.start()] if m else w})
    return {"ok": not errors, "root": args["root_path"],
            "errors": errors, "warnings": warn_items}


async def _tool_list_gates(args: Dict[str, Any]) -> Dict[str, Any]:
    """The mailbox view — same store walk and per-baton shape as
    `yaah list --json` (runtime.list_gates / runtime._baton_json, the
    documented stable contract for driver skills)."""
    from ...runtime import _baton_json
    root, base = _load_root(args["root_path"])
    validate_root(root)
    bstore = BatonStore(_build_store(root.get("state"), base))
    gates = await bstore.list_suspended()
    return {"batons": [_baton_json(b) for b in gates]}


async def _tool_baton_schema(args: Dict[str, Any]) -> Dict[str, Any]:
    """A parked baton's decision-form contract, as runtime.baton_schema prints
    it: {form, schema, example, baton_id, awaiting}. The error cases (no such
    baton / not a gate / no declared form) raise with the runtime's messages
    and surface as isError:true."""
    root, base = _load_root(args["root_path"])
    validate_root(root)
    bstore = BatonStore(_build_store(root.get("state"), base))
    baton = await bstore.load(args["baton_id"])
    if baton is None:
        raise ValueError("no baton with id {!r}".format(args["baton_id"]))
    if baton.pending is None:
        raise ValueError("baton {!r} has no parked envelope (not a human gate?)".format(
            args["baton_id"]))
    form = baton.pending.payload.get("form")
    if form is None:
        raise ValueError(
            "baton {!r} parked without a declared form — add `form: \"...\"` "
            "to the human_gate node to surface its decision shape".format(args["baton_id"]))
    out = lookup(form, inline_schema=baton.pending.payload.get("decision_schema"))
    out["baton_id"] = args["baton_id"]
    out["awaiting"] = baton.awaiting
    return out


async def _tool_resume(args: Dict[str, Any]) -> Dict[str, Any]:
    """Deliver a decision to a parked gate and run to the next gate or
    completion — runtime.resume_gate minus the printing. The engine runs in
    THIS process until it parks again or finishes."""
    from ...runtime import _assemble_harness
    root, base = _load_root(args["root_path"])
    validate_root(root)
    harness = await _assemble_harness(root, base)
    decision = dict(args.get("decision") or {})
    out = await harness.resume(args["baton_id"], Envelope(Kind.RESUME, decision))
    return _outcome_json(out)


async def _tool_run(args: Dict[str, Any]) -> Dict[str, Any]:
    """Start a run and return its outcome — runtime.run_root minus the printing
    and minus the gate DRIVER: a gated pipeline returns `suspended` at the first
    gate, and the MCP client continues via list_gates/baton_schema/resume (the
    mailbox flow is the MCP-native way to answer gates; a root's `interactive`
    stdin prompt has no place on a protocol channel)."""
    from ...runtime import _assemble_harness
    root, base = _load_root(args["root_path"])
    validate_root(root)
    harness = await _assemble_harness(root, base)
    raw_input = root.get("input", {})
    payload = raw_input if isinstance(raw_input, dict) else _read_json(_rel(base, raw_input))
    run_kw = {"ttl": root["baton_ttl"]} if "baton_ttl" in root else {}
    out = await harness.run(Envelope(Kind.TASK, payload), **run_kw)
    return _outcome_json(out)


_ROOT_PATH_PROP = {"type": "string",
                   "description": "Path to the yaah root config JSON file; relative "
                                  "paths in it resolve against its own directory."}
_BATON_ID_PROP = {"type": "string",
                  "description": "Id of a suspended baton (from list_gates or a "
                                 "previous run/resume outcome)."}

TOOLS: List[Dict[str, Any]] = [
    {"name": "validate",
     "description": "Validate a yaah root config and its referenced pipeline "
                    "(no run). Returns {ok, root, errors, warnings} — an invalid "
                    "config is ok:false diagnostics, not a tool error.",
     "inputSchema": {"type": "object",
                     "properties": {"root_path": _ROOT_PATH_PROP},
                     "required": ["root_path"]},
     "handler": _tool_validate},
    {"name": "list_gates",
     "description": "List suspended runs waiting on a decision (the mailbox "
                    "view). Returns {batons: [{id, stage, awaiting, concerns, "
                    "escalation, question}]}. Needs a durable `state:` in the "
                    "root to see gates parked by other processes.",
     "inputSchema": {"type": "object",
                     "properties": {"root_path": _ROOT_PATH_PROP},
                     "required": ["root_path"]},
     "handler": _tool_list_gates},
    {"name": "baton_schema",
     "description": "A parked gate's decision-form contract: {form, schema, "
                    "example, baton_id, awaiting}. Compose the `resume` "
                    "decision against the schema.",
     "inputSchema": {"type": "object",
                     "properties": {"root_path": _ROOT_PATH_PROP,
                                    "baton_id": _BATON_ID_PROP},
                     "required": ["root_path", "baton_id"]},
     "handler": _tool_baton_schema},
    {"name": "resume",
     "description": "Deliver a decision object to a suspended gate and run to "
                    "the next gate or completion. Returns the outcome "
                    "({outcome: done|suspended|cleared, ...}).",
     "inputSchema": {"type": "object",
                     "properties": {"root_path": _ROOT_PATH_PROP,
                                    "baton_id": _BATON_ID_PROP,
                                    "decision": {"type": "object",
                                                 "description": "The decision payload; "
                                                                "shape per baton_schema."}},
                     "required": ["root_path", "baton_id", "decision"]},
     "handler": _tool_resume},
    {"name": "run",
     "description": "Run the root config's pipeline. Returns the outcome: "
                    "{outcome: done, payload} on completion, or {outcome: "
                    "suspended, baton_id, awaiting, ...} when parked at a gate "
                    "(continue via list_gates/baton_schema/resume).",
     "inputSchema": {"type": "object",
                     "properties": {"root_path": _ROOT_PATH_PROP},
                     "required": ["root_path"]},
     "handler": _tool_run},
]
