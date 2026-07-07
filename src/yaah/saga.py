"""saga — auto-saga on terminal failure (ADR-0009), a runtime-boundary policy.

Used by: the runtime entrypoints (`runtime.run_root`, `runtime.resume_gate`) and
the MCP `run` tool, which route their terminal run/`drive`/`resume` call through
`settle_terminal`. NOT a Harness method — the harness stays ignorant of the saga
(ADR-0001 cosmology); the saga is a POLICY on top of the line, like `drive`.
Where: the seam between a run's terminal `StageFailed` and ADR-0008's rollback
executor (`yaah.rollback`), reused wholesale — the saga adds NO new store and NO
new undo capability, only the arming + idempotency-ledger reading.
Why: ADR-0008 shipped rollback as a human VERB and deferred the automatic saga
(its D4 non-goal). ADR-0009 builds it: on an ungated terminal failure a
per-graph opt-in (`graph.on_failure`) replays the rollback executor over the
run's PRIOR completed stages in reverse — cheap-only by default — then ALWAYS
re-raises the original failure (recovery runs; the failure still surfaces).

Two structural facts make this safe (verified against the engine, ADR-0009):
  - the arm point IS the `except StageFailed` boundary, so `Suspended` (a parked
    human gate) and `Cleared` (a reset) can NEVER fire it — invariant 1;
  - the failing stage's own `on_error` (clear/compensate) already ran INSIDE the
    harness before `StageFailed` propagated, and the failing stage emitted an
    ERROR span (not a completion), so `rollback._is_completion` excludes it — the
    saga only ever walks the PRIOR COMPLETED stages, never the failing one.

Also hosts the SLICE-1 config helpers (`check_on_failure_value`,
`check_rollback_trace_sink`) so both author-time validation (`validate_config`)
and run-time arming (`runtime._assemble_harness`) share ONE implementation of the
cross-checks — the dual-surface pattern ADR-0008 D2 pinned (validate_config never
runs on `yaah run`, so a "checked at load" that only lived there would be a lie).

Targets Python 3.9+.
"""
from __future__ import annotations

import sys
from typing import Any, Awaitable, Dict, List, Optional, Set, Tuple

from . import rollback
from .trace.span import Span

# ---------------------------------------------------------------------------
# arming grammar (shared by validation + the runtime policy)
# ---------------------------------------------------------------------------

_MODE = "rollback"


def _armed(on_failure: Any) -> bool:
    """True when `graph.on_failure` opts the graph into the auto-saga. The value
    grammar mirrors `on_error`: a bare string `"rollback"` (cheap-only) or an
    object `{"mode": "rollback", "include_costly"?: bool}`. Anything else (absent,
    None, a typo) leaves the graph un-armed — fully backward compatible."""
    if isinstance(on_failure, str):
        return on_failure == _MODE
    if isinstance(on_failure, dict):
        return on_failure.get("mode") == _MODE
    return False


def _include_costly(on_failure: Any) -> bool:
    """Whether the author opted into auto-undoing `costly` effects (D2). Default
    False (cheap-only) — an unattended undo of a semi-irreversible effect is the
    sharpest edge of D4, so it needs explicit, file-visible consent."""
    return isinstance(on_failure, dict) and bool(on_failure.get("include_costly"))


# ---------------------------------------------------------------------------
# SLICE 1 — config validation helpers (author time AND run time)
# ---------------------------------------------------------------------------

