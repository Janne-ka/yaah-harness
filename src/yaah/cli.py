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
from .runtime_factories import _read_json, _rel
from .validate import validate_root


_USAGE = """\
yaah <command> [args]

Author:
  init <dir>                    scaffold a linear starter pipeline (alias for `scaffold linear <dir>`)
  scaffold <archetype> <dir>    scaffold from a named archetype (linear / branch-with-gate / fork-fanin); see docs/archetypes.md
  scaffold --list               print the archetype catalog with one-line descriptions

Run & inspect:
  run <root>                    run the configured pipeline (the default)
  list <root> [--json]          show parked gates (the mailbox view; --json for a parseable shape)
  resume <root> ID [FILE]       deliver a decision (optionally from FILE) to a parked gate
  baton-schema <root> <id>      print the JSON Schema of decision.json for one parked baton
  clear <root>                  graceful reset: broadcast clear + flush parked + drop batons
  explain <root>                print the EFFECTIVE config (post-_extends/_fake + defaults)

Debug:
  trace <trace.jsonl> [PRICES]  summarize a run's trace (cost / latency / retries / model mix)
                                add --pretty for a per-run tree (stages, calls, errors)
                                add --errors-only for the CI-shaped check (exits non-zero on errors)
                                add --cost for a compact human cost rollup (with PRICES for $)
                                add --last N to filter to the most recent N runs
                                add --corr ID to zoom in on one specific run

Diagnose:
  validate <root>               validate root + referenced pipeline file (no run)
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
        if len(rest) < 2:
            _usage_exit("--resume needs a baton id")
        if len(rest) > 3:
            _usage_exit("--resume takes a baton id and an optional decision file")
        return {"action": "resume", "root": root, "fake": fake, "debug": debug,
                "baton_id": rest[1],
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
    spec = _parse_cli(rest)        # parse root + --fake/--debug, then
    spec["action"] = "validate"    # check-only (never runs the pipeline)
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
    flags = {"--debug", "--pretty", "--errors-only", "--cost"}  # bare flags
    files = [a for a in rest_clean if a not in flags]
    if not files:
        _usage_exit("trace needs a trace.jsonl path")
    view_flags = [f for f in ("--pretty", "--errors-only", "--cost") if f in rest_clean]
    if len(view_flags) > 1:
        _usage_exit("{} are mutually exclusive".format(" and ".join(view_flags)))
    return {"action": "trace", "trace_path": files[0],
            "price_map": files[1] if len(files) > 1 else None,
            "pretty": "--pretty" in rest_clean,
            "errors_only": "--errors-only" in rest_clean,
            "cost": "--cost" in rest_clean,
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


# Registry of verb -> parser. The dict is the single source of truth for the
# CLI surface — adding a verb is one entry here + the matching dispatcher.
_VERB_PARSERS: Dict[str, Callable[[list], dict]] = {
    "init":          _parse_init,
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
}

# Tuple form kept for the test in test_completion.py (asserts no drift between
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
    from .completion import render
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


_SELF_CONTAINED_DISPATCH: Dict[str, Callable[[Dict[str, Any]], None]] = {
    "lint-overlay":  _dispatch_lint_overlay,
    "doctor":        _dispatch_doctor,
    "completion":    _dispatch_completion,
    "trace":         _dispatch_trace,
    "scaffold-list": _dispatch_scaffold_list,
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
    the referenced pipeline had unresolved targets or was malformed."""
    from .validate import validate_pipeline
    pipeline_ref = root.get("pipeline")
    if isinstance(pipeline_ref, str):
        pipeline_cfg = _read_json(_rel(base, pipeline_ref))
        validate_pipeline(pipeline_cfg)
        print("ok: {} is valid (root + pipeline {})".format(
            spec["root"], pipeline_ref))
    elif isinstance(pipeline_ref, dict):
        validate_pipeline(pipeline_ref)
        print("ok: {} is valid (root + inline pipeline)".format(spec["root"]))
    else:
        # Root validation already caught the missing/bad-type case above.
        # If we somehow got here without a `pipeline` field, fall back to the
        # original message rather than guessing.
        print("ok: {} is a valid root config".format(spec["root"]))


def _dispatch_baton_schema(spec: Dict[str, Any], root: Dict[str, Any], base: str) -> None:
    from .runtime import baton_schema
    asyncio.run(baton_schema(root, base, spec["baton_id"]))


def _dispatch_list(spec: Dict[str, Any], root: Dict[str, Any], base: str) -> None:
    from .runtime import list_gates
    asyncio.run(list_gates(root, base, as_json=bool(spec.get("json"))))


def _dispatch_clear(spec: Dict[str, Any], root: Dict[str, Any], base: str) -> None:
    from .runtime import clear_state
    asyncio.run(clear_state(root, base))


def _dispatch_resume(spec: Dict[str, Any], root: Dict[str, Any], base: str) -> None:
    from .runtime import resume_gate
    decision = _read_json(spec["decision_file"]) if spec["decision_file"] else {}
    asyncio.run(resume_gate(root, base, spec["baton_id"], decision))


def _dispatch_run(spec: Dict[str, Any], root: Dict[str, Any], base: str) -> None:
    from .runtime import run_root
    asyncio.run(run_root(root, base))


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
