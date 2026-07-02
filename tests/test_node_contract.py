"""node_contract — each built-in's contract vs the ADR-0006 D2 table, the resolver
precedence, and the flow algebra (apply/meet). These FALSIFY: they encode the exact D2
rows (and the four bugs the design eval caught), so a drift from the table fails here.

Run: cd yaah && PYTHONPATH=src python3 tests/test_node_contract.py
"""
from __future__ import annotations

from yaah.node_contract import (
    Contract, Flow, apply, meet, opaque, preserve, preserve_declared, reset, resolve_contract,
    agent_contract, transform_contract, render_contract, human_gate_contract,
    get_contract, post_contract, shell_contract, worktree_contract,
    agent_loop_contract, validator_contract, builtin_contract_for,
)


# --- reset nodes -------------------------------------------------------------------------

def agent_parse_false_is_closed_raw() -> None:
    c = agent_contract({"type": "agent", "parse": False})
    assert c == reset({"raw"}, closed=True), c
    assert c.closed and c.complete, c

def agent_parse_false_forwards_carry_and_cwd() -> None:
    c = agent_contract({"parse": False, "carry": ["task"], "cwd_from": "workdir"})
    assert c.provides == frozenset({"raw", "task", "workdir"}), c
    assert c.closed, c

def agent_parse_true_no_schema_is_incomplete() -> None:
    c = agent_contract({"type": "agent"})       # parse defaults True
    assert c.mode == "reset" and c.provides == frozenset({"raw"}), c
    assert not c.complete and not c.closed, c   # unknown parsed keys → can't check

def agent_with_schema_is_complete_but_never_closed() -> None:
    c = agent_contract({"output_schema": {"properties": {"verdict": {}}, "required": ["summary"]}})
    assert c.mode == "reset", c
    assert c.provides == frozenset({"verdict", "summary", "raw"}), c
    assert c.complete, c

def agent_never_closed_even_with_additionalProperties_false() -> None:
    # The eval's #1 must-fix: check_schema ignores additionalProperties and the agent merges
    # its whole parsed reply, so an agent's set is never RUNTIME-provable. complete, not closed.
    c = agent_contract({"output_schema": {"properties": {"verdict": {}}, "additionalProperties": False}})
    assert c.complete, c
    assert not c.closed, "agent must NEVER be closed — a closed miss is a hard error: {}".format(c)

def agent_provides_without_schema_is_complete() -> None:
    c = agent_contract({"provides": ["verdict"]})
    assert c.complete and not c.closed, c
    assert "verdict" in c.provides and "raw" in c.provides, c

def shell_is_complete_not_closed() -> None:
    c = shell_contract({"type": "shell"})
    assert c == reset({"exit_code", "ok", "stdout_tail"}, complete=True, closed=False), c
    assert c.complete and not c.closed, "shell has conditional stdout/timed_out → not closed: {}".format(c)

def shell_forwards_cwd_and_carry() -> None:
    c = shell_contract({"cwd_from": "wd", "carry": ["task"]})
    assert c.provides == frozenset({"exit_code", "ok", "stdout_tail", "wd", "task"}), c

def worktree_add_is_closed() -> None:
    c = worktree_contract({"op": "add"})
    assert c == reset({"workdir", "branch", "repo", "base"}, closed=True), c

def worktree_add_default_op() -> None:
    assert worktree_contract({}).provides == frozenset({"workdir", "branch", "repo", "base"})

def worktree_add_carries() -> None:
    c = worktree_contract({"op": "add", "carry": ["task"]})
    assert "task" in c.provides, c

def worktree_remove_does_not_carry() -> None:
    # eval fix: _remove never applies carry — the row must not union it in.
    c = worktree_contract({"op": "remove", "carry": ["task"]})
    assert c == reset({"removed", "ok"}, closed=True), c
    assert "task" not in c.provides, "remove does not carry: {}".format(c)


# --- preserve nodes ----------------------------------------------------------------------