def check_on_failure_value(on_failure: Any) -> List[str]:
    """Hard-check the `graph.on_failure` value grammar (ADR-0009 D1). Absent = no
    saga (returns []). Mirrors `_check_on_error`'s per-key rejection so a typo
    can't silently DISABLE a saga the author believes is armed. Returns the error
    list (the caller raises — the same fatal-only contract as the other graph
    checks)."""
    if on_failure is None:
        return []
    if isinstance(on_failure, str):
        if on_failure != _MODE:
            return ["graph.on_failure string must be {!r}, got {!r}".format(
                _MODE, on_failure)]
        return []
    if isinstance(on_failure, dict):
        errs: List[str] = []
        mode = on_failure.get("mode")
        if mode != _MODE:
            errs.append("graph.on_failure.mode must be {!r}, got {!r}".format(
                _MODE, mode))
        ic = on_failure.get("include_costly", False)
        if not isinstance(ic, bool):
            errs.append("graph.on_failure.include_costly must be a bool, "
                        "got {!r}".format(ic))
        unknown = sorted(k for k in on_failure
                         if k not in ("mode", "include_costly", "note")
                         and not k.startswith("_"))  # note/_* = config comments
        if unknown:
            errs.append("graph.on_failure: unknown key(s) {}; known: mode, "
                        "include_costly".format(unknown))
        return errs
    return ["graph.on_failure must be {!r} or {{\"mode\": {!r}, "
            "\"include_costly\": bool}}, got {!r}".format(_MODE, _MODE, on_failure)]


def _declaring_nodes(nodes: Dict[str, Any]) -> List[str]:
    return sorted(role for role, n in (nodes or {}).items()
                  if isinstance(n, dict) and isinstance(n.get("rollback"), dict))


def check_rollback_trace_sink(root: Dict[str, Any],
                              pipeline: Dict[str, Any]) -> List[str]:
    """ADR-0008 D2 + ADR-0009 D1 cross-file ERRORs, WIDENED to take the whole
    PIPELINE (nodes + graph) so it can see `graph.on_failure` (design-eval #4). ONE
    shared helper, called from `validate_config` (author time) AND
    `runtime._assemble_harness` (run time — the check needs root+pipeline, and
    validate_config does NOT run on `yaah run`, so a "checked at load" that only
    lived in validate_config would be a lie). Returns the error list (empty when
    satisfied); the caller raises.

    Rules (all ERRORs — the helper is fatal-only, design-eval #3):
      - the `graph.on_failure` VALUE grammar (`check_on_failure_value`): a typo'd
        value would otherwise silently leave the graph UN-armed (`_armed` returns
        False on garbage) — the silent-DISABLE class. Enforcing it here makes the
        RUN path loud too, not just `yaah validate` (adversarial-eval finding:
        the grammar check must not be dead code on either surface).
      - ANY node declaring `rollback` (ADR-0008) OR an armed `graph.on_failure`
        (ADR-0009) REQUIRES a persisted JSONL file sink under mode "tracer" — the
        trace record IS the undo input.
      - `graph.on_failure` armed but NO node declares `rollback` → ERROR: the saga
        could never undo anything (a self-contradictory config, same class as a
        sink under `mode: none`).

    The non-inproc trace tail-lag caveat (D5) is a DOCUMENTED limit in the
    reference docs, NOT a coded warning here — this helper has no warning channel,
    and the lag is bounded (stop-on-partial bounds the blast radius)."""
    nodes = (pipeline or {}).get("nodes") or {}
    graph = (pipeline or {}).get("graph") or {}
    on_failure = graph.get("on_failure")
    armed = _armed(on_failure)
    declaring = _declaring_nodes(nodes)
    errs: List[str] = list(check_on_failure_value(on_failure))

    # ADR-0009 D1: an armed saga with nothing to undo is self-contradictory.
    if armed and not declaring:
        errs.append(
            "graph.on_failure arms the auto-saga (rollback) but NO node declares a "
            "`rollback:` block — the saga could never undo anything. Declare "
            "rollback on the side-effecting node(s), or remove graph.on_failure.")

    # File-sink requirement (ADR-0008 D2): applies whenever a rollback can run —
    # a declaring node OR an armed saga. When armed WITHOUT declaring nodes the
    # error above already fires; the sink check then adds nothing (there is
    # nothing to record), so it is scoped to the declaring set.
    if declaring:
        tr = root.get("trace") or {}
        mode = tr.get("mode", "tracer")
        if mode != "tracer":
            errs.append(
                "node(s) {} declare `rollback` but trace.mode is {!r} — sinks are "
                "only wired under mode \"tracer\", so no trace file is persisted, "
                "and the trace record IS the rollback input. Set trace.mode to "
                "\"tracer\" with a {{\"type\": \"file\"}} sink.".format(declaring, mode))
        else:
            sinks = tr.get("sinks")
            sink_list = sinks if isinstance(sinks, list) else (
                [sinks] if isinstance(sinks, dict) else [])
            has_file = any(isinstance(s, dict) and s.get("type") == "file"
                           for s in sink_list)
            if not has_file:
                errs.append(
                    "node(s) {} declare `rollback` but the root config has no "
                    "persisted JSONL trace sink ({{\"type\": \"file\"}} in "
                    "trace.sinks) — the trace record IS the rollback input (the "
                    "recorded `effects` handle the undo reads), so a declared "
                    "rollback can never run without it. Add a file sink to "
                    "trace.sinks.".format(declaring))
    return errs


