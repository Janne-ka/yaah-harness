"""CLI plumbing — argv parsing, subcommand dispatch, entrypoint.

Used by: the `yaah` installed console-script (`pyproject.toml`
`[project.scripts]`) and by `python -m yaah.runtime` (via a shim in
runtime.py that delegates here).
Where: the user-facing seam — argv in, parsed `spec` dict to runtime
action functions. No engine state held here; everything routes through
the action functions in `runtime.py`.
Why: separated from the engine assembly + actions (formerly all in
runtime.py) so the file shape matches the rule the project enforces on
contributors — one concern per file, the kitchen-sink module that
ADR-0001 cosmology argues against is gone. See B2.2 design note in
.notes/refactor-runtime.md.

Shape: each verb is a (parser, dispatcher) pair registered in two small
dicts (`_VERB_PARSERS` + dispatch registries). Adding a verb is a 3-step
edit (parser fn, dispatcher fn, registry entries) instead of an if/elif
arm in three places. The pre-batch shape was ~280 lines of linear
dispatching; this is per-handler so each verb's code reads top-to-bottom
without searching.

Run: `yaah <command> [args]` after `pip install`, or `python -m yaah.cli
<command> [args]` from a source checkout.

Targets Python 3.9+.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, Callable, Dict

from .harness import StageFailed
from .runtime_factories import _read_json
from .validate import validate_root


_USAGE = """\
yaah <command> [args]

Author:
  init <dir>                    scaffold a linear starter pipeline (alias for `scaffold linear <dir>`)
  scaffold <archetype> <dir>    scaffold from a named archetype (linear / branch-with-gate / fork-fanin); see docs/archetypes.md
  scaffold --list               print the archetype catalog with one-line descriptions

Run & inspect:
  run <root>                    run the configured pipeline (the default)
  ab <experiment.json>          run an A/B campaign: variants x inputs x repetitions,
                                one durable row per run (cost + outcomes; see docs)
                                add --report [--json] for the comparison matrix
                                add --rescore SCHEMA to re-score stored raw outputs
                                against a changed contract (zero model calls)
                                add --golden FILE to diff collected outputs against
                                a pinned expected artifact (zero model calls)
  list <root> [--json]          show parked gates (the mailbox view; --json for a parseable shape)
  resume <root> ID [FILE]       deliver a decision (optionally from FILE) to a parked gate
  baton-schema <root> <id>      print the JSON Schema of decision.json for one parked baton
  clear <root>                  graceful reset: broadcast clear + flush parked + drop batons
  explain <root>                print the EFFECTIVE config (post-_extends/_fake + defaults)
  rollback <root> [ID]          walk a completed run's declared undo targets (ADR-0008).
                                no ID lists runs with candidates; with ID prints the undo
                                MENU (dry run, calls nothing; --json for machines). Add
                                --execute to run the undos in reverse completion order:
                                  [--include-costly] run undos the author marked costly
                                  [--only STAGE]...   restrict to named stage(s) (repeatable)
                                  [--accept-partial]  continue past a failed undo (else stop)

Debug:
  trace <trace.jsonl> [PRICES]  summarize a run's trace (cost / latency / retries / model mix)
                                add --pretty for a per-run tree (stages, calls, errors)
                                add --errors-only for the CI-shaped check (exits non-zero on errors)
                                add --cost for a compact human cost rollup (with PRICES for $)
                                add --counts for the per-(stage, model, ladder rung) invocation report (--json for machine output)
                                add --last N to filter to the most recent N runs
                                add --corr ID to zoom in on one specific run

Diagnose:
  manual                        print the generated agent manual (for LLM authors)
  mcp-serve                     serve validate/run/gates as MCP tools over stdio
  validate <root>               validate root + referenced pipeline file (no run)
                                  [--strict] [--from-code] [--json (machine-readable diagnostics)]
  doctor                        diagnose install: Python version, optional deps, packaged base configs
  completion <bash|zsh>         emit a shell completion script (`source <(yaah completion bash)`)

Options (on run/list/resume/clear/validate/explain):
  --fake        merge the root's `_fake` block over the top level (sidecar fake providers/state)
  --debug       full tracebacks on errors (default: message + exit 2 config / 1 run)
  -h --help     show this message
  -V --version  print the installed yaah version

