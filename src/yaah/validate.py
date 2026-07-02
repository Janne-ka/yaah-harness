"""validate_root / validate_pipeline / validate_budgets — the ONE entry for
config validation (R15).

Used by: `runtime.main` (validates a root deployment config before anything is
built); `build.build` / `harness_from_config` / `serve_from_config` (validates the
pipeline config before constructing the graph). Tests in `tests/test_validate.py`.

Where: the load-time gate. Runs AFTER `_extends` expansion and `_fake` overlay so
the EFFECTIVE config is what gets checked — no skipped expansion path. The
mid-build `raise ValueError("unknown ...")` calls in `runtime_factories` and
`build.builders` remain as last-line guards, but in normal flow this module catches
typos first with `did you mean` hints.

Why:
  - **One documented surface.** The constants below (`_ROOT_KEYS`, `_TYPED_BLOCK_KEYS`,
    …) plus the factory maps in `runtime_factories` ({type: (factory, spec-keys)})
    ARE the root-config schema, machine-readable. The R16 AI config-generator
    skill grounds on these. Type enums and per-type keys are READ from the factory
    maps, never hand-copied — the sink/sinks split (factory read one key, validator
    checked another) is the bug class this kills.
  - **Actionable errors at LOAD, not mid-build.** A misspelled `mode: tracor` used to
    fail deep in `_build_tracer`; now it fails here with `did you mean 'tracer'?`.
  - **All errors gathered.** One pass collects every issue rather than failing on the
    first — so an LLM-generated config gets the full repair list in one shot.

Targets Python 3.9+.
"""
from __future__ import annotations

import difflib
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

# Lazy imports for enum tables that depend on third-party modules — pulled inside
# functions to keep this module cheap to import (validators may run in CI sandboxes).


# --- root-config spec (top-level) -------------------------------------------------

# Known top-level keys of a deployment root config. Anything starting with "_" is
# a comment (e.g. "_about", "_fake") and is ignored. MUST stay in lock-step with
# the keys actually read by runtime.* — `_ROOT_KEYS` is the single source of truth
# both for shape-checking and for R16's documented surface.
_ROOT_KEYS = frozenset({
    "providers", "default_provider",
    "prompt_sources", "default_prompt_source",
    "data_sources", "default_data_source",
    "data_sinks", "default_data_sink",
    "mcp_sources", "default_mcp_source",
    "transport", "trace", "state",
    "pipeline", "input",
    "decisions", "interactive", "run", "serve", "baton_ttl",
    "live_config",
})


# --- the mutable-leaf surface (ONE table, three consumers) -------------------
# The leaf-vs-topology / non-code-equivalent line defines THREE surfaces (TODO
# live-vars): what an AI overlay may write (`overlay_lint`), what a RUNNING
# system may pick up from an edited pipeline file (`LiveLeafConfig`, root
# `live_config: true`), and what a future config-push may carry. Defined once
# here so the three can never drift.
#
# MUTABLE_LEAF_KEYS — node-spec keys that are leaf and non-code-equivalent:
# model/prompt/template are LLM-facing strings (never executed), the scalar
# knobs and numeric `config` values are bounds. Everything else on a node spec
# (`command`, `binary`, `target`, `allowed_tools`, `permission_mode`, `tools`,
# `mcp`, `cwd_from`, gate fields, `type`) is execution surface or topology.
MUTABLE_LEAF_KEYS = frozenset({
    "model", "prompt", "template", "effort",
    "temperature", "timeout", "retries", "config", "note",
})
# the subset the live re-read adopts into a running NodeConfig per call (the
# scalar fields `_node_config` reads; `config` numerics are handled separately)
LIVE_NODECONFIG_KEYS = frozenset({"model", "effort", "temperature", "timeout", "retries"})
# node-spec scalars under the numeric tighten-only rule (lint side): a raise
# widens cost/runtime, so an AI overlay may lower but never raise them
MUTABLE_NUMERIC_KEYS = frozenset({"temperature", "timeout", "retries"})

_TYPED_BLOCK_KEYS = ("transport", "state")
_NAMED_MAP_KEYS = (
    "providers", "prompt_sources",
    "data_sources", "data_sinks", "mcp_sources",
)
_STRING_KEYS = (
    "default_provider", "default_prompt_source",
    "default_data_source", "default_data_sink", "default_mcp_source",
    "pipeline",
)
_BOOL_KEYS = ("run", "interactive", "live_config")

# Type enums and per-type spec keys are NOT hand-copied here: they are read from
# the factory maps in `runtime_factories` (each entry is `(factory, spec-keys)`),
# lazily via `_factory_tables`. One entry there = enum value + key check here.
# `_NAMED_MAP_FACTORIES` maps each named-map root key to its factory-map name.
_NAMED_MAP_FACTORIES = {
    "providers": "_PROVIDER_TYPES",
    "prompt_sources": "_PROMPT_TYPES",
    "data_sources": "_DATA_SOURCE_TYPES",
    "data_sinks": "_DATA_SINK_TYPES",
    "mcp_sources": "_MCP_TYPES",
}

