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
from yaah.agents import Agent, FakeProvider

CFG = NodeConfig(model="fake:1")


class _Recording:
    """Backend that captures the rendered prompt so a test can inspect it."""
    def __init__(self) -> None:
        self.prompt = None

    async def complete(self, prompt, *, model=None, **opts):
        self.prompt = prompt
        return "ok"


def _agent(template, *, strict, stage="agent", backend=None):
    return Agent(backend or FakeProvider(responses=["ok"]), template,
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


# ---- optional-placeholder sigil {{?key}} -------------------------------------
# The author marks a placeholder legitimately-absent-on-early-passes with `{{?key}}`
# (declared at the exact use site, domain-free — generalizes the convention beyond
# `loop_feedback`). Under strict_render an absent `{{?key}}` renders empty instead of
# faulting; a genuinely-missing REQUIRED key (bare `{{key}}`) still faults loud.


async def strict_optional_absent_renders_empty_not_fault() -> None:
    # the blessed strict-compatible loop pattern: a produce agent reads
    # `{{?loop_feedback}}`, sets strict_render, and does NOT fault on the fresh
    # first pass (before any tally transform has written the key).
    be = _Recording()
    a = _agent("improve {{task}}\nprior notes: {{?loop_feedback}}", strict=True, backend=be)
    out = await a.invoke(Envelope("task", {"task": "go"}), CFG)  # no loop_feedback yet
    assert out.kind != Kind.VERDICT, out
    assert be.prompt == "improve go\nprior notes: ", be.prompt  # rendered EMPTY, not literal


async def strict_optional_still_faults_on_missing_required() -> None:
    # {{?loop_feedback}} being exempt must NOT exempt a genuinely-missing REQUIRED
    # key: bare {{context}} still faults loud under strict.
    a = _agent("use {{context}} notes: {{?loop_feedback}}", strict=True, stage="produce")
    out = await a.invoke(Envelope("task", {}), CFG)
    assert out.kind == Kind.VERDICT, out
    v = Verdict.from_envelope(out)
    assert not v.ok
    assert v.failures[0].code == "render_unfilled_placeholders", v.failures
    msg = v.failures[0].message
    assert "context" in msg, msg
    assert "loop_feedback" not in msg, msg  # the optional one is NOT reported missing


async def strict_optional_present_value_renders_normally() -> None:
    # loop second pass: the key IS present -> {{?key}} renders its value like {{key}}.
    be = _Recording()
    a = _agent("improve {{task}}\nprior notes: {{?loop_feedback}}", strict=True, backend=be)
    out = await a.invoke(Envelope("task", {"task": "go", "loop_feedback": "tighten it"}), CFG)
    assert out.kind != Kind.VERDICT, out
    assert be.prompt == "improve go\nprior notes: tighten it", be.prompt


async def optional_absent_off_strict_also_renders_empty() -> None:
    # non-strict behavior with {{?key}}: an absent optional renders empty too
    # (the `?` is a render directive, not a strict-only one) — and a bare {{missing}}
    # still stays literal, i.e. the default leave-literal path is unchanged.
    be = _Recording()
    a = _agent("notes: {{?loop_feedback}} tail {{missing}}", strict=False, backend=be)
    out = await a.invoke(Envelope("task", {}), CFG)
    assert out.kind != Kind.VERDICT, out
    assert be.prompt == "notes:  tail {{missing}}", be.prompt


async def optional_untrusted_absent_renders_empty() -> None:
    # {{?!key}} (optional + untrusted) — an absent one renders empty (nothing to
    # fence); a present one is still fenced. Absence must not fault under strict.
    be = _Recording()
    a = _agent("diff: {{?!patch}}", strict=True, backend=be)
    out = await a.invoke(Envelope("task", {}), CFG)
    assert out.kind != Kind.VERDICT, out
    assert be.prompt == "diff: ", be.prompt


async def optional_untrusted_present_value_is_fenced() -> None:
    be = _Recording()
    a = _agent("diff: {{?!patch}}", strict=True, backend=be)
    out = await a.invoke(Envelope("task", {"patch": "rm -rf /"}), CFG)
    assert out.kind != Kind.VERDICT, out
    assert "UNTRUSTED DATA" in be.prompt and "rm -rf /" in be.prompt, be.prompt


async def untrusted_fencing_unaffected_by_optional_change() -> None:
    # regression guard: the plain {{!key}} fencing path is byte-unchanged.
    be = _Recording()
    a = _agent("diff: {{!patch}}", strict=True, backend=be)
    out = await a.invoke(Envelope("task", {"patch": "danger"}), CFG)
    assert out.kind != Kind.VERDICT, out
    assert "UNTRUSTED DATA" in be.prompt and "danger" in be.prompt, be.prompt


# ---- transposed sigil order {{!?key}} == {{?!key}} ---------------------------
# The two markers are set-membership, not a fixed sequence: {{!?key}} must mean
# EXACTLY what {{?!key}} means (optional AND untrusted). The old regex required
# `?` before `!` and left {{!?key}} UNMATCHED — which is the unsafe direction:
# the value would land UNFENCED and strict_render would never record it.


async def transposed_untrusted_present_value_is_fenced_like_canonical() -> None:
    # {{!?patch}} present -> fenced, exactly like {{?!patch}}
    be1, be2 = _Recording(), _Recording()
    a1 = _agent("diff: {{!?patch}}", strict=True, backend=be1)
    a2 = _agent("diff: {{?!patch}}", strict=True, backend=be2)
    p = {"patch": "rm -rf /"}
    out1 = await a1.invoke(Envelope("task", dict(p)), CFG)
    out2 = await a2.invoke(Envelope("task", dict(p)), CFG)
    assert out1.kind != Kind.VERDICT and out2.kind != Kind.VERDICT, (out1, out2)
    assert "UNTRUSTED DATA" in be1.prompt and "rm -rf /" in be1.prompt, be1.prompt
    # identical fenced semantics: both frame the value as untrusted data
    assert ("UNTRUSTED DATA" in be1.prompt) == ("UNTRUSTED DATA" in be2.prompt)


async def transposed_absent_renders_empty_no_fault_under_strict() -> None:
    # {{!?patch}} absent + strict -> renders empty, no fault (optional wins),
    # same as {{?!patch}}. Old regex left {{!?patch}} literal AND unfenced.
    be = _Recording()
    a = _agent("diff: {{!?patch}}", strict=True, backend=be)
    out = await a.invoke(Envelope("task", {}), CFG)
    assert out.kind != Kind.VERDICT, out
    assert be.prompt == "diff: ", be.prompt


async def degenerate_double_untrusted_is_single_sigil_fenced() -> None:
    # {{!!patch}} collapses to a single `!` (untrusted), present -> fenced.
    be = _Recording()
    a = _agent("diff: {{!!patch}}", strict=True, backend=be)
    out = await a.invoke(Envelope("task", {"patch": "danger"}), CFG)
    assert out.kind != Kind.VERDICT, out
    assert "UNTRUSTED DATA" in be.prompt and "danger" in be.prompt, be.prompt


async def degenerate_double_optional_is_single_sigil_optional() -> None:
    # {{??key}} collapses to a single `?` (optional), absent -> empty, no fault.
    be = _Recording()
    a = _agent("notes: {{??loop_feedback}}", strict=True, backend=be)
    out = await a.invoke(Envelope("task", {}), CFG)
    assert out.kind != Kind.VERDICT, out
    assert be.prompt == "notes: ", be.prompt


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
    await strict_optional_absent_renders_empty_not_fault()
    await strict_optional_still_faults_on_missing_required()
    await strict_optional_present_value_renders_normally()
    await optional_absent_off_strict_also_renders_empty()
    await optional_untrusted_absent_renders_empty()
    await optional_untrusted_present_value_is_fenced()
    await untrusted_fencing_unaffected_by_optional_change()
    await transposed_untrusted_present_value_is_fenced_like_canonical()
    await transposed_absent_renders_empty_no_fault_under_strict()
    await degenerate_double_untrusted_is_single_sigil_fenced()
    await degenerate_double_optional_is_single_sigil_optional()
    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