# ---------------------------------------------------------------------------
# SLICE 2 — the runtime-boundary hook + the saga policy
# ---------------------------------------------------------------------------

def _resolve_pipeline(root: Dict[str, Any], base: str) -> Dict[str, Any]:
    """Resolve `root["pipeline"]` (an inline dict or a base-relative file path) to
    the pipeline dict — read ONLY on the terminal-failure path (rare), so the extra
    read costs nothing on the happy path."""
    ref = root.get("pipeline")
    if isinstance(ref, dict):
        return dict(ref)
    if isinstance(ref, str):
        from .runtime_factories import _read_json, _rel
        return _read_json(_rel(base, ref))
    return {}


async def settle_terminal(root: Dict[str, Any], base: str, harness: Any,
                          coro: "Awaitable[Any]") -> Any:
    """Await a run/`drive`/`resume` coroutine and, on a terminal `StageFailed`, arm
    the auto-saga (ADR-0009 D6) — then ALWAYS re-raise the ORIGINAL exception.
    The single runtime-boundary seam shared by `run_root`, `resume_gate`, and the
    MCP `run` tool (the eval verified `StageFailed` escapes uncaught to all three).

    Wrapping the WHOLE `drive`/`run`/`resume` call catches a failure from the
    initial run OR from any resume inside a gate loop, and — because `drive`
    itself calls `harness.run`/`resume` RAW (no inner settle) — fires the saga
    exactly ONCE. A non-`StageFailed` exception (transport/store blip, bug)
    propagates untouched: the saga is precisely "unwind an UNGATED terminal
    failure", nothing else."""
    from .harness import StageFailed  # lazy — avoid an import cycle at module load
    try:
        return await coro
    except StageFailed as exc:
        try:
            await run_auto_saga(root, base, harness, exc)
        except Exception as saga_err:  # noqa: BLE001 — the saga must never mask the run
            # The saga machinery ITSELF failed (a broken trace file, a tracer
            # error). Never let that crash the boundary or hide the run's real
            # failure: report loudly and re-raise the ORIGINAL StageFailed.
            # (Deliberately Exception, not BaseException: a CancelledError /
            # KeyboardInterrupt raised DURING the saga must propagate — the same
            # discipline as Harness._settle's BaseException path.)
            print("auto-saga internal error (original failure re-raised): "
                  "{}: {}".format(type(saga_err).__name__, saga_err),
                  file=sys.stderr, flush=True)
        raise  # bare — re-raises the SAME StageFailed object (fidelity, D3)