# (map_key, default_key, noun) per pluggable layer. Each triple mirrors ONE
# `_build_router(cfg.get(map_key), ..., default=cfg.get(default_key))` call site
# in `runtime_factories` — the authoritative pairing. The noun is the layer's own
# word for an entry (the singular of the map), used in the load-time
# default-resolution error so the message reads in the user's vocabulary. A
# `default_*` that names no declared entry would otherwise surface only as a
# runtime LookupError on the first use of that layer.
_DEFAULT_REFS = (
    ("providers", "default_provider", "provider"),
    ("prompt_sources", "default_prompt_source", "prompt source"),
    ("data_sources", "default_data_source", "data source"),
    ("data_sinks", "default_data_sink", "data sink"),
    ("mcp_sources", "default_mcp_source", "mcp source"),
)

# R13: defaults for keys the runtime fills in when the user omits them. Sourced
# from the `.get(k, <default>)` sites in `runtime_factories`. Used by
# `yaah --explain` to show the EFFECTIVE config (Spring `--debug` / `helm template`
# style). MUST stay in lock-step with those defaults.
_DEFAULTS = {
    "transport": {"type": "inproc"},
    "state": {"type": "memory"},
    "trace": {"mode": "tracer", "capture": ["phase"], "sinks": [{"type": "console"}]},
    "run": False,
    "interactive": False,
}


def _suggest(bad: str, known: Iterable[str]) -> str:
    """Return ' (did you mean 'X'?)' if a close match exists, else ''. The
    Terraform-style actionable-error pattern: tell the user the fix inline."""
    near = difflib.get_close_matches(bad, list(known), n=1)
    return " (did you mean {!r}?)".format(near[0]) if near else ""


def _check_top_level_keys(root: Dict[str, Any], errs: List[str]) -> None:
    for k in root:
        # `$schema` is the editor-side autocomplete pointer the scaffold writes
        # (`yaah init`); it's metadata for the IDE, ignored by the runtime. Allow
        # it the same way `_`-prefixed comment keys are allowed.
        if k.startswith("_") or k == "$schema" or k in _ROOT_KEYS:
            continue
        errs.append("unknown top-level key {!r}{}; known: {}".format(
            k, _suggest(k, _ROOT_KEYS), ", ".join(sorted(_ROOT_KEYS))))


def _check_shapes(root: Dict[str, Any], errs: List[str]) -> None:
    for k in _TYPED_BLOCK_KEYS:
        if k not in root:
            continue
        v = root[k]
        if not isinstance(v, dict):
            kind = v if isinstance(v, str) else "<kind>"
            errs.append('{!r}: expected typed-block dict, got {} {!r} — '
                        'rewrite as {{"type": "{}"}}'.format(
                            k, type(v).__name__, v, kind))
        elif "type" not in v:
            errs.append("{!r}: typed-block is missing required key 'type' "
                        "(got keys: {})".format(k, sorted(v)))
    for k in _NAMED_MAP_KEYS:
        if k not in root:
            continue
        v = root[k]
        if not isinstance(v, dict):
            kind = v if isinstance(v, str) else "<kind>"
            errs.append('{!r}: expected named-map dict, got {} {!r} — '
                        'rewrite as {{"<name>": {{"type": "{}"}}}}'.format(
                            k, type(v).__name__, v, kind))
            continue
        for name, entry in v.items():
            if not isinstance(entry, dict):
                errs.append("{!r}.{!r}: expected typed-block dict, got {} {!r}".format(
                    k, name, type(entry).__name__, entry))
            elif "type" not in entry:
                errs.append("{!r}.{!r}: typed-block missing required key 'type'".format(k, name))
    # `trace` is shaped like a typed block but keyed on `mode`, not `type` —
    # so it needs its own dict-ness check ("trace": "none" used to pass
    # validation here and crash mid-build, assessment #8).
    tr = root.get("trace")
    if tr is not None and not isinstance(tr, dict):
        mode = tr if isinstance(tr, str) else "<mode>"
        errs.append('\'trace\': expected dict, got {} {!r} — '
                    'rewrite as {{"mode": "{}"}}'.format(type(tr).__name__, tr, mode))
    for k in _STRING_KEYS:
        if k in root and not isinstance(root[k], str):
            errs.append("{!r}: expected string, got {} {!r}".format(
                k, type(root[k]).__name__, root[k]))
    if "input" in root and not isinstance(root["input"], (str, dict)):
        errs.append("'input': expected a fixture path or an inline payload object, got {} {!r}".format(
            type(root["input"]).__name__, root["input"]))
    for k in _BOOL_KEYS:
        if k in root and not isinstance(root[k], bool):
            errs.append("{!r}: expected bool, got {} {!r}".format(
                k, type(root[k]).__name__, root[k]))


def _factory_tables() -> Tuple[Any, Dict[str, Any]]:
    """Lazy import of the factory module + contributor map so this module stays
    cheap to import. The factory maps in `runtime_factories` are {type:
    (factory, spec-keys)} — the single source for both type enums and per-type
    key checks (spec-keys None = open spec, the leaf constructor enforces)."""
    from . import runtime_factories
    from .trace.contributors import BUILTIN_CONTRIBUTORS
    return runtime_factories, BUILTIN_CONTRIBUTORS


