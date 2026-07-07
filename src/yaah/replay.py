"""yaah.replay — counterfactual replay: run a CHANGED config against a stored
run's completion, zero model calls (the loader half of feasibility note AI-T4).

Used by: `python3 -m yaah.replay <experiment.json> --against <changed-root.json>`
and programmatic callers.
Where: yaah top level — composes ONLY public engine behavior (the experiment row
store, the `fake_scripted` provider seam, run_root). No new engine seam: the
`fake_scripted` slot IS the seam, this is a loader + runner over it.
Why: after you edit a pipeline (tighten a schema, fix a transform, swap a model,
add a downstream stage) you want to know how the change behaves on the outputs
you ALREADY collected — without paying for live model calls again. A replay
injects a stored raw completion through `fake_scripted` and runs the modified
pipeline against it.

VALIDITY ENVELOPE — read before trusting a green replay. A recorded completion
answers "given prompt P, what did the model emit?". Replay is SOUND only when the
modification leaves every model-calling node's rendered prompt bytes UNCHANGED:
schema edits, model/temperature/escalate wiring swaps, downstream topology ADDED
after the recorded node, min_success / retry tuning, and transform/parse code
fixes (run the fixed code against the old completion — replay's best use). It is
STRUCTURALLY INVALID for:

  - PROMPT EDITS — the stored completion was generated for the OLD prompt; the
    question changed. The original prompt bytes are not stored, so a prompt edit
    CANNOT be detected by diffing. The loader refuses unless the caller explicitly
    asserts `assume_prompts_unchanged` — an assertion the caller owns, not a
    default, so the invalid path can't be entered silently.
  - `{{!key}}` UNTRUSTED FENCING — the per-render fence token is random and
    unrecoverable, so even an UNCHANGED config is byte-non-repeatable. Refused by
    scanning inline templates for `{{!` (a KNOWN LIMIT: file-sourced prompts are
    not scanned — the assertion above is the backstop there).

TWO SOURCES — the ROW loader (v1) and the RECORDING loader (v2).

v1 — the SINGLE-model-call run, from the experiment row store. The only place a
completion string is persisted by a campaign is the row's terminal `output.raw`:
the trace layer records tokens/model/stage but NOT completion text, and
intermediate agents' completions are stored nowhere. One row carries ONE
completion. So v1 (`replay_over_rows` / `build_replay_root`) replays a changed
config that makes exactly ONE model call and REFUSES the rest — >1 agent, an
agent_loop, an escalate_model rung, any fork/fanout/foreach. A script silently
returning its default for a 2nd unrecorded call is the wrong-but-green replay
that refusal prevents.

v2 — MULTI-call, from a completion RECORDING (`replay_recording` /
`build_replay_root_from_recording`). A live run with a provider `record_to: <path>`
opt (RecordingProvider, wired in runtime_factories) appends EVERY model call's
completion to a JSONL, keyed by the bare (post-prefix) model name — exactly
ScriptedProvider's `by_model` shape. Grouped per model in file order, that IS a
multi-call replay script, so v2 LIFTS v1's single-call bound for a LINEAR
multi-stage run and for escalate_model (the escalated rung is recorded too).

v2 KEEPS refused — and WHY the refusals are still sound with a recording in hand:
  - fork/fanout/foreach — concurrent arms draw one per-model FIFO out of order;
    the record-time interleaving is asyncio-scheduler-dependent and NOT
    reproducible from a per-model list, so the FIFO is non-deterministic. The
    recorder is topology-BLIND (a leaf decorator), so it records a concurrent run
    faithfully in global `order`; REPLAY is the gate that refuses it.
  - agent_loop, and any PLAIN agent carrying tools/expose/broker — an unknown
    count of TOOL turns whose tool-call STRUCTURE a text-only recording cannot
    reproduce (ScriptedProvider emits text, not calls; a turn-less replay backend
    would draw only the first turn's text and report green).
  - cyclic model stages — the loop count under a changed config is not provably
    the recorded one, so a per-model FIFO can misalign.

v2 SOUNDNESS ENVELOPE (broader `assume_prompts_unchanged` — the caller OWNS it):
because the script keys by MODEL, not by STAGE, the assertion the caller makes
covers not just "prompt bytes unchanged" but "the change does not REORDER, ADD or
REMOVE model-calling stages, nor change WHETHER an escalate_model rung fires."
Two stages sharing one model, reordered by the change, would draw each other's
completions (a FIFO is stage-blind). Guards: a model stage whose model is ABSENT
from the recording is refused up front (a swap to an unrecorded model, or an added
stage — ScriptedProvider serves a SILENT default for an unknown model, so this
MUST be caught before the run); on_exhaustion="raise" turns any over-draw (an
added call, or a partial/failed recording that is missing entries) into a LOUD
failure, never a silent default. What v2 does NOT re-key (unlike single-call v1):
a model SWAP — with multiple calls the stage↔completion mapping is ambiguous, so
recording-replay requires the recorded model name to still be asked for.

PRIVACY: a recording is model OUTPUT on disk (see recording_provider.py) — opt-in,
off by default, and the wiring prints a loud stderr notice. Prompts are not
recorded (only answers), matching the trace layer's no-prompt-text stance.

NOT hermetic beyond the model: NO model calls run, but every OTHER seam of the
changed pipeline runs LIVE, once per replayed row — data sinks, `http:` / `shell`
/ `node:` transforms, mcp. Running the real downstream IS the point (that's what
replay adds over a pure schema rescore), so this is by design, not a leak; just
don't mistake a replay for a sandbox.

Silent-green guard, two layers: the loader refuses when a stored completion is a
substring of the model node's INLINE template (ScriptedProvider's content-cursor
would skip it and serve its silent "" default), AND every replay provider spec
sets `on_exhaustion: "raise"` (factory support in runtime_factories.py), so any
skipped/exhausted draw this static check can't see fails LOUD instead of green.

Targets Python 3.9+.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

_MODEL_NODE_TYPES = ("agent", "agent_loop")
_PARALLEL_KEYS = ("fork", "fanout", "foreach")


class ReplayRefused(ValueError):
    """A replay the loader will not build because it would be unsound, carrying a
    named structural reason: prompt-edit risk unasserted, untrusted fencing, or a
    shape that makes more model calls than the one stored completion can answer."""


def completion_from_row(row: Dict[str, Any], *, raw_key: str = "raw") -> Optional[str]:
    """The stored completion string in a campaign row, or None for a row that
    carries none (suspended/error rows, or a pipeline that relocated it — pass
    `raw_key`). The row's `output` is the run's TERMINAL payload and
    `output[raw_key]` is the terminal agent's raw completion."""
    output = row.get("output")
    if not isinstance(output, dict):
        return None
    raw = output.get(raw_key)
    return raw if isinstance(raw, str) else None


