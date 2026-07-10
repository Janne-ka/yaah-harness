"""registry contract slots (ADR-0006 D7.1 / D7.2 — the LIVE path).

A custom node TYPE can register a pure `contract(cfg)` / `consumes(cfg, base_path)` beside its
builder, and the data-flow lint can reach it via an injected source. These FALSIFY:
  - a registered CLOSED contract turns a downstream read of an absent key into a hard ERROR via
    analyze_dataflow (where an unregistered custom type stays opaque → silent skip);
  - a registered consumes gets checked;
  - unregistered / undeclared stays opaque/empty (byte-for-byte today's behaviour);
  - precedence per 0006-D7 §4 (registered base #1, inline `provides:` AUGMENTS it #3);
  - the chained source uses `is not None` (an empty frozenset from a builtin reader must NOT
    fall through to the registry — the `or`-vs-`is None` trap);
  - never-raises on malformed registrations / unhashable types.

Run: cd yaah && PYTHONPATH=src python3 tests/test_registry_contracts.py
"""
from __future__ import annotations

from typing import Any, Dict

from yaah.build.registry import Registry
from yaah.build.build_context import BuildContext
from yaah.dataflow import analyze_dataflow
from yaah.node_contract import Contract, opaque, reset


# --- fixtures ----------------------------------------------------------------------------

def _dummy_builder(spec: Dict[str, Any], ctx: BuildContext) -> Any:
    # analyze_dataflow never builds — the builder is only here to satisfy register().
    raise AssertionError("builder must not be called by the lint")


def _reg(**contracts: Any) -> Registry:
    """A registry with one custom type per kwarg. Value is (contract_fn, consumes_fn) or a
    bare contract_fn."""
    r = Registry()
    for name, spec in contracts.items():
        if isinstance(spec, tuple):
            c, cons = spec
        else:
            c, cons = spec, None
        r.register(name, _dummy_builder, contract=c, consumes=cons)
    return r


def _flow(nodes: Dict[str, Any], stages: Dict[str, Any], start: str,
          reg: Registry = None):
    cf = reg.contract_source() if reg is not None else None
    csf = reg.consumes_source() if reg is not None else None
    return analyze_dataflow(nodes, stages, [], start, None,
                            contract_for=cf, consumes_for=csf)


# --- D7.1: the lookup slots --------------------------------------------------------------

def register_stores_contract_and_consumes() -> None:
    r = _reg(my_thing=(lambda cfg: reset({"a", "b"}, closed=True),
                       lambda cfg, bp: frozenset({"x"})))
    assert r.contract_for("my_thing", {}) == reset({"a", "b"}, closed=True)
    assert r.consumes_for("my_thing", {}, None) == frozenset({"x"})


def contract_for_unregistered_is_none() -> None:
    r = _reg(my_thing=lambda cfg: reset({"a"}, closed=True))
    assert r.contract_for("no_such", {}) is None
    assert r.consumes_for("no_such", {}, None) is None


def contract_for_registered_without_slot_is_none() -> None:
    # a type registered with a BUILDER only (no contract=) is undeclared → None (so inline/opaque
    # still applies), NOT an error.
    r = Registry()
    r.register("plain", _dummy_builder)
    assert r.contract_for("plain", {}) is None
    assert r.consumes_for("plain", {}, None) is None


def contract_for_never_raises() -> None:
    # unhashable ntype, non-dict cfg, and a registration whose fn RAISES must all be swallowed.
    def boom(cfg: Any) -> Contract:
        raise RuntimeError("malformed registration")
    r = _reg(bad=(boom, lambda cfg, bp: 1 / 0))
    assert r.contract_for(["list"], {}) is None          # unhashable type
    assert isinstance(r.contract_for("bad", None), Contract)  # non-dict cfg + raising fn
    assert r.contract_for("bad", {}) == opaque()         # raising contract → sound skip
    assert r.consumes_for(["list"], {}, None) is None    # unhashable type
    assert r.consumes_for("bad", {}, None) == frozenset()  # raising consumes → empty


def builtins_are_not_in_the_registry_contract_slot() -> None:
    # built-ins keep BUILTIN_CONTRACTS as their source — the registry's custom slot is empty for
    # them, but the CHAINED source still resolves them (via builtin_contract_for).
    from yaah.build.builders import default_registry
    r = default_registry()
    assert r.contract_for("agent", {"parse": False}) is None           # not in the custom slot
    src = r.contract_source()
    assert src("agent", {"parse": False}) == reset({"raw"}, closed=True)  # chain resolves builtin


# --- D7.2 / AT1: a registered CLOSED contract gives ERROR-grade reach --------------------

_PRODUCER_THEN_RENDER = {
    "stages": {
        "s1": {"node": "producer", "then": "s2"},
        "s2": {"node": "r"},
    },
}