def transform_args_preserves_into() -> None:
    assert transform_contract({"call": "args"}) == preserve("result")
    assert transform_contract({"call": "args", "into": "data"}) == preserve("data")

def transform_envelope_declared_preserves_provides() -> None:
    # a declared envelope-transform adds AUTHOR-declared keys → preserve_declared (NOT closed):
    # keeps `complete` (a contract) but never elevates to a runtime-proven `closed`.
    c = transform_contract({"call": "envelope", "provides": ["verdict", "summary"]})
    assert c == preserve_declared({"verdict", "summary"}), c
    assert c.mode == "preserve" and not c.closed, c

def transform_envelope_undeclared_is_opaque() -> None:
    assert transform_contract({"call": "envelope"}) == opaque()

def render_provides_output_and_path() -> None:
    assert render_contract({}) == preserve("output", "path")

def human_gate_provides_decision() -> None:
    assert human_gate_contract({}) == preserve("decision")

def get_default_into_is_data_not_result() -> None:
    # eval fix: get's builder default is "data", not "result".
    assert get_contract({}) == preserve("data"), get_contract({})
    assert get_contract({"into": "fetched"}) == preserve("fetched")

def post_default_into_is_stored() -> None:
    assert post_contract({}) == preserve("stored"), post_contract({})

def agent_loop_preserves_and_adds_three() -> None:
    assert agent_loop_contract({}) == preserve("answer", "turns", "outcome")

def validators_reset_to_verdict_keys() -> None:
    # a validator as a MAIN node returns a Verdict → fresh payload {status,severity,failures}
    # (it RESETS, dropping inbound). Runtime-exact → closed. NOT a passthrough.
    c = validator_contract({})
    assert c == reset({"status", "severity", "failures"}, closed=True), c
    assert c.mode == "reset" and c.closed, c


# --- never-raises ------------------------------------------------------------------------

def contracts_never_raise_on_malformed_cfg() -> None:
    bad = {"carry": True, "output_schema": 5, "provides": "nope", "into": 7, "cwd_from": []}
    for fn in (agent_contract, transform_contract, shell_contract, worktree_contract,
               get_contract, post_contract, render_contract):
        c = fn(bad)  # must not raise
        assert isinstance(c, Contract), (fn, c)

def builtin_contract_for_unknown_is_none() -> None:
    assert builtin_contract_for("no_such_type", {}) is None

def builtin_contract_for_matches_direct_call() -> None:
    assert builtin_contract_for("agent", {"parse": False}) == agent_contract({"parse": False})


# --- resolver ----------------------------------------------------------------------------

def resolve_unknown_no_inline_is_opaque() -> None:
    assert resolve_contract("custom_node", {"type": "custom_node"}) == opaque()

def resolve_unknown_with_inline_is_checkable_preserve() -> None:
    c = resolve_contract("custom_node", {"provides": ["k1", "k2"]})
    assert c.mode == "preserve" and c.provides == frozenset({"k1", "k2"}), c
    assert not c.closed, "a custom node's declared provides is a contract, not closed: {}".format(c)

def resolve_inline_augments_known_contract() -> None:
    # a shell with an extra declared key: augment only GROWS known (never a false positive).
    c = resolve_contract("shell", {"provides": ["extra"]})
    assert "extra" in c.provides and "exit_code" in c.provides, c
    assert c.mode == "reset", c

def resolve_known_without_inline_is_the_base() -> None:
    assert resolve_contract("render", {}) == render_contract({})

def resolve_never_raises() -> None:
    assert isinstance(resolve_contract("agent", None), Contract)  # cfg not a dict
    assert isinstance(resolve_contract(None, {}), Contract)       # no type

def resolve_manifest_then_live_fallback() -> None:
    # unknown to built-ins, but a manifest supplies it → used; inline still augments.
    man = lambda t, cfg: reset({"m1"}, closed=True) if t == "ext" else None
    c = resolve_contract("ext", {"provides": ["m2"]}, manifest=man)
    assert c.mode == "reset" and {"m1", "m2"} <= c.provides, c