def _check_typed_entry(label: str, entry: Dict[str, Any], type_map: Dict[str, Any],
                       errs: List[str]) -> None:
    """Check ONE {type, ...} spec against a factory map: type is a known enum
    value, and (for closed specs) every other key is one the factory reads —
    an unknown key is a silent no-op, the bug class behind sink/sinks."""
    t = entry.get("type")
    if t is None:
        return  # _check_shapes already flagged the missing 'type'
    if t not in type_map:
        errs.append("{}.type {!r}{}; have {}".format(
            label, t, _suggest(t, type_map), sorted(type_map)))
        return
    keys = type_map[t][1]
    if keys is None:
        return  # open spec: factory forwards **kwargs, constructor enforces
    for k in entry:
        if k == "type" or k.startswith("_") or k in keys:
            continue
        errs.append("{}: unknown key {!r} for type {!r}{}; known: {}".format(
            label, k, t, _suggest(k, keys), ", ".join(sorted(keys | {"type"}))))


def _check_enums(root: Dict[str, Any], errs: List[str]) -> None:
    rf, capture_names = _factory_tables()

    t = root.get("transport")
    if isinstance(t, dict):
        _check_typed_entry("transport", t, rf._TRANSPORT_TYPES, errs)

    s = root.get("state")
    if isinstance(s, dict):
        _check_typed_entry("state", s, rf._STATE_TYPES, errs)

    for block, map_name in _NAMED_MAP_FACTORIES.items():
        v = root.get(block)
        if not isinstance(v, dict):
            continue
        type_map = getattr(rf, map_name)
        for name, entry in v.items():
            if isinstance(entry, dict):
                _check_typed_entry("{}.{}".format(block, name), entry, type_map, errs)

    tr = root.get("trace")
    if isinstance(tr, dict):
        for k in tr:
            if not k.startswith("_") and k not in rf._TRACE_KEYS:
                errs.append("trace: unknown key {!r}{}; known: {}".format(
                    k, _suggest(k, rf._TRACE_KEYS), ", ".join(sorted(rf._TRACE_KEYS))))
        mode = tr.get("mode", "tracer")
        if mode not in rf._TRACE_MODES:
            errs.append("trace.mode {!r}{}; have {}".format(
                mode, _suggest(mode, rf._TRACE_MODES), list(rf._TRACE_MODES)))
        for name in tr.get("capture", []) or []:
            if name not in capture_names:
                errs.append("trace.capture {!r}{}; have {}".format(
                    name, _suggest(name, capture_names), sorted(capture_names)))
        sinks = tr.get("sinks")
        # the factory accepts a single sink dict or a list — validate both shapes
        sink_list = sinks if isinstance(sinks, list) else (
            [sinks] if isinstance(sinks, dict) else [])
        for i, sspec in enumerate(sink_list):
            if isinstance(sspec, dict):
                _check_typed_entry("trace.sinks[{}]".format(i), sspec,
                                   rf._TRACE_SINK_TYPES, errs)


def _check_cross_field(root: Dict[str, Any], errs: List[str]) -> None:
    """Catch silent-no-op / dangling configurations the user almost certainly
    didn't mean: trace mode/field consistency, and every `default_*` resolving to
    a declared map entry."""
    _check_trace_cross_field(root, errs)
    _check_default_refs(root, errs)


def _check_trace_cross_field(root: Dict[str, Any], errs: List[str]) -> None:
    """Mirrors what `_build_tracer` actually reads per mode: `none` reads nothing,
    `envelope` reads only capture + buffer_max (no bus, no sinks), `tracer`
    reads capture + sinks + topic (no buffer)."""
    tr = root.get("trace")
    if not isinstance(tr, dict):
        return
    mode = tr.get("mode", "tracer")
    if mode == "none":
        for k in ("capture", "sinks", "topic", "buffer_max"):
            if tr.get(k):
                errs.append("trace.{} is set but trace.mode is 'none' — it will be "
                            "silently dropped; pick another mode or remove it".format(k))
    elif mode == "envelope":
        for k in ("sinks", "topic"):
            if tr.get(k):
                errs.append("trace.{} is set but trace.mode is 'envelope' — envelope "
                            "carriage has no bus/sinks (spans ride envelope headers); "
                            "use mode 'tracer' for sinks or remove it".format(k))
    elif mode == "tracer" and tr.get("buffer_max"):
        errs.append("trace.buffer_max is set but trace.mode is 'tracer' — the buffer "
                    "only exists in 'envelope' mode; remove it or switch mode")


