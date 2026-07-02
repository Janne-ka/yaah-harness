"""node_contract — a node's output contract, as data (ADR-0006).

The data-flow lint needs one fact per node: which payload keys does it guarantee on the way
out, and is that set exact? A `Contract` holds exactly that, computed from the node's config
by a pure function. The lattice in `dataflow.py` then just APPLIES contracts — it holds no
per-node knowledge of its own. One source for "what a node provides", read by the lint.

Example — what two nodes provide:

    agent "review" (parse=false)  ->  Contract(reset, {"raw"}, closed=True)
    transform "parse"             ->  Contract(preserve, {"verdict","summary"})

    so  render "{{verdict}}"  is provided after "parse", but not right after "review".

Three modes:
  - preserve : keep the inbound keys, ADD `provides`.  (render, gate, get, post, agent_loop, …)
  - reset    : REPLACE the payload with `provides`.     (agent, shell, worktree)
  - opaque   : unknown output — nothing downstream of it can be checked (a sound skip).

Two flags on a reset (see the ‡ note in ADR-0006):
  - complete : `provides` is the DECLARED-exact set — a contract the author stated. A
               downstream read of a missing key is a WARNING (a contract gap).
  - closed   : `provides` is the RUNTIME-exact set — provable, no conditional keys. A
               downstream read of a missing key is an ERROR (fail-loud). closed => complete.

An agent is NEVER closed: it copies its whole parsed reply onto the payload and the schema
check doesn't reject extra keys, so we can't prove its output is exactly its declared keys
(ADR-0006 ‡). It can be `complete` (a stated contract → warn), never `closed` (→ hard error).

This module is domain-free (ADR-0001): it names node TYPES and the ENGINE keys those built-in
types emit (`raw`, `decision`, `exit_code`, …) — never an app concept like "verdict".

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, NamedTuple, Optional


def _as_key_set(val: Any) -> frozenset:
    """A config field meant to be a list of key NAMES → the set of its string entries; any
    other shape contributes nothing. Keeps contract functions never-raising on unvalidated
    input (e.g. `carry: true`, `required: 5`)."""
    if not isinstance(val, list):
        return frozenset()
    return frozenset(v for v in val if isinstance(v, str))


def _into(cfg: Dict[str, Any], default: str) -> str:
    """The `into` key a node nests its result under, guarding a non-string config value."""
    v = cfg.get("into", default)
    return v if isinstance(v, str) and v else default


def _cwd(cfg: Dict[str, Any]) -> frozenset:
    """`carry_cwd` forwards the workdir under `cwd_from` onto the reply, when bound."""
    c = cfg.get("cwd_from")
    return frozenset({c}) if isinstance(c, str) and c else frozenset()


def _carry(cfg: Dict[str, Any]) -> frozenset:
    return _as_key_set(cfg.get("carry"))


# --- the Contract, and how it transforms the flow ------------------------------------------

class Contract(NamedTuple):
    mode: str = "opaque"          # "preserve" | "reset" | "opaque"
    provides: frozenset = frozenset()  # preserve: keys ADDED; reset: the FULL set; opaque: ignored
    complete: bool = False        # reset: declared-exact? (a miss → WARNING)
    # `closed` means "THIS node's key contribution is RUNTIME-exact (provable), not merely
    # declared." For a reset, the whole set is provable. For a preserve, the ADDED keys are
    # engine-defined and always present. A DECLARED envelope-transform (or a custom node's
    # inline `provides`) adds AUTHOR-declared keys — a contract, not a proof — so it is NOT
    # closed: `apply` then keeps `complete` (→ WARNING) but drops `closed` (→ no hard ERROR),
    # which is what stops an under-declaration becoming a false-positive fail-loud. closed => complete.
    closed: bool = False


def preserve(*keys: str) -> Contract:
    """Keep the inbound payload, add these ENGINE-defined keys (always present → runtime-exact,
    so inbound `closed` survives)."""
    return Contract("preserve", frozenset(keys), complete=False, closed=True)


def preserve_declared(keys) -> Contract:
    """Keep the inbound payload, add these AUTHOR-declared keys (a declared envelope-transform
    or custom node). Inbound `complete` survives, but `closed` does NOT — we can't prove the
    fn's output is exactly these keys, so a downstream miss is a WARNING, never a hard ERROR."""
    return Contract("preserve", frozenset(keys), complete=False, closed=False)