Legacy form (still supported): yaah <root> [--list | --resume ID [FILE] | --clear | --explain | --lint-overlay]
(equivalent: `python -m yaah.runtime …` when not installed)"""


def _usage_exit(msg: str = "") -> None:
    if msg:
        print("error: " + msg, file=sys.stderr)
    print("usage: " + _USAGE, file=sys.stderr)
    raise SystemExit(2)


# ---------- Legacy `yaah <root> --flag` parser -------------------------------
# Kept verbatim — anyone with shell history or scripts that use the old shape
# stays unaffected. The git-style subcommands below translate to the same
# action specs via this parser.

def _parse_cli(argv: list) -> dict:
    """Parse the legacy `yaah <root> [--flag]` shape into an action descriptor.
    The hand-rolled parser stays here because argparse fights the
    optional-with-trailing-positional shape `--resume ID FILE`; a tight
    ~50-line hand-parser is the cleaner answer for the back-compat path."""
    if not argv:
        _usage_exit("missing root config")
    if argv[0] in ("-h", "--help"):
        print("usage: " + _USAGE)
        raise SystemExit(0)
    root, rest = argv[0], list(argv[1:])
    fake = "--fake" in rest
    if fake:
        rest.remove("--fake")
    debug = "--debug" in rest
    if debug:                  # global like --fake: full tracebacks instead of
        rest.remove("--debug") # the message-only error boundary in main()
    as_json = "--json" in rest
    if as_json:                # scoped to --list (machine-readable mailbox view);
        rest.remove("--json")  # noise on any other action triggers the unknown-arg path below
    if not rest:
        if as_json:
            _usage_exit("--json is only valid with --list")
        return {"action": "run", "root": root, "fake": fake, "debug": debug}
    cmd = rest[0]
    if cmd in ("-h", "--help"):
        print("usage: " + _USAGE)
        raise SystemExit(0)
    if cmd == "--list":
        if len(rest) > 1:
            _usage_exit("--list takes no extra arguments")
        return {"action": "list", "root": root, "fake": fake, "debug": debug,
                "json": as_json}
    if as_json:
        _usage_exit("--json is only valid with --list")
    if cmd == "--clear":
        if len(rest) > 1:
            _usage_exit("--clear takes no extra arguments")
        return {"action": "clear", "root": root, "fake": fake, "debug": debug}
    if cmd == "--explain":
        if len(rest) > 1:
            _usage_exit("--explain takes no extra arguments")
        return {"action": "explain", "root": root, "fake": fake, "debug": debug}
    if cmd == "--lint-overlay":
        # here the positional file IS the overlay (not a root config)
        if len(rest) > 1:
            _usage_exit("--lint-overlay takes no extra arguments")
        return {"action": "lint-overlay", "root": root, "fake": fake, "debug": debug}
    if cmd == "--resume":
        rest = list(rest)
        approver = None
        if "--approver" in rest:
            # WHO is delivering this decision — lands on the resume audit span as
            # identity metadata (a reserved HEADER, never a decision key; putting
            # it in the decision file would flow it downstream as payload).
            i = rest.index("--approver")
            if i + 1 >= len(rest):
                _usage_exit("--approver needs a value (who is approving)")
            approver = rest[i + 1]
            del rest[i:i + 2]
        if len(rest) < 2:
            _usage_exit("--resume needs a baton id")
        if len(rest) > 3:
            _usage_exit("--resume takes a baton id and an optional decision file")
        return {"action": "resume", "root": root, "fake": fake, "debug": debug,
                "baton_id": rest[1], "approver": approver,
                "decision_file": rest[2] if len(rest) == 3 else None}
    _usage_exit("unknown argument {!r}".format(cmd))
    return {}  # unreachable; satisfies type checkers


# ---------- Per-verb parsers (git-style subcommands) -------------------------
# Each `_parse_<verb>` takes `rest` (argv after the verb) and returns the
# action spec dict. Registered in `_VERB_PARSERS` below; the registry is the
# single source of truth for which verbs the CLI accepts.

def _parse_init(rest: list) -> dict:
    """`init <dir>` — alias for `scaffold linear <dir>`. `init --list` is an
    alias for `scaffold --list` so first-time users discover archetypes via
    the verb they typed."""
    if rest == ["--list"]:
        return {"action": "scaffold-list"}
    if not rest:
        _usage_exit("init needs a target directory (or --list to see archetypes)")
    if len(rest) > 1:
        _usage_exit("init takes one argument (the target directory)")
    return {"action": "scaffold", "target_dir": rest[0], "archetype": "linear"}


def _parse_mcp_serve(rest: list) -> dict:
    if rest:
        _usage_exit("mcp-serve takes no arguments (configs are named per tool call)")
    return {"action": "mcp-serve"}


def _parse_manual(rest: list) -> dict:
    if rest:
        _usage_exit("manual takes no arguments")
    return {"action": "manual"}


def _parse_ab(rest: list) -> dict:
    """`ab <experiment.json> [--report [--json]] [--rescore <schema.json>]
    [--golden <expected.json>]` — run an A/B campaign (one durable row per run);
    --report reduces collected rows + trace into the comparison matrix; --rescore
    re-scores the stored raw outputs against a (changed) contract; --golden diffs
    the collected outputs against a pinned expected artifact — all three pure
    reads, zero model calls, safe mid-campaign, and mutually exclusive."""
    rescore: Any = None
    golden: Any = None
    rest = list(rest)
    for flag, attr in (("--rescore", "rescore"), ("--golden", "golden")):
        if flag in rest:
            i = rest.index(flag)
            if i + 1 >= len(rest) or rest[i + 1].startswith("-"):
                _usage_exit("{} needs a file argument "
                            "(yaah ab exp.json {} FILE)".format(flag, flag))
            if attr == "rescore":
                rescore = rest[i + 1]
            else:
                golden = rest[i + 1]
            del rest[i:i + 2]
    args = [a for a in rest if not a.startswith("-")]
    flags = set(rest) - set(args)
    unknown = flags - {"--report", "--json"}
    if unknown:
        _usage_exit("ab: unknown flag(s) {}".format(", ".join(sorted(unknown))))
    reads = [name for name, on in (("--report", "--report" in flags),
                                   ("--rescore", bool(rescore)),
                                   ("--golden", bool(golden))) if on]
    if "--json" in flags and not reads:
        _usage_exit("ab: --json applies to --report / --rescore / --golden")
    if len(reads) > 1:
        _usage_exit("ab: {} are separate reads — pick one".format(
            " and ".join(reads)))
    if len(args) != 1:
        _usage_exit("ab needs exactly one experiment config (yaah ab "
                    "my-experiment.json [--report|--rescore SCHEMA|--golden FILE] "
                    "[--json])")
    return {"action": "ab", "experiment": args[0], "rescore": rescore,
            "golden": golden, "report": "--report" in flags,
            "json": "--json" in flags}


def _parse_scaffold(rest: list) -> dict:
    """`scaffold <archetype> <dir>` — pick the named archetype and write its
    template. `scaffold --list` prints the archetype catalog with one-liners."""
    if rest == ["--list"]:
        return {"action": "scaffold-list"}
    if len(rest) < 2:
        from .init_template import ARCHETYPES
        _usage_exit(
            "scaffold needs an archetype and a target directory "
            "(archetypes: {}; or --list for descriptions)".format(
                ", ".join(sorted(ARCHETYPES))))
    if len(rest) > 2:
        _usage_exit("scaffold takes two arguments (archetype, target directory)")
    return {"action": "scaffold", "archetype": rest[0], "target_dir": rest[1]}


def _parse_run(rest: list) -> dict:
    if not rest:
        _usage_exit("run needs a root config")
    return _parse_cli(rest)


def _parse_via_flag(flag: str) -> Callable[[list], dict]:
    """Build a parser that translates `yaah <verb> <root> [args]` into the
    legacy `yaah <root> --<flag> [args]` shape. Used for list/clear/explain —
    verbs that just rename a legacy flag."""
    verb_label = flag.lstrip("-")
    def _parse(rest: list) -> dict:
        if not rest:
            _usage_exit("{} needs a root config".format(verb_label))
        return _parse_cli([rest[0], flag] + rest[1:])
    return _parse


def _parse_resume(rest: list) -> dict:
    if len(rest) < 2:
        _usage_exit("resume needs a root config and a baton id")
    return _parse_cli([rest[0], "--resume", rest[1]] + rest[2:])


def _parse_validate(rest: list) -> dict:
    if not rest:
        _usage_exit("validate needs a root config")
    rest = list(rest)
    strict = "--strict" in rest
    if strict:
        rest.remove("--strict")    # --strict: lint warnings FAIL (exit 2), for CI
    from_code = "--from-code" in rest
    if from_code:
        rest.remove("--from-code")  # --from-code: read @provides off fn: transforms (imports app code)
    as_json = "--json" in rest
    if as_json:
        rest.remove("--json")      # --json: ONE machine-readable diagnostics object on stdout
    spec = _parse_cli(rest)        # parse root + --fake/--debug, then
    spec["action"] = "validate"    # check-only (never runs the pipeline)
    spec["strict"] = strict
    spec["from_code"] = from_code
    spec["json"] = as_json
    return spec


def _parse_trace(rest: list) -> dict:
    """`trace <jsonl> [PRICES]` + several view/filter flags. --last and --corr
    each take a value; the three view flags (--pretty/--errors-only/--cost)
    are mutually exclusive."""
    last_n = 0
    corr = None
    rest_clean = list(rest)
    if "--last" in rest_clean:
        i = rest_clean.index("--last")
        if i + 1 >= len(rest_clean):
            _usage_exit("--last needs a positive integer (N)")
        try:
            last_n = int(rest_clean[i + 1])
        except ValueError:
            _usage_exit("--last N: N must be an integer (got {!r})".format(rest_clean[i + 1]))
        if last_n <= 0:
            _usage_exit("--last N: N must be positive (got {})".format(last_n))
        del rest_clean[i:i + 2]
    if "--corr" in rest_clean:
        i = rest_clean.index("--corr")
        if i + 1 >= len(rest_clean):
            _usage_exit("--corr needs a correlation id")
        corr = rest_clean[i + 1]
        del rest_clean[i:i + 2]
    flags = {"--debug", "--pretty", "--errors-only", "--cost", "--counts",
             "--json"}  # bare flags
    files = [a for a in rest_clean if a not in flags]
    if not files:
        _usage_exit("trace needs a trace.jsonl path")
    view_flags = [f for f in ("--pretty", "--errors-only", "--cost", "--counts")
                  if f in rest_clean]
    if len(view_flags) > 1:
        _usage_exit("{} are mutually exclusive".format(" and ".join(view_flags)))
    return {"action": "trace", "trace_path": files[0],
            "price_map": files[1] if len(files) > 1 else None,
            "pretty": "--pretty" in rest_clean,
            "errors_only": "--errors-only" in rest_clean,
            "cost": "--cost" in rest_clean,
            "counts": "--counts" in rest_clean,
            "json": "--json" in rest_clean,
            "last_n": last_n,
            "corr": corr,
            "debug": "--debug" in rest_clean}


def _parse_doctor(rest: list) -> dict:
    if rest:
        _usage_exit("doctor takes no arguments")
    return {"action": "doctor"}


def _parse_completion(rest: list) -> dict:
    if len(rest) != 1:
        _usage_exit("completion needs one shell name (bash or zsh)")
    return {"action": "completion", "shell": rest[0]}


def _parse_baton_schema(rest: list) -> dict:
    if len(rest) < 2:
        _usage_exit("baton-schema needs a root config and a baton id")
    if len(rest) > 2:
        _usage_exit("baton-schema takes one root config and one baton id")
    return {"action": "baton-schema", "root": rest[0], "baton_id": rest[1],
            "fake": False, "debug": False}


def _parse_rollback(rest: list) -> dict:
    """`rollback <root> [<corr>] [--json] [--execute --include-costly
    --only S ... --accept-partial]` — read a run's trace and either SHOW the undo
    menu (default, a pure dry run) or --EXECUTE the declared undo targets in
    reverse completion order (ADR-0008). Menu/list default; `--execute` needs a
    corr; the execute-only flags are rejected without it; `--json` machine-formats
    whichever output (menu / run-list / report). `--only` is repeatable."""
    rest = list(rest)
    only: list = []
    while "--only" in rest:
        i = rest.index("--only")
        if i + 1 >= len(rest) or rest[i + 1].startswith("-"):
            _usage_exit("--only needs a stage name (yaah rollback <root> <corr> "
                        "--execute --only STAGE)")
        only.append(rest[i + 1])
        del rest[i:i + 2]
    debug = "--debug" in rest
    if debug:
        rest.remove("--debug")
    execute = "--execute" in rest
    if execute:
        rest.remove("--execute")
    include_costly = "--include-costly" in rest
    if include_costly:
        rest.remove("--include-costly")
    accept_partial = "--accept-partial" in rest
    if accept_partial:
        rest.remove("--accept-partial")
    as_json = "--json" in rest
    if as_json:
        rest.remove("--json")
    unknown = [a for a in rest if a.startswith("-")]
    if unknown:
        _usage_exit("rollback: unknown flag(s) {}".format(", ".join(unknown)))
    pos = [a for a in rest if not a.startswith("-")]
    if not pos:
        _usage_exit("rollback needs a root config")
    if len(pos) > 2:
        _usage_exit("rollback takes a root config and an optional correlation id")
    corr = pos[1] if len(pos) == 2 else None
    if execute and corr is None:
        _usage_exit("rollback --execute needs a correlation id "
                    "(yaah rollback <root> <corr> --execute)")
    if not execute and (include_costly or accept_partial or only):
        _usage_exit("rollback: --include-costly / --only / --accept-partial only "
                    "apply with --execute")
    return {"action": "rollback", "root": pos[0], "corr": corr, "json": as_json,
            "execute": execute, "include_costly": include_costly, "only": only,
            "accept_partial": accept_partial, "fake": False, "debug": debug}


# Registry of verb -> parser. The dict is the single source of truth for the
# CLI surface — adding a verb is one entry here + the matching dispatcher.
_VERB_PARSERS: Dict[str, Callable[[list], dict]] = {
    "ab":            _parse_ab,
    "init":          _parse_init,
    "manual":        _parse_manual,
    "mcp-serve":     _parse_mcp_serve,
    "scaffold":      _parse_scaffold,
    "run":           _parse_run,
    "list":          _parse_via_flag("--list"),
    "clear":         _parse_via_flag("--clear"),
    "explain":       _parse_via_flag("--explain"),
    "resume":        _parse_resume,
    "validate":      _parse_validate,
    "trace":         _parse_trace,
    "doctor":        _parse_doctor,
    "completion":    _parse_completion,
    "baton-schema":  _parse_baton_schema,
    "rollback":      _parse_rollback,
}

# Tuple form kept for the test in test_shell_completion.py (asserts no drift between
# the parser surface and the shell-completion script's verb list).
_SUBCOMMANDS = tuple(_VERB_PARSERS.keys())


def _parse_subcommand(argv: list) -> dict:
    """Dispatch the first argv token to its per-verb parser. The registry
    lookup is the only place that needs to know the verb set."""
    verb, rest = argv[0], list(argv[1:])
    parser = _VERB_PARSERS.get(verb)
    if parser is None:
        _usage_exit("unknown command {!r}".format(verb))
        return {}  # unreachable; satisfies type checkers
    return parser(rest)


# ---------- helpers + per-action dispatchers ---------------------------------

def _apply_fake_overlay(root: Dict[str, Any]) -> Dict[str, Any]:
    """Merge the root's `_fake` block over the top level (shallow). The `_fake`
    key is a `_`-prefixed comment ignored by `validate_root`; when `--fake` is
    on the CLI, its contents replace the matching top-level keys (typically
    `providers` / `default_provider`, sometimes `state` / `prompt_sources`).
    Lets one root file carry both a real config and its testable fake overlay
    so the dev doesn't maintain two near-duplicate roots."""
    overlay = root.pop("_fake", None)
    if not overlay:
        return root
    if not isinstance(overlay, dict):
        raise ValueError("root `_fake` must be a dict (got {})".format(type(overlay).__name__))
    out = dict(root)
    out.update(overlay)
    return out


