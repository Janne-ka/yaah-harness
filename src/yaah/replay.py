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

v1 SCOPE — the SINGLE-model-call run. The only place a completion string is
persisted today is the experiment row store's terminal `output.raw`: the trace
layer records tokens/model/stage but NOT completion text, and intermediate
agents' completions are stored nowhere. One row therefore carries ONE completion.
So v1 replays a changed config that makes exactly one model call and REFUSES the
rest — more than one agent, an agent_loop (unbounded tool turns), an
escalate_model rung (an unrecorded 2nd call), or any fork/fanout/foreach (arms
draining one fake_scripted queue out of order). A script silently returning its
default for a 2nd unrecorded call is exactly the wrong-but-green replay this
refusal exists to prevent. Multi-agent replay needs a per-call completion
RECORDING (a wrapping provider or a trace-text capture) — out of v1 scope.

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
    """CLI: `python3 -m yaah.replay <experiment.json> --against <changed-root.json>
    --assume-prompts-unchanged`. Self-contained (no cli.py edit); a `yaah replay`
    subcommand would be a one-line dispatch to `replay_over_rows` when wanted."""
    import argparse
    import asyncio
    from .runtime_factories import _read_json

    p = argparse.ArgumentParser(
        prog="python3 -m yaah.replay",
        description="Replay a campaign's stored completions through a CHANGED config "
                    "with zero model calls. Read the validity envelope in the module "
                    "docstring: valid for schema/model/knob swaps, downstream "
                    "additions and transform fixes; INVALID for prompt edits and "
                    "{{!key}} fencing.")
    p.add_argument("experiment", help="the experiment JSON (its rows + inputs)")
    p.add_argument("--against", required=True, metavar="ROOT",
                   help="the CHANGED root config to replay stored completions through")
    p.add_argument("--assume-prompts-unchanged", action="store_true",
                   help="ASSERT the change leaves every model-calling node's prompt "
                        "bytes unchanged — required (replay is invalid for prompt edits)")
    p.add_argument("--variant", default=None, help="only replay rows from this variant")
    p.add_argument("--raw-key", default="raw",
                   help="payload key holding the raw completion (default: raw)")
    args = p.parse_args(argv)

    base = os.path.dirname(os.path.abspath(args.experiment))
    cfg = _read_json(args.experiment)
    try:
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