def _resolve_pipeline(root: Dict[str, Any], base: str) -> Dict[str, Any]:
    from .runtime_factories import _read_json, _rel
    ref = root.get("pipeline")
    if isinstance(ref, dict):
        return ref
    if isinstance(ref, str):
        return _read_json(_rel(base, ref))
    raise ReplayRefused(
        "the changed root has no resolvable `pipeline` (got {!r})".format(ref))


def _model_key(ref: str) -> str:
    """The leaf model name the routed pipeline will ASK the fake provider for: the
    part after a `provider:` prefix (RoutingProvider strips it before the leaf sees
    it), or the whole ref when there is no prefix (routed via default_provider
    verbatim). So the fake_scripted `by_model` key matches what arrives at it."""
    name, sep, rest = ref.partition(":")
    return rest if sep else ref


def _scan_fence(role: str, node: Dict[str, Any]) -> None:
    template = node.get("template")
    if isinstance(template, str) and "{{!" in template:
        raise ReplayRefused(
            "node {!r} uses an untrusted-fence placeholder ({{{{!key}}}}) in its "
            "inline template — the per-render fence token is random and "
            "unrecoverable, so a stored completion cannot be replayed against it "
            "(invalid even with NO config change). Refused.".format(role))


def _stage_successors(s: Dict[str, Any]) -> "List[str]":
    """The stages a stage routes to via `then` + `branch` (routes + default). Only
    these matter for loop detection: fork/fanout/foreach are refused wholesale
    before this is consulted, so they never widen the edge set here."""
    nxt: List[str] = []
    if isinstance(s.get("then"), str):
        nxt.append(s["then"])
    b = s.get("branch") or {}
    if isinstance(b, dict):
        nxt.extend(v for v in (b.get("routes") or {}).values() if isinstance(v, str))
        if isinstance(b.get("default"), str):
            nxt.append(b["default"])
    return nxt


def _reaches_itself(stages: Dict[str, Any], start: str) -> bool:
    """Does `start` lie on a cycle — i.e. can the run route back INTO it? A model
    stage revisited this way is a SECOND model call the one stored completion can't
    answer (the fake provider would serve its silent default)."""
    seen: set = set()
    todo = list(_stage_successors(stages.get(start) or {}))
    while todo:
        cur = todo.pop()
        if cur == start:
            return True
        if cur in seen or cur not in stages:
            continue
        seen.add(cur)
        todo.extend(_stage_successors(stages.get(cur) or {}))
    return False