async def run_auto_saga(root: Dict[str, Any], base: str, harness: Any,
                        exc: Any) -> Optional[Dict[str, Any]]:
    """The saga policy for one terminal `StageFailed` (ADR-0009 D1–D5). Returns the
    five-bucket report actually executed, or None when the saga did NOT unwind
    (graph un-armed, or a corr-guard / `compensation_failed` skip). NEVER re-raises
    — the caller (`settle_terminal`) owns re-raising the original failure.

    Reads and writes ONLY the persisted trace file ADR-0008 already mandates (no
    new store), and reuses `rollback.execute` wholesale with `accept_partial=False`
    (the automatic path is NEVER granted a half-unwind — invariant 4)."""
    pipeline = _resolve_pipeline(root, base)
    on_failure = (pipeline.get("graph") or {}).get("on_failure")
    if not _armed(on_failure):
        return None  # not opted in — the default, fully backward compatible

    # D1 corr guard (design-eval #6): the run corr is StageFailed.output's
    # correlation_id. output is Optional; never guess a corr, never a silent empty
    # unwind (ADR-0008 invariant #2 applies to the auto path too).
    out = getattr(exc, "output", None)
    if out is None:
        _skip("auto-saga SKIPPED: the terminal failure carries no output envelope, "
              "so its correlation id is unknown — cannot safely pick what to unwind. "
              "Run `yaah rollback <root>` to inspect and unwind manually.")
        return None
    corr = out.correlation_id

    # D2 compensation_failed skip: a failed LOCAL compensate leaves the failing
    # stage's committed effects in an UNKNOWN state; unwinding priors around that
    # unknown could make things worse, and an unattended saga must not own that
    # judgment. Under the default on_compensate_fail:"error" the terminal verdict
    # carries the `compensation_failed` code; under "warn" the author's explicit
    # tolerance drops it and the saga runs.
    verdict = getattr(exc, "verdict", None)
    codes = {getattr(f, "code", None) for f in getattr(verdict, "failures", ())}
    if "compensation_failed" in codes:
        report = _empty_report(corr)
        await _emit_saga_record(harness, corr, report, skipped="compensation_failed")
        _skip("auto-saga SKIPPED for run {!r}: the failing stage's local compensate "
              "ITSELF failed (compensation_failed) — its committed effects are in an "
              "unknown state, so the cross-stage unwind is left to a human. Run "
              "`yaah rollback <root> {}` to inspect.".format(corr, corr))
        return None

    # D5: the execution source is the persisted trace FILE (reused via rollback.py).
    # A missing sink / not-yet-written file (a run that failed before any stage
    # completed) is a loud SKIP, not a crash — there is nothing to unwind.
    try:
        records = rollback.read_trace(root, base)
    except rollback.RollbackError as e:
        _skip("auto-saga SKIPPED for run {!r}: {}".format(corr, e))
        return None
    if not any(r.get("corr") == corr for r in records):
        _skip("auto-saga SKIPPED: run {!r} has no records in the trace file — the "
              "failure's correlation id does not appear in the persisted trace, so "
              "there is nothing safe to unwind (a fresh-envelope corr, or a sink "
              "that did not persist this run).".format(corr))
        return None

    nodes, stages = rollback.load_pipeline(root, base)
    exclude = _prior_exclusions(records, corr)  # D4 idempotency ledger
    report = await rollback.execute(
        records, stages, nodes, corr,
        include_costly=_include_costly(on_failure),
        accept_partial=False, exclude=exclude)

    # D4: the durable ledger — a `saga` record for this corr (buckets are
    # identities only, values-free, consistent with the keys-only trace contract).
    await _emit_saga_record(harness, corr, report)
    # D3: the report is printed (the trace carries the durable copy).
    print(rollback.render_report(report))

    if report["failed"]:
        # D3 partial unwind: no park (disproven against the engine, eval #2) — a
        # LOUD stderr block naming the half-unwound state and the finishing verb.
        _partial_pointer(corr, report)
    return report


