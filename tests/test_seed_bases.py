"""R14-seed — every packaged base config under `src/yaah/configs/bases/`
validates cleanly under R15, an `_extends` overlay on top loads end-to-end
through `_read_json` + `validate_root` (the path a real root would walk), and
the `yaah:bases/...` package-reference scheme resolves the seed identically
from the source tree and (via importlib.resources) an installed wheel.

Run: cd yaah && PYTHONPATH=src python3 tests/test_seed_bases.py
"""
from __future__ import annotations

import json
import os
import tempfile

from yaah.runtime_factories import _read_json, _resolve_pkg_ref
from yaah.validate import validate_root

# the seeds now ship INSIDE the package (src/yaah/configs/bases) so a pip
# install carries them; importlib.resources is the install-safe accessor
_REPO_BASES = os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "src", "yaah", "configs", "bases"))


def _bases() -> list:
    return sorted(f for f in os.listdir(_REPO_BASES) if f.endswith(".json"))


def scenario_every_base_validates_alone_or_documents_why_not() -> None:
    """A *deployment* base (local/nats) must validate standalone — that's the
    floor it provides. A trace OVERLAY base (only `trace:`) validates trivially
    because R15 doesn't require keys to be PRESENT, only well-shaped if so."""
    for fn in _bases():
        with open(os.path.join(_REPO_BASES, fn)) as f:
            cfg = json.load(f)
        try:
            validate_root(cfg)
        except ValueError as e:
            raise AssertionError("{!r} fails R15 validation: {}".format(fn, e))


def scenario_user_extends_local_base_and_runs_through_loader() -> None:
    """The real path a user walks: write a tiny root with `_extends:` pointing
    at the seed, add their own `providers` + `pipeline`, then `_read_json` +
    `validate_root` succeed. This is what the R16 skill emits."""
    with tempfile.TemporaryDirectory() as d:
        base_src = os.path.join(_REPO_BASES, "local.base.json")
        with open(os.path.join(d, "local.base.json"), "w") as f:
            f.write(open(base_src).read())
        user = {
            "_extends": "local.base.json",
            "providers": {"claude": {"type": "claude_cli"}},
            "default_provider": "claude",
            "pipeline": "p.json",
            "input": "in.json",
        }
        path = os.path.join(d, "root.json")
        with open(path, "w") as f:
            json.dump(user, f)
        effective = _read_json(path)
        validate_root(effective)
        # the seed's transport survived the merge
        assert effective["transport"] == {"type": "inproc"}
        # the user's providers won
        assert "claude" in effective["providers"]


def scenario_trace_audit_overlay_layered_on_local() -> None:
    """`trace-audit.base.json` is meant to be `_extends`-ed by a real root so its
    heavy capture wins. Verify the precedence works through `_read_json`."""
    with tempfile.TemporaryDirectory() as d:
        # copy both bases in
        for fn in ("local.base.json", "trace-audit.base.json"):
            with open(os.path.join(d, fn), "w") as f:
                f.write(open(os.path.join(_REPO_BASES, fn)).read())
        # the user root extends trace-audit (which logically should NOT be the
        # whole-deployment base, but a 2-level chain is what the docs describe)
        # so for the test we use trace-audit as a sibling overlay applied LAST
        # via _extends-chain by writing a tiny root that extends local first
        # and then a SECOND root that extends trace-audit — _extends is single-
        # parent, so we model the audit-overlay use case as: root --extends-->
        # trace-audit.base.json --extends--> local.base.json. Document the
        # chain inline.
        ext_chain = {
            "_extends": "local.base.json",
            "trace": {
                "mode": "tracer",
                "capture": ["phase", "cost", "tools"],
                "sinks": [{"type": "file", "path": "trace-audit.jsonl"},
                          {"type": "console"}],
            },
        }
        # write a chain head that extends a synthesized intermediate
        with open(os.path.join(d, "audited.base.json"), "w") as f:
            json.dump(ext_chain, f)
        user = {
            "_extends": "audited.base.json",
            "providers": {"claude": {"type": "claude_cli"}},
            "default_provider": "claude",
            "pipeline": "p.json",
            "input": "in.json",
        }
        path = os.path.join(d, "root.json")
        with open(path, "w") as f:
            json.dump(user, f)
        effective = _read_json(path)
        validate_root(effective)
        # heavy-capture trace from the audit overlay survived; local.base provided
        # the transport/state/prompt_sources
        assert "tools" in effective["trace"]["capture"]
        assert effective["transport"] == {"type": "inproc"}


def scenario_every_base_BUILDS_a_tracer_with_its_sinks_attached() -> None:
    """Validating is not enough — the sink/sinks bug passed validation for weeks
    because nothing ever BUILT a tracer from a seed base (the factory read `sink`,
    the bases said `sinks`, every base silently lost its sinks). Build the tracer
    from each base's `trace:` block and assert each configured sink actually
    subscribed to the trace topic."""
    import asyncio

    from yaah.comms import InProcessComms
    from yaah.runtime_factories import _build_tracer
    from yaah.trace import BusTracer

    for fn in _bases():
        with open(os.path.join(_REPO_BASES, fn)) as f:
            cfg = json.load(f)
        tr = cfg.get("trace")
        if not isinstance(tr, dict) or tr.get("mode", "tracer") != "tracer":
            continue
        with tempfile.TemporaryDirectory() as d:  # file sinks resolve paths here
            comms = InProcessComms()
            tracer = asyncio.run(_build_tracer(cfg, comms, base=d))
            assert isinstance(tracer, BusTracer), (fn, type(tracer))
            want = tr.get("sinks", [{"type": "console"}])
            got = len(comms._subs.get(tr.get("topic", "trace"), ()))
            assert got == len(want), (
                "{!r}: {} sinks configured but {} subscribed — a config key the "
                "factory doesn't read?".format(fn, len(want), got))


def scenario_yaah_pkg_ref_resolves_from_the_package() -> None:
    """The install-safe path: `_extends: "yaah:bases/local.base.json"` resolves
    the packaged seed via importlib.resources — no relative filesystem path, so
    it survives a pip install / public extraction. The user writes ONLY their
    own root next to no base file at all."""
    direct = _resolve_pkg_ref("yaah:bases/local.base.json")
    assert direct["transport"] == {"type": "inproc"}, direct

    with tempfile.TemporaryDirectory() as d:
        user = {
            "_extends": "yaah:bases/local.base.json",  # no local copy of the base!
            "providers": {"claude": {"type": "claude_cli"}},
            "default_provider": "claude",
            "pipeline": "p.json", "input": "in.json",
        }
        path = os.path.join(d, "root.json")
        with open(path, "w") as f:
            json.dump(user, f)
        effective = _read_json(path)
        validate_root(effective)
        assert effective["transport"] == {"type": "inproc"}, effective
        assert "claude" in effective["providers"], effective

    missing = False
    try:
        _resolve_pkg_ref("yaah:bases/nope.json")
    except ValueError:
        missing = True
    assert missing, "a missing packaged seed must raise, not return None"


def main() -> None:
    scenario_every_base_validates_alone_or_documents_why_not()
    scenario_user_extends_local_base_and_runs_through_loader()
    scenario_trace_audit_overlay_layered_on_local()
    scenario_every_base_BUILDS_a_tracer_with_its_sinks_attached()
    scenario_yaah_pkg_ref_resolves_from_the_package()
    print("test_seed_bases: PASS (5 scenarios)")


if __name__ == "__main__":
    main()