def _single_model_ref(pipeline: Dict[str, Any]) -> "Tuple[str, Dict[str, Any]]":
    """The one model ref the pipeline will call + that node — or a ReplayRefused
    naming the unsound shape. Enforces v1's single-model-call envelope (see module
    docstring): the row store persists only the terminal completion, so any shape
    that would make a 2nd unrecorded call is refused rather than silently answered
    by the fake provider's default. The unit counted is the model-calling STAGE (a
    node reused by two stages is two calls), and a model stage on a cycle is
    refused (a back-edge re-invokes it)."""
    nodes = pipeline.get("nodes") or {}
    stages = (pipeline.get("graph") or {}).get("stages") or {}
    model_node_ids = {rid for rid, n in nodes.items()
                      if isinstance(n, dict) and n.get("type") in _MODEL_NODE_TYPES}
    # STAGES that call a model — a single agent node wired into two stages is two
    # calls, so counting stages (not node definitions) is what bounds the calls.
    model_stages = {sname: s for sname, s in stages.items()
                    if isinstance(s, dict) and s.get("node") in model_node_ids}
    if not model_stages:
        raise ReplayRefused(
            "the changed pipeline has no model-calling stage — there is nothing to "
            "inject a stored completion into")
    if len(model_stages) > 1:
        raise ReplayRefused(
            "v1 replays a SINGLE model call, but {} stages call a model ({}). The "
            "row store persists only the terminal completion, so a fake_scripted "
            "script cannot answer the others — the extra calls would draw its silent "
            "default and produce a wrong-but-green replay. Multi-agent replay needs "
            "a per-call recording (not built).".format(
                len(model_stages), ", ".join(sorted(model_stages))))
    sname, stage = next(iter(model_stages.items()))
    role = stage["node"]
    node = nodes[role]
    if node.get("type") == "agent_loop":
        raise ReplayRefused(
            "node {!r} is an agent_loop — a model-driven tool loop makes an UNKNOWN "
            "number of model calls; one stored completion cannot answer them. "
            "Refused.".format(role))
    if node.get("escalate_model"):
        raise ReplayRefused(
            "node {!r} has escalate_model — the escalation rung is a SECOND model "
            "call whose completion is never persisted, so replay cannot answer it. "
            "Refused.".format(role))
    for name, s in stages.items():
        if isinstance(s, dict):
            shape = [k for k in _PARALLEL_KEYS if k in s]
            if shape:
                raise ReplayRefused(
                    "stage {!r} is a parallel shape ({}) — it can multiply a model "
                    "call into concurrent arms draining one fake_scripted queue out "
                    "of order. v1 refuses any fork/fanout/foreach.".format(
                        name, ", ".join(shape)))
    if _reaches_itself(stages, sname):
        raise ReplayRefused(
            "the model stage {!r} lies on a cycle (a branch/then routes back into "
            "it) — the back-edge re-invokes the model, a 2nd call the one stored "
            "completion can't answer (the fake provider would serve its silent "
            "default). Refused.".format(sname))
    model = node.get("model")
    if not (isinstance(model, str) and model):
        raise ReplayRefused(
            "node {!r} has no explicit `model` — cannot key the replay script".format(role))
    _scan_fence(role, node)
    return model, node


def _replay_model_ref(changed_root: Dict[str, Any], base: str,
                      assume_prompts_unchanged: bool) -> "Tuple[str, Dict[str, Any]]":
    """The soundness gate: the prompt-edit assertion + the single-model-call check,
    returning (model ref, the model node). Shared by `build_replay_root` (per row)
    and `replay_over_rows` (once up front, so an unsound config aborts even with
    zero rows)."""
    if not assume_prompts_unchanged:
        raise ReplayRefused(
            "replay is structurally INVALID for prompt edits: the stored completion "
            "answers the OLD prompt, and the original prompt bytes are not stored to "
            "diff against. Pass assume_prompts_unchanged=True to ASSERT the change "
            "leaves every model-calling node's prompt bytes unchanged (a schema / "
            "model / knob swap, a downstream topology addition, or a transform-code "
            "fix) — an assertion you own, not a default.")
    return _single_model_ref(_resolve_pipeline(changed_root, base))