def _prior_exclusions(records: List[Dict[str, Any]], corr: str) -> Set[Tuple[str, int]]:
    """The `(stage, occurrence)` keys a PRIOR saga already rolled back for this
    corr, read from earlier `saga` records in the trace (D4). Stable across
    re-reads because the trace is append-only: `rollback._resolve` numbers a
    stage's completions 1,2,… in file-append order, so a later saga re-resolving
    the grown file assigns the SAME numbers to the same completions and the next
    number to a new post-resume run. This closes the retried-resume double-undo
    hole. (`rollback._resolve` keys candidates on name=="stage", so a `saga`
    record is never itself mistaken for a candidate.)"""
    excl: Set[Tuple[str, int]] = set()
    for r in records:
        if r.get("corr") == corr and r.get("name") == "saga":
            for e in (r.get("rolled_back") or []):
                stage, occ = e.get("stage"), e.get("occurrence")
                if stage is not None and isinstance(occ, int):
                    excl.add((stage, occ))
    return excl


_BUCKETS = ("rolled_back", "skipped_costly", "impossible", "failed", "not_attempted")


def _empty_report(corr: str) -> Dict[str, Any]:
    report: Dict[str, Any] = {"run": corr}
    for b in _BUCKETS:
        report[b] = []
    return report


async def _emit_saga_record(harness: Any, corr: str, report: Dict[str, Any],
                            *, skipped: Optional[str] = None) -> None:
    """Emit the `saga` trace record through the harness's OWN tracer, so it lands
    in the SAME file the executor and the idempotency ledger read (D4). The record
    NAME is "saga" (not "stage"), so `rollback._resolve` never treats it as a
    candidate. The five bucket attrs + a `skipped` marker are whitelisted by the
    phase contributor, so `project()` copies them onto the persisted record (the
    ledger is EMPTY without that whitelist add — design-eval #1)."""
    tracer = getattr(harness, "_tracer", None)
    if tracer is None:
        return  # no tracer wired (e.g. trace.mode none) — nothing to persist
    clock = getattr(harness, "_clock", None)
    t0 = clock() if callable(clock) else 0.0
    attrs: Dict[str, Any] = {b: report.get(b, []) for b in _BUCKETS}
    if skipped is not None:
        attrs["skipped"] = skipped
    span = Span.timed("saga", corr=corr, t0=t0, t1=t0, status="ok", attrs=attrs)
    await tracer.emit(span)


def _skip(msg: str) -> None:
    """A loud, named stderr notice for a SKIPPED saga (never a silent empty
    unwind). The run still re-raises its original failure at the caller."""
    print("[auto-saga] " + msg, file=sys.stderr, flush=True)


def _partial_pointer(corr: str, report: Dict[str, Any]) -> None:
    """The UNMISSABLE stderr block after a PARTIAL unwind (an undo raised): name
    the half-unwound state and the exact finishing command. The manual verb IS the
    human control surface (ADR-0008 menu-first); a park that can't be resumed to
    closure is not (eval #2)."""
    failed = ", ".join("{}#{}".format(e.get("stage"), e.get("occurrence"))
                       for e in report.get("failed", [])) or "(none)"
    remaining = ", ".join("{}#{}".format(e.get("stage"), e.get("occurrence"))
                          for e in report.get("not_attempted", [])) or "(none)"
    bar = "!" * 72
    print(
        "\n" + bar + "\n"
        "[auto-saga] PARTIAL UNWIND for run {corr} — an undo FAILED and the saga\n"
        "  STOPPED (a half-unwound run is never continued automatically).\n"
        "  failed to undo : {failed}\n"
        "  NOT attempted  : {remaining}\n"
        "  Finish by hand:  yaah rollback <root-config> {corr} --execute "
        "[--accept-partial] [--only <stage>] [--include-costly]\n"
        "  (the menu shows the failed undo and the not-attempted remainder.)\n"
        "{bar}".format(corr=corr, failed=failed, remaining=remaining, bar=bar),
        file=sys.stderr, flush=True)
