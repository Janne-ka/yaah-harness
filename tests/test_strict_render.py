"""Y1 strict-render: opt-in fail-loud on an unfilled {{placeholder}} in an agent prompt.

By default an unknown {{placeholder}} is left literal (unchanged — a strict default
would brick the {{feedback}} retry-loop convention, which is absent on the first pass).
With strict_render on, a placeholder
with no value in payload ∪ extras surfaces a `render_unfilled_placeholders` failure
verdict naming the key + stage — the one check that catches the stage-local case
(BUG-697: `{{context}}` exists globally but is unfilled AT premise-check). Engine-
injected keys (tool_manifest) and present-but-empty values never trip it.

Run: cd yaah && PYTHONPATH=src python3 tests/test_strict_render.py
"""
from __future__ import annotations

import asyncio

from yaah.core import Envelope, Kind, NodeConfig, Verdict
from yaah.agents import Agent, FakeBackend

CFG = NodeConfig(model="fake:1")


class _Recording:
    """Backend that captures the rendered prompt so a test can inspect it."""
    def __init__(self) -> None:
        self.prompt = None

    async def complete(self, prompt, *, model=None, **opts):
        self.prompt = prompt
        return "ok"


def _agent(template, *, strict, stage="agent", backend=None):
    return Agent(backend or FakeBackend(responses=["ok"]), template,
                 parse=False, stage=stage, strict_render=strict)


# ---- criterion 1: off by default -> byte-identical to today ------------------

async def default_off_leaves_unknown_literal() -> None:
    be = _Recording()
    a = _agent("hi {{name}} -- {{missing}}", strict=False, backend=be)
    out = await a.invoke(Envelope("task", {"name": "Ada"}), CFG)
    assert out.kind != Kind.VERDICT, out
    assert be.prompt == "hi Ada -- {{missing}}", be.prompt  # literal preserved


# ---- criterion 2: strict + absent key -> fail loud, both placeholder forms ---

async def strict_on_missing_key_fails_loud() -> None:
    a = _agent("draft for {{request}} using {{context}}", strict=True, stage="premise-check")
    out = await a.invoke(Envelope("task", {"request": "x"}), CFG)
    assert out.kind == Kind.VERDICT, out
    v = Verdict.from_envelope(out)
    assert not v.ok
    assert v.failures[0].code == "render_unfilled_placeholders", v.failures
    msg = v.failures[0].message
    assert "context" in msg and "premise-check" in msg, msg


async def strict_on_multiple_missing_keys_named_deduped_in_order() -> None:
    # the failure names EVERY missing key once, in first-seen order (a repeated
    # {{a}} is not listed twice) — the single-loud-failure contract.
    a = _agent("{{a}} {{b}} {{a}} {{c}}", strict=True, stage="draft")
    out = await a.invoke(Envelope("task", {}), CFG)
    assert out.kind == Kind.VERDICT, out
    msg = Verdict.from_envelope(out).failures[0].message
    assert "a, b, c" in msg, msg


async def strict_on_untrusted_form_also_fails() -> None:
    a = _agent("diff: {{!patch}}", strict=True, stage="review")
    out = await a.invoke(Envelope("task", {}), CFG)
    assert out.kind == Kind.VERDICT, out
    v = Verdict.from_envelope(out)
    assert not v.ok and "patch" in v.failures[0].message, v.failures


async def strict_on_tool_manifest_never_trips() -> None:
    a = _agent("tools:\n{{tool_manifest}}\ndo {{task}}", strict=True)
    out = await a.invoke(Envelope("task", {"task": "go"}), CFG)
    assert out.kind != Kind.VERDICT, out
    assert out.payload["raw"] == "ok", out


async def strict_on_feedback_convention_key_never_trips_first_pass() -> None:
    # {{feedback}} is the retry-loop convention key — absent on the FIRST attempt
    # (only set after a reject). strict_render must NOT fail pass 1, else it bricks
    # every feedback loop — the exact agents you'd most want to harden. (eval finding)
    be = _Recording()
    a = _agent("do {{task}}\nprior feedback: {{feedback}}", strict=True, backend=be)
    out = await a.invoke(Envelope("task", {"task": "go"}), CFG)  # no feedback yet
    assert out.kind != Kind.VERDICT, out
    assert "{{feedback}}" in be.prompt, be.prompt  # left literal, did not trip


# ---- criterion 3: present-but-empty is a legit value, not "missing" ----------

async def strict_on_present_but_empty_does_not_trip() -> None:
    be = _Recording()
    a = _agent("ctx=[{{context}}] do {{task}}", strict=True, backend=be)
    out = await a.invoke(Envelope("task", {"context": "", "task": "go"}), CFG)
    assert out.kind != Kind.VERDICT, out
    assert be.prompt == "ctx=[] do go", be.prompt


# ---- happy paths: strict on, everything resolvable ---------------------------

async def strict_on_all_filled_renders() -> None:
    a = _agent("hi {{name}}", strict=True)
    out = await a.invoke(Envelope("task", {"name": "Ada"}), CFG)
    assert out.payload["raw"] == "ok", out


async def strict_extras_key_counts_as_filled() -> None:
    # config.extras participates in the namespace -> a key only in extras isn't missing
    be = _Recording()
    a = _agent("mood={{mood}}", strict=True, backend=be)
    out = await a.invoke(Envelope("task", {}), NodeConfig(model="fake:1", extras={"mood": "calm"}))
    assert out.kind != Kind.VERDICT, out
    assert be.prompt == "mood=calm", be.prompt


async def main() -> None:
    await default_off_leaves_unknown_literal()
    await strict_on_missing_key_fails_loud()
    await strict_on_multiple_missing_keys_named_deduped_in_order()
    await strict_on_untrusted_form_also_fails()
    await strict_on_tool_manifest_never_trips()
    await strict_on_feedback_convention_key_never_trips_first_pass()
    await strict_on_present_but_empty_does_not_trip()
    await strict_on_all_filled_renders()
    await strict_extras_key_counts_as_filled()
    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