def build_replay_root(changed_root: Dict[str, Any], base: str, *, completion: Any,
                      assume_prompts_unchanged: bool,
                      input_override: Any = None) -> Dict[str, Any]:
    """Turn the CHANGED root into a runnable replay root: every provider swapped to
    `fake_scripted` carrying `completion` keyed by the pipeline's single model,
    `live_config` and `trace` dropped (a re-read would break determinism; the trace
    cost sink is meaningless with no model calls), `input` optionally overridden
    with the recorded run's input. NO model calls run — but every OTHER seam of the
    changed pipeline runs LIVE, once per row (data sinks, `http:`/`shell`/`node:`
    transforms, mcp): replay runs the REAL downstream, which is the point. Pure —
    the input dict is not mutated. Raises ReplayRefused on any unsound shape (see
    `_single_model_ref`) or an unasserted prompt-edit risk."""
    model_ref, node = _replay_model_ref(changed_root, base, assume_prompts_unchanged)
    if not isinstance(completion, str):
        raise ReplayRefused(
            "no completion to inject (got {!r}) — the source row has no stored raw "
            "output (a suspended/error row, or the raw lives under a different key)"
            .format(completion))
    # ScriptedProvider's content-cursor (scripted_provider.py) advances past a seq
    # entry that appears VERBATIM in the rendered prompt — a resume-durability
    # heuristic that, on a single-entry replay script, SKIPS the one completion and
    # serves the silent "" default. Two-layer guard: refuse here when the completion
    # is a substring of the model node's inline template (the statically-checkable
    # half), AND the provider specs below set on_exhaustion="raise" so ANY skipped/
    # exhausted draw — including a completion that only matches AFTER payload
    # substitution, or a file-sourced prompt this static check can't see — fails
    # LOUD instead of serving a wrong-but-green "".
    template = node.get("template")
    if completion and isinstance(template, str) and completion in template:
        raise ReplayRefused(
            "the stored completion appears verbatim in the model node's template, so "
            "the ScriptedProvider content-cursor would treat it as already-seen and "
            "drop it (serving its silent default) — a wrong-but-green replay. This is "
            "the short-classifier case; a per-call recording is the real fix. Refused.")
    script = {_model_key(model_ref): [completion]}
    # on_exhaustion="raise": a valid single-call replay draws its one entry cleanly
    # and never trips this; only the unrecorded-2nd-draw / skipped-entry cases the
    # module exists to prevent do — the anti-silent-green guard.
    _fake = {"type": "fake_scripted", "by_model": script, "on_exhaustion": "raise"}

    replay = dict(changed_root)
    replay.pop("live_config", None)
    replay.pop("trace", None)
    providers = changed_root.get("providers")
    if isinstance(providers, dict) and providers:
        replay["providers"] = {name: dict(_fake) for name in providers}
    else:
        replay["providers"] = {"replay": dict(_fake)}
        replay["default_provider"] = "replay"
    replay["run"] = True
    if input_override is not None:
        replay["input"] = input_override
    return replay


# --- v2: the completion-RECORDING source (multi-call) -----------------------

def load_recording(path: str) -> "List[Dict[str, Any]]":
    """Read a completion recording (the JSONL RecordingProvider writes) into a list
    of {order, model, completion} dicts in FILE order. Loud on a missing file, a
    corrupt line, or a record missing a string `model`/`completion`: a silently
    partial recording is exactly what makes a multi-call replay wrong-but-green, so
    it must fail here, not draw a wrong completion at run time."""
    if not os.path.exists(path):
        raise ValueError("recording file not found: {}".format(path))
    records: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError("recording {}: line {} is not JSON — {}".format(
                    path, lineno, e.msg)) from None
            if not (isinstance(rec, dict) and isinstance(rec.get("model"), str)
                    and isinstance(rec.get("completion"), str)):
                raise ValueError(
                    "recording {}: line {} must be an object with string 'model' and "
                    "'completion' (got {!r})".format(path, lineno, rec))
            records.append(rec)
    if not records:
        raise ValueError("recording {} is empty — nothing to replay".format(path))
    return records


def by_model_from_recording(records: "List[Dict[str, Any]]") -> Dict[str, "List[str]"]:
    """Group recorded completions by (bare) model name, preserving file order —
    exactly ScriptedProvider's `by_model` shape (per-model FIFO). File order is call
    order for a LINEAR run (calls are sequential), which is what makes the FIFO
    sound; a concurrent run's order is scheduler-dependent, so `_recorded_model_gate`
    refuses concurrent topologies even with a recording present."""
    by_model: Dict[str, List[str]] = {}
    for rec in records:
        by_model.setdefault(rec["model"], []).append(rec["completion"])
    return by_model