# Self-contained dispatchers — don't load a root config. Late imports avoid
# pulling engine assembly machinery for `yaah scaffold` / `yaah trace` /
# `yaah doctor` / `yaah completion` (keeps the no-engine paths cheap).

def _dispatch_lint_overlay(spec: Dict[str, Any]) -> None:
    from .overlay_lint import lint_overlay
    problems = lint_overlay(spec["root"])
    if problems:
        print("overlay rejected ({} problem{}):".format(
            len(problems), "s" if len(problems) != 1 else ""))
        for p in problems:
            print("  - " + p)
        raise SystemExit(1)
    print("overlay ok — within the AI-mutable surface")


def _dispatch_doctor(spec: Dict[str, Any]) -> None:
    from .doctor import diagnose
    code, report = diagnose()
    print(report, end="")
    raise SystemExit(code)


def _dispatch_completion(spec: Dict[str, Any]) -> None:
    from .shell_completion import render
    print(render(spec["shell"]))


def _dispatch_trace(spec: Dict[str, Any]) -> None:
    """`yaah trace` — load JSONL records, apply filters (--corr/--last) then
    render in the requested view (--pretty / --errors-only / --cost / JSON
    aggregate default). Each view path imports its renderer lazily so the
    JSON aggregate path never imports the pretty module and vice versa."""
    from .trace.aggregate import aggregate, load_jsonl
    records = load_jsonl(spec["trace_path"])
    if spec.get("corr"):
        from .trace.pretty import keep_corr
        records = keep_corr(records, spec["corr"])
    if spec.get("last_n"):
        from .trace.pretty import keep_last_runs
        records = keep_last_runs(records, spec["last_n"])
    price_map = _read_json(spec["price_map"]) if spec.get("price_map") else None
    if spec.get("errors_only"):
        # CI-shaped: exit code mirrors error presence; the print is just
        # informational — the meaningful signal is the exit code.
        from .trace.pretty import errors_only
        code, report = errors_only(records)
        print(report, end="")
        raise SystemExit(code)
    if spec.get("cost"):
        from .trace.pretty import cost_summary
        print(cost_summary(records, price_map=price_map), end="")
        return
    if spec.get("pretty"):
        from .trace.pretty import pretty
        print(pretty(records, price_map=price_map), end="")
        return
    if spec.get("counts"):
        # Invocation-count report (M12): the client's per-(stage, model, ladder
        # rung) table. --json composes to the machine shape (list of row dicts).
        if spec.get("json"):
            from .trace.aggregate import count_by_stage_model
            print(json.dumps(count_by_stage_model(records, price_map=price_map),
                             indent=2))
            return
        from .trace.pretty import counts_table
        print(counts_table(records, price_map=price_map), end="")
        return
    print(json.dumps(aggregate(records, price_map=price_map), indent=2))