def _check_default_refs(root: Dict[str, Any], errs: List[str]) -> None:
    """Each `default_*` must name a declared entry of its named map — the same
    spirit as the pipeline's 'branch default must resolve to a declared node'.
    A dangling default (e.g. `default_provider: "ghost"` with no provider named
    "ghost") otherwise slips past load and dies as a runtime LookupError on the
    first call into that layer.

    Only fires for a NON-EMPTY string default against a PRESENT dict-shaped map:
    a missing/empty/non-dict map is either already flagged by `_check_shapes` or
    legitimately deferred (an absent providers map is valid), so resolving against
    it here would be a spurious second error."""
    for map_key, default_key, noun in _DEFAULT_REFS:
        want = root.get(default_key)
        names = root.get(map_key)
        if not (isinstance(want, str) and want) or not isinstance(names, dict) or not names:
            continue
        if want not in names:
            errs.append("{} {!r} is not a declared {}; have {}".format(
                default_key, want, noun, sorted(names)))


def validate_root(root: Dict[str, Any]) -> None:
    """Fail fast (R15) on a malformed deployment root config, with actionable
    errors gathered into one ValueError. Pure data; no I/O. Call AFTER `_extends`
    expansion and any `_fake` overlay so the EFFECTIVE config is what's checked."""
    errs: List[str] = []
    _check_top_level_keys(root, errs)
    _check_shapes(root, errs)
    _check_enums(root, errs)
    _check_cross_field(root, errs)
    if errs:
        raise ValueError("invalid root config:\n  - " + "\n  - ".join(errs))


# --- pipeline-config spec (graph cross-refs) --------------------------------------

def _is_fork(stage_config: Dict[str, Any], stage_names: set) -> bool:
    """A stage is a FORK iff it declares the explicit `fork` key (a list of
    STAGE names — independent branch chains, rejoined by a `fanin`). `fanout`
    is the OTHER parallel primitive: a one-stage barrier over ROLES (ask N
    workers, merge the replies). They used to share the `fanout` key with the
    meaning inferred from the targets; the split made each explicit (the
    sniffing was the confusing part — same key, two machines). `stage_names`
    is kept for signature compatibility with callers."""
    return bool(stage_config.get("fork"))


# Every key build_graph reads off a stage (build.py). An unknown stage key is a
# silent no-op — a typo'd `concerns_form`/`vaildators`/`fannout` changes nothing
# and fails quietly at runtime (the silent-misconfig class, review 2026-06-11).
# `note` is the config comment convention; any `_`-prefixed key is meta (`_about`).
_STAGE_KEYS = frozenset({
    "node", "id", "validators", "max_attempts", "feedback", "escalate", "then",
    "fanout", "fork", "branch", "fanin", "wait", "clears", "concerns_from",
    "concerns_into", "clearable", "on_error", "note",
})

# Every key build_graph reads off the graph object itself. Same silent-no-op
# class as stage keys: a typo'd `stiky` would quietly change nothing.
# `constraints` is validation-only (never read by build_graph): declared
# ordering rules checked below.
_GRAPH_KEYS = frozenset({"start", "stages", "sticky", "constraints", "note"})

_CONSTRAINT_KEYS = frozenset({"precedes", "note"})


def _check_on_error(stage: str, oe: Any, errs: List[str]) -> None:
    """Hard-check the `on_error` recovery shape (the contract harness._handle_error
    executes): "clear" | null | {"compensate": target, "on_compensate_fail"?:
    "error"|"warn"}. A typo here would otherwise silently DISABLE recovery
    ("claer" matches no branch — no clear, no compensate, ever) or silently flip
    the rollback-failure severity ("warning" -> the "error" default). Recovery an
    author believes is configured must not be a no-op, so this is a load ERROR,
    not a lint."""
    if oe == "clear":
        return
    if isinstance(oe, dict):
        target = oe.get("compensate")
        if not (isinstance(target, str) and target):
            errs.append("stage {!r}: on_error object needs a non-empty `compensate` "
                        "target string (fn:/node:/http:)".format(stage))
        ocf = oe.get("on_compensate_fail", "error")
        if ocf not in ("error", "warn"):
            errs.append("stage {!r}: on_compensate_fail must be \"error\" or \"warn\", "
                        "got {!r}".format(stage, ocf))
        unknown = sorted(set(oe) - {"compensate", "on_compensate_fail"})
        if unknown:
            errs.append("stage {!r}: unknown on_error key(s) {}; known: compensate, "
                        "on_compensate_fail".format(stage, unknown))
        return
    errs.append("stage {!r}: on_error must be \"clear\", null, or "
                "{{\"compensate\": target}}, got {!r}".format(stage, oe))


def _successor_edges(stages: Dict[str, Any]) -> Dict[str, set]:
    """stage -> set of possible NEXT stages, from every routing key build_graph
    reads (then, branch routes + default, fork targets). The fanin `expect`
    list names PREDECESSORS, not successors, so it adds no edge."""
    edges: Dict[str, set] = {}
    for name, s in stages.items():
        nxt = set()
        if s.get("then"):
            nxt.add(s["then"])
        b = s.get("branch") or {}
        nxt.update(v for v in (b.get("routes") or {}).values())
        if b.get("default"):
            nxt.add(b["default"])
        nxt.update(s.get("fork") or [])
        edges[name] = {t for t in nxt if t in stages}
    return edges


