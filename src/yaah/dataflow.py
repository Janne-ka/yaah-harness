"""dataflow — the requires↔provides graph analysis behind the broad data-flow lint
(ADR-0005). Author-time, advisory, NEVER raises.

The problem (the silent-dataflow class, `.notes/silent-dataflow-class-2026-06-29.md`):
every node REPLACES the payload, so a key a downstream `render` / `branch` needs can
simply not be there — failing silently or late. This computes, per stage, the set of
payload keys GUARANTEED present when it runs, then flags a consumer that reads a key the
graph doesn't guarantee.

Soundness model — a `(known, complete)` lattice:
  - `known`  : frozenset of keys guaranteed present on EVERY path to this point.
  - `complete`: True iff `known` is the EXACT key set (a key not in `known` is ABSENT).
    Reached only AFTER a node that REPLACES the payload with a known set (an agent, or a
    declared envelope-transform). Before that the payload still carries unknowable INPUT
    keys, so `complete` is False and we cannot prove any key absent.
We WARN on a required key only when `complete` is True and the key is missing — i.e. some
path definitely lacks it. Merge is INTERSECTION (a key counts only if every path provides
it) with `complete = AND` (sound: a key present on one path but provably absent on another
still fails on that other path). UNION would be unsound (false negatives that defeat the
lint). An UNDECLARED envelope-transform spreads unknown keys → its output is incomplete
(it "taints" downstream, which then can't be checked) AND earns its own actionable warning
so the non-coverage is never silent.

Honest framing (the 1a lesson, ADR-0005 §6): `check_schema` doesn't enforce
additionalProperties, so an agent MAY emit keys it didn't declare and a run MAY pass — a
warning flags a CONTRACT gap (an undeclared dependency), it does not predict a certain crash.

Targets Python 3.9+.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Set, Tuple

# Mirror of render_node._PLACEHOLDER (the {{mustache}} the render node fills). Duplicated,
# not imported, to keep this module cheap to import — same convention validate.py uses.
_PLACEHOLDER = re.compile(r"{{\s*(\w+)\s*}}")

# A provides value is either TOP (the fixpoint identity — "not yet constrained") or a
# concrete (known: frozenset, complete: bool).
_TOP = object()
Provides = Any  # _TOP | Tuple[frozenset, bool]

# Node types that REPLACE the payload with keys the lint can't see statically (N6). Like an
# undeclared envelope-transform they're INCOMPLETE unless they declare `provides`.
_OPAQUE_RESET = frozenset({"shell", "worktree", "agent_loop"})


def _meet(a: Provides, b: Provides) -> Provides:
    """Greatest lower bound over predecessor paths: intersect guaranteed keys, AND the
    completeness flags. TOP is the identity (an unprocessed predecessor)."""
    if a is _TOP:
        return b
    if b is _TOP:
        return a
    ak, ac = a
    bk, bc = b
    return (ak & bk, ac and bc)


def _agent_provides_keys(node: Dict[str, Any], sticky: Set[str]) -> Set[str]:
    """Keys a `parse:true` agent's reply DECLARES (mirrors agent.py:388 +
    harness re-applying sticky): output_schema props ∪ required ∪ {raw} ∪ carry ∪
    cwd_from ∪ sticky. NOT the runtime set (check_schema allows undeclared emitted keys);
    counting only declared keys is what makes the lint a contract check (ADR-0005 §6)."""
    schema = node.get("output_schema")
    if not isinstance(schema, dict):     # a malformed (non-dict) output_schema must NOT crash
        schema = {}                       # the lint (never-raises) — treat as no declared shape
    keys = (set((schema.get("properties") or {}).keys())
            | set(schema.get("required") or [])
            | {"raw"} | set(node.get("carry") or []) | set(sticky))
    cwd_from = node.get("cwd_from")
    if isinstance(cwd_from, str) and cwd_from:
        keys.add(cwd_from)
    return keys


def _transfer(node: Optional[Dict[str, Any]], pin: Provides, sticky: Set[str],
              tainted: List[str], stage_name: str) -> Provides:
    """provides_out = how this stage's node rewrites the incoming provides. RESET nodes
    (agent, declared envelope-transform) yield a COMPLETE known set regardless of `pin`;
    PRESERVE nodes add their keys to `pin` and keep its completeness; an UNDECLARED
    envelope-transform resets to INCOMPLETE (unknown keys) and records a taint."""
    if pin is _TOP:
        pin = (frozenset(), False)   # unreachable-safe; reachable stages get a concrete pin
    known, complete = pin
    explicit = node.get("provides") if isinstance(node, dict) else None
    explicit_set = set(explicit) if isinstance(explicit, list) else set()

    if node is None:
        # a pure routing stage (fork/fanin with no node) passes the payload through
        return (known | explicit_set | sticky, complete)

    ntype = node.get("type")
    if ntype == "agent":
        carry = set(node.get("carry") or [])
        # `carry_cwd` forwards the cwd_from key onto EVERY agent reply (agent.py:342,
        # before the parse branch), so it's provided regardless of parse / output_schema.
        cwd_from = node.get("cwd_from")
        cwd = {cwd_from} if isinstance(cwd_from, str) and cwd_from else set()
        if node.get("parse", True) is False:
            # parse:false → exactly {raw} (+carry +cwd_from); the full set is KNOWN.
            return (frozenset({"raw"} | carry | cwd | explicit_set | sticky), True)
        if isinstance(node.get("output_schema"), dict) or explicit_set:
            # a declared contract: treat its keys as the complete provides (an
            # undeclared-but-emitted key is the gap the lint flags; ADR-0005 §6).
            # `_agent_provides_keys` already folds in cwd_from.
            return (frozenset(_agent_provides_keys(node, sticky) | explicit_set), True)
        # parse:true with NO contract: the parsed keys are unknown → INCOMPLETE, so
        # we can't prove any consumer's key absent (the weak-output-schema lint nudges
        # declaring output_schema first). Skip, never false-warn.
        return (frozenset({"raw"} | carry | cwd | sticky), False)
    if ntype == "transform" and node.get("call") == "envelope":
        if explicit_set:
            return (frozenset(explicit_set | sticky), True)      # declared RESET → complete
        tainted.append(stage_name)                                # undeclared → taint (companion warn)
        return (frozenset(sticky), False)                         # only sticky survives, INCOMPLETE
    if ntype in _OPAQUE_RESET:
        # shell/worktree/agent_loop REPLACE the payload with keys the lint can't see (N6).
        # A declared `provides` makes them precise; undeclared they're INCOMPLETE so
        # downstream consumers are conservatively skipped (a known false-NEGATIVE — never a
        # false warning). No companion warning here (unlike envelope-transform, whose whole
        # job is to emit payload keys): a shell is often a side-effect with no key output, so
        # warning on every one would be noise. Declare `provides` to re-enable downstream
        # checks. (Refining this — warn only when a consumer is actually skipped — is a
        # review item.)
        if explicit_set:
            return (frozenset(explicit_set | sticky), True)
        return (frozenset(sticky), False)
    # PRESERVE nodes: add the keys this node type puts on the payload, keep completeness.
    added: Set[str] = set(explicit_set)
    if ntype == "transform":                 # call:"args" → result nested under `into`
        added.add(node.get("into", "result"))
    elif ntype in ("get", "post"):
        added.add(node.get("into", "result"))
    elif ntype == "render":
        added |= {"output", "path"}
    elif ntype == "human_gate":
        added.add("decision")
    # validators (json_object/json_schema/expect_field) and anything else: pass-through.
    return (known | added | sticky, complete)


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
                     start: Optional[str], tainted: List[str]) -> Dict[str, Provides]:
    """Forward dataflow to a least fixpoint: provides_in(stage) = meet over predecessors
    of provides_out(pred). Monotone (sets only shrink from TOP), so it converges; loops
    (retry/`then` cycles) are handled by the fixpoint, not a special case. Unreached
    stages stay TOP. Returns {stage: provides_in}."""
    pin: Dict[str, Provides] = {s: _TOP for s in stages}
    if start in stages:
        pin[start] = (frozenset(), False)   # the entry payload is the unknowable INPUT
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
            incoming = [_transfer(nodes.get(stages[p].get("node")), pin[p], sticky,
                                  tainted, p) for p in preds[s] if pin[p] is not _TOP]
            if not incoming:
                continue  # no reachable predecessors: keep the seed (start) or TOP (unreached)
            merged = incoming[0]
            for nxt in incoming[1:]:
                merged = _meet(merged, nxt)
            if start in stages and s == start:
                merged = _meet(merged, pin[start])  # entry payload always joins the start
            if merged != pin[s]:
                pin[s] = merged
                changed = True
        if not changed:
            break
    # `tainted` accumulates duplicates across passes; de-dup preserving order.
    seen: Set[str] = set()
    tainted[:] = [t for t in tainted if not (t in seen or seen.add(t))]
    return pin


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


def lint_dataflow(nodes: Dict[str, Any], stages: Dict[str, Any], sticky_list: Any,
                  start: Optional[str], base_path: Optional[str],
                  warnings: List[str]) -> None:
    """The broad requires↔provides lint (ADR-0005 slice B). Absorbs the 1a single-hop
    render/branch checks (a 1-length path) and extends them across transform chains. Emits
    ACTIONABLE warnings only; never raises."""
    sticky = set(k for k in sticky_list if isinstance(k, str)) if isinstance(sticky_list, list) else set()
    tainted: List[str] = []
    pin = compute_provides(nodes, stages, sticky, start, tainted)
    tainted_set = set(tainted)
    preds = _edges(stages)

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

    # Collect consumers we CAN'T check because an undeclared envelope-transform upstream
    # made their provides unknown — then emit ONE consolidated nudge (a per-transform or
    # per-consumer warning floods a real pipeline; a parse transform with no downstream
    # consumer stays silent because nothing is actually lost).
    blocked_consumers: List[str] = []
    blocking_transforms: Set[str] = set()

    def note_blocked(consumer: str) -> None:
        culprits = tainted_ancestors(consumer)
        if culprits:
            blocked_consumers.append(consumer)
            blocking_transforms.update(culprits)

    for s_name, s in stages.items():
        node = nodes.get(s.get("node")) or {}
        pin_here = pin.get(s_name, _TOP)
        if pin_here is _TOP:
            continue  # unreachable stage — never runs, nothing to check
        # branch.on reads the payload AFTER this stage's node runs (the node's OUTPUT).
        on = (s.get("branch") or {}).get("on")
        if isinstance(on, str) and on:
            known, complete = _transfer(node, pin_here, sticky, [], s_name)
            if complete:
                if on not in known:
                    warnings.append(
                        "stage {!r}: branches on {!r}, but nothing on the path to it provides "
                        "that key (provides {}). The branch then depends on UNDECLARED output "
                        "— it falls through to branch.default on any run where {!r} is absent. "
                        "Declare {!r} (in the producing agent's output_schema, a transform's "
                        "`provides`, or graph `sticky`). [lint: branch-key-unprovided]".format(
                            s_name, on, sorted(known - {"raw"}), on, on))
            else:
                note_blocked(s_name)
        # a render reads the payload that flows INTO it (provides_in).
        if node.get("type") == "render" and not node.get("allow_unfilled"):
            text = _render_template_text(node, base_path)
            needs = sorted(set(_PLACEHOLDER.findall(text))) if text is not None else []
            if not needs:
                continue  # nothing read (no template / no placeholders) — nothing to check
            known, complete = pin_here
            if complete:
                missing = [ph for ph in needs if ph not in known]
                if missing:
                    warnings.append(
                        "stage {!r}: render template needs {} which nothing on the path to it "
                        "provides (provides {}). The render then depends on undeclared output — "
                        "it FAILS with render_unfilled_placeholders on any run where they're "
                        "absent. Declare them (in the producing agent's output_schema, a "
                        "transform's `provides`, or graph `sticky`), or set allow_unfilled:true "
                        "if intentionally literal. [lint: render-key-unprovided]".format(
                            s_name, missing, sorted(known - {"raw"})))
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