def _dispatch_scaffold_list(spec: Dict[str, Any]) -> None:
    """Discovery affordance — print the archetype catalog. Names + one-liners
    come from init_template.ARCHETYPE_DESCRIPTIONS (single source of truth)."""
    from .init_template import ARCHETYPE_DESCRIPTIONS, ARCHETYPES
    width = max(len(k) for k in ARCHETYPES)
    for name in sorted(ARCHETYPES):
        desc = ARCHETYPE_DESCRIPTIONS.get(name, "(no description)")
        print("  {}  {}".format(name.ljust(width), desc))
    print("\nUse: yaah scaffold <archetype> <dir>")


def _dispatch_manual(spec: Dict[str, Any]) -> None:
    """Print the generated agent manual — ONE token-budgeted document an LLM
    needs in context to author valid configs (generated from the same tables
    validate/build read, so it cannot drift)."""
    from .manual import build_manual
    print(build_manual())


def _dispatch_mcp_serve(spec: Dict[str, Any]) -> None:
    """Serve yaah's operator surface as MCP tools over stdio (newline-delimited
    JSON-RPC): validate / run / list_gates / baton_schema / resume — so any
    MCP-capable agent host operates yaah natively, no CLI string parsing."""
    from .adapters.mcp_server import serve_process_stdio
    asyncio.run(serve_process_stdio())


