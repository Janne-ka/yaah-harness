"""authoring_eval — INTERNAL eval: can a cold LLM author a valid yaah config
from `yaah manual` alone, via the generate -> validate -> repair loop?

Used by: maintainers measuring the manual's authoring affordance (and CI,
which runs the scripted mode to prove the LOOP mechanics without any model).
Where: a sibling of `scripts/check_docs.py` — same stance (validate against
the real engine entry points), opposite direction: check_docs keeps the DOCS
honest against the validator; this measures whether the docs' one generated
manual is ENOUGH for a model to author configs the validator accepts.
Why this shape: the harness is deterministic around a nondeterministic model.
The author model sits behind ONE callable seam (`AuthorFn`); everything else
— prompt construction, tolerant JSON extraction, validation, diagnostics
feedback, scoring, the JSONL report — is plain deterministic code, so the
loop itself is testable offline (tests/test_authoring_eval.py) and a real
run differs ONLY in who answers the prompt.

This is an internal eval with published methodology (docs/authoring-eval.md),
NOT a public benchmark — the task set is small, self-chosen, and domain-free
by construction.

The loop, per task (max_rounds author calls):
    prompt(manual + task [+ previous reply + diagnostics])
      -> author reply -> extract_json (fence-tolerant)
      -> validate_config (the SAME check behind `yaah validate --json`)
      -> valid: record warnings, stop | invalid: split_diagnostics -> repair round

Modes:
    scripted (default)  — ScriptedAuthor double, canned replies; one task's
                          first draft is deliberately invalid so CI proves the
                          diagnostics actually drive a repair.
    real                — env YAAH_AUTHORING_EVAL_MODEL="<provider>:<model>"
                          (claude_cli / claude-cli / litellm) calls the model
                          through the engine's own provider adapters.

Run:  PYTHONPATH=src python3 scripts/authoring_eval.py [--out results.jsonl]
      YAAH_AUTHORING_EVAL_MODEL=claude_cli: PYTHONPATH=src python3 scripts/authoring_eval.py

Exit code: 0 when every task landed valid, 1 otherwise (CI signal in scripted
mode; in real mode it just mirrors "validity rate < 100%").

Targets Python 3.9+.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from yaah.jsonio import extract_json                      # noqa: E402
from yaah.manual import build_manual                      # noqa: E402
from yaah.validate import split_diagnostics, validate_config  # noqa: E402

DEFAULT_MAX_ROUNDS = 4   # 1 cold draft + up to 3 repair rounds
MODEL_ENV = "YAAH_AUTHORING_EVAL_MODEL"


# --- Task set ----------------------------------------------------------------
# Domain-free by construction (the engine invariant): each description names a
# TOPOLOGY the manual teaches, never an application domain. Written for the
# real-model mode; the scripted double keys on `id` and ignores the text.

class Task(NamedTuple):
    id: str
    description: str


TASKS: List[Task] = [
    Task("summarize-and-render",
         "A pipeline that sends the input text to one agent which replies with "
         "JSON {\"summary\": ...}, then renders 'Summary: <summary>' to a file "
         "summary.txt."),
    Task("gate-branch",
         "A pipeline where an agent drafts JSON {\"summary\": ...} from the "
         "input text, then a human gate asks for review and branches on the "
         "human's `decision`: \"revise\" loops back to the draft stage, anything "
         "else proceeds to a render stage that writes the summary to a file."),
    Task("fork-fanin",
         "A pipeline that forks the input text to two agents in parallel (one "
         "returns JSON {\"security\": ...}, the other {\"style\": ...}), joins "
         "both results with a fanin, then renders both fields to one file."),
    Task("retry-validator",
         "A pipeline where an agent must reply with JSON containing an `items` "
         "key; a json_object validator enforces that, with up to 3 attempts and "
         "validation feedback sent back to the agent; on success a render stage "
         "writes the items to a file."),
    Task("verdict-branch",
         "A pipeline with a drafting agent (returns JSON {\"draft\": ...}) and a "
         "reviewing agent (returns JSON {\"verdict\": \"pass\"|\"fail\"}); the "
         "review stage branches on `verdict`: \"pass\" goes to a render stage, "
         "any other verdict loops back to the draft stage."),
    Task("shell-then-summarize",
         "A pipeline that first runs a fixed shell command (e.g. echo ok), then "
         "an agent summarizes the command's stdout as JSON {\"summary\": ...}, "
         "then a render stage writes the summary to a file."),
]

# the one task whose canned first draft is deliberately broken (see
# default_scripted_author) — named so tests and docs point at the same string
REPAIR_TASK_ID = "retry-validator"


# --- The author seam ----------------------------------------------------------

@dataclass(frozen=True)
class AuthorRequest:
    """Everything one author call sees. `prompt` is the full text a real model
    receives (the harness builds it — prompt construction is deterministic and
    lives HERE, not in the author); the structured fields let a double key on
    the task and the tests assert what the loop fed back."""
    task_id: str
    description: str
    round: int                                   # 0 = cold draft, 1+ = repair
    prompt: str
    previous_reply: Optional[str]                # raw text of the last reply
    diagnostics: Optional[List[Dict[str, Any]]]  # split_diagnostics output


AuthorFn = Callable[[AuthorRequest], str]


def build_prompt(manual: str, task: Task,
                 previous_reply: Optional[str] = None,
                 diagnostics: Optional[List[Dict[str, Any]]] = None) -> str:
    """The whole context a cold model gets: the generated manual, the task,
    the output contract — and on repair rounds, its own previous reply plus
    the validator's diagnostics (the same items `yaah validate --json` emits)."""
    parts = [
        "You are authoring a configuration for the yaah pipeline runtime.",
        "The complete reference manual follows; it is ALL you have — do not",
        "assume any key or type it does not list.",
        "",
        manual,
        "",
        "## Your task",
        "",
        task.description,
        "",
        "## Output contract",
        "",
        "Reply with EXACTLY ONE JSON object and nothing else: a ROOT config",
        "with the pipeline INLINED as an object under its \"pipeline\" key.",
        "Inline everything (agent `template` or a static prompt source,",
        "render `template_text`); never reference external files. Make it",
        "offline-runnable: declare a fake provider and point every agent's",
        "`model` at it.",
    ]
    if diagnostics is not None:
        parts += [
            "",
            "## Your previous reply",
            "",
            previous_reply or "",
            "",
            "## Validator diagnostics (yaah validate --json)",
            "",
            json.dumps(diagnostics, indent=1),
            "",
            "Fix ALL diagnostics and reply again with the complete corrected",
            "JSON object only.",
        ]
    return "\n".join(parts)