def reset(keys, *, complete: bool = True, closed: bool = False) -> Contract:
    """Replace the payload with `keys`. `complete`/`closed` say how exact the set is;
    `closed` implies `complete` (a provable set is also a declared one)."""
    if closed:
        complete = True
    return Contract("reset", frozenset(keys), complete, closed)


def opaque() -> Contract:
    """Unknown output — downstream can't be checked."""
    return Contract("opaque", frozenset(), False, False)


class Flow(NamedTuple):
    """The lattice value on an edge: keys guaranteed present on EVERY path here, plus how
    exact that set is (see Contract.complete/closed)."""
    known: frozenset = frozenset()
    complete: bool = False
    closed: bool = False


def apply(c: Contract, pin: Flow, sticky: frozenset) -> Flow:
    """How a node's contract rewrites the flow coming into it. `sticky` keys are re-applied
    by the harness after every stage, so they survive a reset."""
    if c.mode == "preserve":
        # closed survives only if inbound was closed AND this node's additions are runtime-exact
        # (c.closed) — a declared envelope-transform (c.closed False) keeps complete, drops closed.
        return Flow(pin.known | c.provides | sticky, pin.complete, pin.closed and c.closed)
    if c.mode == "reset":
        return Flow(c.provides | sticky, c.complete, c.closed)
    return Flow(frozenset(sticky), False, False)   # opaque


def meet(a: Flow, b: Flow) -> Flow:
    """Greatest lower bound over predecessor paths: intersect guaranteed keys, AND the flags
    (a key present on one path but provably absent on another still fails on that path)."""
    return Flow(a.known & b.known, a.complete and b.complete, a.closed and b.closed)


# --- the built-in contracts (one row per D2 table in ADR-0006) -----------------------------

def agent_contract(cfg: Dict[str, Any]) -> Contract:
    carry, cwd = _carry(cfg), _cwd(cfg)
    if cfg.get("parse", True) is False:
        # parse:false → exactly {raw} (+carry +cwd). Provable → closed.
        return reset({"raw"} | carry | cwd, closed=True)
    schema = cfg.get("output_schema")
    declared = _as_key_set(cfg.get("provides"))
    if isinstance(schema, dict) or declared:
        props = schema.get("properties") if isinstance(schema, dict) else None
        prop_keys = frozenset(props.keys()) if isinstance(props, dict) else frozenset()
        req = _as_key_set(schema.get("required")) if isinstance(schema, dict) else frozenset()
        # A declared contract: complete (the author stated it) but NEVER closed — the model
        # can emit keys the schema never listed and the check lets them through (ADR-0006 ‡).
        return reset(prop_keys | req | {"raw"} | carry | cwd | declared, complete=True, closed=False)
    # parse:true with no declared shape: parsed keys unknown → incomplete, can't check.
    return reset({"raw"} | carry | cwd, complete=False, closed=False)


def transform_contract(cfg: Dict[str, Any]) -> Contract:
    if cfg.get("call") == "envelope":
        p = cfg.get("provides")
        if isinstance(p, list):                       # declared envelope-transform
            return preserve_declared(_as_key_set(p))  # merges inbound + adds AUTHOR-declared keys
        return opaque()                                # undeclared → unknown output
    return preserve(_into(cfg, "result"))              # call:"args" → nests under `into`


def render_contract(cfg: Dict[str, Any]) -> Contract:
    return preserve("output", "path")


def human_gate_contract(cfg: Dict[str, Any]) -> Contract:
    return preserve("decision")


def get_contract(cfg: Dict[str, Any]) -> Contract:
    return preserve(_into(cfg, "data"))                # builder default is "data"


def post_contract(cfg: Dict[str, Any]) -> Contract:
    return preserve(_into(cfg, "stored"))              # builder default is "stored"