def _recorded_model_gate(pipeline: Dict[str, Any]) -> "List[Tuple[str, Dict[str, Any]]]":
    """The model stages a recorded multi-call replay may script — [(model_ref, node),
    ...] — or a ReplayRefused naming a shape a per-model FIFO recording can't soundly
    reproduce. LIFTS v1's single-call bound for LINEAR multi-stage and escalate_model
    (NOT refused here); KEEPS refused fork/fanout/foreach, agent_loop and cycles (see
    the module docstring's v2 section for why each stays unsound with a recording)."""
    nodes = pipeline.get("nodes") or {}
    stages = (pipeline.get("graph") or {}).get("stages") or {}
    model_node_ids = {rid for rid, n in nodes.items()
                      if isinstance(n, dict) and n.get("type") in _MODEL_NODE_TYPES}
    model_stages = {sname: s for sname, s in stages.items()
                    if isinstance(s, dict) and s.get("node") in model_node_ids}
    if not model_stages:
        raise ReplayRefused(
            "the changed pipeline has no model-calling stage — nothing to inject a "
            "recording into")
    for name, s in stages.items():
        if isinstance(s, dict):
            shape = [k for k in _PARALLEL_KEYS if k in s]
            if shape:
                raise ReplayRefused(
                    "stage {!r} is a parallel shape ({}) — concurrent arms draw one "
                    "per-model FIFO out of order, so the record-time interleaving is "
                    "not reproducible. Refused (concurrent stays refused even WITH a "
                    "recording).".format(name, ", ".join(shape)))
    out: List[Tuple[str, Dict[str, Any]]] = []
    for sname, stage in model_stages.items():
        role = stage["node"]
        node = nodes[role]
        if node.get("type") == "agent_loop":
            raise ReplayRefused(
                "node {!r} is an agent_loop — a model-driven TOOL loop makes an unknown "
                "number of calls whose tool-call structure a text-only recording cannot "
                "reproduce (a script emits text, not tool calls). Refused.".format(role))
        # A PLAIN agent with tools/expose/broker drives the SAME engine tool loop
        # when the live backend is turn-capable: one recorded completion PER TURN.
        # The replay backend (ScriptedProvider) has no turn(), so at replay the
        # agent takes the plain path, draws only the FIRST turn's text, leaves the
        # rest of the seq UNDRAWN (on_exhaustion catches over-draws, not under-
        # draws) and reports green with an intermediate completion as the answer.
        if node.get("tools") or node.get("expose") or node.get("broker"):
            raise ReplayRefused(
                "node {!r} carries tools/expose/broker — on a turn-capable live "
                "backend these drive a tool loop (one recorded completion per turn), "
                "which a text-only replay script cannot reproduce: the replay backend "
                "has no turn(), so only the first turn's text would be drawn — a "
                "wrong-but-green replay. Refused.".format(role))
        if _reaches_itself(stages, sname):
            raise ReplayRefused(
                "the model stage {!r} lies on a cycle — the loop count under a changed "
                "config is not provably the recorded one, so a per-model FIFO can "
                "misalign. Refused.".format(sname))
        model = node.get("model")
        if not (isinstance(model, str) and model):
            raise ReplayRefused(
                "node {!r} has no explicit `model` — cannot key the replay script".format(role))
        _scan_fence(role, node)
        out.append((model, node))
    return out


def _check_escalate_coverage(model_ref: str, node: Dict[str, Any],
                             by_model: Dict[str, "List[str]"]) -> None:
    """Refuse when a node's escalate_model rung WOULD fire at replay but its model
    has no recorded completions — ScriptedProvider serves a SILENT default for an
    unknown model, uncatchable by on_exhaustion (eval finding #2: the primary may be
    recorded on one provider while the escalation target sits on a provider without
    `record_to`). The rung fires iff a parsed primary completion carries truthy
    `help` (agent.py's M7 ladder), so mirror that parse (`extract_json` with the
    schema's `required` keys) over the primary's recorded completions: no help
    anywhere -> the rung provably never fires under the prompts-unchanged assertion
    and an uncovered escalate_model is fine (the common case — escalation is rare).
    CONSERVATIVE by design: a help-carrying completion that the schema gate would
    reject at runtime still counts as help here (over-refusal, never silent-green)."""
    esc = node.get("escalate_model")
    if not (isinstance(esc, str) and esc) or node.get("parse") is False:
        return   # no rung wired, or parse off -> the help ladder never runs
    esc_key = _model_key(esc)
    if esc_key in by_model:
        return
    from .jsonio import extract_json
    required = (node.get("output_schema") or {}).get("required") or None
    for comp in by_model.get(_model_key(model_ref)) or []:
        try:
            parsed = extract_json(comp, keys=required)
        except Exception:
            continue   # unparseable -> the runtime ladder can't read `help` either
        if isinstance(parsed, dict) and parsed.get("help"):
            raise ReplayRefused(
                "a recorded completion for model {!r} carries truthy `help`, so the "
                "escalate_model rung ({!r}) WOULD fire at replay — but model {!r} has "
                "no entries in the recording (was its provider missing `record_to`?). "
                "ScriptedProvider would serve a silent default for it. Refused; "
                "re-record with `record_to` on every provider.".format(
                    _model_key(model_ref), esc, esc_key))