# --- The loop -----------------------------------------------------------------

def run_task(task: Task, author: AuthorFn, manual: str, *,
             max_rounds: int = DEFAULT_MAX_ROUNDS,
             base_path: Optional[str] = None) -> Dict[str, Any]:
    """Drive one task through generate -> validate -> repair. Returns the row:

        task              task id
        mode              filled in by run_eval
        valid             did any round produce a config validate_config accepts
        rounds            author calls made (valid on the first call -> 1)
        round_details     [{round, parse_ok, errors, prompt_chars, reply_chars}]
        warnings          lint warnings on the accepted config ([] until valid)
        final_diagnostics last round's diagnostics when the task never landed

    Diagnostics fed back are exactly `split_diagnostics` of the validate error
    — errors drive repair; lint WARNINGS are recorded but not fed back (the
    eval measures validity, not lint-cleanliness). A reply that isn't JSON is
    itself a diagnostic, on the same channel, so a chatty model gets one more
    chance instead of crashing the harness. NOTE: validate_config raises per
    LAYER (root first, then pipeline), so a draft broken in both can honestly
    cost two repair rounds."""
    base = base_path or tempfile.mkdtemp(prefix="authoring-eval-")
    row: Dict[str, Any] = {"task": task.id, "mode": "", "valid": False,
                           "rounds": 0, "round_details": [],
                           "warnings": [], "final_diagnostics": []}
    previous_reply: Optional[str] = None
    diagnostics: Optional[List[Dict[str, Any]]] = None

    for round_i in range(max_rounds):
        prompt = build_prompt(manual, task, previous_reply, diagnostics)
        request = AuthorRequest(task_id=task.id, description=task.description,
                                round=round_i, prompt=prompt,
                                previous_reply=previous_reply,
                                diagnostics=diagnostics)
        reply = author(request)
        row["rounds"] = round_i + 1
        detail = {"round": round_i, "parse_ok": False, "errors": 0,
                  "prompt_chars": len(prompt), "reply_chars": len(reply)}
        row["round_details"].append(detail)
        previous_reply = reply

        # parse_msg is rebuilt EVERY round: extract_json returns None for a
        # bare `null` reply WITHOUT raising (null is legitimate JSON), so the
        # non-dict message must not depend on the except branch having run —
        # and must never be a stale message from an earlier round.
        parse_msg: Optional[str] = None
        config: Any = None
        try:
            config = extract_json(reply)
        except json.JSONDecodeError as e:
            parse_msg = "reply did not contain parseable JSON: {}".format(e)
        if not isinstance(config, dict):
            if parse_msg is None:
                parse_msg = ("reply parsed as JSON but not as an OBJECT "
                             "(got {})".format(type(config).__name__))
            diagnostics = [{"message": parse_msg}]
            detail["errors"] = 1
            row["final_diagnostics"] = diagnostics
            continue
        detail["parse_ok"] = True

        # The eval's own output contract, enforced by the HARNESS: validate
        # accepts a pipeline-less root (`{}` is a valid root config — nothing
        # to check), so without this a trivial reply would score as authored.
        if not isinstance(config.get("pipeline"), dict):
            diagnostics = [{"message": "config carries no inline pipeline object "
                                       "under the \"pipeline\" key — the eval's "
                                       "output contract requires one"}]
            detail["errors"] = 1
            row["final_diagnostics"] = diagnostics
            continue

        try:
            warnings = validate_config(config, base)
        except ValueError as e:
            diagnostics = split_diagnostics(str(e))
            detail["errors"] = len(diagnostics)
            row["final_diagnostics"] = diagnostics
            continue

        row["valid"] = True
        row["warnings"] = warnings
        row["final_diagnostics"] = []
        break
    return row