def _reachable(edges: Dict[str, Any], frm: str, *, avoid: Optional[str] = None) -> set:
    """Stages reachable FROM `frm` (not including it, unless via a loop). With
    `avoid`, traversal may not enter that stage — so `x in
    _reachable(edges, start, avoid=early)` answers "can x run without `early`
    having run first?", which stays correct in graphs with legitimate
    back-loops (a plain reverse-reachability test would not)."""
    seen: set = set()
    todo = [frm]
    while todo:
        for t in edges.get(todo.pop(), ()):
            if t not in seen and t != avoid:
                seen.add(t)
                todo.append(t)
    return seen


def _check_constraints(cons: Any, start: Any, stages: Dict[str, Any],
                       errs: List[str]) -> None:
    """Gate-ordering rules as config (bash-era checklist rules — 'DB-migration
    gate BEFORE test stages', 'data-audit presented LAST to the human' — were
    enforced by convention and lost silently in any port). Each
    `constraints.precedes` entry is an `[early, late]` stage pair meaning:
    no execution may reach `late` without having passed `early` (dominator
    semantics, loop-tolerant). The engine validates only the declared pairs —
    the app names its own stages, the engine stays domain-free."""
    if not isinstance(cons, dict):
        errs.append("graph.constraints must be an object")
        return
    for k in cons:
        if k not in _CONSTRAINT_KEYS and not k.startswith("_"):
            errs.append("graph.constraints: unknown key {!r}{}; known: {}".format(
                k, _suggest(k, _CONSTRAINT_KEYS), ", ".join(sorted(_CONSTRAINT_KEYS))))
    pairs = cons.get("precedes", [])
    if not isinstance(pairs, list):
        errs.append("graph.constraints.precedes must be a list of [early, late] stage pairs")
        return
    edges = _successor_edges(stages)
    from_start = _reachable(edges, start) | {start} if start in stages else set(stages)
    for i, pair in enumerate(pairs):
        if (not isinstance(pair, (list, tuple)) or len(pair) != 2
                or not all(isinstance(x, str) for x in pair)):
            errs.append('constraints.precedes[{}]: expected ["early", "late"], '
                        "got {!r}".format(i, pair))
            continue
        early, late = pair
        bad = False
        for x in (early, late):
            if x not in stages:
                errs.append("constraints.precedes[{}]: {!r} is not a stage{}".format(
                    i, x, _suggest(x, stages)))
                bad = True
        if bad:
            continue
        if late not in from_start or early not in from_start:
            errs.append("constraints.precedes[{}]: {!r} is unreachable from graph.start "
                        "— the constraint is vacuous (dead stage or typo)".format(
                            i, late if late not in from_start else early))
            continue
        if late == start or late in _reachable(edges, start, avoid=early):
            errs.append("constraints.precedes[{}]: {!r} can run without {!r} having "
                        "run first — a route from graph.start reaches it while "
                        "bypassing the required stage".format(i, late, early))