def _dispatch_ab(spec: Dict[str, Any]) -> None:
    """Run an A/B experiment campaign — or, with --report, reduce its collected
    rows + trace into the comparison matrix (no runs)."""
    path = os.path.abspath(spec["experiment"])
    cfg = _read_json(path)
    base = os.path.dirname(path)
    if spec.get("rescore"):
        from .experiment import rescore_rows
        schema = _read_json(spec["rescore"])
        result = asyncio.run(rescore_rows(cfg, base, schema))
        if spec.get("json"):
            print(json.dumps(result, indent=2))
            return
        _render_rescore(result)
        return
    if spec.get("golden"):
        from .experiment import golden_diff_rows, load_golden
        golden = load_golden(spec["golden"])
        result = asyncio.run(golden_diff_rows(cfg, base, golden))
        if spec.get("json"):
            print(json.dumps(result, indent=2))
            return
        _render_golden(result)
        return
    if spec.get("report"):
        from .experiment import build_matrix
        matrix = asyncio.run(build_matrix(cfg, base))
        if spec.get("json"):
            print(json.dumps(matrix, indent=2))
            return
        _render_matrix(matrix)
        return
    from .experiment import run_experiment
    summary = asyncio.run(run_experiment(cfg, base))
    print("experiment {!r}: {} rows appended".format(
        summary["experiment"], summary["rows"]))
    for name, counts in summary["by_variant"].items():
        line = ", ".join("{} {}".format(v, k) for k, v in counts.items() if v)
        print("  {:<12} {}".format(name, line or "no runs"))
    print("rows + trace under the experiment store (trace: {})".format(summary["trace"]))
    print("compare:  yaah ab {} --report".format(spec["experiment"]))


def _render_rescore(result: Dict[str, Any]) -> None:
    """The rescore tier table on a terminal: per population — parse tiers
    (strict/recovered/reject) and the conform gate against the candidate
    contract, with the top schema errors (a count alone is not actionable)."""
    print("experiment {!r} rescored against schema (required: {}) — {} cell(s)".format(
        result["experiment"], ", ".join(result["schema_required"]) or "none",
        len(result["cells"])))
    for c in result["cells"]:
        t = c["tiers"]
        print("  {:<12} fp {}  N={} (no_raw={})".format(
            c["variant"], c["fingerprint"][:12], c["n"], c["no_raw"]))
        print("    parse: {} strict / {} recovered / {} reject".format(
            t["strict"], t["recovered"], t["reject"]))
        print("    conform: {} pass / {} fail".format(
            c["conform"]["pass"], c["conform"]["fail"]))
        for e in c["conform"]["top_errors"]:
            print("      mismatch: {}".format(e))
    for w in result["warnings"]:
        print("  warning: " + w)


def _render_golden(result: Dict[str, Any]) -> None:
    """The golden diff on a terminal: per (variant, population) cell — how many
    collected runs MATCH the pinned golden, and for those that drifted the
    compact added/removed/changed keys (values bounded). A REPORT, not a gate:
    n_differ==0 is your green, but the read never fails on drift; warnings carry
    the no-output, multi-input, and never-hit-scrub flags."""
    print("experiment {!r} vs golden (scrub: {}) — {} cell(s)".format(
        result["experiment"], ", ".join(result["scrub"]) or "none",
        len(result["cells"])))
    for c in result["cells"]:
        print("  {:<12} fp {}  N={}  {} match / {} differ (no_output={})".format(
            c["variant"], c["fingerprint"][:12], c["n"],
            c["n_match"], c["n_differ"], c["n_no_output"]))
        for dd in c["diffs"]:
            parts = []
            for label in ("added", "removed", "changed"):
                if dd[label]:
                    parts.append("{} {}".format(label, ", ".join(sorted(dd[label]))))
            print("    x{}: {}".format(dd["count"], "; ".join(parts)))
    for w in result["warnings"]:
        print("  warning: " + w)


def _render_matrix(matrix: Dict[str, Any]) -> None:
    """The comparison matrix on a terminal: one line per (variant, population)
    cell — N, outcomes, cost, duration, declared metrics. NO winner column by
    design: the matrix presents, the human decides; warnings carry the
    statistical-honesty flags (N<2, mid-campaign population splits)."""
    print("experiment {!r} — {} cell(s)".format(
        matrix["experiment"], len(matrix["cells"])))
    for c in matrix["cells"]:
        cost = c["cost_usd"]
        cost_s = ("${:.4f} mean (${:.4f}-${:.4f}, sd {:.4f}, {} priced/{} un)".format(
            cost["mean"], cost["min"], cost["max"], cost["stdev"],
            cost["n_priced"], cost["n_unpriced"]) if cost.get("n")
            else "no cost data ({} unpriced)".format(cost["n_unpriced"]))
        outcomes = ", ".join("{} {}".format(v, k) for k, v in sorted(c["outcomes"].items()))
        print("  {:<12} fp {}  N={}{}".format(
            c["variant"], c["fingerprint"][:12], c["n"],
            "  [INSUFFICIENT N]" if c["insufficient_n"] else ""))
        print("    outcomes: {}   cost: {}".format(outcomes, cost_s))
        dur = c["duration_s"]
        if dur.get("n"):
            print("    duration: {:.2f}s mean ({:.2f}-{:.2f})".format(
                dur["mean"], dur["min"], dur["max"]))
        for m, s in sorted(c["metrics"].items()):
            if s.get("n"):
                print("    metric {}: {:.3f} mean ({:.3f}-{:.3f}, sd {:.3f}, "
                      "n={}, missing={})".format(m, s["mean"], s["min"], s["max"],
                                                 s["stdev"], s["n"], s["missing"]))
            else:
                print("    metric {}: no numeric values (missing={})".format(
                    m, s.get("missing", 0)))
    for w in matrix["warnings"]:
        print("  warning: " + w)