def shell_contract(cfg: Dict[str, Any]) -> Contract:
    # complete, NOT closed: `stdout`/`timed_out` are emitted only on some run paths, so the
    # guaranteed set is {exit_code, ok, stdout_tail} but the runtime set varies.
    return reset({"exit_code", "ok", "stdout_tail"} | _cwd(cfg) | _carry(cfg),
                 complete=True, closed=False)


def worktree_contract(cfg: Dict[str, Any]) -> Contract:
    if cfg.get("op") == "remove":                      # _remove does NOT apply carry
        return reset({"removed", "ok"}, closed=True)
    return reset({"workdir", "branch", "repo", "base"} | _carry(cfg), closed=True)


def agent_loop_contract(cfg: Dict[str, Any]) -> Contract:
    return preserve("answer", "turns", "outcome")      # spreads inbound, adds these three


def validator_contract(cfg: Dict[str, Any]) -> Contract:
    # A validator used as a MAIN node returns a Verdict, which `to_envelope`/`reply_with`
    # turn into a FRESH payload of exactly these three keys (core/verdict.py, core/envelope.py)
    # — it RESETS, dropping all inbound keys. Runtime-exact (all three always set) → closed.
    # (In the `validators:` slot it isn't the stage's node, so this contract doesn't apply.)
    return reset({"status", "severity", "failures"}, closed=True)


# type name → contract function. The registry wires these beside each builder in B2; kept
# together here so the whole table reads against the ADR-0006 D2 table in one place.
BUILTIN_CONTRACTS: Dict[str, Callable[[Dict[str, Any]], Contract]] = {
    "agent": agent_contract,
    "transform": transform_contract,
    "render": render_contract,
    "human_gate": human_gate_contract,
    "get": get_contract,
    "post": post_contract,
    "shell": shell_contract,
    "worktree": worktree_contract,
    "agent_loop": agent_loop_contract,
    "json_object": validator_contract,
    "json_schema": validator_contract,
    "expect_field": validator_contract,
    "shell_check": validator_contract,
}


def builtin_contract_for(ntype: Any, cfg: Dict[str, Any]) -> Optional[Contract]:
    """The built-in contract for a node type, or None if the type is unknown (custom). Never
    raises — a malformed cfg yields the safest contract, not an exception."""
    fn = BUILTIN_CONTRACTS.get(ntype)
    if fn is None:
        return None
    try:
        return fn(cfg if isinstance(cfg, dict) else {})
    except Exception:
        return opaque()


# --- the resolver: pick a node's contract from the available sources -----------------------

ContractFor = Callable[[Any, Dict[str, Any]], Optional[Contract]]


def resolve_contract(ntype: Any, cfg: Dict[str, Any], *,
                     contract_for: Optional[ContractFor] = None,
                     manifest: Optional[ContractFor] = None,
                     live: Optional[ContractFor] = None) -> Contract:
    """Resolve a node's contract, in precedence order (ADR-0006 §D3). NEVER raises. Must NOT
    be called for a routing stage (node is None) — the lattice short-circuits those to
    `preserve()` before calling here (D4).

    1. a registered/built-in contract for the type (the static `describe()` binding),
    2. a frozen manifest (D7, later), 3. a live `describe()` (D7, later),
    4. else: inline config `provides:` → a checkable preserve; otherwise OPAQUE (sound skip —
       NEVER a preserve-with-complete default, which is the current soundness bug).

    Inline `provides:` on a KNOWN node AUGMENTS its contract (only grows `known`, so it can
    hide a gap — a documented escape hatch — never manufacture a false positive)."""
    if contract_for is None:
        contract_for = builtin_contract_for
    try:
        base = contract_for(ntype, cfg)
        if base is None and manifest is not None:
            base = manifest(ntype, cfg)
        if base is None and live is not None:
            base = live(ntype, cfg)
        inline = cfg.get("provides") if isinstance(cfg, dict) else None
        inline_set = (frozenset(x for x in inline if isinstance(x, str))
                      if isinstance(inline, list) else None)
        if base is None:
            if inline_set is not None:
                return preserve_declared(inline_set)   # author-declared → checkable, not closed
            return opaque()                             # sound skip
        if inline_set:
            base = base._replace(provides=base.provides | inline_set)
        return base
    except Exception:
        return opaque()   # the lint never raises; the safest contract on any surprise