def validate_pipeline(config: Dict[str, Any], base_path: Optional[str] = None) -> None:
    """Fail fast on a malformed pipeline at BUILD time instead of mid-run. Every
    cross-reference must resolve: graph.start, each `then`, branch routes/default
    → a declared stage; each stage's node, validators, fanout roles → a declared
    node. Every stage key must be known (an unknown key is a silent no-op — the
    sink/sinks bug class). Catches the typo'd `then` (KeyError deep in the loop)
    and the missing node role (LookupError in-proc, silent NATS timeout when
    distributed) before anything runs. Raises ValueError with every problem found.

    `base_path` (the root config's dir, as `build` passes its `base_dir`) lets the
    data-flow contract check read `template_file` renders; omit it and a
    `template_file` edge is left to the runtime's fail-loud instead of guessed at."""
    nodes = set(config.get("nodes", {}))
    g = config.get("graph") or {}
    stages = g.get("stages", {})
    errs: List[str] = []
    # A node with no `type` is almost always a STALE OVERLAY KEY: an `_extends`
    # overlay set fields on a role the base pipeline renamed/removed, the merge
    # created an orphan, and the build then failed elsewhere with no culprit
    # named (BUG-695 #6b: overlay said role:green, pipeline had role:green-run).
    for role, n in (config.get("nodes") or {}).items():
        if role.startswith("_"):
            continue
        if not isinstance(n, dict) or not n.get("type"):
            errs.append(
                "node {!r} has no 'type'{} — if this comes from an `_extends` "
                "overlay, the base pipeline has no such node (stale overlay key "
                "after a rename/removal?)".format(role, _suggest(role, nodes - {role})))
            continue
        # ADR-0005 `provides` (data-flow contract): the keys a node GUARANTEES on the
        # payload. Required to lint across an envelope-transform (whose output keys are
        # otherwise opaque), optional elsewhere as an explicit override. Must be a list
        # of non-empty key strings; the requires↔provides lint reads it.
        prov = n.get("provides")
        if prov is not None and (not isinstance(prov, list)
                                 or not all(isinstance(k, str) and k for k in prov)):
            errs.append("node {!r}: 'provides' must be a list of non-empty payload-key "
                        "strings (the keys this node guarantees on the payload)".format(role))
    for k in g:
        if k not in _GRAPH_KEYS and not k.startswith("_"):
            errs.append("graph: unknown key {!r}{}; known: {}".format(
                k, _suggest(k, _GRAPH_KEYS), ", ".join(sorted(_GRAPH_KEYS))))
    sticky = g.get("sticky")
    if sticky is not None and (not isinstance(sticky, list)
                               or not all(isinstance(k, str) and k for k in sticky)):
        errs.append("graph.sticky must be a list of non-empty payload-key strings")
    if not stages:
        errs.append("graph has no stages")
    start = g.get("start")
    if start not in stages:
        errs.append("graph.start {!r} is not a stage".format(start))
    stage_names = set(stages)
    for name, s in stages.items():
        fo = s.get("fanout") or []
        fk = s.get("fork") or []
        is_fork = bool(fk)
        if fo and fk:
            errs.append("stage {!r}: has both 'fanout' and 'fork' — one stage, one parallel shape".format(name))
        node = s.get("node")
        if not node:
            if not is_fork and not s.get("fanin"):
                errs.append("stage {!r}: missing 'node'".format(name))
        elif node not in nodes:
            errs.append("stage {!r}: node {!r} is not a declared node".format(name, node))
        for v in s.get("validators", []) or []:
            if v not in nodes:
                errs.append("stage {!r}: validator {!r} is not a declared node".format(name, v))
        for r in fo:  # fanout = the role BARRIER: every target must be a declared node
            if r not in nodes:
                hint = " (it IS a stage — did you mean \"fork\"?)" if r in stage_names else ""
                errs.append("stage {!r}: fanout role {!r} is not a declared node{}".format(name, r, hint))
        for t in fk:  # fork = branch CHAINS: every target must be a declared stage
            if t not in stage_names:
                hint = " (it IS a node — did you mean \"fanout\"?)" if t in nodes else ""
                errs.append("stage {!r}: fork target {!r} is not a stage{}".format(name, t, hint))
        fi = s.get("fanin") or {}
        if fi and not isinstance(fi, dict):
            errs.append("stage {!r}: fanin must be an object".format(name))
        for e in (fi.get("expect") if isinstance(fi.get("expect"), list) else []):
            if e not in stages:
                errs.append("stage {!r}: fanin expects {!r}, not a stage".format(name, e))
        then = s.get("then")
        if then is not None and then not in stages:
            errs.append("stage {!r}: then {!r} is not a stage".format(name, then))
        b = s.get("branch") or {}
        for val, dest in (b.get("routes") or {}).items():
            if dest not in stages:
                errs.append("stage {!r}: branch route {!r} -> {!r} is not a stage".format(name, val, dest))
        dflt = b.get("default")
        if dflt is not None and dflt not in stages:
            errs.append("stage {!r}: branch default {!r} is not a stage".format(name, dflt))
        cf = s.get("concerns_from")
        if cf is not None and not (isinstance(cf, str) and cf):
            errs.append("stage {!r}: concerns_from must be a non-empty payload-key string".format(name))
        ci = s.get("concerns_into")
        if ci is not None and not (isinstance(ci, str) and ci):
            errs.append("stage {!r}: concerns_into must be a non-empty payload-key string".format(name))
        if s.get("on_error") is not None:  # absent -> default "clear"; null -> opt out
            _check_on_error(name, s["on_error"], errs)
        for k in s:
            if k not in _STAGE_KEYS and not k.startswith("_"):
                errs.append("stage {!r}: unknown key {!r}; known: {}".format(
                    name, k, ", ".join(sorted(_STAGE_KEYS))))
    cons = g.get("constraints")
    if cons is not None:
        _check_constraints(cons, start, stages, errs)
    # The data-flow contract (ADR-0005 + ADR-0006 §D5): fail loud at LOAD on a consumer that
    # reads a key provably ABSENT on a closed path. One analysis, shared with lint_pipeline —
    # validate takes the ERRORS (here), the lint takes the WARNINGS.
    from .dataflow import analyze_dataflow
    df_errors, _ = analyze_dataflow(config.get("nodes") or {}, stages,
                                    g.get("sticky") or [], g.get("start"), base_path)
    errs.extend(df_errors)
    if errs:
        raise ValueError("invalid pipeline:\n  - " + "\n  - ".join(errs))


def _augment_provides_from_code(nodes: Dict[str, Any],
                                resolve: Callable[[Any], Optional[List[str]]]) -> Dict[str, Any]:
    """Return a shallow copy of `nodes` with `provides` filled in for any UNDECLARED
    envelope-transform whose fn: target `resolve` maps to keys (ADR-0005 slice D, the
    @provides decorator read from code). Pure — never mutates the input; a node that
    resolves to nothing is left untouched, so the lint taints it exactly as before."""
    out: Dict[str, Any] = {}
    for role, n in nodes.items():
        # "undeclared" == provides is not a list — the SAME predicate dataflow._transfer uses,
        # so a deliberate `provides: []` (declares "adds nothing") counts as declared here too
        # and is never overridden by code-read keys.
        if (isinstance(n, dict) and n.get("type") == "transform"
                and n.get("call") == "envelope" and not isinstance(n.get("provides"), list)):
            keys = resolve(n.get("target"))
            if keys:
                n = dict(n)
                n["provides"] = list(keys)
        out[role] = n
    return out


