"""The `attach: [...]` builder branch in `_build_agent`.

What it proves: a happy-path agent spec with `attach: [...]` is built as an
AttachingAgent wrapping the inner Agent; non-fn: items reject; fn:-resolved
non-Attacher targets reject; tracer-missing-required-capture rejects with the
exact `trace: {capture: [...]}` snippet (review S7); empty/absent `attach`
falls through to plain Agent (no wrapping). ADR-0003 / review S7.

Run: cd yaah && PYTHONPATH=src python3 tests/test_attach_builder.py

Targets Python 3.9+.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, Optional

# Allow importing fixture attachers from this test file via `fn:` resolution:
# the resolver imports module by name, so we expose a module-named one below.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from yaah.agents.attacher import Attacher
from yaah.agents.attaching_agent import AttachingAgent
from yaah.agents.agent import Agent
from yaah.build.build_context import BuildContext
from yaah.build.builders import _build_agent
from yaah.core import Envelope, Kind
from yaah.trace.contributors.cost import CostContributor
from yaah.trace.null_tracer import NullTracer
from yaah.trace.recording_tracer import RecordingTracer


# --- Attachers + non-attacher exposed for `fn:` resolution -------------------

class _CostAttacher(Attacher):
    name = "test_cost"
    requires_capture = ("cost",)
    def attach(self, envelope, span):
        return {"_test_cost": True}


class _NoReqAttacher(Attacher):
    name = "test_noreq"
    requires_capture = ()
    def attach(self, envelope, span):
        return {"_test_noreq": True}


def _not_an_attacher(envelope, span):
    """A plain function — NOT a subclass of Attacher. Builder must reject."""
    return {}


# --- helpers -----------------------------------------------------------------

class _FakeBackend:
    """Minimal stand-in backend — only needs to exist as ctx.backend
    so the agent builder doesn't reject."""


def _ctx(tracer):
    return BuildContext(comms=None, backend=_FakeBackend(), tracer=tracer)


def _spec(attach=None):
    s = {"type": "agent", "model": "fake:m", "template": "hi"}
    if attach is not None:
        s["attach"] = attach
    return s


def _expect_reject(spec, ctx, *needles):
    try:
        _build_agent(spec, ctx)
    except ValueError as e:
        msg = str(e)
        for n in needles:
            assert n in msg, "missing {!r} in error: {}".format(n, msg)
        return
    raise AssertionError("expected ValueError for spec={!r}".format(spec))


# --- the tests ---------------------------------------------------------------

def main() -> None:
    THIS = "test_attach_builder"

    # ---- absent / empty attach: no wrapping ---------------------------------
    plain1 = _build_agent(_spec(), _ctx(NullTracer()))
    assert isinstance(plain1, Agent) and not isinstance(plain1, AttachingAgent), \
        "no attach -> plain Agent"
    plain2 = _build_agent(_spec(attach=[]), _ctx(NullTracer()))
    assert isinstance(plain2, Agent) and not isinstance(plain2, AttachingAgent), \
        "empty attach -> plain Agent"

    # ---- happy path: cost-capable tracer + an attacher requiring 'cost' -----
    tracer = RecordingTracer(contributors=[CostContributor()])
    wrapped = _build_agent(
        _spec(attach=["fn:{}:_CostAttacher".format(THIS)]),
        _ctx(tracer))
    assert isinstance(wrapped, AttachingAgent), "wrapping must happen"

    # ---- attacher with no required captures works on any tracer (NullTracer
    # has empty captures) — the capture-check only triggers when there is a
    # required capture missing
    wrapped_noreq = _build_agent(
        _spec(attach=["fn:{}:_NoReqAttacher".format(THIS)]),
        _ctx(NullTracer()))
    assert isinstance(wrapped_noreq, AttachingAgent)

    # ---- review S7: tracer missing required capture -> reject with hint -----
    null_tracer = NullTracer()
    _expect_reject(
        _spec(attach=["fn:{}:_CostAttacher".format(THIS)]),
        _ctx(null_tracer),
        "cost", "trace", "capture")

    # The error message must include the suggested `capture` list with 'cost'
    # added (review "concrete error message" — actionable, not just diagnostic).
    try:
        _build_agent(
            _spec(attach=["fn:{}:_CostAttacher".format(THIS)]),
            _ctx(null_tracer))
    except ValueError as e:
        msg = str(e)
        assert "'cost'" in msg, "must name the missing capture: {}".format(msg)

    # ---- non-fn: item rejected ---------------------------------------------
    _expect_reject(_spec(attach=["usage"]), _ctx(tracer),
                   "fn:module:func", "ADR-0003")
    _expect_reject(_spec(attach=[{"name": "usage"}]), _ctx(tracer),
                   "fn:module:func")

    # ---- fn: resolves to non-Attacher rejected ------------------------------
    _expect_reject(
        _spec(attach=["fn:{}:_not_an_attacher".format(THIS)]),
        _ctx(tracer),
        "subclass of yaah.agents.attacher.Attacher")

    print("PASS _build_agent attach branch: wraps, capture-check, fn-only, "
          "type-check, default no-op")


if __name__ == "__main__":
    main()
