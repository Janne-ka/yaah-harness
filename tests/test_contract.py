"""contract — the @provides decorator + opt-in extractor (ADR-0005 slice D).

Proves: the decorator records keys (and rejects typos at import); `provides_of` reads them;
`fn_provides_resolver` imports a real decorated fn and returns its keys (and degrades to None
on a non-fn / unimportable / undecorated target); and the lint, given a resolver, sees across
an otherwise-opaque envelope-transform — clearing the taint and checking downstream.

Run: cd yaah && PYTHONPATH=src python3 tests/test_contract.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import textwrap

from yaah.contract import fn_provides_resolver, provides, provides_of
from yaah.validate import _augment_provides_from_code, lint_pipeline


def decorator_records_keys_and_is_transparent() -> None:
    @provides("verdict", "summary")
    def f(envelope, config):
        return {"verdict": 1, "summary": 2}
    assert provides_of(f) == ("verdict", "summary")
    assert f(None, None) == {"verdict": 1, "summary": 2}   # behaviour unchanged


def decorator_rejects_bad_keys() -> None:
    for bad in ([""], [None], [3]):
        try:
            provides(*bad)
        except ValueError:
            pass
        else:
            raise AssertionError("@provides should reject {!r}".format(bad))


def provides_of_undecorated_is_none() -> None:
    def plain():
        pass
    assert provides_of(plain) is None
    assert provides_of(lambda: None) is None


def _write_module(d: str, name: str, body: str) -> None:
    with open(os.path.join(d, name + ".py"), "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(body))


def resolver_reads_real_decorated_fn() -> None:
    mod = "yaah_test_d_transforms"
    with tempfile.TemporaryDirectory() as d:
        _write_module(d, mod, """
            from yaah.contract import provides
            @provides("verdict", "summary")
            def parse(envelope, config):
                return {"verdict": "x", "summary": "y"}
            def undecorated(envelope, config):
                return {}
        """)
        resolve = fn_provides_resolver(d)
        try:
            assert resolve("fn:{}:parse".format(mod)) == ["verdict", "summary"]
            assert resolve("fn:{}:undecorated".format(mod)) is None   # not decorated
            assert resolve("fn:{}:missing".format(mod)) is None       # import/attr error
            assert resolve("fn:no_such_module:f") is None             # unimportable
            assert resolve("node:role") is None                      # non-fn target
            assert resolve(None) is None
        finally:
            sys.modules.pop(mod, None)


def _chain_cfg(target="fn:m:parse", template="{{verdict}}"):
    return {"nodes": {
        "a": {"type": "agent", "parse": False},
        "t": {"type": "transform", "target": target, "call": "envelope"},
        "r": {"type": "render", "template_text": template}},
        "graph": {"start": "s1", "stages": {
            "s1": {"node": "a", "then": "s2"},
            "s2": {"node": "t", "then": "s3"},
            "s3": {"node": "r"}}}}


def lint_without_resolver_taints_undeclared_transform() -> None:
    w = lint_pipeline(_chain_cfg())
    assert any("transform-provides-undeclared" in m for m in w), w


def lint_with_resolver_clears_taint_and_checks_downstream() -> None:
    cfg = _chain_cfg(template="{{verdict}}")
    # resolver declares the transform provides `verdict` -> no taint, render satisfied
    w_ok = lint_pipeline(cfg, resolve=lambda t: ["verdict"] if t == "fn:m:parse" else None)
    assert not any("transform-provides-undeclared" in m for m in w_ok), w_ok
    assert not any("render-key-unprovided" in m for m in w_ok), w_ok
    # resolver declares the WRONG key -> render is now CHECKABLE and the gap is caught
    w_bad = lint_pipeline(cfg, resolve=lambda t: ["other"])
    assert not any("transform-provides-undeclared" in m for m in w_bad), w_bad
    assert any("render-key-unprovided" in m and "verdict" in m for m in w_bad), w_bad


def resolver_does_not_override_explicit_config_provides() -> None:
    for declared in (["verdict"], []):               # a non-empty list AND a deliberate []
        cfg = _chain_cfg()
        cfg["nodes"]["t"]["provides"] = declared      # author already declared it
        called = []
        lint_pipeline(cfg, resolve=lambda t: called.append(t) or ["WRONG"])
        assert called == [], (
            "resolver must not be consulted when config declares provides "
            "(incl. []): {!r}".format(declared))


def augment_is_pure() -> None:
    nodes = {"t": {"type": "transform", "call": "envelope", "target": "fn:m:f"}}
    out = _augment_provides_from_code(nodes, lambda t: ["k"])
    assert out["t"]["provides"] == ["k"]
    assert "provides" not in nodes["t"], "input nodes must not be mutated"


def main() -> None:
    decorator_records_keys_and_is_transparent()
    decorator_rejects_bad_keys()
    provides_of_undecorated_is_none()
    resolver_reads_real_decorated_fn()
    lint_without_resolver_taints_undeclared_transform()
    lint_with_resolver_clears_taint_and_checks_downstream()
    resolver_does_not_override_explicit_config_provides()
    augment_is_pure()
    print("ok")


if __name__ == "__main__":
    main()
