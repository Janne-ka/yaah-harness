"""dataflow — the requires↔provides graph analysis behind the data-flow lint (ADR-0005 +
ADR-0006). Author-time; NEVER raises.

The problem (the silent-dataflow class, `.notes/silent-dataflow-class-2026-06-29.md`):
every node REPLACES the payload, so a key a downstream `render` / `branch` needs can
simply not be there — failing silently or late. This computes, per stage, the set of
payload keys GUARANTEED present when it runs, then flags a consumer that reads a key the
graph doesn't guarantee.

This module holds ONLY the graph math. What each node PROVIDES comes from its contract
(`node_contract.resolve_contract`) — there is no per-node key table here (that hand-mirror
was ADR-0006's biggest slop). The lattice value on an edge is a `Flow(known, complete, closed)`:
  - `known`   : keys guaranteed present on EVERY path to this point.
  - `complete`: `known` is the DECLARED-exact set (a contract, e.g. an agent's output_schema).
  - `closed`  : `known` is the RUNTIME-exact set — provably (a parse=false agent, a worktree,
                carried through built-in preserve nodes). closed ⟹ complete.
Merge over predecessors is INTERSECTION of `known` with `complete`/`closed` AND-ed (a key on
one path but provably absent on another still fails there; UNION would be unsound).

Two severities (ADR-0006 §D5), both from `analyze_dataflow`:
  - a consumer reads a key missing where the set is `closed` → ERROR (fail-loud;
    `validate_pipeline` rejects at load — the run WOULD fail/misroute).
  - missing where only `complete` → WARNING (a contract gap; `lint_pipeline`). Honest framing
    (the 1a lesson): `check_schema` ignores additionalProperties and an agent merges its whole
    reply, so a declared set is `complete` not `closed` — a warning, not a certain crash.
An UNDECLARED envelope-transform is `opaque` (unknown output) → downstream can't be checked,
and it earns one consolidated warning so the non-coverage is never silent.

Targets Python 3.9+.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Set, Tuple

from .node_contract import Flow, apply, may_suspend, meet, resolve_contract
from .templating import PLACEHOLDER as _PLACEHOLDER   # the {{mustache}} a render fills — one copy

# A provides value on an edge is None (the fixpoint identity — "not yet reached") or a
# concrete Flow (known keys + how exact the set is). This module holds NO per-node key
# knowledge: each node's contract comes from `node_contract.resolve_contract`, and the
# lattice just applies it (ADR-0006).
Provides = Optional[Flow]


def _undeclared_envelope_transform(node: Dict[str, Any]) -> bool:
    """An envelope-transform with no `provides` — the one opaque node worth NAGGING about
    (the author can fix it by declaring `provides`), as opposed to an unknown custom type
    (opaque and silently skipped). This is lint POLICY, not node key knowledge."""
    return (node.get("type") == "transform" and node.get("call") == "envelope"
            and not isinstance(node.get("provides"), list))


# The ENGINE-set keys of a fanout stage's merged payload (harness._produce_fanout):
# the merge is `dict(input.payload)` updated with exactly `results=`, `roles=`,
# `failed_roles=` — inbound keys survive, these three are always added, and role
# outputs stay NESTED under `results` (never top-level). A k-of-n pass
# (`min_success`) hands forward the same shape.
_FANOUT_MERGE_KEYS = frozenset({"results", "roles", "failed_roles"})


def _fanout_role_may_suspend(role: Any, nodes: Dict[str, Any]) -> bool:
    """Whether a fanout ROLE could reply AWAIT and park its stage. If one does, the
    harness parks with NO artifact (`_Suspend(last_output=None)`), so resume's
    `_merge_decision` hands the human's response ALONE to the next stage — the merged
    payload never happens on that lane. A role that isn't a declared node (or isn't
    a string) widens to True; never raises."""
    cfg = nodes.get(role) if isinstance(role, str) else None
    return may_suspend(cfg.get("type") if isinstance(cfg, dict) else None)


def _transfer(stage: Any, node: Optional[Dict[str, Any]], pin: Provides, sticky: Set[str],
              tainted: List[str], stage_name: str,
              nodes: Optional[Dict[str, Any]] = None) -> Flow:
    """How this stage rewrites the incoming flow. A routing stage (no node) passes the
    payload through; every real node's effect comes from its resolved contract — the module
    has no per-type key table. An undeclared envelope-transform resolves to `opaque` (nothing
    checkable downstream) AND records a taint so its consumers get a companion nudge.

    A PARALLEL-SHAPE stage (`fork` / `fanout` / `fanin`) is modeled by what the ENGINE
    hands forward, NOT its `node` (the runtime never runs a fanout/fork stage's node —
    harness._run_stage / _drive; `nodes` is only read here, to resolve fanout roles).
    Validate rejects only fanout+fork; a `fanin` COMBINED with either still loads, and
    the two runtime walkers disagree on such a stage (`_drive` runs `fork` first, the
    branch walker `_walk` runs `fanin` first) — so the order below IS load-bearing:
    the JOIN shapes come first because their transfer is the widest (no flag
    survives), which stays sound whichever walker hits the stage.
      - fork / fanin: what flows onward is a fan-in REDUCE output (a fork's rejoin
        `then` carries the clear; a fanin's continuation carries the reduce directly)
        — or, for a fork, an exact input copy on the branch-head lane / a wait
        degrade. The default reduce UNIONS the arrivals (a superset of the lattice's
        intersection-`known`, so keeping `known` is sound) and an app `reduce` target
        is arbitrary, so NEITHER `complete` NOR `closed` survives: downstream of a
        join is unchecked rather than wrongly flagged (keeping `complete` here
        manufactured `render-key-unprovided` warnings — `--strict` failures — on keys
        the reduce provably delivers; runtime-probed 2026-07).
      - fanout: the merged payload is inbound + exactly `results`/`roles`/`failed_roles`
        (role outputs stay nested under `results`), so `known` grows by the engine keys
        and `complete` survives. `closed` survives ONLY if no role can reply AWAIT
        (node_contract.may_suspend): a suspended fanout resumes with the human's
        response ALONE (the merge lane never ran), so the set isn't runtime-exact.

    One STAGE-level widening on top of the node's contract: `escalate: "human"` resumes
    through the same open merge as a human_gate (harness._merge_decision folds the human's
    whole reply onto the failed artifact — arbitrary keys), so `closed` cannot survive the
    stage: on the escalation lane a key only the human supplies is NOT provably absent.
    `known`/`complete` are kept from the node's own contract — OPTIMISTIC on the
    escalation lane (the merge base is the FAILED artifact, e.g. an error payload, which
    may lack happy-path keys), an accepted blind spot: it can only mute warnings, never
    manufacture a hard error (errors need `closed`, which is dropped here). The fanout
    suspend lane and the join lanes keep `known` under the same accepted mute-only
    optimism.

    STILL NOT MODELED (each can only mute findings, never manufacture a hard error):
    a fanned-out gate role's own `provides` (human-supplied keys there warn until the
    author declares them somewhere on the path); which keys a specific fan-in reduce
    yields; and the feedback-retry keys (`feedback`/`priorAttempt`) the harness folds
    onto a retried stage's INPUT (a lattice-wide pre-existing blind spot, not fanout's).

    The lattice applies `sticky` on EVERY transfer (`sticky_fs` below), which the
    harness now honours everywhere it walks: `_drive` folds sticky after each linear
    stage + each fork join, and — since the 2026-07 fix, runtime-probed — so does
    ForkCoordinator._walk BETWEEN branch stages (it calls the same _fold_sticky helper).
    Before that fix the harness skipped the in-branch fold, so this sticky-everywhere
    assumption over-claimed on in-branch closed lanes; it is now sound there too."""
    if pin is None:
        pin = Flow()   # unreachable-safe; reachable stages get a concrete pin
    sticky_fs = frozenset(sticky)
    stage_d = stage if isinstance(stage, dict) else {}
    fanout = stage_d.get("fanout")
    fanin = stage_d.get("fanin")
    if stage_d.get("fork") or (isinstance(fanin, dict) and fanin):
        # a JOIN shape (fork rejoin / fan-in continuation): reduce output flows on —
        # checked FIRST so an accepted fanin+fanout combo widens instead of keeping
        # the fanout arm's flags on a stage the branch walker treats as a fanin
        flow = Flow(pin.known | sticky_fs, False, False)
    elif fanout:
        roles = fanout if isinstance(fanout, list) else []   # malformed → no proof
        role_suspends = (not roles or any(
            _fanout_role_may_suspend(r, nodes or {}) for r in roles))
        flow = Flow(pin.known | _FANOUT_MERGE_KEYS | sticky_fs, pin.complete,
                    pin.closed and not role_suspends)
    elif node is None:
        # a pure routing stage (no node) passes the payload through
        flow = Flow(pin.known | sticky_fs, pin.complete, pin.closed)
    else:
        contract = resolve_contract(node.get("type"), node)
        if contract.mode == "opaque" and _undeclared_envelope_transform(node):
            tainted.append(stage_name)
        flow = apply(contract, pin, sticky_fs)
    if stage_d.get("escalate") == "human":
        flow = flow._replace(closed=False)
    return flow


def _edges(stages: Dict[str, Any]) -> Dict[str, List[str]]:
    """stage -> predecessor stages, from every routing key build_graph reads (then,
    branch routes + default, fork targets) PLUS fanin.expect (the fan-in's inputs)."""
    preds: Dict[str, List[str]] = {s: [] for s in stages}

    def add(src: str, dst: str) -> None:
        if dst in stages and src in stages:
            preds[dst].append(src)

    for name, s in stages.items():
        if s.get("then"):
            add(name, s["then"])
        b = s.get("branch") or {}
        for dst in (b.get("routes") or {}).values():
            if isinstance(dst, str):
                add(name, dst)
        if isinstance(b.get("default"), str):
            add(name, b["default"])
        for dst in (s.get("fork") or []):
            if isinstance(dst, str):
                add(name, dst)
        fi = s.get("fanin") or {}
        for src in (fi.get("expect") if isinstance(fi.get("expect"), list) else []):
            add(src, name)
    return preds


def compute_provides(nodes: Dict[str, Any], stages: Dict[str, Any], sticky: Set[str],
                     start: Optional[str], tainted: List[str],
                     entry: Optional[Flow] = None) -> "Tuple[Dict[str, Provides], Dict[str, List[str]]]":
    """Forward dataflow to a least fixpoint: provides_in(stage) = meet over predecessors
    of provides_out(pred). Monotone (sets only shrink from TOP), so it converges; loops
    (retry/`then` cycles) are handled by the fixpoint, not a special case. Unreached
    stages stay TOP. Returns ({stage: provides_in}, predecessor-map) — the caller reuses
    the predecessor map rather than recomputing it.

    `entry` seeds the start stage with what the ENTRY PAYLOAD is known to provide.
    Default None = Flow() — the unknowable input of the load-time lint. A caller that
    KNOWS the entry keys (e.g. the `yaah ab` pre-flight, which holds the experiment's
    concrete inputs) passes a closed Flow to make entry-key mismatches provable."""
    pin: Dict[str, Provides] = {s: None for s in stages}
    if start in stages:
        pin[start] = entry if entry is not None else Flow()
    preds = _edges(stages)
    # Iterate to convergence. The meet is monotone (preserve-transfers only grow `known`
    # as TOP collapses; reset-transfers are constant), so it descends from TOP to a least
    # fixpoint; |stages|+1 passes suffice (fuzz-verified over random loop/diamond graphs).
    # `break`-on-stable means a hypothetical non-convergence would emit the last (non-fixpoint)
    # estimate rather than loop forever — a safe degradation for an advisory, never-raises lint.
    for _ in range(len(stages) + 1):
        changed = False
        for s in stages:
            # Only REACHABLE predecessors contribute — one still at TOP hasn't been reached
            # from `start`, so it never runs and must not taint the merge (a real path that
            # provides the key would otherwise be intersected away → false warning).
            incoming = [_transfer(stages[p], nodes.get(stages[p].get("node")), pin[p],
                                  sticky, tainted, p, nodes)
                        for p in preds[s] if pin[p] is not None]
            if not incoming:
                continue  # no reachable predecessors: keep the seed (start) or TOP (unreached)
            merged: Flow = incoming[0]
            for nxt in incoming[1:]:
                merged = meet(merged, nxt)          # all incoming are concrete Flows
            if s == start:
                at_start = pin.get(s)               # == pin[start], which DESCENDS from the
                if at_start is not None:            # `entry` seed (don't shadow the param);
                    merged = meet(merged, at_start)  # the entry payload always joins the start
            if merged != pin[s]:
                pin[s] = merged
                changed = True
        if not changed:
            break
    # `tainted` accumulates duplicates across passes; de-dup preserving order.
    seen: Set[str] = set()
    tainted[:] = [t for t in tainted if not (t in seen or seen.add(t))]
    return pin, preds


def stage_outflows(nodes: Dict[str, Any], stages: Dict[str, Any], sticky_list: Any,
                   start: Optional[str], *,
                   entry: Optional[Flow] = None) -> "Dict[str, Provides]":
    """Provides-OUT per stage: the Flow LEAVING each reachable stage (None = unreachable).
    The read surface for callers reasoning about what a stage HANDS FORWARD — e.g. the
    `yaah ab` pre-flight checking terminal payloads against declared metric paths — so
    they consume the same lattice `analyze_dataflow` walks instead of re-deriving it."""
    sticky = set(k for k in sticky_list if isinstance(k, str)) if isinstance(sticky_list, list) else set()
    pin, _ = compute_provides(nodes, stages, sticky, start, [], entry=entry)
    out: Dict[str, Provides] = {}
    for s_name, s in stages.items():
        p = pin.get(s_name)
        out[s_name] = None if p is None else _transfer(
            s, nodes.get(s.get("node")), p, sticky, [], s_name, nodes)
    return out


def terminal_stages(stages: Dict[str, Any]) -> List[str]:
    """Stages where a run can END — the harness walk stops when no next stage resolves
    (mirror of harness._next_stage returning None): no branch and no `then`; or a branch
    whose effective default (its `default`, else the stage's `then`) is null, or any
    explicit null route. Graph math only, exposed so callers reasoning about the FINAL
    payload (e.g. metric plausibility in `yaah ab`) don't re-derive routing."""
    out: List[str] = []
    for name, s in stages.items():
        if not isinstance(s, dict):
            continue
        b = s.get("branch")
        if not b:
            if not s.get("then"):
                out.append(name)
        elif (b.get("default", s.get("then")) is None
              or any(v is None for v in (b.get("routes") or {}).values())):
            out.append(name)
    return out


def _render_template_text(rnode: Dict[str, Any], base_path: Optional[str]) -> Optional[str]:
    """The render's template source, or None when it can't be read statically (skip — not
    the linter's job to report a missing file). Inline `template_text` is always available;
    a `template_file` is read relative to `base_path` (the root config's dir, matching
    `_build_render`) when known, else by absolute path."""
    inline = rnode.get("template_text")
    if isinstance(inline, str):
        return inline
    tfile = rnode.get("template_file")
    if not isinstance(tfile, str) or not tfile:
        return None
    path = tfile if os.path.isabs(tfile) else (
        os.path.join(base_path, tfile) if base_path else None)
    if path is None:
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def analyze_dataflow(nodes: Dict[str, Any], stages: Dict[str, Any], sticky_list: Any,
                     start: Optional[str], base_path: Optional[str], *,
                     entry: Optional[Flow] = None) -> "Tuple[List[str], List[str]]":
    """The requires↔provides graph analysis (ADR-0005 slice B + ADR-0006 §D5). ONE pass, two
    severities, split by how exact the provided set is where a consumer reads it:

      - a key read but provably ABSENT on a `closed` path (a parse=false agent, a worktree —
        the runtime set is exactly known) → an ERROR: the run WILL fail/misroute, so
        `validate_pipeline` fails loud at load.
      - a key read but missing where the set is `complete` (declared-exact, e.g. an agent's
        output_schema — the model MAY still emit it) → a WARNING: a contract gap.
      - where the set is incomplete because an undeclared envelope-transform upstream hid its
        keys → one consolidated WARNING naming the transform(s) to fix.

    Returns (errors, warnings). Never raises. `validate_pipeline` consumes the errors,
    `lint_pipeline` the warnings (it runs on an already-valid config, so it sees only
    warnings). `entry` (see compute_provides) lets a caller that KNOWS the entry payload's
    keys seed the start stage — entry-key mismatches then surface as errors/warnings under
    the same two-severity rules."""
    errors: List[str] = []
    warnings: List[str] = []
    sticky = set(k for k in sticky_list if isinstance(k, str)) if isinstance(sticky_list, list) else set()
    tainted: List[str] = []
    pin, preds = compute_provides(nodes, stages, sticky, start, tainted, entry=entry)
    tainted_set = set(tainted)

    def tainted_ancestors(stage: str) -> List[str]:
        """Undeclared envelope-transforms reverse-reachable from `stage` — the culprits
        whose missing `provides` left this consumer un-checkable."""
        seen: Set[str] = set()
        todo = list(preds.get(stage, []))
        while todo:
            p = todo.pop()
            if p not in seen:
                seen.add(p)
                todo.extend(preds.get(p, []))
        return sorted(seen & tainted_set)

    # Consumers we CAN'T check because an undeclared envelope-transform upstream made their
    # provides unknown → ONE consolidated nudge (a per-transform/consumer warning floods a
    # real pipeline; a parse transform with no downstream consumer stays silent — nothing lost).
    blocked_consumers: List[str] = []
    blocking_transforms: Set[str] = set()

    def note_blocked(consumer: str) -> None:
        culprits = tainted_ancestors(consumer)
        if culprits:
            blocked_consumers.append(consumer)
            blocking_transforms.update(culprits)

    def branch_msg(s_name: str, on: str, known: "frozenset", hard: bool) -> str:
        if hard:
            return (
                "stage {!r}: branches on {!r}, which is provably ABSENT here — the payload is a "
                "fixed set providing {}. `branch.on` reads as missing, so the run takes "
                "`branch.default` EVERY time. Provide {!r} before it (a transform with "
                "`call: \"envelope\"` that parses + merges, an agent output_schema, or graph "
                "`sticky`). [dataflow: branch-key-absent]".format(
                    s_name, on, sorted(known - {"raw"}), on))
        return (
            "stage {!r}: branches on {!r}, but nothing on the path to it provides that key "
            "(provides {}). The branch then depends on UNDECLARED output — it falls through to "
            "branch.default on any run where {!r} is absent. Declare {!r} (in the producing "
            "agent's output_schema, a transform's `provides`, or graph `sticky`). "
            "[lint: branch-key-unprovided]".format(s_name, on, sorted(known - {"raw"}), on, on))

    def render_msg(s_name: str, missing: List[str], known: "frozenset", hard: bool) -> str:
        if hard:
            return (
                "stage {!r}: render template needs {} which is provably ABSENT here — the payload "
                "is a fixed set providing {}. This render FAILS with render_unfilled_placeholders "
                "EVERY run. Provide them (a transform with `call: \"envelope\"` that parses + "
                "merges, an agent output_schema, or graph `sticky`), or set allow_unfilled:true if "
                "the literal is intentional. [dataflow: render-key-absent]".format(
                    s_name, missing, sorted(known - {"raw"})))
        return (
            "stage {!r}: render template needs {} which nothing on the path to it provides "
            "(provides {}). The render then depends on undeclared output — it FAILS with "
            "render_unfilled_placeholders on any run where they're absent. Declare them (in the "
            "producing agent's output_schema, a transform's `provides`, or graph `sticky`), or set "
            "allow_unfilled:true if intentionally literal. [lint: render-key-unprovided]".format(
                s_name, missing, sorted(known - {"raw"})))

    for s_name, s in stages.items():
        node = nodes.get(s.get("node")) or {}
        pin_here = pin.get(s_name)
        if pin_here is None:
            continue  # unreachable stage — never runs, nothing to check
        # branch.on reads the payload AFTER this stage's node runs (the node's OUTPUT).
        on = (s.get("branch") or {}).get("on")
        if isinstance(on, str) and on:
            flow = _transfer(s, node, pin_here, sticky, [], s_name, nodes)
            if flow.closed:
                if on not in flow.known:
                    errors.append(branch_msg(s_name, on, flow.known, hard=True))
            elif flow.complete:
                if on not in flow.known:
                    warnings.append(branch_msg(s_name, on, flow.known, hard=False))
            else:
                note_blocked(s_name)
        # a render reads the payload that flows INTO it (provides_in).
        if node.get("type") == "render" and not node.get("allow_unfilled"):
            text = _render_template_text(node, base_path)
            needs = sorted(set(_PLACEHOLDER.findall(text))) if text is not None else []
            if not needs:
                continue  # nothing read (no template / no placeholders) — nothing to check
            if pin_here.closed:
                missing = [ph for ph in needs if ph not in pin_here.known]
                if missing:
                    errors.append(render_msg(s_name, missing, pin_here.known, hard=True))
            elif pin_here.complete:
                missing = [ph for ph in needs if ph not in pin_here.known]
                if missing:
                    warnings.append(render_msg(s_name, missing, pin_here.known, hard=False))
            else:
                note_blocked(s_name)

    if blocked_consumers:
        warnings.append(
            "envelope-transform(s) {} don't declare `provides`, so the lint can't see the "
            "keys they put on the payload and SKIPS requires-checks on their downstream "
            "consumer(s) {}. Add `provides: [\"key\", ...]` to each transform to enable those "
            "checks. [lint: transform-provides-undeclared]".format(
                ", ".join(repr(t) for t in sorted(blocking_transforms)),
                ", ".join(repr(c) for c in sorted(set(blocked_consumers)))))
    return errors, warnings