def _dispatch_scaffold(spec: Dict[str, Any]) -> None:
    """Write the named archetype's template into target_dir. `yaah init <dir>`
    enters here with archetype="linear" (back-compat)."""
    from .init_template import scaffold
    target = spec["target_dir"]
    archetype = spec.get("archetype", "linear")
    try:
        n = scaffold(target, archetype)
    except (FileExistsError, ValueError) as e:
        print("error: " + str(e), file=sys.stderr)
        raise SystemExit(2)
    print("Created {} files in {}/  (archetype: {})".format(n, target, archetype))
    print("Next:  yaah run {}/starter.local.json".format(target))
    print("Then:  open the prompts/ dir and edit; see docs/tutorial.md and docs/archetypes.md")


def _dispatch_rollback(spec: Dict[str, Any]) -> None:
    """`yaah rollback` — read the run's trace + the pipeline's rollback
    declarations and either print the undo MENU (default / no corr = list runs)
    or --EXECUTE the undo targets. Self-contained (loads its own root) and does
    NOT run `validate_root`: rollback is a read/undo tool the operator may need
    precisely when a run left a config in a rejected state; requiring full
    validation would block the cleanup. `base` goes on `sys.path` so `fn:` undo
    targets resolve relative to the config dir (same rule as a running pipeline)."""
    from . import rollback as rb
    root = _read_json(spec["root"])
    base = os.path.dirname(os.path.abspath(spec["root"]))
    if base not in sys.path:
        sys.path.insert(0, base)
    nodes, stages = rb.load_pipeline(root, base)
    records = rb.read_trace(root, base)
    corr = spec.get("corr")
    if spec["execute"]:
        # the parser rejects --execute without a corr; narrow for the type checker
        assert isinstance(corr, str) and corr
        report = asyncio.run(rb.execute(
            records, stages, nodes, corr,
            include_costly=spec["include_costly"], only=spec["only"],
            accept_partial=spec["accept_partial"]))
        print(json.dumps(report, indent=2) if spec["json"]
              else rb.render_report(report), end="" if not spec["json"] else "\n")
        return
    if corr is None:
        runs = rb.list_runs(records, stages, nodes)
        print(json.dumps({"runs": runs}, indent=2) if spec["json"]
              else rb.render_list(runs), end="" if not spec["json"] else "\n")
        return
    menu = rb.build_menu(records, stages, nodes, corr)
    print(json.dumps(menu, indent=2) if spec["json"]
          else rb.render_menu(menu), end="" if not spec["json"] else "\n")


_SELF_CONTAINED_DISPATCH: Dict[str, Callable[[Dict[str, Any]], None]] = {
    "ab":            _dispatch_ab,
    "rollback":      _dispatch_rollback,
    "lint-overlay":  _dispatch_lint_overlay,
    "doctor":        _dispatch_doctor,
    "completion":    _dispatch_completion,
    "trace":         _dispatch_trace,
    "scaffold-list": _dispatch_scaffold_list,
    "manual":        _dispatch_manual,
    "mcp-serve":     _dispatch_mcp_serve,
    "scaffold":      _dispatch_scaffold,
}


# Root-required dispatchers — each receives the loaded+overlay'd root + base
# dir. The orchestrator in `_dispatch` runs validate_root once before
# delegating, so dispatchers can assume the root structure is sound.
# `explain` is special — it runs BEFORE validate_root (the action shows
# provenance even for a malformed root), handled inline.

def _dispatch_explain(spec: Dict[str, Any], root: Dict[str, Any], base: str) -> None:
    """R13: print the effective config + provenance. `explain_root` runs
    `validate_root` itself, so config errors surface here too. Special-cased
    in `_dispatch` because it must run BEFORE the orchestrator's `validate_root`."""
    from .runtime import explain_root
    with open(spec["root"], "r", encoding="utf-8") as f:
        raw_user = json.load(f)
    explain_root(raw_user, root, base, root_path=spec["root"], fake=spec.get("fake", False))