def build_replay_root_from_recording(changed_root: Dict[str, Any], base: str, *,
                                     by_model: Dict[str, "List[str]"],
                                     assume_prompts_unchanged: bool,
                                     input_override: Any = None) -> Dict[str, Any]:
    """Turn the CHANGED root into a runnable MULTI-call replay root: every provider
    swapped to a `fake_scripted` carrying the whole `by_model` recording (per-model
    FIFO), `on_exhaustion="raise"`, `live_config`/`trace` dropped. Pure — inputs are
    not mutated. Raises ReplayRefused on an unsound shape (`_recorded_model_gate`),
    an unasserted prompt/sequence edit, a model stage whose model is ABSENT from the
    recording (would draw ScriptedProvider's SILENT default), or a recorded completion
    that appears verbatim in its stage's template (content-cursor would skip it)."""
    if not assume_prompts_unchanged:
        raise ReplayRefused(
            "recording replay is structurally INVALID for a prompt OR model-call-"
            "SEQUENCE edit: the recording is keyed by model, in call order, so a change "
            "that edits a prompt, or REORDERS / ADDS / REMOVES model-calling stages, or "
            "changes WHETHER an escalate_model rung fires, draws the wrong completion. "
            "Pass assume_prompts_unchanged=True to ASSERT the change leaves every "
            "model-calling node's prompt bytes AND the per-model call sequence unchanged "
            "(a schema / knob swap, or a downstream-of-the-model topology addition) — an "
            "assertion you own, not a default.")
    pipeline = _resolve_pipeline(changed_root, base)
    model_stages = _recorded_model_gate(pipeline)
    for model_ref, node in model_stages:
        key = _model_key(model_ref)
        if key not in by_model:
            raise ReplayRefused(
                "model {!r} (node model {!r}) has NO entries in the recording — the "
                "recording does not cover this config (a model swapped to an unrecorded "
                "model, or a stage added). Refused rather than serving a silent empty "
                "completion (ScriptedProvider returns its default for an unknown model, "
                "which on_exhaustion cannot catch).".format(key, model_ref))
        _check_escalate_coverage(model_ref, node, by_model)
        template = node.get("template")
        if isinstance(template, str):
            for comp in by_model[key]:
                if comp and comp in template:
                    raise ReplayRefused(
                        "a recorded completion for model {!r} appears verbatim in node "
                        "{!r}'s template — ScriptedProvider's content-cursor would treat "
                        "it as already-seen and skip it (silent default). Refused.".format(
                            key, node.get("stage") or model_ref))
    script = {m: list(c) for m, c in by_model.items()}
    _fake = {"type": "fake_scripted", "by_model": script, "on_exhaustion": "raise"}

    replay = dict(changed_root)
    replay.pop("live_config", None)
    replay.pop("trace", None)
    providers = changed_root.get("providers")
    if isinstance(providers, dict) and providers:
        replay["providers"] = {name: dict(_fake) for name in providers}
    else:
        replay["providers"] = {"replay": dict(_fake)}
        replay["default_provider"] = "replay"
    replay["run"] = True
    if input_override is not None:
        replay["input"] = input_override
    return replay