def lint_pipeline(config: Dict[str, Any], base_path: Optional[str] = None,
                  resolve: Optional[Callable[[Any], Optional[List[str]]]] = None) -> List[str]:
    """Advisory lint over a VALID pipeline config — returns WARNINGS, never raises.

    Catches valid-but-RISKY shapes that otherwise bite deep in a run, each rule traced
    to a real failure (mailbox M5-r). Distinct from `validate_pipeline` (hard errors):
    a config can be perfectly valid yet weak enough that a run dies far from the cause.
    Callers surface these (e.g. `yaah validate` prints them) WITHOUT blocking the run;
    `yaah validate --strict` fails (exit 2) on any warning for CI.

    `base_path` is the directory the pipeline's `template_file` paths resolve against — the
    ROOT config's dir, which the runtime passes to `build` as `base_dir` (so it must match
    `_build_render`'s resolution, NOT the pipeline file's own dir). When omitted, the
    render-template lint checks only inline `template_text`; a `template_file` it can't
    locate is skipped, never a false warning.

    `resolve` (ADR-0005 slice D, OPT-IN) maps a transform's `target` to the keys its
    `@provides`-decorated fn declares, so the lint sees across an envelope-transform without
    the author writing `provides` in config too. It IMPORTS app code, so it is injected by
    the caller only on opt-in (`yaah validate --from-code`); the default lint stays pure.

    SCOPE (be honest — a clean lint is NOT "production-safe"): these rules catch CONFIG
    contract weakness only — they do NOT check transform/agent LOGIC, semantic output
    correctness, or runtime data values. That's the job of tests, the counterfactual
    agents, and the followability eval, not the linter."""
    warnings: List[str] = []
    nodes = config.get("nodes") or {}
    if resolve is not None:
        nodes = _augment_provides_from_code(nodes, resolve)
    g = config.get("graph") or {}
    stages = g.get("stages") or {}
    sticky = g.get("sticky") or []
    _lint_weak_output_schema(nodes, warnings)
    _lint_gate_ignores_rejection(nodes, stages, warnings)
    # ADR-0005 slice B: the broad requires↔provides graph analysis (absorbs the 1a
    # single-hop render/branch checks as the 1-length-path case). Lives in its own module
    # (the dataflow lattice + fixpoint are independently testable); imported lazily to keep
    # this module cheap to import.
    from .dataflow import analyze_dataflow
    _, df_warnings = analyze_dataflow(nodes, stages, sticky, g.get("start"), base_path)
    warnings.extend(df_warnings)
    return warnings


def _lint_weak_output_schema(nodes: Dict[str, Any], warnings: List[str]) -> None:
    """Rule `weak-output-schema` (M5-r row 3 — the validation wall in lint form). A
    `parse:true` agent whose `output_schema` REQUIRES keys but does not TYPE them only
    checks key PRESENCE, so a parseable-but-WRONG value passes `check_schema` and
    surfaces as a confusing symptom many stages downstream. Declare `type`/`enum` on
    each required key so bad output is caught at the stage that produced it.

    Known limit (we can't read intent): a `type: string` field is treated as constrained
    and does NOT warn — even when an `enum` was meant — because a genuine free-form field
    (a `reason`) legitimately is `type: string`. The rule flags the unambiguous case (a
    required key with NO type/enum at all), not weak-but-plausible typing."""
    for role, node in nodes.items():
        if role.startswith("_") or not isinstance(node, dict):
            continue
        if node.get("type") != "agent" or node.get("parse") is False:
            continue
        schema = node.get("output_schema")
        if not isinstance(schema, dict):
            continue
        # `required`/`properties` may be malformed (e.g. `required: 5`, `properties: [...]`):
        # validate_pipeline doesn't check output_schema internals, so guard here — the lint
        # NEVER raises (it's advisory; a bad schema is for the schema validator, not a crash).
        required = schema.get("required")
        if not isinstance(required, list):
            continue
        props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        untyped = [k for k in required
                   if not isinstance(props.get(k), dict)
                   or not ("type" in props[k] or "enum" in props[k])]
        if required and untyped:
            warnings.append(
                "node {!r}: output_schema requires {} but leaves {} untyped (no "
                "type/enum). A parseable-but-wrong value then passes check_schema and "
                "surfaces far downstream — declare type/enum on each so bad output is "
                "caught here. [lint: weak-output-schema]".format(role, required, untyped))


def _gate_decision_outcomes(node: Dict[str, Any], forms: Dict[str, Any]) -> Optional[int]:
    """How many values a human_gate's `decision` may take, per its declared form (the built-in
    catalog, or the inline `decision_schema` for form 'json_schema'). None when the form declares
    no `decision` enum — free_text (key is `answer`), an unconstrained decision, or no form — so
    there is nothing to branch on. A count >= 2 means the gate needs the decision routed."""
    form = node.get("form")
    if form == "json_schema":
        schema = node.get("decision_schema")
    elif isinstance(form, str) and form in forms:
        schema = forms[form].get("schema")
    else:
        return None
    props = schema.get("properties") if isinstance(schema, dict) else None
    dec = props.get("decision") if isinstance(props, dict) else None
    enum = dec.get("enum") if isinstance(dec, dict) else None
    return len(enum) if isinstance(enum, list) else None


