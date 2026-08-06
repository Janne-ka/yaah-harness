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
import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .node_keys import legal_keys, unknown_node_keys  # pure table, no third-party deps

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
    "checkpoint_ttl", "lease_horizon", "lease_host", "run_dir",
    "live_config", "plugins", "strict_resume",
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
)
# `pipeline` (like `input`) is a path string OR an inline object — the schema,
# the runtime, and `yaah validate` all accept both; checked separately below.
_BOOL_KEYS = ("run", "interactive", "live_config", "strict_resume")

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
    rd = root.get("run_dir")
    if rd is not None and not isinstance(rd, str):
        errs.append("'run_dir': expected a path string (this run's artifact root, "
                    "base-relative or absolute), got {} {!r} — it is joined against "
                    "the config's own directory on the way in and is what the "
                    "{{run_dir}} node-spec macro expands to, so a non-string dies as "
                    "a bare TypeError inside assembly".format(type(rd).__name__, rd))
    if "input" in root and not isinstance(root["input"], (str, dict)):
        errs.append("'input': expected a fixture path or an inline payload object, got {} {!r}".format(
            type(root["input"]).__name__, root["input"]))
    pipe = root.get("pipeline")
    if pipe is not None and not isinstance(pipe, (str, dict)):
        errs.append("'pipeline': expected a path string or an inline pipeline "
                    "object, got {} {!r}".format(type(pipe).__name__, pipe))
    plugins = root.get("plugins")
    if plugins is not None and not (isinstance(plugins, list)
                                    and all(isinstance(m, str) and m for m in plugins)):
        errs.append('`plugins` must be a list of module-path strings '
                    '(imported before validation; see yaah.plugins)')
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
    "node", "id", "validators", "max_attempts", "error_retries", "feedback",
    "escalate", "then", "final", "fanout", "min_success", "fork", "branch", "fanin",
    "foreach", "wait", "clears", "concerns_from", "concerns_into", "clearable",
    "on_error", "effects_from", "note",
})

# The continuation keys that make a stage NON-terminal — build_graph reads each as a
# routing edge (then/branch/fork/fanout/fanin/foreach). `final: true` is illegal
# alongside any of them: sticky is the run frame the dataflow lattice re-folds on
# EVERY edge, so only a terminal stage (the last word) may drop the re-fold.
_CONTINUATION_KEYS = ("then", "branch", "fork", "fanout", "fanin", "foreach")

# the keys a `foreach` block may carry (ADR-0007) — a typo'd `max_parallel`
# would otherwise silently run unbounded, the same silent-no-op class as
# _STAGE_KEYS itself.
_FOREACH_KEYS = frozenset({"items", "into", "carry", "max_concurrent"})

# Every key build_graph reads off the graph object itself. Same silent-no-op
# class as stage keys: a typo'd `stiky` would quietly change nothing.
# `constraints` is validation-only (never read by build_graph): declared
# ordering rules checked below.
# `on_failure` (ADR-0009 D1, the auto-saga opt-in) is read by the RUNTIME
# BOUNDARY (yaah.saga), never by build_graph — listed here so an armed pipeline
# is not rejected as a typo'd key; its value grammar is checked in
# validate_pipeline (yaah.saga.check_on_failure_value, the one implementation).
_GRAPH_KEYS = frozenset({"start", "stages", "sticky", "constraints", "note",
                         "on_failure"})

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
        unknown = sorted(k for k in oe
                         if k not in ("compensate", "on_compensate_fail", "note")
                         and not k.startswith("_"))  # note/_* = config comments
        if unknown:
            errs.append("stage {!r}: unknown on_error key(s) {}; known: compensate, "
                        "on_compensate_fail".format(stage, unknown))
        return
    errs.append("stage {!r}: on_error must be \"clear\", null, or "
                "{{\"compensate\": target}}, got {!r}".format(stage, oe))


def _check_rollback(role: str, rb: Any, errs: List[str]) -> None:
    """Hard-check the node-level `rollback` capability block (ADR-0008 D1): the
    author-declared undo the `yaah rollback` verb walks in reverse. Shape:
    {"target": <fn:/http: call_target>, "cost"?: "cheap"|"costly"}. Mirrors
    _check_on_error's per-key rejection so a typo can't silently disable the undo
    (a rollback the author believes is configured must not be a no-op).

    `target` is restricted to fn:/http: in v1: the verb runs OUTSIDE a harness (a
    CLI reading the trace file), which is comms-free, and a `node:` target
    hard-requires comms (external_call raises without it). A node: target is
    REJECTED here rather than left to validate, record, list in the menu, and die
    at --execute (design-eval #3)."""
    if not isinstance(rb, dict):
        errs.append("node {!r}: rollback must be an object {{target, cost?}}, "
                    "got {!r}".format(role, rb))
        return
    target = rb.get("target")
    if not (isinstance(target, str) and target):
        errs.append("node {!r}: rollback needs a non-empty `target` call_target "
                    "string (fn:/http:)".format(role))
    elif target.startswith("node:"):
        errs.append("node {!r}: rollback target {!r} is a node: target, but the "
                    "rollback verb runs OUTSIDE a harness (a CLI reading the trace) "
                    "with no comms, and a node: target requires comms — use an "
                    "fn:/http: target".format(role, target))
    elif not (target.startswith("fn:") or target.startswith("http:")):
        errs.append("node {!r}: rollback target {!r} must be an fn:/http: "
                    "call_target".format(role, target))
    cost = rb.get("cost", "cheap")
    if cost not in ("cheap", "costly"):
        errs.append("node {!r}: rollback.cost must be \"cheap\" or \"costly\", "
                    "got {!r}".format(role, cost))
    unknown = sorted(k for k in rb
                     if k not in ("target", "cost", "note")
                     and not k.startswith("_"))  # note/_* = config comments
    if unknown:
        errs.append("node {!r}: unknown rollback key(s) {}; known: target, "
                    "cost".format(role, unknown))


def _check_node_keys(role: str, n: Dict[str, Any], errs: List[str]) -> None:
    """Reject a spec key no builder of this node's TYPE reads — the same
    silent-no-op class as `_STAGE_KEYS`, one level down. An unread key is DROPPED:
    the run looks healthy and the feature the author configured was never there.
    That is how `target_from`/`interpolate_from` sat in a live pipeline for five
    weeks against an engine that had never heard of them, which is also how long
    `docs/shape-grammar.md` had been claiming this check existed.

    The legal set per type is `node_keys.BUILTIN_NODE_KEYS` (one row per builder).
    A CUSTOM type the engine does not build is skipped — its keys are the
    registering app's business, not ours."""
    ntype = n.get("type")
    unknown = unknown_node_keys(ntype, n)
    if not unknown:
        return
    legal = legal_keys(ntype)
    for k in unknown:
        errs.append(
            "node {!r} (type {!r}): unknown key {!r}{} — no {!r} builder reads it, "
            "so it is silently dropped at run time. Legal keys for {!r}: {} (plus "
            "any `_`-prefixed comment key)".format(
                role, ntype, k, _suggest(k, legal), ntype, ntype,
                ", ".join(sorted(legal))))


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


def _fork_reachable_scope(stages: Dict[str, Any]) -> set:
    """The set of stages the ForkCoordinator (not the linear `_drive`) walks — a
    fork BRANCH chain or a fan-in `then` chain — reached from any fork stage's
    targets. `final` (skip the sticky re-fold) is honored only on the linear
    terminal, so a `final` on one of these is a silent no-op that validate rejects.

    DEFENSIVE by contract: `validate_pipeline` calls this BEFORE it has flagged a
    malformed `fork`/`branch` value, so it must never crash on one (the gather-all
    contract). Every routing value is type-checked; a malformed one contributes no
    edge and is rejected by the shape checks elsewhere. Follows `then`/`branch`
    routes + `fork` targets — the same successor relation `_successor_edges` uses,
    but tolerant of bad shapes."""
    edges: Dict[str, set] = {}
    for name, s in stages.items():
        nxt: set = set()
        if isinstance(s, dict):
            then = s.get("then")
            if isinstance(then, str):
                nxt.add(then)
            b = s.get("branch")
            if isinstance(b, dict):
                routes = b.get("routes")
                if isinstance(routes, dict):
                    nxt.update(v for v in routes.values() if isinstance(v, str))
                if isinstance(b.get("default"), str):
                    nxt.add(b["default"])
            fork = s.get("fork")
            if isinstance(fork, list):
                nxt.update(t for t in fork if isinstance(t, str))
        edges[name] = {t for t in nxt if t in stages}
    scope: set = set()
    for s in stages.values():
        if not isinstance(s, dict):
            continue
        fork = s.get("fork")
        if isinstance(fork, list):
            for t in fork:
                if isinstance(t, str) and t in stages:
                    scope |= _reachable(edges, t) | {t}
    return scope


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