async def replay_recording(changed_root_path: str, recording_path: str, *,
                           assume_prompts_unchanged: bool,
                           input_override: Any = None) -> Dict[str, Any]:
    """Replay a completion RECORDING through the CHANGED root at `changed_root_path`
    — ONE run, zero model calls, every recorded call scripted back in per-model order.
    Returns {recording, calls, done, outcome, payload, ...}. An unsound changed config
    (or a recording that doesn't cover it) aborts LOUD before the run (ReplayRefused).
    The recording is from ONE live run, so the changed root's own `input` stands unless
    `input_override` is given — it MUST match the input that produced the recording
    (part of the prompt), which the assume_prompts_unchanged assertion covers."""
    from .runtime import run_root
    from .runtime_factories import _read_json
    from .validate import validate_config
    from .harness import Done, StageFailed, Suspended
    from .plugins import load_plugins

    records = load_recording(recording_path)
    by_model = by_model_from_recording(records)
    changed_root = _read_json(changed_root_path)
    vbase = os.path.dirname(os.path.abspath(changed_root_path))
    if vbase not in sys.path:
        sys.path.insert(0, vbase)   # fn:/plugins beside the changed config resolve
    load_plugins(changed_root.get("plugins"), vbase)
    replay_root = build_replay_root_from_recording(
        changed_root, vbase, by_model=by_model,
        assume_prompts_unchanged=assume_prompts_unchanged, input_override=input_override)
    validate_config(replay_root, vbase)
    rec: Dict[str, Any] = {"recording": recording_path, "calls": len(records)}
    try:
        out = await run_root(replay_root, vbase)
    except StageFailed as e:
        rec.update({"done": False, "outcome": "failed", "payload": {},
                    "failure": [f.code for f in e.verdict.failures]})
        return rec
    if isinstance(out, Done):
        rec.update({"done": True, "outcome": "done", "payload": dict(out.output.payload)})
    elif isinstance(out, Suspended):
        rec.update({"done": False, "outcome": "suspended", "payload": {},
                    "awaiting": out.awaiting})
    else:
        rec.update({"done": False, "payload": {},
                    "outcome": type(out).__name__.lower() if out is not None else "none"})
    return rec


def _check_source_variants(exp_cfg: Dict[str, Any], base: str,
                           variant: Optional[str]) -> None:
    """Refuse if any SOURCE campaign variant whose rows we'll replay is not itself a
    single-model-call pipeline — its terminal `output.raw` would then be some OTHER
    node's completion, injected with the wrong provenance (eval #1). Reuses the same
    single-model-call check as the changed side. Structural read only (no plugins /
    validation): an unreadable variant is left for the store/run path to surface."""
    from .runtime_factories import _read_json, _rel
    variants = exp_cfg.get("variants") or {}
    for name, path in variants.items():
        if variant is not None and name != variant:
            continue
        if not isinstance(path, str):
            continue
        try:
            root = _read_json(_rel(base, path))
            pipeline = _resolve_pipeline(root, os.path.dirname(_rel(base, path)))
        except (OSError, ValueError):
            continue   # the run/store path owns a broken variant; don't double-fault
        try:
            _single_model_ref(pipeline)
        except ReplayRefused as e:
            raise ReplayRefused(
                "source campaign variant {!r} is not single-model-call, so its rows' "
                "terminal output.raw is not the completion of the node you're "
                "replaying into (wrong provenance): {}".format(name, e)) from e