def _dispatch_validate(spec: Dict[str, Any], root: Dict[str, Any], base: str) -> None:
    """Validate root + the referenced pipeline file. Closes the gap where the
    pre-batch `yaah validate` only checked the root and pronounced "ok" while
    the referenced pipeline had unresolved targets or was malformed. The check
    itself is `validate.validate_config` — shared with the MCP `validate` tool
    so the two operator surfaces can't drift; this dispatcher owns the terminal
    rendering and exit codes.

    `--json` prints ONE machine-readable object on stdout —
    {ok, root, errors: [{message, stage?}], warnings: [{id, message}]} —
    so a generate->validate->repair loop (an LLM author) consumes diagnostics
    without prose parsing. Exit codes unchanged: 0 ok / 1 invalid / 2 strict.
    Root validation happens HERE (this action dispatches before the
    orchestrator's validate_root) so root errors become diagnostics too."""
    from .validate import split_diagnostics, split_lint_id, validate_config
    as_json = spec.get("json", False)
    # --from-code (ADR-0005 slice D): read @provides off fn: transforms so the lint sees
    # across them without hand-written `provides`. OPT-IN because it IMPORTS app code; the
    # default lint stays pure.
    resolve = None
    if spec.get("from_code"):
        from .contract import fn_provides_resolver
        resolve = fn_provides_resolver(base)
    errors: list = []
    warnings: list = []
    try:
        warnings = validate_config(root, base, resolve=resolve)
    except ValueError as e:
        if not as_json:
            raise                      # prose mode: same loud failure as before
        errors = split_diagnostics(str(e))
    if as_json:
        warn_items = [{"id": wid, "message": msg}
                      for wid, msg in map(split_lint_id, warnings)]
        print(json.dumps({"ok": not errors, "root": spec["root"],
                          "errors": errors, "warnings": warn_items}, indent=2))
        if errors:
            raise SystemExit(1)
        if warnings and spec.get("strict"):
            raise SystemExit(2)
        return
    # Advisory lint: print warnings to stderr (stdout stays clean for "ok"). `--strict`
    # makes ANY warning FAIL with exit code 2 — distinct from the hard-error path (so CI
    # can tell 'invalid config' from 'valid-but-weak'). Default stays advisory so a
    # mid-migration pipeline still validates and runs.
    # Each lint message carries its rule id as a "[lint: id]" trailer; present it
    # UP FRONT ("warning[id]: ...") — on a long unwrapped stderr line the rule name
    # is what the author scans for, and the trailer would be the last thing seen.
    for w in warnings:
        wid, msg = split_lint_id(w)
        if wid:
            print("warning[{}]: {}".format(wid, msg), file=sys.stderr)
        else:
            print("warning: " + msg, file=sys.stderr)
    if warnings and spec.get("strict"):
        print("strict: {} lint warning(s) — failing with exit 2 (rerun without --strict "
              "to treat as advisory)".format(len(warnings)), file=sys.stderr)
        raise SystemExit(2)
    pipeline_ref = root.get("pipeline")
    if isinstance(pipeline_ref, str):
        print("ok: {} is valid (root + pipeline {})".format(spec["root"], pipeline_ref))
    elif isinstance(pipeline_ref, dict):
        print("ok: {} is valid (root + inline pipeline)".format(spec["root"]))
    else:
        print("ok: {} is a valid root config".format(spec["root"]))


# ---------- outcome / mailbox rendering ---------------------------------------
# The runtime actions RETURN data (Outcome / Batons / dicts); how that data
# reads on a terminal is this surface's concern. The MCP server renders the
# same returns as JSON (adapters/mcp_server/tools.py) — two renderings, one
# source of truth for the data itself.

_RESULT_PRINT_MAX = 4000


def _short(out: object) -> str:
    """Truncated render of an Outcome for the console. The payload can carry
    large fields (a full diff, a spec); an operator — especially an AI in a
    session — polls state via this print, so it must stay cheap to read.
    Artifacts live on disk by reference (`*_path` keys); fetch on demand."""
    s = str(out)
    if len(s) <= _RESULT_PRINT_MAX:
        return s
    return s[:_RESULT_PRINT_MAX] + " … [{} chars truncated — artifacts are on disk via *_path keys]".format(
        len(s) - _RESULT_PRINT_MAX)


def _print_concerns(concerns: list) -> None:
    """The concern TEXTS, one line each — a count alone is not actionable; the
    whole point of soft/sceptic concerns is that the human reads them AT the gate."""
    for c in concerns:
        line = "  concern [{}/{}]: {}".format(c.get("stage", "?"), c.get("code", "?"),
                                              c.get("message", ""))
        if c.get("fix_hint"):
            line += " ({})".format(c["fix_hint"])
        print(line)


def _render_outcome(out: Any) -> None:
    """One run/resume Outcome on the console: the GATE banner when parked
    (durable state lets another process resume it), then the RESULT line."""
    from .harness import Suspended
    if isinstance(out, Suspended):
        print("GATE baton_id={} awaiting={} concerns={}".format(
            out.baton_id, out.awaiting, len(out.concerns)))
        _print_concerns(out.concerns)
    print("RESULT:", _short(out))


def _dispatch_baton_schema(spec: Dict[str, Any], root: Dict[str, Any], base: str) -> None:
    from .runtime import ActionError, baton_schema
    try:
        out = asyncio.run(baton_schema(root, base, spec["baton_id"]))
    except ActionError as e:
        # ONLY the action's domain errors (no such baton / not a gate / no
        # form) get the documented exit code 1 — a driver skill can tell
        # "wrong baton" from "broken root/store" (which stays exit 2 via
        # main()'s boundary; catching all ValueError would net e.g. a
        # corrupted store's JSONDecodeError into the wrong class).
        print("error: {}".format(e), file=sys.stderr)
        raise SystemExit(1) from None
    print(json.dumps(out, indent=2))


def _dispatch_list(spec: Dict[str, Any], root: Dict[str, Any], base: str) -> None:
    from .runtime import list_gates, _baton_json
    gates = asyncio.run(list_gates(root, base))
    if spec.get("json"):
        # one JSON document with the same fields the prose view shows — so a
        # driver skill can parse instead of interpret (shape: _baton_json).
        print(json.dumps({"batons": [_baton_json(b) for b in gates]}, indent=2))
        return
    for b in gates:
        print("GATE baton_id={} stage={} awaiting={} concerns={}".format(
            b.id, b.stage, b.awaiting, len(b.concerns)))
        _print_concerns(b.concerns)
        if b.pending is not None:
            # surface the failed verdict that escalated this stage (Y3) — the
            # failure that broke the stage, shown where the human first looks...
            for f in (b.pending.payload.get("escalation") or {}).get("failures", []):
                print("  failed: {}: {}".format(f.get("code", "?"), f.get("message", "")))
            # ...and the pending question/ask, so they know what to answer.
            q = b.pending.payload.get("question") or b.pending.payload.get("ask")
            if q:
                print("  question: {}".format(q))
    if not gates:
        print("(no suspended gates)")


def _dispatch_clear(spec: Dict[str, Any], root: Dict[str, Any], base: str) -> None:
    from .runtime import clear_state
    print("CLEARED:", asyncio.run(clear_state(root, base)))