def _lint_gate_ignores_rejection(nodes: Dict[str, Any], stages: Dict[str, Any],
                                 warnings: List[str]) -> None:
    """Rule `gate-decision-ignored`. A human_gate whose form admits >= 2 decision
    outcomes (e.g. `approve_or_revise`) is only meaningful if the run ROUTES on `decision` — the
    harness's `_next_stage` returns `then` whenever a stage has no branch, so with nothing
    branching on `decision` a non-approve decision is silently ignored (the worst HITL failure).

    Heuristic, hence a WARNING not a load error: the engine can't PROVE the decision is unhandled
    — a transform could consume it instead of a branch (opaque to the linter). So we only nudge,
    and we suppress the nudge if ANY stage branches on `decision` (assume it's handled → no false
    positive). `approve` (one outcome) and `free_text` (no decision) never trigger.

    The suppression is pipeline-wide, so a SECOND gate whose decision goes unrouted is a false
    negative — acceptable while gates share one `decision` key."""
    from .harness.decision_forms import FORMS
    branches_on_decision = any(
        (s.get("branch") or {}).get("on") == "decision" for s in stages.values())
    if branches_on_decision:
        return
    for name, s in stages.items():
        node = nodes.get(s.get("node"))
        if not (isinstance(node, dict) and node.get("type") == "human_gate"):
            continue
        outcomes = _gate_decision_outcomes(node, FORMS)
        if outcomes is not None and outcomes >= 2:
            warnings.append(
                "stage {!r}: human_gate form {!r} admits {} decision outcomes, but nothing in "
                "the pipeline branches on `decision` — every outcome routes to `then`, so a "
                "non-approve decision is silently ignored. Add `branch: {{\"on\": \"decision\", "
                "\"routes\": {{...}}}}` on this gate (or confirm a transform consumes the "
                "decision). [lint: gate-decision-ignored]".format(
                    name, node.get("form"), outcomes))


def validate_budgets(root: Dict[str, Any], pipeline: Dict[str, Any]) -> None:
    """Timeout budget coherence (BUG-635/626 class): a per-call timeout that
    cannot fit its enclosing ceiling is a config bug — the work outlives the
    window that waits for it, the caller sees a generic timeout, and the worker
    keeps running as a zombie whose result is lost. Checked at LOAD (admission)
    by the runtime, the one place the deployment root and the pipeline meet:
      - distributed transport: a node's `timeout` must fit
        `transport.request_timeout` (the NATS reply window);
      - a fork's `wait.timeout` must cover the largest single node `timeout`
        inside its branches (the join would abandon a branch that was
        CONFIGURED to take longer).
    Pure data, no I/O; raises ValueError listing every violation."""
    errs: List[str] = []
    nodes = pipeline.get("nodes") or {}
    stages = (pipeline.get("graph") or {}).get("stages") or {}
    transport = root.get("transport") or {}
    ceiling = transport.get("request_timeout", 300.0) if transport.get("type") == "nats" else None
    if isinstance(ceiling, (int, float)):
        for role, n in nodes.items():
            t = n.get("timeout") if isinstance(n, dict) else None
            if isinstance(t, (int, float)) and t > ceiling:
                errs.append(
                    "node {!r}: timeout {}s exceeds transport.request_timeout {}s — "
                    "the caller's reply window closes before the node can finish".format(
                        role, t, ceiling))
    edges = _successor_edges(stages)
    for name, s in stages.items():
        wait = s.get("wait") if isinstance(s.get("wait"), dict) else {}
        w = wait.get("timeout")
        fork = s.get("fork") or []
        if not (fork and isinstance(w, (int, float))):
            continue
        branch_stages = set(fork)
        for t in fork:
            branch_stages |= _reachable(edges, t)
        worst: Optional[Tuple[str, float]] = None
        for bs in branch_stages:
            node = (stages.get(bs) or {}).get("node")
            nt = (nodes.get(node) or {}).get("timeout") if node else None
            if isinstance(nt, (int, float)) and (worst is None or nt > worst[1]):
                worst = (bs, nt)
        if worst and worst[1] > w:
            errs.append(
                "stage {!r}: wait.timeout {}s is smaller than branch stage {!r}'s "
                "node timeout {}s — the join would abandon a branch configured to "
                "take longer".format(name, w, worst[0], worst[1]))
    if errs:
        raise ValueError("incoherent timeout budget:\n  - " + "\n  - ".join(errs))


def is_fork_config(stage_config: Dict[str, Any], stage_names: set) -> bool:
    """Public single source of truth for "is this stage a fork?" — used by
    scripts/render_pipeline_svg. Since the explicit-key split this just reads
    the `fork` key (no more target-sniffing); `stage_names` kept for signature
    compatibility."""
    return _is_fork(stage_config, stage_names)
