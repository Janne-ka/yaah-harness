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

Run: `yaah <command> [args]` after `pip install`, or `python -m yaah.cli
<command> [args]` from a source checkout.

Targets Python 3.9+.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, Dict

from .harness import StageFailed
from .runtime_factories import _read_json, _rel
from .validate import validate_root


_USAGE = """\
yaah <command> [args]

Commands:
  init <dir>                    scaffold a linear starter pipeline (alias for `scaffold linear <dir>`)
  scaffold <archetype> <dir>    scaffold from a named archetype (linear / branch-with-gate / fork-fanin); see docs/archetypes.md
  run <root>                    run the configured pipeline (the default)
  list <root> [--json]          show parked gates (the mailbox view; --json for a parseable shape)
  resume <root> ID [FILE]       deliver a decision (optionally from FILE) to a parked gate
  clear <root>                  graceful reset: broadcast clear + flush parked + drop batons
  validate <root>               validate the config and exit (no run)
  explain <root>                print the EFFECTIVE config (post-_extends/_fake + defaults)
  trace <trace.jsonl> [PRICES]  summarize a run's trace (cost / latency / retries / model mix)
                                add --pretty for a per-run tree (stages, calls, errors)
                                add --errors-only for the CI-shaped check (exits non-zero on errors)
  baton-schema <root> <id>      print the JSON Schema of decision.json for one parked baton
  doctor                        diagnose install: Python version, optional deps, packaged base configs

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


def _parse_cli(argv: list) -> dict:
    """Parse the CLI into an action descriptor. Elegance #4 (assessment): the
    hand-rolled `args[0]` / `rest[:1]` parser silently fell through to "run"
    on any unknown flag — typos caught only by behavior. Now: unknown flags
    error out, `-h/--help` is documented, exclusivity is enforced. (argparse
    fights the optional-with-trailing-positional shape `--resume ID FILE`, so
    a tight ~30-line hand-parser is the cleaner answer here.)"""
    if not argv:
        _usage_exit("missing root config")
    if argv[0] in ("-h", "--help"):
        print("usage: " + _USAGE)
        raise SystemExit(0)
    root, rest = argv[0], list(argv[1:])
    fake = False
    if "--fake" in rest:
        fake = True
        rest.remove("--fake")
    debug = False
    if "--debug" in rest:  # global like --fake: full tracebacks instead of the
        debug = True       # message-only error boundary in main()
        rest.remove("--debug")
    as_json = False
    if "--json" in rest:   # scoped to --list (machine-readable mailbox view);
        as_json = True     # noise on any other action triggers the unknown-arg path below
        rest.remove("--json")
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


# Git-style subcommands (usability-gaps #1): the surface users expect, on top of
# the legacy `yaah <root> --flag` parser (kept working). Pipeline verbs translate
# to the same action spec _parse_cli produces; `validate`/`trace` add two actions.
_SUBCOMMANDS = ("init", "scaffold", "run", "list", "resume", "clear", "explain",
                "validate", "trace", "baton-schema", "doctor")
_VERB_FLAG = {"list": "--list", "clear": "--clear", "explain": "--explain"}


def _parse_subcommand(argv: list) -> dict:
    verb, rest = argv[0], list(argv[1:])
    if verb == "init":
        if not rest:
            _usage_exit("init needs a target directory")
        if len(rest) > 1:
            _usage_exit("init takes one argument (the target directory)")
        # `init` is a back-compat alias for `scaffold linear` — same dispatch path.
        return {"action": "scaffold", "target_dir": rest[0], "archetype": "linear"}
    if verb == "scaffold":
        # `scaffold <archetype> <target-dir>` — pick the named archetype and
        # write its template. See docs/archetypes.md for what each shape is for.
        if len(rest) < 2:
            from .init_template import ARCHETYPES
            _usage_exit(
                "scaffold needs an archetype and a target directory "
                "(archetypes: {})".format(", ".join(sorted(ARCHETYPES))))
        if len(rest) > 2:
            _usage_exit("scaffold takes two arguments (archetype, target directory)")
        return {"action": "scaffold", "archetype": rest[0], "target_dir": rest[1]}
    if verb == "run":
        return _parse_cli(rest) if rest else _usage_exit("run needs a root config")
    if verb in _VERB_FLAG:
        if not rest:
            _usage_exit("{} needs a root config".format(verb))
        return _parse_cli([rest[0], _VERB_FLAG[verb]] + rest[1:])
    if verb == "resume":
        if len(rest) < 2:
            _usage_exit("resume needs a root config and a baton id")
        return _parse_cli([rest[0], "--resume", rest[1]] + rest[2:])
    if verb == "validate":
        if not rest:
            _usage_exit("validate needs a root config")
        spec = _parse_cli(rest)        # parse root + --fake/--debug, then
        spec["action"] = "validate"    # check-only (never runs the pipeline)
        return spec
    if verb == "trace":
        flags = {"--debug", "--pretty", "--errors-only"}   # everything else is positional
        files = [a for a in rest if a not in flags]
        if not files:
            _usage_exit("trace needs a trace.jsonl path")
        if "--pretty" in rest and "--errors-only" in rest:
            _usage_exit("--pretty and --errors-only are mutually exclusive")
        return {"action": "trace", "trace_path": files[0],
                "price_map": files[1] if len(files) > 1 else None,
                "pretty": "--pretty" in rest,
                "errors_only": "--errors-only" in rest,
                "debug": "--debug" in rest}
    if verb == "doctor":
        # Diagnostic verb: no root, no positional args, no flags. Anything
        # extra is a typo — fail fast rather than silently dropping it.
        if rest:
            _usage_exit("doctor takes no arguments")
        return {"action": "doctor"}
    if verb == "baton-schema":
        # surface the decision-form shape of one parked baton so a driver skill
        # can compose decision.json mechanically. Needs a durable state: to see
        # batons parked by other processes.
        if len(rest) < 2:
            _usage_exit("baton-schema needs a root config and a baton id")
        if len(rest) > 2:
            _usage_exit("baton-schema takes one root config and one baton id")
        return {"action": "baton-schema", "root": rest[0], "baton_id": rest[1],
                "fake": False, "debug": False}
    _usage_exit("unknown command {!r}".format(verb))
    return {}  # unreachable


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


def _dispatch(spec: Dict[str, Any]) -> None:
    """Execute one parsed CLI action. Split from main() so the error boundary
    there wraps EVERYTHING that can raise a config/run error — load, _fake
    overlay, validate, assembly, and the run itself."""
    # Late imports for the action functions: avoids importing the engine's
    # assembly machinery on `yaah scaffold` / `yaah trace` / `yaah init` which
    # never assemble a harness (keeps the no-engine paths cheap and lets the
    # cli module stay small).
    if spec["action"] == "lint-overlay":
        # the positional file is an OVERLAY, not a root — lint and exit
        from .overlay_lint import lint_overlay
        problems = lint_overlay(spec["root"])
        if problems:
            print("overlay rejected ({} problem{}):".format(
                len(problems), "s" if len(problems) != 1 else ""))
            for p in problems:
                print("  - " + p)
            raise SystemExit(1)
        print("overlay ok — within the AI-mutable surface")
        return
    if spec["action"] == "doctor":
        from .doctor import diagnose
        code, report = diagnose()
        print(report, end="")
        raise SystemExit(code)
    if spec["action"] == "trace":
        # summarize a run's trace JSONL — no root config involved
        from .trace.aggregate import aggregate, load_jsonl
        records = load_jsonl(spec["trace_path"])
        price_map = _read_json(spec["price_map"]) if spec.get("price_map") else None
        if spec.get("errors_only"):
            # CI-shaped: exit code mirrors error presence; the print is just
            # informational — the meaningful signal is the exit code.
            from .trace.pretty import errors_only
            code, report = errors_only(records)
            print(report, end="")
            raise SystemExit(code)
        if spec.get("pretty"):
            from .trace.pretty import pretty
            print(pretty(records, price_map=price_map), end="")
            return
        print(json.dumps(aggregate(records, price_map=price_map), indent=2))
        return
    if spec["action"] == "scaffold":
        # Write the named archetype's template into target_dir.
        # `yaah init <dir>` enters here with archetype="linear" (back-compat).
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
        return
    from .runtime import (
        baton_schema, clear_state, explain_root, list_gates,
        resume_gate, run_root,
    )
    root = _read_json(spec["root"])
    if spec.get("fake"):
        root = _apply_fake_overlay(root)
    base = os.path.dirname(os.path.abspath(spec["root"]))
    action = spec["action"]
    if action == "explain":
        # R13: print effective config + provenance and exit. explain_root runs
        # validate_root itself so config errors surface here too.
        with open(spec["root"], "r", encoding="utf-8") as f:
            raw_user = json.load(f)
        explain_root(raw_user, root, base, root_path=spec["root"], fake=spec.get("fake", False))
        return
    validate_root(root)  # R15: one entry — unknown-key, shape, enum, cross-field
    if action == "validate":
        # Closes a real audit gap: previously `yaah validate` only checked the
        # root and pronounced "ok" even when the referenced pipeline file was
        # malformed, had an unknown node type, or pointed at a nonexistent
        # graph stage. Now both files are validated; the operator sees the
        # actual problem here, not when they `yaah run`.
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
            # If we somehow got here without a `pipeline` field, fall back to
            # the original message rather than guessing.
            print("ok: {} is a valid root config".format(spec["root"]))
        return
    if action == "baton-schema":
        asyncio.run(baton_schema(root, base, spec["baton_id"]))
        return
    if action == "list":
        asyncio.run(list_gates(root, base, as_json=bool(spec.get("json"))))
    elif action == "clear":
        asyncio.run(clear_state(root, base))
    elif action == "resume":
        decision = _read_json(spec["decision_file"]) if spec["decision_file"] else {}
        asyncio.run(resume_gate(root, base, spec["baton_id"], decision))
    else:
        asyncio.run(run_root(root, base))


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
    # bare `yaah` / -h / --help. Putting them here keeps _parse_cli's "missing
    # root config" branch focused on the real error case (user typed a flag
    # without a root) instead of confusingly firing on `yaah` alone.
    if argv and argv[0] in ("--version", "-V"):
        print("yaah {}".format(_resolve_version()))
        raise SystemExit(0)
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: " + _USAGE)
        raise SystemExit(0)
    spec = _parse_subcommand(argv) if (argv and argv[0] in _SUBCOMMANDS) else _parse_cli(argv)
    try:
        _dispatch(spec)
    except StageFailed as e:
        # the run failed a hard gate: the message names the stage + failures
        # (stage_failed.py carries the verdict) — that's the operator's answer;
        # the traceback is engine internals, shown only under --debug.
        if spec.get("debug"):
            raise
        print("pipeline failed: {}".format(e), file=sys.stderr)
        raise SystemExit(1) from None
    except (ValueError, OSError, ImportError) as e:
        # config-class errors (missing file, bad JSON, failed validation,
        # unknown type mid-build, fn: target whose module isn't on PYTHONPATH):
        # the message IS the fix; a 40-line traceback into the factory or
        # importlib buries it. --debug restores it.
        if spec.get("debug"):
            raise
        print("error: {}".format(e), file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