def _render_pipe(template: str):
    return ({"producer": {"type": "my_thing"},
             "r": {"type": "render", "template_text": template}},
            _PRODUCER_THEN_RENDER["stages"])


def at1_registered_closed_contract_makes_absent_read_a_hard_error() -> None:
    reg = _reg(my_thing=lambda cfg: reset({"a", "b"}, closed=True))
    nodes, stages = _render_pipe("{{missing}}")
    errs, warns = _flow(nodes, stages, "s1", reg)
    assert any("missing" in e for e in errs), errs   # provably absent on a closed path → ERROR


def at1_registered_contract_read_of_a_provided_key_is_clean() -> None:
    reg = _reg(my_thing=lambda cfg: reset({"a", "b"}, closed=True))
    nodes, stages = _render_pipe("{{a}}")
    errs, warns = _flow(nodes, stages, "s1", reg)
    assert not errs, errs


def without_injection_a_custom_type_stays_opaque_silent() -> None:
    # THE FALSIFIER: the SAME pipeline with NO injected source → my_thing is unknown → opaque →
    # the render is unchecked (a warning at most), never an ERROR. Proves the registered contract
    # is what upgrades it to ERROR-grade.
    nodes, stages = _render_pipe("{{missing}}")
    errs, _ = _flow(nodes, stages, "s1", None)
    assert not errs, errs


def unregistered_custom_type_with_injection_still_opaque() -> None:
    # a registry that knows OTHER types but not `my_thing` → my_thing resolves to None both sides
    # → opaque → no ERROR (unchanged from builtins-only).
    reg = _reg(other=lambda cfg: reset({"z"}, closed=True))
    nodes, stages = _render_pipe("{{missing}}")
    errs, _ = _flow(nodes, stages, "s1", reg)
    assert not errs, errs


# --- severity: a registered contract keeps the closed/complete split (ADR-0006 D5) -------

def registered_complete_not_closed_contract_is_warning_not_error() -> None:
    # the soundness split: a registered contract that is DECLARED-exact (complete) but not
    # provable (closed) must yield a WARNING, never a hard ERROR — same rule as an agent
    # output_schema. A regression that hard-errors here fails a working pipeline at load.
    reg = _reg(my_thing=lambda cfg: reset({"a"}, complete=True, closed=False))
    nodes, stages = _render_pipe("{{missing}}")
    errs, warns = _flow(nodes, stages, "s1", reg)
    assert not errs, "complete-not-closed must never hard-error: {}".format(errs)
    assert any("missing" in w for w in warns), warns


# --- a custom contract reaches branch.on and foreach paths (the threading, not just then) --

def registered_contract_reaches_branch_on() -> None:
    # branch.on reads the stage node's OUTPUT — the _transfer call inside analyze_dataflow's
    # check loop must ALSO receive contract_for, or a registered closed contract would not
    # error on a branch key it provably cannot provide.
    reg = _reg(my_thing=lambda cfg: reset({"a"}, closed=True))
    nodes = {"producer": {"type": "my_thing"},
             "sink": {"type": "render", "template_text": "{{a}}"}}
    stages = {"s1": {"node": "producer",
                     "branch": {"on": "not_provided", "routes": {"x": "s2"}, "default": "s2"}},
              "s2": {"node": "sink"}}
    errs, _ = _flow(nodes, stages, "s1", reg)
    assert any("not_provided" in e for e in errs), errs


def registered_contract_reaches_foreach_reads() -> None:
    # foreach.items is read from the stage's INBOUND flow (computed via compute_provides,
    # which must thread contract_for): a registered closed producer that can't provide the
    # items key is a provable every-run failure.
    reg = _reg(my_thing=lambda cfg: reset({"a"}, closed=True),
               worker_t=lambda cfg: reset({"out"}, closed=True))
    nodes = {"producer": {"type": "my_thing"}, "w": {"type": "worker_t"}}
    stages = {"s1": {"node": "producer", "then": "s2"},
              "s2": {"node": "w", "foreach": {"items": "todo"}}}
    errs, _ = _flow(nodes, stages, "s1", reg)
    assert any("todo" in e for e in errs), errs


# --- precedence (0006-D7 §4): inline provides AUGMENTS the registered base ---------------

def inline_provides_augments_registered_contract() -> None:
    reg = _reg(my_thing=lambda cfg: reset({"a"}, closed=True))
    nodes = {"producer": {"type": "my_thing", "provides": ["b"]},
             "r": {"type": "render", "template_text": "{{b}}"}}
    errs, _ = _flow(nodes, _PRODUCER_THEN_RENDER["stages"], "s1", reg)
    assert not errs, "inline `b` must augment the registered {a} base: {}".format(errs)