# --- flow algebra: apply + meet ----------------------------------------------------------

def apply_preserve_adds_and_keeps_flags() -> None:
    pin = Flow(frozenset({"a"}), complete=True, closed=True)
    out = apply(preserve("b"), pin, frozenset())
    assert out == Flow(frozenset({"a", "b"}), True, True), out

def apply_reset_replaces_and_sets_flags() -> None:
    pin = Flow(frozenset({"a"}), complete=True, closed=True)
    out = apply(reset({"raw"}, closed=True), pin, frozenset())
    assert out == Flow(frozenset({"raw"}), True, True), out
    assert "a" not in out.known, out

def apply_preserve_declared_drops_closed_keeps_complete() -> None:
    # THE soundness fix: after a closed reset (parse=false agent), a declared envelope-transform
    # must DROP closed (its added keys are declared, not runtime-proven) so a downstream miss is
    # a WARNING not a hard ERROR — while KEEPING complete so the miss is still surfaced.
    pin = Flow(frozenset({"raw"}), complete=True, closed=True)
    out = apply(preserve_declared({"verdict"}), pin, frozenset())
    assert out.known == frozenset({"raw", "verdict"}), out
    assert out.complete and not out.closed, out


def apply_sticky_survives_reset() -> None:
    out = apply(reset({"raw"}, closed=True), Flow(), frozenset({"task"}))
    assert out.known == frozenset({"raw", "task"}), out

def apply_opaque_drops_all_but_sticky_and_flags_false() -> None:
    pin = Flow(frozenset({"a"}), complete=True, closed=True)
    out = apply(opaque(), pin, frozenset({"s"}))
    assert out == Flow(frozenset({"s"}), False, False), out

def meet_intersects_and_ands() -> None:
    a = Flow(frozenset({"x", "y"}), complete=True, closed=True)
    b = Flow(frozenset({"y", "z"}), complete=True, closed=False)
    assert meet(a, b) == Flow(frozenset({"y"}), True, False), meet(a, b)

def closed_implies_complete() -> None:
    assert reset({"k"}, closed=True).complete is True


def main() -> None:
    agent_parse_false_is_closed_raw()
    agent_parse_false_forwards_carry_and_cwd()
    agent_parse_true_no_schema_is_incomplete()
    agent_with_schema_is_complete_but_never_closed()
    agent_never_closed_even_with_additionalProperties_false()
    agent_provides_without_schema_is_complete()
    shell_is_complete_not_closed()
    shell_forwards_cwd_and_carry()
    worktree_add_is_closed()
    worktree_add_default_op()
    worktree_add_carries()
    worktree_remove_does_not_carry()
    transform_args_preserves_into()
    transform_envelope_declared_preserves_provides()
    transform_envelope_undeclared_is_opaque()
    render_provides_output_and_path()
    human_gate_provides_decision()
    get_default_into_is_data_not_result()
    post_default_into_is_stored()
    agent_loop_preserves_and_adds_three()
    validators_reset_to_verdict_keys()
    contracts_never_raise_on_malformed_cfg()
    builtin_contract_for_unknown_is_none()
    builtin_contract_for_matches_direct_call()
    resolve_unknown_no_inline_is_opaque()
    resolve_unknown_with_inline_is_checkable_preserve()
    resolve_inline_augments_known_contract()
    resolve_known_without_inline_is_the_base()
    resolve_never_raises()
    resolve_manifest_then_live_fallback()
    apply_preserve_adds_and_keeps_flags()
    apply_reset_replaces_and_sets_flags()
    apply_preserve_declared_drops_closed_keeps_complete()
    apply_sticky_survives_reset()
    apply_opaque_drops_all_but_sticky_and_flags_false()
    meet_intersects_and_ands()
    closed_implies_complete()
    print("ok")


if __name__ == "__main__":
    main()