async def replay_over_rows(exp_cfg: Dict[str, Any], base: str, changed_root_path: str, *,
                           assume_prompts_unchanged: bool,
                           variant: Optional[str] = None, raw_key: str = "raw",
                           store: Any = None) -> Dict[str, Any]:
    """Replay every stored completion in `exp_cfg`'s rows through the CHANGED root at
    `changed_root_path`, running each with the recorded run's input and zero model
    calls. Returns {experiment, replayed, results:[{input_id, done, outcome,
    payload, ...}]}. Rows with no stored completion (suspended/error) are skipped.
    An unsound changed config aborts loud BEFORE any run (ReplayRefused)."""
    from .runtime import run_root
    from .runtime_factories import _read_json, _rel
    from .validate import validate_config
    from .harness import Done, StageFailed, Suspended
    from .experiment.runner import _check_experiment
    from .experiment.store_factory import opened_store
    from .plugins import load_plugins

    _check_experiment(exp_cfg)
    exp_id = exp_cfg["id"]
    changed_root = _read_json(changed_root_path)
    vbase = os.path.dirname(os.path.abspath(changed_root_path))
    if vbase not in sys.path:
        sys.path.insert(0, vbase)   # fn:/plugins beside the changed config resolve, as the CLI does
    load_plugins(changed_root.get("plugins"), vbase)
    _replay_model_ref(changed_root, vbase, assume_prompts_unchanged)   # early abort on an unsound changed config
    # PROVENANCE (eval #1): a row's `output.raw` is the run's TERMINAL payload — the
    # LAST agent's completion. If a SOURCE variant was multi-model, that raw belongs
    # to a different node/prompt than the one we inject it into. So the source side
    # must ALSO be single-model-call, or the completion's provenance is wrong.
    _check_source_variants(exp_cfg, base, variant)

    input_by_id: Dict[str, Any] = {}
    for i, inp in enumerate(exp_cfg["inputs"]):
        if isinstance(inp, str):
            input_by_id[inp] = _rel(base, inp)              # fixture path, experiment-relative
        else:
            input_by_id["inline-{}".format(i)] = inp        # inline payload, matches the runner's id

    async with opened_store(exp_cfg, base, store) as st:
        rows = await st.rows(exp_id)

    results: List[Dict[str, Any]] = []
    for row in rows:
        if variant is not None and row.get("variant") != variant:
            continue
        completion = completion_from_row(row, raw_key=raw_key)
        if completion is None:
            continue
        replay_root = build_replay_root(
            changed_root, vbase, completion=completion,
            assume_prompts_unchanged=assume_prompts_unchanged,
            input_override=input_by_id.get(str(row.get("input_id") or "")))
        validate_config(replay_root, vbase)
        rec: Dict[str, Any] = {"input_id": row.get("input_id"), "variant": row.get("variant")}
        try:
            out = await run_root(replay_root, vbase)
        except StageFailed as e:
            rec.update({"done": False, "outcome": "failed", "payload": {},
                        "failure": [f.code for f in e.verdict.failures]})
            results.append(rec)
            continue
        if isinstance(out, Done):
            rec.update({"done": True, "outcome": "done",
                        "payload": dict(out.output.payload)})
        elif isinstance(out, Suspended):
            rec.update({"done": False, "outcome": "suspended", "payload": {},
                        "awaiting": out.awaiting})
        else:
            rec.update({"done": False, "outcome": type(out).__name__.lower(), "payload": {}})
        results.append(rec)
    return {"experiment": exp_id, "replayed": len(results), "results": results}


def main(argv: Optional[List[str]] = None) -> int:
    """CLI, two sources:
      ROW loader (v1):    python3 -m yaah.replay <experiment.json> --against ROOT ...
      RECORDING (v2):     python3 -m yaah.replay --recording <rec.jsonl> --against ROOT ...
    Self-contained (no cli.py edit); a `yaah replay` subcommand would be a one-line
    dispatch. Read the validity envelope in the module docstring: valid for
    schema/model/knob swaps and downstream additions; INVALID for prompt edits,
    {{!key}} fencing, and (v2) any change to the per-model call SEQUENCE."""
    import argparse
    import asyncio
    from .runtime_factories import _read_json

    p = argparse.ArgumentParser(
        prog="python3 -m yaah.replay",
        description="Replay stored completions through a CHANGED config with zero "
                    "model calls — from a campaign's rows (single call) or a "
                    "completion RECORDING (multi-call: linear multi-stage + "
                    "escalate_model; concurrent/agent_loop/cyclic stay refused).")
    p.add_argument("experiment", nargs="?",
                   help="the experiment JSON (row source) — omit when --recording is used")
    p.add_argument("--recording", metavar="JSONL", default=None,
                   help="a completion recording (RecordingProvider `record_to` output) "
                        "— the MULTI-call source; mutually exclusive with `experiment`")
    p.add_argument("--against", required=True, metavar="ROOT",
                   help="the CHANGED root config to replay stored completions through")
    p.add_argument("--assume-prompts-unchanged", action="store_true",
                   help="ASSERT the change leaves every model-calling node's prompt "
                        "bytes unchanged (and, for --recording, the per-model call "
                        "sequence) — required (replay is invalid for prompt edits)")
    p.add_argument("--variant", default=None, help="only replay rows from this variant")
    p.add_argument("--raw-key", default="raw",
                   help="payload key holding the raw completion (default: raw)")
    args = p.parse_args(argv)

    if bool(args.recording) == bool(args.experiment):
        p.error("give exactly one source: an <experiment> (rows) OR --recording (JSONL)")

    try:
        if args.recording:
            summary = asyncio.run(replay_recording(
                os.path.abspath(args.against), os.path.abspath(args.recording),
                assume_prompts_unchanged=args.assume_prompts_unchanged))
        else:
            base = os.path.dirname(os.path.abspath(args.experiment))
            cfg = _read_json(args.experiment)
            summary = asyncio.run(replay_over_rows(
                cfg, base, os.path.abspath(args.against),
                assume_prompts_unchanged=args.assume_prompts_unchanged,
                variant=args.variant, raw_key=args.raw_key))
    except ReplayRefused as e:
        print("replay refused: {}".format(e), file=sys.stderr)
        return 2
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