def run_eval(author: AuthorFn, *, mode: str, out_path: str,
             tasks: Optional[List[Task]] = None,
             max_rounds: int = DEFAULT_MAX_ROUNDS,
             manual: Optional[str] = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Run every task, write one JSONL row per task to `out_path`, print the
    summary table, return (rows, summary). Summary fields: tasks, valid,
    validity_rate, mean_rounds_to_valid (None when nothing landed valid)."""
    manual = manual if manual is not None else build_manual()
    tasks = tasks if tasks is not None else TASKS
    base = tempfile.mkdtemp(prefix="authoring-eval-")

    # rows are written INCREMENTALLY (one line per finished task, flushed):
    # real mode pays per model reply, so a provider crash on task N must not
    # discard the N-1 rows already earned
    rows: List[Dict[str, Any]] = []
    with open(out_path, "w", encoding="utf-8") as f:
        for task in tasks:
            row = run_task(task, author, manual, max_rounds=max_rounds, base_path=base)
            row["mode"] = mode
            rows.append(row)
            f.write(json.dumps(row, sort_keys=True) + "\n")
            f.flush()

    valid_rows = [r for r in rows if r["valid"]]
    summary: Dict[str, Any] = {
        "tasks": len(rows),
        "valid": len(valid_rows),
        "validity_rate": (len(valid_rows) / len(rows)) if rows else 0.0,
        "mean_rounds_to_valid": (sum(r["rounds"] for r in valid_rows) / len(valid_rows)
                                 if valid_rows else None),
    }
    _print_summary(rows, summary, mode=mode, out_path=out_path)
    return rows, summary


def _print_summary(rows: List[Dict[str, Any]], summary: Dict[str, Any], *,
                   mode: str, out_path: str) -> None:
    width = max([len(r["task"]) for r in rows] + [4])
    print("authoring eval — mode: {}".format(mode))
    print("{:<{w}}  {:>5}  {:>6}  errors/round".format("task", "valid", "rounds", w=width))
    for r in rows:
        errs = ",".join(str(d["errors"]) for d in r["round_details"])
        print("{:<{w}}  {:>5}  {:>6}  {}".format(
            r["task"], "yes" if r["valid"] else "NO", r["rounds"], errs, w=width))
    mean = summary["mean_rounds_to_valid"]
    print("valid {}/{} ({:.0%}); mean rounds to valid: {}".format(
        summary["valid"], summary["tasks"], summary["validity_rate"],
        "{:.2f}".format(mean) if mean is not None else "n/a"))
    print("rows: {}".format(out_path))


# --- Scripted mode (CI) ---------------------------------------------------------

class ScriptedAuthor:
    """The deterministic author double: canned replies per task id, consumed in
    order; every request is recorded on `.requests` so tests can assert what the
    LOOP fed back (the double never peeks at the harness). Running out of
    replies raises — a silently-repeating double would mask a loop that stopped
    converging."""

    def __init__(self, scripts: Dict[str, List[str]]) -> None:
        self.scripts = {k: list(v) for k, v in scripts.items()}
        self._cursor: Dict[str, int] = {}
        self.requests: List[AuthorRequest] = []

    def __call__(self, request: AuthorRequest) -> str:
        self.requests.append(request)
        replies = self.scripts.get(request.task_id, [])
        i = self._cursor.get(request.task_id, 0)
        if i >= len(replies):
            raise RuntimeError(
                "ScriptedAuthor exhausted for task {!r} (have {} replies, "
                "asked for #{}) — the loop wants more repair rounds than the "
                "script anticipated".format(request.task_id, len(replies), i + 1))
        self._cursor[request.task_id] = i + 1
        return replies[i]


def _canned_root(pipeline: Dict[str, Any], models: List[str]) -> Dict[str, Any]:
    """A minimal offline-runnable root around an inline pipeline — the shape the
    prompt's output contract asks a real model for."""
    return {
        "transport": {"type": "inproc"},
        "state": {"type": "memory"},
        "providers": {"fake": {"type": "fake_scripted",
                               "by_model": {m: ["{}"] for m in models}}},
        "default_provider": "fake",
        "input": {"text": "hello world"},
        "pipeline": pipeline,
    }


def _canned_configs() -> Dict[str, Dict[str, Any]]:
    """One valid config per built-in task — each actually matching its task's
    topology, so the scripted mode is an honest dry-run of the real one."""
    agent = lambda model, template: {"type": "agent", "model": "fake:" + model,  # noqa: E731
                                     "stage": model, "template": template}
    cfgs: Dict[str, Dict[str, Any]] = {}

    cfgs["summarize-and-render"] = _canned_root({
        "nodes": {
            "role:summarize": agent("summarize",
                                    "Summarize as JSON {\"summary\": \"...\"}:\n{{text}}"),
            "role:report": {"type": "render", "template_text": "Summary: {{summary}}",
                            "out": "summary.txt"},
        },
        "graph": {"start": "summarize", "stages": {
            "summarize": {"node": "role:summarize", "then": "report"},
            "report": {"node": "role:report", "then": None},
        }},
    }, ["summarize"])

    cfgs["gate-branch"] = _canned_root({
        "nodes": {
            "role:draft": agent("draft", "Draft JSON {\"summary\": \"...\"} for:\n{{text}}"),
            "role:approve": {"type": "human_gate", "awaiting": "review",
                             "ask": "Approve?\n{{summary}}\nReply {\"decision\": "
                                    "\"approve\"} or {\"decision\": \"revise\"}."},
            "role:publish": {"type": "render", "template_text": "PUBLISHED: {{summary}}",
                             "out": "published.txt"},
        },
        "graph": {"start": "draft", "stages": {
            "draft": {"node": "role:draft", "then": "approve"},
            "approve": {"node": "role:approve",
                        "branch": {"on": "decision", "routes": {"revise": "draft"},
                                   "default": "publish"}},
            "publish": {"node": "role:publish", "then": None},
        }},
    }, ["draft"])

    cfgs["fork-fanin"] = _canned_root({
        "nodes": {
            "role:security": agent("security", "Return JSON {\"security\": \"...\"}:\n{{text}}"),
            "role:style": agent("style", "Return JSON {\"style\": \"...\"}:\n{{text}}"),
            "role:report": {"type": "render", "template_text": "{{security}} / {{style}}",
                            "out": "review.txt"},
        },
        "graph": {"start": "spread", "stages": {
            "spread": {"node": "", "fork": ["security", "style"], "then": "report"},
            "security": {"node": "role:security", "then": "join"},
            "style": {"node": "role:style", "then": "join"},
            "join": {"node": "", "fanin": {"expect": ["security", "style"], "wait": "all"},
                     "then": None},
            "report": {"node": "role:report", "then": None},
        }},
    }, ["security", "style"])

    cfgs["retry-validator"] = _canned_root({
        "nodes": {
            "role:extract": agent("extract", "Return JSON {\"items\": [...]}:\n{{text}}"),
            "role:check": {"type": "json_object", "required": ["items"]},
            "role:report": {"type": "render", "template_text": "Items: {{items}}",
                            "out": "items.txt"},
        },
        "graph": {"start": "extract", "stages": {
            "extract": {"node": "role:extract", "validators": ["role:check"],
                        "max_attempts": 3, "feedback": True, "then": "report"},
            "report": {"node": "role:report", "then": None},
        }},
    }, ["extract"])

    cfgs["verdict-branch"] = _canned_root({
        "nodes": {
            "role:draft": agent("draft", "Draft JSON {\"draft\": \"...\"}:\n{{text}}"),
            "role:review": agent("review",
                                 "Review; return JSON {\"verdict\": \"pass\"|\"fail\"}:\n{{draft}}"),
            "role:report": {"type": "render", "template_text": "OK: {{draft}}",
                            "out": "final.txt"},
        },
        "graph": {"start": "draft", "stages": {
            "draft": {"node": "role:draft", "then": "review"},
            "review": {"node": "role:review",
                       "branch": {"on": "verdict", "routes": {"pass": "report"},
                                  "default": "draft"}},
            "report": {"node": "role:report", "then": None},
        }},
    }, ["draft", "review"])

    cfgs["shell-then-summarize"] = _canned_root({
        "nodes": {
            "role:run": {"type": "shell", "cmd": ["echo", "ok"], "stage": "run"},
            "role:summarize": agent("summarize",
                                    "Summarize as JSON {\"summary\": \"...\"}:\n{{stdout}}"),
            "role:report": {"type": "render", "template_text": "{{summary}}", "out": "out.txt"},
        },
        "graph": {"start": "run", "stages": {
            "run": {"node": "role:run", "then": "summarize"},
            "summarize": {"node": "role:summarize", "then": "report"},
            "report": {"node": "role:report", "then": None},
        }},
    }, ["summarize"])
    return cfgs


def default_scripted_author() -> ScriptedAuthor:
    """The CI script: every task lands valid, with two deliberate wrinkles that
    make the run PROVE the harness rather than flatter it —

    - `summarize-and-render`'s reply arrives fenced inside prose, the way real
      models answer, proving the tolerant-extraction path (yaah.jsonio);
    - REPAIR_TASK_ID's first draft has a broken `then` target (a mistake
      validate_config actually reports — an unknown NODE TYPE would not do:
      that is a build-time check, plugins can register types later), so the
      diagnostics-feedback round is exercised end to end."""
    cfgs = _canned_configs()
    scripts: Dict[str, List[str]] = {}
    for task_id, cfg in cfgs.items():
        scripts[task_id] = [json.dumps(cfg)]

    scripts["summarize-and-render"] = [
        "Here is the config you asked for:\n\n```json\n"
        + json.dumps(cfgs["summarize-and-render"], indent=1)
        + "\n```\nLet me know if you need changes."]

    broken = json.loads(json.dumps(cfgs[REPAIR_TASK_ID]))
    broken["pipeline"]["graph"]["stages"]["extract"]["then"] = "reprot"
    scripts[REPAIR_TASK_ID] = [json.dumps(broken), json.dumps(cfgs[REPAIR_TASK_ID])]
    return ScriptedAuthor(scripts)


# --- Real mode -----------------------------------------------------------------

def make_real_author(model_spec: str) -> AuthorFn:
    """An AuthorFn over the engine's own provider adapters. `model_spec` is
    '<provider>:<model>' — provider one of claude_cli (alias claude-cli) or
    litellm; an empty model part means the adapter's default. One provider
    instance is built up front; each call is one complete() turn (no
    conversation state — every round's prompt is self-contained by design,
    so a repair round cannot silently lean on hidden chat history)."""
    provider_name, _, model = model_spec.partition(":")
    provider_name = provider_name.replace("-", "_")
    if provider_name == "claude_cli":
        from yaah.adapters.providers.claude_cli_provider import ClaudeCliProvider
        provider: Any = ClaudeCliProvider()
    elif provider_name == "litellm":
        from yaah.adapters.providers.litellm_provider import LiteLLMProvider
        provider = LiteLLMProvider()
    else:
        raise SystemExit(
            "{}={!r}: unknown provider {!r} (want claude_cli:<model> or "
            "litellm:<model>)".format(MODEL_ENV, model_spec, provider_name))

    from yaah.agents.api_provider import complete

    def author(request: AuthorRequest) -> str:
        return asyncio.run(complete(provider, request.prompt, model=model or None))

    return author


# --- CLI -------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=(
        "Internal authoring eval: cold model + `yaah manual` + the validate-"
        "repair loop. Scripted (CI) mode by default; set {}=<provider>:<model> "
        "for a real run.".format(MODEL_ENV)))
    ap.add_argument("--out", default="authoring-eval-results.jsonl",
                    help="JSONL output path (default: %(default)s)")
    ap.add_argument("--max-rounds", type=int, default=DEFAULT_MAX_ROUNDS,
                    help="author calls per task, first draft included "
                         "(default: %(default)s)")
    ap.add_argument("--task", action="append", dest="only",
                    help="run only this task id (repeatable); default: all")
    args = ap.parse_args(argv)

    tasks = TASKS
    if args.only:
        known = {t.id for t in TASKS}
        unknown = sorted(set(args.only) - known)
        if unknown:
            raise SystemExit("unknown task id(s) {}; have {}".format(
                unknown, sorted(known)))
        tasks = [t for t in TASKS if t.id in set(args.only)]

    model_spec = os.environ.get(MODEL_ENV, "").strip()
    if model_spec:
        author: AuthorFn = make_real_author(model_spec)
        mode = "real:" + model_spec
    else:
        author = default_scripted_author()
        mode = "scripted"

    _rows, summary = run_eval(author, mode=mode, out_path=args.out,
                              tasks=tasks, max_rounds=args.max_rounds)
    return 0 if summary["valid"] == summary["tasks"] else 1


if __name__ == "__main__":
    sys.exit(main())
