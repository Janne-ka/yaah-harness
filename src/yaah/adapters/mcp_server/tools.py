"""The five MCP tools: validate / list_gates / baton_schema / resume / run.

Who: McpServer (server.py) dispatches `tools/call` to the handlers in TOOLS;
each handler CALLS the matching runtime/validate action (the same functions
behind `yaah validate --json`, `--list --json`, `baton-schema`, `--resume`,
run) and returns its data as the JSON object the CLI would print — so the MCP
client gets the same stable shapes the driver skills already consume.
Where: yaah.adapters.mcp_server — an operator entry surface beside yaah.cli
(entry points may call runtime assembly; port adapters may not).
Why: the actions RETURN data and each surface owns only its rendering; a
handler adds nothing but its surface-specific choices (no gate driver, no
serve-forever — flagged where they apply).

Every handler loads the root the way the CLI does (`_load_root`): base dir =
dirname of the root file, base on sys.path for `fn:` resolution, plugins
imported BEFORE any validation so registered types are known enum values.

Targets Python 3.9+.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Tuple

from ...harness import Cleared, Done, Suspended
from ...plugins import load_plugins
from ...runtime_factories import _read_json
from ...validate import split_diagnostics, split_lint_id, validate_config, validate_root


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
    """Same check and result shape as `yaah validate --json` — both call
    validate.validate_config: {ok, root, errors: [{message, stage?}],
    warnings: [{id, message}]}. An INVALID config is a SUCCESSFUL validation
    (ok:false, not isError)."""
    root, base = _load_root(args["root_path"])
    errors: List[Dict[str, Any]] = []
    warnings: List[str] = []
    try:
        warnings = validate_config(root, base)
    except ValueError as e:
        errors = split_diagnostics(str(e))
    warn_items = [{"id": wid, "message": msg}
                  for wid, msg in map(split_lint_id, warnings)]
    return {"ok": not errors, "root": args["root_path"],
            "errors": errors, "warnings": warn_items}


async def _tool_list_gates(args: Dict[str, Any]) -> Dict[str, Any]:
    """The mailbox view — runtime.list_gates, rendered with the same per-baton
    shape as `yaah list --json` (runtime._baton_json, the documented stable
    contract for driver skills)."""
    from ...runtime import _baton_json, list_gates
    root, base = _load_root(args["root_path"])
    validate_root(root)
    return {"batons": [_baton_json(b) for b in await list_gates(root, base)]}


async def _tool_baton_schema(args: Dict[str, Any]) -> Dict[str, Any]:
    """A parked baton's decision-form contract — runtime.baton_schema:
    {form, schema, example, baton_id, awaiting}. The error cases (no such
    baton / not a gate / no declared form) raise ValueError there and surface
    as isError:true."""
    from ...runtime import baton_schema
    root, base = _load_root(args["root_path"])
    validate_root(root)
    return await baton_schema(root, base, args["baton_id"])


async def _tool_resume(args: Dict[str, Any]) -> Dict[str, Any]:
    """Deliver a decision to a parked gate and run to the next gate or
    completion — runtime.resume_gate. The engine runs in THIS process until
    it parks again or finishes."""
    from ...runtime import resume_gate
    root, base = _load_root(args["root_path"])
    validate_root(root)
    out = await resume_gate(root, base, args["baton_id"],
                            dict(args.get("decision") or {}))
    return _outcome_json(out)


async def _tool_run(args: Dict[str, Any]) -> Dict[str, Any]:
    """Start a run and return its outcome. Deliberately NOT runtime.run_root:
    no gate DRIVER (a gated pipeline returns `suspended` at the first gate, and
    the MCP client continues via list_gates/baton_schema/resume — the mailbox
    flow is the MCP-native way to answer gates; a root's `interactive` stdin
    prompt has no place on a protocol channel) and no serve-forever (a
    serve-only root would block the protocol loop for the process lifetime).
    The run itself is seeded identically (runtime._seed_task)."""
    from ...runtime import _assemble_harness, _seed_task
    root, base = _load_root(args["root_path"])
    validate_root(root)
    harness = await _assemble_harness(root, base)
    task, run_kw = _seed_task(root, base)
    out = await harness.run(task, **run_kw)
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