def _dispatch_resume(spec: Dict[str, Any], root: Dict[str, Any], base: str) -> None:
    from .runtime import resume_gate
    decision = _read_json(spec["decision_file"]) if spec["decision_file"] else {}
    # The originally-detached engine exited at the park; THIS process now runs
    # the engine until the next gate or completion. Banner sets expectations
    # (was previously silently blocking).
    print("[yaah resume] engine running in this process until next gate or completion",
          file=sys.stderr)
    _render_outcome(asyncio.run(resume_gate(root, base, spec["baton_id"], decision,
                                            approver=spec.get("approver"))))


def _dispatch_run(spec: Dict[str, Any], root: Dict[str, Any], base: str) -> None:
    from .runtime import run_root
    out = asyncio.run(run_root(root, base))
    if out is not None:   # None = the serve-only path (which normally never returns)
        _render_outcome(out)
        # Decisions-driven mode drives gates to completion, but can reach a gate
        # it has no answer for; drive() then leaves the run PARKED rather than
        # crash. Point the operator at the exact resume command so the walk-away
        # is actionable. Exit stays the normal suspended-run code (0) — same as a
        # plain `yaah run` that stops at its first gate.
        from .harness import Suspended
        if isinstance(out, Suspended) and root.get("decisions"):
            print("parked at {}, no decision configured; resume with "
                  "`yaah resume {} {}`".format(out.awaiting, spec["root"], out.baton_id),
                  file=sys.stderr)


_ROOT_DISPATCH: Dict[str, Callable[[Dict[str, Any], Dict[str, Any], str], None]] = {
    "validate":     _dispatch_validate,
    "baton-schema": _dispatch_baton_schema,
    "list":         _dispatch_list,
    "clear":        _dispatch_clear,
    "resume":       _dispatch_resume,
    "run":          _dispatch_run,
}


def _dispatch(spec: Dict[str, Any]) -> None:
    """Execute one parsed CLI action. Split from main() so the error boundary
    there wraps EVERYTHING that can raise a config/run error — load, _fake
    overlay, validate, assembly, and the run itself."""
    action = spec["action"]
    self_contained = _SELF_CONTAINED_DISPATCH.get(action)
    if self_contained is not None:
        self_contained(spec)
        return
    # Root-required path: load + apply _fake overlay + dispatch.
    root = _read_json(spec["root"])
    if spec.get("fake"):
        root = _apply_fake_overlay(root)
    base = os.path.dirname(os.path.abspath(spec["root"]))
    # `fn:` modules resolve relative to the config file's directory — the same
    # mental model as running a script from that dir, which is what `python -m
    # yaah.runtime` gave us implicitly via cwd. The installed console script
    # doesn't add cwd, so front-insert `base` here (config dir wins over stdlib /
    # site-packages, matching `-m` semantics). The guard avoids duplicate path
    # entries across runs. Caveat for long-lived hosts that dispatch many configs
    # in ONE process: Python caches imports by top-level name in `sys.modules`, so
    # two configs that each ship a `transforms.py` collide on the first one loaded.
    # That's inherent to flat module names — the durable fix for shared code is to
    # package it and use a dotted `fn:pkg.mod:func` path (see docs/node-reference).
    if base not in sys.path:
        sys.path.insert(0, base)
    # Plugins load BEFORE any validation so a registered type is a known enum
    # value by the time validate_root / validate_pipeline read the factory maps.
    from .plugins import load_plugins
    load_plugins(root.get("plugins"), base)
    if root.get("plugins"):
        # plugins run code at IMPORT time, even for validate/explain — say so.
        print("note: imported plugins: {}".format(", ".join(root["plugins"])),
              file=sys.stderr)
    if action == "validate":
        # validates the root ITSELF (inside its error collection) so `--json`
        # reports root errors as diagnostics instead of a traceback.
        _dispatch_validate(spec, root, base)
        return
    if action == "explain":
        # `explain_root` runs `validate_root` itself with extra provenance
        # context, so it has to bypass the orchestrator's validate call.
        _dispatch_explain(spec, root, base)
        return
    validate_root(root)        # R15: one entry — unknown-key, shape, enum, cross-field
    dispatcher = _ROOT_DISPATCH.get(action)
    if dispatcher is None:
        raise ValueError("unknown action {!r}".format(action))
    dispatcher(spec, root, base)


# ---------- entrypoint --------------------------------------------------------

def _resolve_version() -> str:
    """The installed wheel's metadata version, with a clear fallback for the
    source-checkout path (PYTHONPATH=src) where no dist-info exists. Falling
    back loudly to "(source checkout)" is the honest answer — pretending to
    know a version we can't read would be worse than admitting it."""
    try:
        from importlib.metadata import PackageNotFoundError, version
        try:
            return version("yaah-harness")
        except PackageNotFoundError:
            return "(source checkout)"
    except ImportError:    # pragma: no cover - importlib.metadata is stdlib on 3.9+
        return "(unknown)"


def main() -> None:
    argv = sys.argv[1:]
    # Top-level intercepts before any subcommand dispatch: --version / -V and
    # bare `yaah` / -h / --help. Putting them here keeps _parse_cli's
    # "missing root config" branch focused on the real error case (user typed
    # a flag without a root) instead of confusingly firing on `yaah` alone.
    if argv and argv[0] in ("--version", "-V"):
        print("yaah {}".format(_resolve_version()))
        raise SystemExit(0)
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: " + _USAGE)
        raise SystemExit(0)
    spec = _parse_subcommand(argv) if argv[0] in _VERB_PARSERS else _parse_cli(argv)
    try:
        _dispatch(spec)
    except StageFailed as e:
        # The run failed a hard gate: the message names the stage + failures
        # (stage_failed.py carries the verdict) — that's the operator's answer;
        # the traceback is engine internals, shown only under --debug.
        if spec.get("debug"):
            raise
        print("pipeline failed: {}".format(e), file=sys.stderr)
        raise SystemExit(1) from None
    except (ValueError, OSError, ImportError) as e:
        # Config-class errors (missing file, bad JSON, failed validation,
        # unknown type mid-build, fn: target whose module isn't on PYTHONPATH):
        # the message IS the fix; a 40-line traceback into the factory or
        # importlib buries it. --debug restores it.
        if spec.get("debug"):
            raise
        print("error: {}".format(e), file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