def augmented_contract_keeps_closed_so_other_keys_still_error() -> None:
    reg = _reg(my_thing=lambda cfg: reset({"a"}, closed=True))
    nodes = {"producer": {"type": "my_thing", "provides": ["b"]},
             "r": {"type": "render", "template_text": "{{c}}"}}
    errs, _ = _flow(nodes, _PRODUCER_THEN_RENDER["stages"], "s1", reg)
    assert any("c" in e for e in errs), errs   # augment only GROWS known; c still provably absent


# --- consumes (D7.1/AT6): a registered consumes gets checked -----------------------------

def registered_consumes_reading_absent_key_is_error() -> None:
    reg = _reg(producer_t=lambda cfg: reset({"a", "b"}, closed=True),
               reader_t=(lambda cfg: opaque(), lambda cfg, bp: frozenset({"x"})))
    nodes = {"producer": {"type": "producer_t"}, "rd": {"type": "reader_t"}}
    stages = {"s1": {"node": "producer", "then": "s2"}, "s2": {"node": "rd"}}
    errs, _ = _flow(nodes, stages, "s1", reg)
    assert any("x" in e for e in errs), errs   # reader consumes x, closed inbound lacks it → ERROR


def registered_consumes_satisfied_is_clean() -> None:
    reg = _reg(producer_t=lambda cfg: reset({"a", "b", "x"}, closed=True),
               reader_t=(lambda cfg: opaque(), lambda cfg, bp: frozenset({"x"})))
    nodes = {"producer": {"type": "producer_t"}, "rd": {"type": "reader_t"}}
    stages = {"s1": {"node": "producer", "then": "s2"}, "s2": {"node": "rd"}}
    errs, _ = _flow(nodes, stages, "s1", reg)
    assert not errs, errs


# --- the chain uses `is not None`, not truthiness (empty-frozenset trap) -----------------

def consumes_source_uses_is_not_none_for_empty_builtin() -> None:
    # a render with allow_unfilled → builtin_consumes_for returns frozenset() (EMPTY, not None):
    # the chain must return that empty set, NOT fall through to the registry. A naive `builtin or
    # registry` would treat frozenset() as falsy and consult the registry.
    reg = _reg(other=(lambda cfg: opaque(), lambda cfg, bp: frozenset({"NEVER"})))
    src = reg.consumes_source()
    got = src("render", {"template_text": "{{a}}", "allow_unfilled": True}, None)
    assert got is not None and got == frozenset(), got


def contract_source_never_raises_on_unhashable() -> None:
    reg = _reg(other=lambda cfg: reset({"z"}, closed=True))
    assert reg.contract_source()(["bad"], {}) is None
    assert reg.consumes_source()(["bad"], {}, None) is None


# --- built-ins still resolved through the chain (byte-for-byte) --------------------------

def builtin_agent_still_errors_through_the_chain() -> None:
    reg = _reg(other=lambda cfg: reset({"z"}, closed=True))
    nodes = {"a": {"type": "agent", "parse": False},
             "r": {"type": "render", "template_text": "{{missing}}"}}
    stages = {"s1": {"node": "a", "then": "s2"}, "s2": {"node": "r"}}
    errs, _ = _flow(nodes, stages, "s1", reg)
    assert any("missing" in e for e in errs), errs   # parse=false agent is closed {raw} → ERROR


def builtin_agent_raw_read_is_clean_through_the_chain() -> None:
    reg = _reg(other=lambda cfg: reset({"z"}, closed=True))
    nodes = {"a": {"type": "agent", "parse": False},
             "r": {"type": "render", "template_text": "{{raw}}"}}
    stages = {"s1": {"node": "a", "then": "s2"}, "s2": {"node": "r"}}
    errs, _ = _flow(nodes, stages, "s1", reg)
    assert not errs, errs


def default_none_is_byte_identical() -> None:
    # analyze_dataflow with NO contract_for/consumes_for == today's builtins-only behaviour.
    nodes = {"a": {"type": "agent", "parse": False},
             "r": {"type": "render", "template_text": "{{missing}}"}}
    stages = {"s1": {"node": "a", "then": "s2"}, "s2": {"node": "r"}}
    a = analyze_dataflow(nodes, stages, [], "s1", None)
    b = analyze_dataflow(nodes, stages, [], "s1", None, contract_for=None, consumes_for=None)
    assert a == b, (a, b)
    assert any("missing" in e for e in a[0]), a


def main() -> None:
    import types
    for name, fn in sorted(globals().items()):
        if (isinstance(fn, types.FunctionType) and fn.__module__ == __name__
                and not name.startswith("_") and name != "main"):
            fn()
    print("ok")


if __name__ == "__main__":
    main()