def validate_pipeline(config: Dict[str, Any], base_path: Optional[str] = None, *,
                      contract_for: Optional[Any] = None,
                      consumes_for: Optional[Any] = None,
                      strict_resume: bool = True) -> None:
    """Fail fast on a malformed pipeline at BUILD time instead of mid-run. Every
    cross-reference must resolve: graph.start, each `then`, branch routes/default
    → a declared stage; each stage's node, validators, fanout roles → a declared
    node. Every stage key must be known (an unknown key is a silent no-op — the
    sink/sinks bug class). Catches the typo'd `then` (KeyError deep in the loop)
    and the missing node role (LookupError in-proc, silent NATS timeout when
    distributed) before anything runs. Raises ValueError with every problem found.

    `base_path` (the root config's dir, as `build` passes its `base_dir`) lets the
    data-flow contract check read `template_file` renders; omit it and a
    `template_file` edge is left to the runtime's fail-loud instead of guessed at.

    `contract_for` / `consumes_for` (ADR-0006 D7.2): an embedding app's registry
    contract sources (`Registry.contract_source()` / `consumes_source()`) so its
    CUSTOM node types get the same ERROR-grade data-flow checking as built-ins.
    None (the default) = built-ins + inline declarations only, byte-identical.

    `strict_resume` (default True, matching the harness/runtime default) tiers ONE
    finding: a human_gate branch route the form's decision enum can never produce is
    PROVABLY DEAD under resume enforcement (decision_rejected) → an ERROR here. With
    strict_resume=False the lenient blind-merge makes the forbidden decision reachable
    again, so that finding downgrades to a lint WARNING (see `lint_pipeline`) and is NOT
    raised here. `build()` passes the harness's own strict_resume so the load-time verdict
    matches the run's actual enforcement.

    The per-node-type key check has NO blanket opt-out, deliberately: a pipeline
    carrying keys this engine cannot read is a pipeline whose features are simply not
    there (the five-week `target_from` silence this table was built for), and a flag
    that tolerates them tolerates exactly that. A pipeline authored against a newer
    engine is answered by pinning the engine, not by silencing the reader."""
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
        _check_node_keys(role, n, errs)
        # ADR-0005 `provides` (data-flow contract): the keys a node GUARANTEES on the
        # payload. Required to lint across an envelope-transform (whose output keys are
        # otherwise opaque), optional elsewhere as an explicit override. Must be a list
        # of non-empty key strings; the requires↔provides lint reads it.
        prov = n.get("provides")
        if prov is not None and (not isinstance(prov, list)
                                 or not all(isinstance(k, str) and k for k in prov)):
            errs.append("node {!r}: 'provides' must be a list of non-empty payload-key "
                        "strings (the keys this node guarantees on the payload)".format(role))
        # ADR-0010 attach: a list of `fn:` attachers merged onto an agent's output. The SHAPE
        # is checked here (a hard ERROR at LOAD, not mid-build) — a non-list / non-string item
        # otherwise fell through the contract's truthiness read (silently dropping the `closed`
        # proof) and exploded in `_build_agent` only AFTER paid model calls. The fn:-target
        # grammar + Attacher-subclass check stay in the builder (they import consumer code).
        att = n.get("attach")
        if att is not None and (not isinstance(att, list)
                                or not all(isinstance(a, str) and a for a in att)):
            errs.append("node {!r}: 'attach' must be a list of non-empty 'fn:module:func' "
                        "attacher strings (each merges post-invoke keys onto the agent's "
                        "output payload; declare those keys in `provides` so the data-flow "
                        "lint can see them)".format(role))
        # M7 ladder: escalate_model's trigger is a PARSED `help` key, so it needs
        # the parse path — with parse:false it would silently never fire (the
        # silent-misconfig class); reject loud instead.
        em = n.get("escalate_model")
        if em is not None:
            if not (isinstance(em, str) and em):
                errs.append("node {!r}: escalate_model must be a non-empty model "
                            "string (e.g. \"claude:sonnet\")".format(role))
            elif n.get("parse") is False:
                errs.append("node {!r}: escalate_model needs parse (the `help` "
                            "trigger is a parsed key) — remove `parse: false` or "
                            "drop escalate_model".format(role))
        # ADR-0008 D1: the node-level `rollback` capability block (the author's
        # per-node undo the `yaah rollback` verb runs). Absent = the node is
        # honestly-unknown/irreversible; present = shape-checked here.
        rb = n.get("rollback")
        if rb is not None:
            _check_rollback(role, rb, errs)
    for k in g:
        if k not in _GRAPH_KEYS and not k.startswith("_"):
            errs.append("graph: unknown key {!r}{}; known: {}".format(
                k, _suggest(k, _GRAPH_KEYS), ", ".join(sorted(_GRAPH_KEYS))))
    # ADR-0009 D1: the auto-saga opt-in's value grammar ("rollback" | {mode,
    # include_costly}). ONE implementation shared with the runtime arm point
    # (yaah.saga) — a typo'd value must not silently leave un-armed a saga the
    # author believes is armed (the same silent-disable class as _check_on_error).
    if "on_failure" in g:
        from .saga import check_on_failure_value
        errs.extend(check_on_failure_value(g.get("on_failure")))
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
    # Stages the ForkCoordinator walks (a fork BRANCH chain or a fan-in `then`
    # chain) rather than the linear `_drive`. `final` (skip the sticky re-fold) is
    # honored ONLY on the linear walk's terminal — the fold sites inside the fork
    # machinery are unconditional — so `final` on a fork-scoped stage would silently
    # do nothing. Computed once here (DEFENSIVELY — a malformed fork/branch is
    # flagged below, not crashed on) to reject that no-op loud.
    _fork_scope = _fork_reachable_scope(stages)
    for name, s in stages.items():
        # a non-list here used to escape into the loops below as a raw TypeError
        bad_shape = [k for k in ("fanout", "fork", "validators")
                     if k in s and not isinstance(s[k], list)]
        if bad_shape:
            errs.append("stage {!r}: {} must be a list of names, got {}".format(
                name, "/".join(repr(k) for k in bad_shape),
                "/".join(type(s[k]).__name__ for k in bad_shape)))
            s = {k: v for k, v in s.items() if k not in bad_shape}
        fo = s.get("fanout") or []
        fk = s.get("fork") or []
        is_fork = bool(fk)
        if fo and fk:
            errs.append("stage {!r}: has both 'fanout' and 'fork' — one stage, one parallel shape".format(name))
        # A `fanin` is a parallel JOIN; `fanout`/`fork` is a parallel SOURCE. One stage
        # that is both is a walker-dependent trap — `_drive` reaches it fork-first, the
        # branch walker fanin-first, so which shape "wins" depends on the walk, not the
        # config. Reject at load rather than run something order-dependent (the dataflow
        # lattice tolerates it soundly, but the runtime behaviour is ambiguous).
        if s.get("fanin") and (fo or fk):
            src = "fanout" if fo else "fork"
            errs.append("stage {!r}: has both 'fanin' (a parallel JOIN) and {!r} (a "
                        "parallel SOURCE) — one stage cannot be both; split the join and "
                        "the {} into separate stages".format(name, src, src))
        # foreach (ADR-0007): dynamic per-item fan-out — one stage, one parallel shape,
        # so it excludes every other shape; then the block's own structure is checked
        # (a malformed foreach that slipped to the harness would fail at RUN time on
        # the first swarm, the exact late-discovery class validate exists to kill).
        fe = s.get("foreach")
        if fe is not None:
            for other in ("fanout", "fork", "fanin"):
                if s.get(other):
                    errs.append("stage {!r}: has both 'foreach' and {!r} — one stage, "
                                "one parallel shape".format(name, other))
            if not isinstance(fe, dict):
                errs.append("stage {!r}: foreach must be an object "
                            "{{items, into?, carry?, max_concurrent?}}, got {}".format(
                                name, type(fe).__name__))
            else:
                for k in fe:
                    if k not in _FOREACH_KEYS:
                        errs.append("stage {!r}: unknown foreach key {!r}; known: {}".format(
                            name, k, ", ".join(sorted(_FOREACH_KEYS))))
                items = fe.get("items")
                if not (isinstance(items, str) and items):
                    errs.append("stage {!r}: foreach.items must be a non-empty payload-key "
                                "string (the upstream-provided list), got {!r}".format(
                                    name, items))
                into = fe.get("into")
                if into is not None and not (isinstance(into, str) and into):
                    errs.append("stage {!r}: foreach.into must be a non-empty string".format(name))
                carry = fe.get("carry")
                if carry is not None and (not isinstance(carry, list)
                                          or not all(isinstance(c, str) and c for c in carry)):
                    errs.append("stage {!r}: foreach.carry must be a list of payload-key "
                                "strings".format(name))
                mc = fe.get("max_concurrent")
                if mc is not None and (not isinstance(mc, int) or isinstance(mc, bool)
                                       or mc < 1):
                    errs.append("stage {!r}: foreach.max_concurrent must be a positive "
                                "int, got {!r}".format(name, mc))
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
        _expect = fi.get("expect") if isinstance(fi, dict) else None
        for e in (_expect if isinstance(_expect, list) else []):
            if e not in stages:
                errs.append("stage {!r}: fanin expects {!r}, not a stage".format(name, e))
        then = s.get("then")
        if then is not None and then not in stages:
            errs.append("stage {!r}: then {!r} is not a stage".format(name, then))
        _branch_raw = s.get("branch")
        b = _branch_raw if isinstance(_branch_raw, dict) else {}
        if _branch_raw is not None and not isinstance(_branch_raw, dict):
            errs.append("stage {!r}: branch must be an object "
                        "{{on, routes?, default?}}, got {}".format(name, type(_branch_raw).__name__))
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
        # ADR-0008 D2: effects_from names the payload key whose value the harness
        # copies onto the completion span (the effect HANDLE the rollback verb
        # reads). REJECTED on a fork/fanin stage: the fork PARENT's completion span
        # is emitted on a separate no-output path, so the copy would silently
        # record nothing (design-eval #6) — forbidden loudly instead. Linear,
        # branch-child, and foreach stages all complete through the shared seam.
        ef = s.get("effects_from")
        if ef is not None:
            if not (isinstance(ef, str) and ef):
                errs.append("stage {!r}: effects_from must be a non-empty payload-key "
                            "string (the effect handle the rollback verb reads)".format(name))
            elif s.get("fork") or s.get("fanin"):
                errs.append("stage {!r}: effects_from is not allowed on a fork/fanin "
                            "stage — the fork parent's completion span is emitted on a "
                            "separate no-output path, so the effect descriptor would "
                            "silently record nothing; put effects_from on the stage "
                            "that actually produces the effect".format(name))
        ms = s.get("min_success")
        if ms is not None:
            fo = s.get("fanout")
            fe = s.get("foreach")
            if isinstance(fe, dict):
                # foreach's item count is RUNTIME-sized (ADR-0007, design-eval #1):
                # the only static bound is >= 1 — a compile-time upper bound would
                # need the list length, which doesn't exist yet.
                if not (isinstance(ms, int) and not isinstance(ms, bool) and ms >= 1):
                    errs.append("stage {!r}: min_success must be a positive int "
                                "(got {!r})".format(name, ms))
            elif not isinstance(fo, list):
                errs.append("stage {!r}: min_success only applies to a fanout or "
                            "foreach stage (set `fanout: [roles...]` / `foreach: "
                            "{{...}}`, or remove it)".format(name))
            elif not (isinstance(ms, int) and not isinstance(ms, bool)
                      and 1 <= ms <= len(fo)):
                errs.append("stage {!r}: min_success must be an int between 1 and "
                            "len(fanout)={} (got {!r})".format(name, len(fo), ms))
        fin = s.get("final")
        if fin is not None:
            if not isinstance(fin, bool):
                errs.append("stage {!r}: final must be true or false, got {!r}".format(name, fin))
            elif fin:
                cont = [k for k in _CONTINUATION_KEYS if s.get(k)]
                if cont:
                    errs.append(
                        "stage {!r}: final: true is only legal on a TERMINAL stage, but "
                        "this stage also has {} — sticky keys are the run frame the "
                        "dataflow lattice re-folds on every edge (downstream cwd_from "
                        "threading depends on it), so only the LAST word (a stage with no "
                        "then/branch/fork/fanout/fanin/foreach) may drop the re-fold. "
                        "Remove `final` or make this stage terminal.".format(
                            name, "/".join(cont)))
                elif name in _fork_scope:
                    errs.append(
                        "stage {!r}: final: true is set on a stage inside a fork's scope "
                        "(a fork branch or a fan-in `then` chain), which the fork "
                        "coordinator walks — the sticky re-fold there is unconditional, so "
                        "`final` would silently do nothing. `final` is honored only on the "
                        "linear terminal of the run; move the cleanup after the fork (on "
                        "the fork stage's own `then`) or drop `final`.".format(name))
        if s.get("on_error"):  # falsy (absent/null/false) = default-or-opt-out, like the harness
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
    # validate takes the ERRORS (here), the lint takes the WARNINGS. Only on a structurally
    # sound graph: the analysis assumes valid shapes (a `fanout: 5` would crash it), and the
    # structural findings above already fail the config loud.
    if not errs:
        from .dataflow import analyze_dataflow
        df_errors, _ = analyze_dataflow(config.get("nodes") or {}, stages,
                                        g.get("sticky") or [], g.get("start"), base_path,
                                        contract_for=contract_for,
                                        consumes_for=consumes_for)
        errs.extend(df_errors)
        # A human_gate branch route the form's decision enum can never produce is PROVABLY DEAD
        # under resume enforcement — an ERROR (like the provably-wrong dataflow findings above),
        # but ONLY when strict_resume enforces the form. With strict_resume off, the lenient
        # blind-merge reaches it again → downgraded to a lint WARNING in lint_pipeline, not here.
        # Gated on `not errs` for the same reason: the detector reads branch.routes shapes the
        # structural checks above must first have vouched for.
        if strict_resume:
            errs.extend(_gate_dead_route_msg(st, key, form, enum, lenient=False)
                        for st, key, form, enum in _gate_dead_routes(config.get("nodes") or {},
                                                                     stages))
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
                  resolve: Optional[Callable[[Any], Optional[List[str]]]] = None, *,
                  contract_for: Optional[Any] = None,
                  consumes_for: Optional[Any] = None,
                  strict_resume: bool = True) -> List[str]:
    """Advisory lint over a VALID pipeline config — returns WARNINGS, never raises.

    Catches valid-but-RISKY shapes that otherwise bite deep in a run, each rule traced
    to a real failure (mailbox M5-r). Distinct from `validate_pipeline` (hard errors):
    a config can be perfectly valid yet weak enough that a run dies far from the cause.
    Callers surface these (e.g. `yaah validate` prints them) WITHOUT blocking the run;
    `yaah validate --strict` fails (exit 2) on any warning for CI.

    `strict_resume` (default True) mirrors `validate_pipeline`'s tiering of the
    `gate-route-not-in-form` finding: when True the dead-route check is an ERROR raised by
    validate_pipeline (so it is NOT re-emitted here — that would double-report). When False
    the route is reachable via the lenient blind-merge, so it appears HERE as a warning with
    the "enforcement is off" note. Passed down from `validate_config` (which reads the root's
    `strict_resume`).

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
    _graph = config.get("graph")
    g: Dict[str, Any] = _graph if isinstance(_graph, dict) else {}
    stages = g.get("stages") or {}
    sticky = g.get("sticky") or []
    # never-raises contract: a non-dict container (nodes: "x", stages: [...]) or a
    # non-dict stage VALUE is validate_pipeline's to reject — the lint treats it as
    # absent rather than crash every rule below (and `dataflow._edges`) on `.items()`
    # / `.get()` (adversarial-eval finding, 2026-07-07).
    if not isinstance(nodes, dict):
        nodes = {}
    if not isinstance(stages, dict):
        stages = {}
    elif any(not isinstance(s, dict) for s in stages.values()):
        stages = {k: s for k, s in stages.items() if isinstance(s, dict)}
    _lint_weak_output_schema(nodes, warnings)
    _lint_attach_undeclared_keys(nodes, warnings)
    _lint_gate_ignores_rejection(nodes, stages, warnings)
    # gate-route-not-in-form: only the LENIENT tier surfaces here (a WARNING). Under
    # strict_resume the same finding is a validate_pipeline ERROR (raised, not linted) — so
    # emitting it here too would double-report. When strict_resume is off the route is reachable
    # via the blind-merge, so it is a genuine advisory-only warning with the enforcement caveat.
    if not strict_resume:
        for st, key, form, enum in _gate_dead_routes(nodes, stages):
            warnings.append(_gate_dead_route_msg(st, key, form, enum, lenient=True))
    _lint_render_template_unreadable(nodes, warnings)
    _lint_rollback_without_effects(nodes, stages, warnings)
    _lint_reserved_key_collision(nodes, stages, sticky, warnings)
    _lint_clear_is_not_rollback(nodes, stages, warnings)
    _lint_untrusted_unfenced(nodes, stages, base_path, warnings)
    # ADR-0005 slice B: the broad requires↔provides graph analysis (absorbs the 1a
    # single-hop render/branch checks as the 1-length-path case). Lives in its own module
    # (the dataflow lattice + fixpoint are independently testable); imported lazily to keep
    # this module cheap to import.
    from .dataflow import analyze_dataflow
    _, df_warnings = analyze_dataflow(nodes, stages, sticky, g.get("start"), base_path,
                                      contract_for=contract_for, consumes_for=consumes_for)
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
        _props = schema.get("properties")
        props = _props if isinstance(_props, dict) else {}
        untyped = [k for k in required
                   if not isinstance(props, dict)
                   or not isinstance(props.get(k), dict)
                   or not ("type" in props[k] or "enum" in props[k])]
        if required and untyped:
            warnings.append(
                "node {!r}: output_schema requires {} but leaves {} untyped (no "
                "type/enum). A parseable-but-wrong value then passes check_schema and "
                "surfaces far downstream — declare type/enum on each so bad output is "
                "caught here. [lint: weak-output-schema]".format(role, required, untyped))


def _lint_render_template_unreadable(nodes: Dict[str, Any], warnings: List[str]) -> None:
    """Rule `render-template-unreadable`. A render whose `template_file` is macro'd with
    `{run_dir}` cannot be read where templates are read statically — that artifact root
    exists only once a run starts — so the consumes lint sees no `{{key}}` reads and every
    data-flow check on this node passes vacuously. Say so: "reads nothing checkable" and
    "could not be read" are different verdicts and only one of them is a clean bill.

    `{base_dir}` does NOT fire this: it IS the lint's `base_path`, so those templates are
    expanded and read (templating.render_template_text). `allow_unfilled: true` silences
    it — the author already declared they accept literal holes."""
    from .templating import unresolvable_macro
    for role, node in nodes.items():
        if role.startswith("_") or not isinstance(node, dict):
            continue
        if node.get("type") != "render" or node.get("allow_unfilled"):
            continue
        macro = unresolvable_macro(node)
        if macro:
            warnings.append(
                "node {!r}: `template_file` contains {}, which resolves only at run start "
                "— the lint could NOT read this template, so its {{{{key}}}} reads are "
                "UNCHECKED (a key missing from the payload renders a literal hole into the "
                "output at exit 0). Keep the template under {} where the lint can read it, "
                "or set allow_unfilled:true to accept the gap explicitly. "
                "[lint: render-template-unreadable]".format(role, macro, "{base_dir}"))


def _lint_attach_undeclared_keys(nodes: Dict[str, Any], warnings: List[str]) -> None:
    """Rule `attach-undeclared-keys` (ADR-0010). A `parse:false` agent with a non-empty
    `attach:` merges attacher-supplied keys (fn: code, unenumerable statically) onto its
    output — but the data-flow lint cannot see them, so a downstream `render`/`branch` reading
    one surfaces as an `-unprovided` warning that looks like a bug. Declaring the keys in the
    agent's own `provides:` makes them visible (`resolve_contract` augments provides uniformly).

    Scoped to `parse:false`: there the agent is otherwise CLOSED {raw} (runtime-exact), so
    attach is the SOLE loosener and declaring its keys is the whole remedy. A `parse:true`
    agent is never closed (its parsed keys are already only `complete`), so an undeclared
    attach key there is caught by the ordinary downstream `-unprovided` warning without a
    separate node-level nudge (and firing here would newly flag the shipped parse:true+attach
    examples, which route their attach keys into opaque transforms — noise, not a finding).

    It is NOT redundant with the downstream `-unprovided` warning: that warning fires only when
    a checkable consumer (a render/branch) reads an attach key — when the keys flow into an
    OPAQUE transform (or nothing reads them), no downstream warning fires, and this nudge is the
    only signal for the exact silent gap ADR-0010 named. Fires only when NO `provides` is
    declared; ANY declared `provides` list silences it — so `provides: []` is the explicit
    opt-out for an author who intends an open-ended attach with no lint-visible keys."""
    for role, node in nodes.items():
        if role.startswith("_") or not isinstance(node, dict):
            continue
        if node.get("type") != "agent" or node.get("parse") is not False:
            continue
        attach = node.get("attach")
        if not (isinstance(attach, list)
                and any(isinstance(a, str) and a for a in attach)):
            continue  # absent / empty / malformed (malformed is a validate_pipeline ERROR)
        if isinstance(node.get("provides"), list):
            continue  # already declared → keys are visible to the lint
        warnings.append(
            "node {!r}: `attach:` merges attacher-supplied keys onto the payload, but they "
            "are invisible to the data-flow lint (fn: code) — a downstream render/branch that "
            "reads one warns as unprovided. Declare them in this agent's `provides: [...]` so "
            "the lint can check them. [lint: attach-undeclared-keys]".format(role))


def _gate_decision_outcomes(node: Dict[str, Any], forms: Dict[str, Any]) -> Optional[int]:
    """How many values a human_gate's `decision` may take, per its declared form. None when the
    form declares no `decision` enum (free_text, an unconstrained decision, or no form) — nothing
    to branch on. A count >= 2 means the gate needs the decision routed. Thin wrapper over
    `_gate_decision_enum` (the shared form-schema resolver)."""
    enum = _gate_decision_enum(node, forms)
    return len(enum) if enum is not None else None


def _gate_decision_enum(node: Dict[str, Any], forms: Dict[str, Any]) -> Optional[List[str]]:
    """The list of values a human_gate's `decision` may take, per its declared form (or the
    inline `decision_schema` for form 'json_schema'). None when the form declares no `decision`
    enum — free_text (key is `answer`), a json_schema `decision` with a `type` but no enum, an
    unconstrained decision, or no form. A returned list is the CLOSED set of decisions resume
    enforcement will accept, so a branch route key outside it can never fire under enforcement."""
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
    return [v for v in enum if isinstance(v, str)] if isinstance(enum, list) else None


def _gate_dead_routes(nodes: Dict[str, Any], stages: Dict[str, Any]) -> List[Tuple[str, str, str, List[str]]]:
    """Every (stage, route_key, form, enum) where a human_gate stage BRANCHES on its own
    `decision` and a NAMED route key falls outside the form's decision enum — a route provably
    unreachable under resume enforcement (the human can never submit that decision). Only NAMED
    routes are checked; `default` (the catch-all) is never dead. Forms with no decision enum
    (free_text, an un-enum'd json_schema) contribute nothing. Never raises: a malformed
    branch/routes shape (validate_pipeline's to reject) contributes no finding.

    Shared by the ERROR path (`validate_pipeline`, strict_resume on → provably dead) and the
    WARNING path (`lint_pipeline`, strict_resume off → only reachable via the lenient blind
    merge). ONE detector so the two tiers can never diverge on WHAT is dead."""
    from .harness.decision_forms import FORMS
    out: List[Tuple[str, str, str, List[str]]] = []
    for name, s in stages.items():
        if not isinstance(s, dict):
            continue
        ref = s.get("node")
        node = nodes.get(ref) if isinstance(ref, str) else None
        if not (isinstance(node, dict) and node.get("type") == "human_gate"):
            continue
        b = s.get("branch")
        if not (isinstance(b, dict) and b.get("on") == "decision"):
            continue
        routes = b.get("routes")
        if not isinstance(routes, dict):
            continue
        enum = _gate_decision_enum(node, FORMS)
        if enum is None:
            continue   # no enum constraint → nothing to check (free_text, un-enum'd json_schema)
        allowed = set(enum)
        for key in routes:
            if isinstance(key, str) and key not in allowed:
                out.append((name, key, str(node.get("form")), enum))
    return out


def _gate_dead_route_msg(stage: str, key: str, form: str, enum: List[str], *,
                         lenient: bool) -> str:
    """The house-style message for a gate route the form can never produce. Names the stage, the
    dead route key, the form + its enum, and the remedy. The `lenient` tail (strict_resume off)
    explains the route is only reachable because enforcement is off — downgrading the finding's
    force to a warning while keeping the same diagnosis."""
    tail = (" This is reachable ONLY because strict_resume is false (the lenient blind-merge "
            "accepts a non-conforming decision); under enforcement it is dead."
            if lenient else "")
    return (
        "stage {s!r}: branch routes on `decision` {k!r}, but the human_gate form {f!r} admits "
        "only {enum} — the human can never submit {k!r}, so the route can never fire under resume "
        "enforcement (decision_rejected). Use a form whose decisions include {k!r} — e.g. "
        "approve_or_revise (approve/revise) or a json_schema form with {k!r} in the decision "
        "enum — or drop the dead route.{tail} [lint: gate-route-not-in-form]".format(
            s=stage, k=key, f=form, enum=enum, tail=tail))


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
        isinstance(s.get("branch"), dict) and s["branch"].get("on") == "decision"
        for s in stages.values()
        # non-dict stage value OR non-dict branch value: validate's to reject, not a lint crash
        if isinstance(s, dict))
    if branches_on_decision:
        return
    for name, s in stages.items():
        if not isinstance(s, dict):
            continue
        _ref = s.get("node")
        node = nodes.get(_ref) if isinstance(_ref, str) else None
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


def _lint_rollback_without_effects(nodes: Dict[str, Any], stages: Dict[str, Any],
                                   warnings: List[str]) -> None:
    """Rule `rollback-without-effects` (ADR-0008 D2). A node declares `rollback`
    but no stage running it declares `effects_from` — the undo target will receive
    no effect HANDLE (effects=None). Legal (some undos key off the correlation id
    alone), hence a WARNING not an error: the author should choose it knowingly,
    not arrive at it by omission. A node with at least one stage carrying
    effects_from is satisfied; a rollback node never wired into a stage is a
    different (out-of-scope) smell and is not flagged here."""
    nodes_with_effects = {s.get("node") for s in stages.values()
                          if isinstance(s, dict) and s.get("effects_from") and s.get("node")}
    for role, node in nodes.items():
        if role.startswith("_") or not isinstance(node, dict):
            continue
        if not isinstance(node.get("rollback"), dict):
            continue
        wired = any(isinstance(s, dict) and s.get("node") == role for s in stages.values())
        if wired and role not in nodes_with_effects:
            warnings.append(
                "node {!r} declares `rollback` but no stage running it declares "
                "`effects_from` — the undo target will receive no effect handle "
                "(effects=None). Legal if the undo keys off the correlation id "
                "alone; otherwise add `effects_from: \"<key>\"` to the stage that "
                "produces the effect. [lint: rollback-without-effects]".format(role))


# Payload keys the ENGINE injects on a `feedback: true` retry — declaring one as your OWN key
# silently collides. `harness._with_feedback` writes BOTH onto the payload before each retry
# (`feedback` = the validator failures, `priorAttempt` = the prior output payload), and the agent
# node's `_render` (agents/agent.py, exempted via its own `_ENGINE_INJECTED` frozenset) auto-
# appends a non-empty `feedback` value to the prompt. This set is the two `_with_feedback` keys —
# `_ENGINE_INJECTED` also carries `tool_manifest`, which is a render-mechanism key, not a
# retry-loop collision; kept in lock-step with those sites (a shared cross-module constant would
# be ideal but lives outside this module's edit surface).
_RESERVED_INJECTED_KEYS = frozenset({"feedback", "priorAttempt"})


def _reserved_hits(names: Iterable[Any], where: str, warnings: List[str]) -> None:
    """Append a reserved-key-collision warning for each reserved name found in `names` (an
    iterable of author-declared key strings) at surface `where`. Non-reserved / non-string
    members are ignored — the lint never raises (a malformed shape is validate_pipeline's job)."""
    for k in names:
        if k in _RESERVED_INJECTED_KEYS:
            warnings.append(
                "{}: uses reserved key {!r} as its own — the harness AUTO-INJECTS `feedback` "
                "(the validator failures) and `priorAttempt` (the prior output) onto the payload "
                "on every `feedback: true` retry, and an agent prompt auto-appends a non-empty "
                "`feedback` value, so your {!r} is silently overwritten/double-injected. Rename it "
                "(e.g. `loop_feedback`). [lint: reserved-key-collision]".format(where, k, k))


def _lint_reserved_key_collision(nodes: Dict[str, Any], stages: Dict[str, Any],
                                 sticky: Any, warnings: List[str]) -> None:
    """Rule `reserved-key-collision`. The harness OWNS `feedback`/`priorAttempt` on a
    `feedback: true` retry (see `_RESERVED_INJECTED_KEYS`). An author who DECLARES either as
    their OWN payload key gets silent double-injection/collision — the verify-loop cookbook
    example hit this and renamed its key to `loop_feedback`.

    Flags every AUTHORED-KEY surface: a node's `provides`, an agent's `output_schema`
    properties/required, `graph.sticky`, a `foreach` block's `into`/`items`/`carry`, and a
    stage's `effects_from`/`concerns_from`/`concerns_into`. Deliberately NOT flagged: a
    NODE-level `carry` (arch-drift carries `feedback` to keep the engine's own `{{feedback}}`
    filled — cooperation, not collision) and a `human_gate`'s `decision_schema` (the human's
    revise note, a different key surface). A transform that merely RETURNS a `feedback` key from
    its fn body is invisible here (opaque code) — undetectable statically, by design."""
    for role, node in nodes.items():
        if role.startswith("_") or not isinstance(node, dict):
            continue
        prov = node.get("provides")
        if isinstance(prov, list):
            _reserved_hits(prov, "node {!r} `provides`".format(role), warnings)
        if node.get("type") == "agent":
            schema = node.get("output_schema")
            if isinstance(schema, dict):
                props = schema.get("properties")
                if isinstance(props, dict):
                    _reserved_hits(props.keys(),
                                   "node {!r} `output_schema` properties".format(role), warnings)
                req = schema.get("required")
                if isinstance(req, list):
                    _reserved_hits(req, "node {!r} `output_schema` required".format(role), warnings)
    if isinstance(sticky, list):
        _reserved_hits(sticky, "graph.sticky", warnings)
    for name, s in stages.items():
        if not isinstance(s, dict):
            continue
        fe = s.get("foreach")
        if isinstance(fe, dict):
            for fk in ("into", "items"):
                v = fe.get(fk)
                if isinstance(v, str):
                    _reserved_hits([v], "stage {!r} foreach.{}".format(name, fk), warnings)
            carry = fe.get("carry")
            if isinstance(carry, list):
                _reserved_hits(carry, "stage {!r} foreach.carry".format(name), warnings)
        for sk in ("effects_from", "concerns_from", "concerns_into"):
            v = s.get(sk)
            if isinstance(v, str):
                _reserved_hits([v], "stage {!r} {}".format(name, sk), warnings)


def _stage_acknowledges_effects(stage: Dict[str, Any]) -> bool:
    """True when a stage's `on_error` acknowledges a committed external effect on failure: an
    explicit `null` (fail-as-is opt-out) or a `{compensate: target}` undo. ABSENT or "clear" do
    NOT — clear drops ENGINE state only (ADR-0008), leaving any external effect in place."""
    if "on_error" not in stage:
        return False                        # default is "clear"
    oe = stage["on_error"]
    if oe is None:
        return True                         # explicit fail-as-is opt-out
    return isinstance(oe, dict) and bool(oe.get("compensate"))


def _lint_clear_is_not_rollback(nodes: Dict[str, Any], stages: Dict[str, Any],
                                warnings: List[str]) -> None:
    """Rule `clear-is-not-rollback` (ADR-0008 Context; slop-audit D#6). `on_error: "clear"` (the
    default) drops ENGINE state only — a side-effecting node failing under it READS as cleaned up
    while its external effect persists. Nudge when a node the author marked `idempotent: true`
    (the OnceNode wrapper: a replay-sensitive COMMITTED external effect, architecture.md) is run
    by any stage under bare `clear`, with no undo declared.

    Covered (no warn): the node declares a node-level `rollback` (undo later via `yaah rollback`,
    covers every stage), OR the stage declares `on_error: {compensate: ...}` (undo at failure),
    OR the stage declares `on_error: null` (the author explicitly owns the fail-as-is risk).
    Aggregation is per-STAGE and sound: if even ONE stage runs the node under bare `clear`, warn
    naming that stage — a node run by a compensating stage AND a clearing stage still has the
    silent-effect gap on the clearing one (the fast counter-arg's must-fix, verified).

    Deliberate scope (a WARNING, hence advisory, hence tolerant of the rare miss): `idempotent`
    is the author's OWN committed-effect signal, so this is precise and low-false-positive.
    `post`/`shell` nodes WITHOUT `idempotent` are NOT flagged — most are read-only or safely
    re-runnable (`git status`, a GET-shaped post), and firing on all of them would false-positive
    enough to get the rule ignored. An author with an undo-unnecessary idempotent effect writes
    `on_error: null` once to say so."""
    for role, node in nodes.items():
        if role.startswith("_") or not isinstance(node, dict) or not node.get("idempotent"):
            continue
        if isinstance(node.get("rollback"), dict):
            continue                        # node-level undo declared -> covered for every stage
        uncovered = sorted(name for name, s in stages.items()
                           if isinstance(s, dict) and s.get("node") == role
                           and not _stage_acknowledges_effects(s))
        if uncovered:
            warnings.append(
                "node {!r} is `idempotent: true` (a committed external effect) but stage(s) {} "
                "run it under bare `clear` — on failure `clear` drops ENGINE state only, so the "
                "external effect persists while the run reads as cleaned up. Declare `compensate` "
                "(undo at failure), a node-level `rollback` (undo later via yaah rollback), or "
                "`on_error: null` to acknowledge fail-as-is. "
                "[lint: clear-is-not-rollback]".format(role, uncovered))


# Fence-aware placeholder for the untrusted-unfenced lint (M12). The `\w+` KEY group matches
# `templating.PLACEHOLDER` exactly (the token render/human_gate actually fill); the optional
# leading `!` mirrors the AGENT node's fence syntax (`agents/agent.py::_PLACEHOLDER`, `{{!key}}`
# = mark the value untrusted). NOTE the honest asymmetry this lint is built on: only the AGENT
# node honors `!`; render and human_gate render via `templating.fill`, which leaves `{{!key}}`
# as a LITERAL and never frames a bare `{{key}}` value. So at those sites `!` is read here only
# as the author's untrusted-INTENT marker (→ quiet), not as a working runtime defense.
_UNTRUSTED_PLACEHOLDER = re.compile(r"{{\s*(!?)\s*(\w+)\s*}}")


def _agent_authored_keys(node: Dict[str, Any]) -> "frozenset":
    """The payload keys an AGENT node AUTHORS from its model output — the untrusted set. This is
    NARROWER than its data-flow contract: `carry`/`carry_cwd` keys are FORWARDED unchanged (they
    keep their upstream provenance, so they aren't attributed to this agent), and only these
    authored keys are. `raw` is always authored (the raw model text). With `parse` and an
    `output_schema`, the declared/required keys are the model's parsed fields; an inline
    `provides` on the agent counts too. A parse:true agent with NO schema authors keys we can't
    ENUMERATE, so only `raw` is named (its other parsed keys stay unattributed → the lint is
    quiet on them rather than guess). Never raises on a malformed schema."""
    authored = {"raw"}
    if node.get("parse") is False:
        return frozenset(authored)
    schema = node.get("output_schema")
    if isinstance(schema, dict):
        props = schema.get("properties")
        if isinstance(props, dict):
            authored.update(k for k in props if isinstance(k, str))
        req = schema.get("required")
        if isinstance(req, list):
            authored.update(k for k in req if isinstance(k, str))
    prov = node.get("provides")
    if isinstance(prov, list):
        authored.update(k for k in prov if isinstance(k, str))
    return frozenset(authored)


def _ancestors(preds: Dict[str, List[str]], target: str) -> set:
    """Every stage reverse-reachable from `target` via the predecessor map (dataflow._edges,
    which includes fanin edges) — i.e. every stage that CAN run before `target` on some path."""
    seen: set = set()
    todo = list(preds.get(target, ()))
    while todo:
        p = todo.pop()
        if p not in seen:
            seen.add(p)
            todo.extend(preds.get(p, ()))
    return seen


def _consumer_template(node: Dict[str, Any], base_path: Optional[str]) -> Optional[str]:
    """The template text a CONSUMER node interpolates payload keys into, or None if this node
    type isn't an unframed consumer (or its template can't be read statically). The two unframed
    consumers: `human_gate`'s inline `ask` string, and `render`'s `template_text`/`template_file`
    (read relative to base_path, reusing the render lint's file resolver)."""
    ntype = node.get("type")
    if ntype == "human_gate":
        ask = node.get("ask")
        return ask if isinstance(ask, str) else None
    if ntype == "render":
        from .templating import render_template_text
        return render_template_text(node, base_path)
    return None


def _untrusted_msg(consumer: str, ntype: Any, key: str, producers: List[str]) -> str:
    ph = "{{" + key + "}}"
    fenced = "{{!" + key + "}}"
    prod = ", ".join(repr(p) for p in producers)
    # Neither consumer can fence (templating.fill leaves {{!key}} literal), so the
    # per-site opt-out is `allow_untrusted: true` on the consumer node — the author's
    # acknowledgment that the value reaches its consumer unfenced BY DESIGN. The
    # wording differs by consumer: a render's output feeds a human/file (not a model
    # prompt); a human_gate's `ask` reaches a human decision-maker who IS the firewall
    # (ITEM 2 — the reviewer is meant to see the agent text as-is and act on it).
    if ntype == "render":
        render_optout = (" — or, if the render's output feeds a human/file and not a "
                         "model prompt, set allow_untrusted:true on the render")
    elif ntype == "human_gate":
        render_optout = (" — or, if the human reviewer is meant to see this agent text "
                         "as-is (the decision-maker is the firewall), set "
                         "allow_untrusted:true on the gate")
    else:
        render_optout = ""
    return (
        "stage {c!r}: the {t} interpolates {ph} UNFENCED, but {k!r} is agent-authored "
        "(produced by {prod}). A {t} renders via the plain templater — it does NOT frame or "
        "escape the value and does NOT honor {fenced} fencing (only an agent prompt does), so "
        "the raw model text reaches the consumer (a human, an AI operator driving the gate, or "
        "a rendered document) as-is. HEURISTIC, not an injection-safety proof: sanitize the "
        "value in an upstream transform, or confirm the consumer cannot act on injected "
        "instructions{optout}. [lint: untrusted-unfenced]".format(
            c=consumer, t=ntype, ph=ph, k=key, prod=prod, fenced=fenced,
            optout=render_optout))


def _lint_untrusted_unfenced(nodes: Dict[str, Any], stages: Dict[str, Any],
                             base_path: Optional[str], warnings: List[str]) -> None:
    """Rule `untrusted-unfenced` (mailbox M12). An ADVISORY heuristic — explicitly NOT an
    injection-safety proof. It flags a consumer site (`human_gate` ask / `render` template) that
    interpolates `{{key}}` UNFENCED where an AGENT stage on some path to it AUTHORS `key`. Agent
    output is untrusted text; those two consumer types render it UNFRAMED (see
    `_UNTRUSTED_PLACEHOLDER`), so a crafted value reaches a human / AI operator / document as-is.

    Per-site opt-out (`allow_untrusted: true`): on EITHER consumer, the author's acknowledgment
    that the value reaches its consumer unfenced by design — a render whose output feeds a
    human/file, or a human_gate whose human reviewer is the firewall (ITEM 2). Neither can fence
    ({{!key}} is a literal there), so this flag is the in-place remedy; it silences ONLY the node
    that sets it. Set on any other node (e.g. the producing agent) it is ignored.

    Provenance is graph reachability, NOT the data-flow lattice: an agent ancestor that authors
    the key taints the consumer EVEN THROUGH an opaque parse-transform (the client's real shape:
    agent → parse envelope-transform → gate). Deliberately QUIET when the key's provenance is
    unknowable — a key only an opaque transform could have invented, an engine key (shell
    `exit_code`), a human_gate's own `decision`, or an entry/carried key — because the honest
    move is to flag agent-AUTHORED text, not to guess. A `{{!key}}` (author's untrusted marker)
    is quiet. One warning per (consumer, key). Never raises.

    Two honesty notes (adversarial eval 2026-06-30, judged against the real client config):
      - the findings are a FLOOR, not a clean bill: agent text RENAMED by a transform (a parse
        fn folding one agent key into a new name) loses its attribution and is NOT flagged — on
        the audited real config that was the MAJORITY of the exposed gate surface. So when any
        hit fires, ONE consolidated caveat warning says so; a reader of lint output alone must
        not conclude the flagged set is complete. (Adding the renamed key to the producing
        agent's `provides` restores attribution.)
      - an enum-constrained schema key (a decision limited to two values) is STILL flagged: it
        is agent-authored, and treating the schema constraint as a sanitizer would be exactly
        the guessing this rule refuses. Intentional; pinned by the judge-decision test."""
    from .dataflow import _edges
    preds = _edges(stages)
    authored_by: Dict[str, "frozenset"] = {}
    for s_name, s in stages.items():
        ref = s.get("node") if isinstance(s, dict) else None
        node = nodes.get(ref) if isinstance(ref, str) else None
        if isinstance(node, dict) and node.get("type") == "agent":
            if isinstance(s, dict) and isinstance(s.get("foreach"), dict):
                # ADR-0007: a foreach stage's agent output lands NESTED under
                # `results[i].payload`, never at the top level — attributing its
                # authored keys here would flag downstream reads of keys that
                # aren't actually there (design-eval nit #9). The nested content
                # is untrusted too, but a top-level {{key}} can't read it.
                continue
            authored_by[s_name] = _agent_authored_keys(node)
    if not authored_by:
        return
    hit_count = 0
    for c_name, s in stages.items():
        ref = s.get("node") if isinstance(s, dict) else None
        node = nodes.get(ref) if isinstance(ref, str) else None
        if not isinstance(node, dict):
            continue
        text = _consumer_template(node, base_path)
        if not text:
            continue
        # `allow_untrusted: true` opts a CONSUMER site out (neither render nor gate can
        # fence). On a render it asserts the output feeds a human/file; on a human_gate
        # (ITEM 2) it acknowledges the human decision-maker is the firewall — the agent
        # text is meant to reach them unfenced. Honored on those two consumer types only;
        # the key on any other node (e.g. the producing agent) is ignored here.
        if node.get("type") in ("render", "human_gate") and node.get("allow_untrusted"):
            continue
        producers_of: Dict[str, List[str]] = {}
        for anc in _ancestors(preds, c_name):
            for key in authored_by.get(anc, ()):
                producers_of.setdefault(key, []).append(anc)
        if not producers_of:
            continue
        seen: set = set()
        for m in _UNTRUSTED_PLACEHOLDER.finditer(text):
            fenced, key = m.group(1), m.group(2)
            if fenced or key in seen:
                continue
            producers = producers_of.get(key)
            if producers:
                seen.add(key)
                hit_count += 1
                warnings.append(_untrusted_msg(c_name, node.get("type"), key, sorted(set(producers))))
    if hit_count:
        # The floor-not-clean-bill caveat (see the docstring). Emitted only WITH hits, so it
        # never adds a fresh --strict failure to an otherwise-quiet pipeline — but anyone
        # triaging the hits sees, in the same output, that fixing them is not the whole surface.
        warnings.append(
            "the {} untrusted-unfenced finding(s) above are a FLOOR, not a clean bill: agent "
            "text RENAMED by a transform (a parse fn folding an agent key into a new name) "
            "loses its attribution and is NOT flagged. To widen coverage, add such renamed "
            "keys to the producing agent's `provides`. [lint: untrusted-unfenced]".format(hit_count))


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
        CONFIGURED to take longer);
      - `lease_horizon` must fit the effective running-checkpoint window
        (`checkpoint_ttl`, or `baton_ttl` when it is absent). A record swept
        before it can be declared stale is incoherent: a foreign host's crashed
        run would be deleted by the sweep while still inside the window that
        says "too fresh to recover", so it could never be recovered at all.
    Pure data, no I/O; raises ValueError listing every violation."""
    # Imported lazily: the defaults live with the code that OWNS them (one source of
    # truth), but pulling `yaah.harness` at module import would drag comms/store/trace
    # into every `yaah validate`.
    from .harness.baton import DEFAULT_BATON_TTL
    from .harness.lease_state import DEFAULT_LEASE_HORIZON

    errs: List[str] = []
    # TYPE FIRST, coherence second. A QUOTED number ("3600") is not comparable to a
    # number in py3, so the coherence check below did not fire on it — the config
    # was accepted and the window it describes was never checked. A wrong type IS
    # the config bug; report it rather than step over it. `null` stays legal on all
    # three (the ttls mean "never expire", the horizon means "use the default"),
    # and a bool is not a number here even though Python says it is.
    for key in ("lease_horizon", "checkpoint_ttl", "baton_ttl"):
        v = root.get(key)
        if key in root and v is not None and (isinstance(v, bool)
                                              or not isinstance(v, (int, float))):
            errs.append(
                "{}: expected a number of seconds (or null), got {} {!r} — a quoted "
                "number is not a number, and the lease/sweep coherence check below "
                "cannot compare what it cannot read".format(key, type(v).__name__, v))
    horizon = root.get("lease_horizon", DEFAULT_LEASE_HORIZON)
    checkpoint_window = root.get("checkpoint_ttl",
                                 root.get("baton_ttl", DEFAULT_BATON_TTL))
    if (isinstance(horizon, (int, float)) and isinstance(checkpoint_window, (int, float))
            and horizon > checkpoint_window):
        errs.append(
            "lease_horizon {}s exceeds the running-checkpoint window {}s ({}) — a "
            "crashed run on another host would be SWEPT before its lease is old "
            "enough to declare stale, so it could never be recovered. Lower "
            "lease_horizon or raise checkpoint_ttl.".format(
                horizon, checkpoint_window,
                "checkpoint_ttl" if "checkpoint_ttl" in root
                else ("baton_ttl" if "baton_ttl" in root else "the 72h default")))
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


# ---------- the whole-config check + its machine-readable diagnostics ---------
# Shared by the two operator surfaces (`yaah validate [--json]` in cli.py, the
# MCP `validate` tool in adapters/mcp_server/tools.py) so their diagnostics
# can never drift — they were hand-copied before, with a "kept in step" comment
# doing the synchronization.

def check_rollback_trace_sink(root: Dict[str, Any],
                              pipeline: Dict[str, Any]) -> List[str]:
    """ADR-0008 D2 + ADR-0009 D1 cross-file ERRORs — WIDENED (ADR-0009
    design-eval #4) to take the whole PIPELINE (nodes + graph) so the check can
    see `graph.on_failure`: a node declaring `rollback` OR an armed on_failure
    saga requires a persisted file trace sink under mode "tracer"; an armed saga
    with NO rollback-declaring node is a self-contradictory config (ERROR); a
    malformed on_failure value is an ERROR (never a silent un-arm).

    ONE shared implementation, in `yaah.saga` (this name stays exported here for
    back-compat importers), called from BOTH the author-time surface
    (`validate_config`) AND the runtime assembly (`runtime._assemble_harness`),
    because the check needs root+pipeline in scope and `validate_config` does
    NOT run on `yaah run` — so a "checked at load" that only lived in
    validate_config would be a lie (design-eval #2). Returns the error list
    (empty when satisfied); the caller raises."""
    from .saga import check_rollback_trace_sink as _impl
    return _impl(root, pipeline)


def validate_config(root: Dict[str, Any], base_path: str,
                    resolve: Optional[Callable[[Any], Optional[List[str]]]] = None) -> List[str]:
    """Validate a loaded root AND the pipeline it references — the full check
    behind `yaah validate` and the MCP `validate` tool. Raises ValueError on the
    first invalid layer (root, then pipeline); returns the pipeline's lint
    WARNINGS when everything is valid.

    The one I/O here: `root["pipeline"]` as a string is read as a JSON file
    relative to `base_path` (the ROOT config's dir — the same base the runtime
    builds with, so `template_file` lint paths match `_build_render`'s
    resolution). An inline pipeline dict is validated as-is; an absent/bad
    `pipeline` key is validate_root's to report. `resolve` is the opt-in
    `--from-code` @provides resolver, passed through to lint_pipeline.

    The root's `strict_resume` (default True) is threaded to both validate_pipeline and
    lint_pipeline so the `gate-route-not-in-form` finding is tiered against the SAME
    enforcement the deployment runs with — ERROR when on, WARNING when off."""
    validate_root(root)
    strict_resume = bool(root.get("strict_resume", True))
    pipeline_ref = root.get("pipeline")
    if isinstance(pipeline_ref, str):
        from .runtime_factories import _read_json, _rel
        pipeline_cfg = _read_json(_rel(base_path, pipeline_ref))
    elif isinstance(pipeline_ref, dict):
        pipeline_cfg = pipeline_ref
    else:
        return []   # no pipeline to check; root validation already vouched for the shape
    validate_pipeline(pipeline_cfg, base_path=base_path, strict_resume=strict_resume)
    # ADR-0008 D2 + ADR-0009 D1 cross-file ERRORs (root + pipeline in scope):
    # rollback / an armed on_failure saga needs a persisted file trace sink, and
    # an armed saga needs a rollback-declaring node. Also enforced in
    # runtime._assemble_harness so `yaah run` (which never calls validate_config)
    # refuses too. WIDENED call site (ADR-0009): passes the whole pipeline.
    rb_errs = check_rollback_trace_sink(root, pipeline_cfg)
    if rb_errs:
        raise ValueError("invalid config:\n  - " + "\n  - ".join(rb_errs))
    return lint_pipeline(pipeline_cfg, base_path=base_path, resolve=resolve,
                         strict_resume=strict_resume)


def split_diagnostics(exc_text: str) -> List[Dict[str, Any]]:
    """A validate ValueError's bulleted message as per-item diagnostics:
    [{message, stage?}]. Best-effort structure — `stage` is extracted where the
    message follows this module's "stage '<name>': ..." convention (a full
    code/path taxonomy is a follow-up; it needs per-site changes at every
    errs.append). The `{ok, errors, warnings}` shapes both operator surfaces
    emit are built from this."""
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


def split_lint_id(warning: str) -> "Tuple[Optional[str], str]":
    """A lint warning's (rule_id, message) — the "[lint: id]" trailer every
    lint_pipeline message carries, parsed HERE because this module owns that
    format. (None, warning) when there is no trailer."""
    m = re.search(r"\s*\[lint: ([a-z0-9-]+)\]$", warning)
    if m:
        return m.group(1), warning[:m.start()]
    return None, warning


# ---- terminal collapse for multi-item advisory classes -----------------------
# A few lint rules can name MANY nodes at once — either as one warning that lists
# them all (`transform-provides-undeclared`) or as one warning per site sharing a
# rule id (`untrusted-unfenced`). On a live run that wall of names buries the
# `ok: ... is valid` verdict the operator is actually looking for. The DEFAULT
# terminal rendering therefore COLLAPSES such a class to a single count line; the
# validate CLI's `--verbose` flag restores the full per-item listing. This is a
# RENDER concern (the lint strings themselves are unchanged, so `--json`,
# `--strict` counting, and every lint test still see the full set). A collapser
# lives HERE because this module owns the lint-string format each one must read.

def _collapse_transform_provides(msgs: List[str]) -> "Optional[str]":
    """`transform-provides-undeclared` is ONE consolidated warning naming every
    undeclared envelope-transform. Count them off the stable
    "envelope-transform(s) 't1', 't2', ... don't declare" prefix. One transform
    reads fine as-is, so collapse only kicks in from two."""
    head = msgs[0].split(" don't declare", 1)[0]
    n = len(re.findall(r"'[^']*'", head))
    if n < 2:
        return None
    return ("{} envelope-transform(s) don't declare `provides` (the lint skips "
            "requires-checks on their consumers) — run with --verbose to list "
            "them".format(n))


def _collapse_untrusted_unfenced(msgs: List[str]) -> "Optional[str]":
    """`untrusted-unfenced` emits ONE warning per consumer site; the count IS the
    number of warnings. A single site reads fine as-is."""
    n = len(msgs)
    if n < 2:
        return None
    return ("{} template site(s) interpolate agent-authored keys UNFENCED — run "
            "with --verbose to list them".format(n))


# rule id -> collapser(msgs_for_that_id) -> summary line, or None to keep as-is.
_COLLAPSIBLE_ADVISORIES = {
    "transform-provides-undeclared": _collapse_transform_provides,
    "untrusted-unfenced": _collapse_untrusted_unfenced,
}


def collapse_lint_warnings(warnings: List[str],
                           verbose: bool = False) -> "List[Tuple[Optional[str], str]]":
    """Render lint warnings for a terminal as (rule_id, message) pairs, in the
    original emission order. When `verbose` is False, a multi-item advisory class
    (see `_COLLAPSIBLE_ADVISORIES`) collapses to a single count line placed at the
    class's first occurrence; its other lines are dropped. Single-item warnings
    and rules with no collapser pass through unchanged. `verbose` restores the
    full one-line-per-warning listing."""
    parsed = [split_lint_id(w) for w in warnings]
    if verbose:
        return parsed
    grouped: Dict[str, List[str]] = {}
    for wid, msg in parsed:
        if wid in _COLLAPSIBLE_ADVISORIES:
            grouped.setdefault(wid, []).append(msg)
    out: List[Tuple[Optional[str], str]] = []
    emitted = set()
    for wid, msg in parsed:
        if wid in _COLLAPSIBLE_ADVISORIES:
            summary = _COLLAPSIBLE_ADVISORIES[wid](grouped[wid])
            if summary is not None:
                if wid not in emitted:
                    out.append((wid, summary))
                    emitted.add(wid)
                continue          # collapsed — drop the individual line
        out.append((wid, msg))
    return out
