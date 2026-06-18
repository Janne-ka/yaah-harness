"""Config-driven runtime bootstrap.

The 'what we spin up' config, separate from the pipeline config ('what the
stages are'). One root/deployment file declares the transport, the model
providers, the prompt sources, which pipeline to load, which roles this host
serves, and (optionally) the input to run. The runner is generic — spinning up
is config, not code. The same root config drives local in-proc, local-over-NATS,
or a cloud node (serve a subset of roles).

Root config shape (JSON; paths are relative to the root file's directory):
{
  "transport": {"type": "nats", "url": "nats://127.0.0.1:4222"}    // or {"type": "inproc"}
  "providers": {                          // model backends, keyed by provider name
    "claude": {"type": "claude_cli"},
    "fake":   {"type": "fake_scripted", "fixtures": "fixtures/eval.fake.json"}
  },
  "default_provider": "fake",             // provider used when a node's model has no 'provider:' prefix
  "state": {"type": "memory"},            // durable state store (default memory); backs baton resume + execute-once
  "prompt_sources": {"file": {"type": "file", "dir": "prompts"}},
  "default_prompt_source": "file",
  "trace": {"mode": "tracer", "capture": ["phase", "cost"],    // example OVERRIDE; omit `trace` for the
            "sinks": [{"type": "file", "path": "trace.jsonl"}]},  // defaults: mode tracer, capture [phase], console sink
  "pipeline": "eval-pipeline.json",
  "serve": "all",                         // roles this host serves: "all", ["role:eval", ...], or {"placement": "cloud"} (by node placement tag)
  "input": "fixtures/findings.json",      // optional
  "run": true,                            // run the orchestrator here too
  "decisions": {"data-audit": {"approved": true}},  // optional: gate-driver answers keyed by a gate's awaiting tag
  "interactive": false                    // optional: prompt stdin at each gate with no configured decision
}

When `decisions` or `interactive` is set, the orchestrator runs via the GATE
DRIVER (drives Suspended -> resume to completion) instead of stopping at the
first gate.

Layout: this module is assembly + the CLI entrypoints (`_assemble_harness`,
`run_root`/`list_gates`/`resume_gate`, `main`) plus the config-policy helper
`_resolve_serve`. Load-time validation lives in `yaah.validate` (R15: ONE entry
for unknown-key, shape, enum did-you-mean, and cross-field checks); the
config-block→runtime-leaf FACTORIES (the type maps + `_build_*`) live in
`runtime_factories.py`; the gate-driver's decider (`build_decider`) lives next
to `drive()` in `harness/gate_driver.py`. `_read_json`, `_build_decider`, and
`validate_root` are re-exported here for callers/tests.

Run: `yaah <root-config.json>` (installed console-script) or
`python -m yaah.runtime <root-config.json>` (uninstalled, with PYTHONPATH=src).
Targets Python 3.9+.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, Dict, Optional

from .build import build, harness_from_config, serve_from_config
from .core import Envelope, Kind
from .harness import BatonStore, StageFailed, Suspended, build_decider as _build_decider, drive
from .store import EnvelopeStore, IdempotencyStore
# Config-block → runtime-leaf factories (the maps + builders, split out so this
# module is just assembly + entrypoints). _read_json is re-exported here because
# tests/callers reach for it on the runtime namespace.
from .runtime_factories import (  # noqa: F401  (_read_json re-exported)
    _build_backend,
    _build_data_sink,
    _build_data_source,
    _build_mcp_source,
    _build_prompt_source,
    _build_store,
    _build_tracer,
    _build_transport,
    _read_json,
    _rel,
)


def _resolve_serve(serve: Any, pipeline: Dict[str, Any]) -> Optional[set]:
    """Resolve the root `serve` directive to the set of roles this host serves, or
    None to serve all. Forms:
      "all"                         -> None (every node)
      ["role:a", "role:b"]          -> exactly those roles
      {"placement": "cloud"}        -> every node tagged placement: cloud
      {"placement": ["cloud","either"]} -> union of those placements
    Placement tags live on the pipeline NODES (the single source of truth), so a
    host declares WHERE it runs and the role set follows — no manual per-host role
    list to keep in sync. A placement selector matching nothing raises (almost
    always a typo'd placement). Used by: run_root."""
    if serve == "all":
        return None
    if isinstance(serve, dict) and "placement" in serve:
        want = serve["placement"]
        want = {want} if isinstance(want, str) else set(want)
        nodes = pipeline.get("nodes", {})
        roles = {role for role, spec in nodes.items() if spec.get("placement") in want}
        if not roles:
            present = sorted({spec.get("placement") for spec in nodes.values()
                              if spec.get("placement")})
            raise ValueError(
                "serve placement {} matched no nodes; placements present: {}".format(
                    sorted(want), present))
        return roles
    if isinstance(serve, str):  # a bare role string is ONE role, not a char iterable
        serve = [serve]         # ("role:eval" must not become {'r','o','l',...})
    return set(serve)


async def _assemble_harness(root: Dict[str, Any], base: str) -> Any:
    """Spin up the orchestrator-side Harness from the root config — transport,
    backend, prompt/data/mcp layers, the durable state store, and the nodes (served
    over a bus, or registered in-process). Shared by run_root and resume_gate so a
    run and a later cross-process resume use the SAME wiring over the SAME store.
    The store is what makes a parked gate resumable from another process."""
    backend = _build_backend(root, base)
    prompts = _build_prompt_source(root, base)
    data = _build_data_source(root, base)
    sink = _build_data_sink(root, base)
    mcp = _build_mcp_source(root, base)
    comms = await _build_transport(root.get("transport") or {}, base)
    # injected tracer (default ON at minimum capture); its sinks subscribe to the
    # trace topic on this comms so spans the harness/agents publish are persisted.
    tracer = await _build_tracer(root, comms, base)

    pipeline = _read_json(_rel(base, root["pipeline"]))
    # Timeout budget coherence (BUG-635/626 class) — the assemble step is the
    # one place the deployment root (transport ceiling) and the pipeline
    # (per-node timeouts, fork waits) meet, so the admission check lives here.
    validate_budgets(root, pipeline)
    # live-vars mechanism (a): `live_config: true` makes every node re-read its
    # MUTABLE leaves (model/knobs/numeric bounds — validate.MUTABLE_LEAF_KEYS
    # line) from the pipeline file per invocation, mtime-cached. Opt-in;
    # default stays frozen-at-build.
    live_path = _rel(base, root["pipeline"]) if root.get("live_config") else None
    roles = _resolve_serve(root.get("serve", "all"), pipeline)

    # One state store backs the resume-cursor (BatonStore) and execute-once
    # (IdempotencyStore); default in-memory = today's behavior.
    store = _build_store(root.get("state"), base)
    baton_store = BatonStore(store)
    idem_store = IdempotencyStore(store)
    env_store = EnvelopeStore(store)  # gate parking (fan-in arrivals) over the same Store

    if hasattr(comms, "serve_node"):  # bus / NATS: serve, then orchestrate
        served = await serve_from_config(pipeline, comms, backend=backend, prompt_source=prompts,
                                         data_source=data, data_sink=sink, mcp_source=mcp,
                                         idempotency_store=idem_store, tracer=tracer,
                                         roles=roles, base_dir=base,
                                         live_config_path=live_path)
        print("served:", served)
        return harness_from_config(pipeline, comms, baton_store=baton_store,
                                   envelope_store=env_store, tracer=tracer)
    # in-process: build registers everything
    return build(pipeline, comms=comms, backend=backend, prompt_source=prompts,
                 data_source=data, data_sink=sink, mcp_source=mcp,
                 idempotency_store=idem_store, baton_store=baton_store,
                 envelope_store=env_store, tracer=tracer, base_dir=base,
                 live_config_path=live_path)


async def run_root(root: Dict[str, Any], base: str) -> Optional[Envelope]:
    harness = await _assemble_harness(root, base)

    if not root.get("run", "input" in root):
        # serve-only worker: stay alive so the served subscriptions keep handling
        # requests for the process's lifetime (it's a remote node, not a one-shot).
        print("serving; awaiting requests", flush=True)
        await asyncio.Event().wait()
        return None

    # Build the ONE starting message of the run. Everything in YAAH is an Envelope
    # (one message shape, used everywhere), and the run begins by dropping a single
    # Kind.TASK envelope into the harness; the graph's `start` stage receives it and
    # each stage's output becomes the next stage's input. The envelope's `payload`
    # is whatever the FIRST stage's prompt/template reads (e.g. {"task": "...",
    # "request": "..."}); the harness adds the headers (correlation_id, etc.) itself.
    #
    # That payload is the root's `input`, supplied two ways (hug-the-world): an inline
    # object for small demos/tests (no one-line fixture file needed), or a path to a
    # JSON fixture for real inputs. A dict is used verbatim; a string is read as a
    # base-relative fixture path. Absent `input` -> an empty payload (a pipeline whose
    # start stage needs no seed data).
    raw_input = root.get("input", {})
    payload = raw_input if isinstance(raw_input, dict) else _read_json(_rel(base, raw_input))
    run_kw = {"ttl": root["baton_ttl"]} if "baton_ttl" in root else {}
    task = Envelope(Kind.TASK, payload)
    decider = _build_decider(root)
    if decider is not None:  # drive gates to completion (resume at each Suspended)
        out = await drive(harness, task, decider, **run_kw)
    else:  # default: run once; a gated pipeline stops (Suspended) at the first gate
        out = await harness.run(task, **run_kw)
    if isinstance(out, Suspended):  # parked — durable state lets another process resume it
        print("GATE baton_id={} awaiting={} concerns={}".format(
            out.baton_id, out.awaiting, len(out.concerns)))
        _print_concerns(out.concerns)
    print("RESULT:", _short(out))
    return getattr(out, "output", None)


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


def _baton_json(b: "Baton") -> Dict[str, Any]:
    """The mailbox-view JSON shape for one suspended baton. Stable contract for
    driver skills consuming `yaah list --json`: `{id, stage, awaiting, concerns,
    question}` (question is null when the gate has no `question`/`ask` key)."""
    q = None
    if b.pending is not None:
        q = b.pending.payload.get("question") or b.pending.payload.get("ask")
    return {"id": b.id, "stage": b.stage, "awaiting": b.awaiting,
            "concerns": [dict(c) for c in b.concerns],
            "question": q}


async def list_gates(root: Dict[str, Any], base: str, *, as_json: bool = False) -> None:
    """The mailbox view: print every suspended run waiting on a decision. Needs a
    durable `state:` to see gates parked by other processes. `--list` entrypoint.

    `as_json=True` emits a single JSON document `{"batons": [...]}` with the
    same fields the prose view shows — so a driver skill can parse instead of
    interpret. Per-baton shape lives in `_baton_json`.
    """
    bstore = BatonStore(_build_store(root.get("state"), base))
    gates = await bstore.list_suspended()
    if as_json:
        print(json.dumps({"batons": [_baton_json(b) for b in gates]}, indent=2))
        return
    for b in gates:
        print("GATE baton_id={} stage={} awaiting={} concerns={}".format(
            b.id, b.stage, b.awaiting, len(b.concerns)))
        _print_concerns(b.concerns)
        # surface the pending question/ask so the human knows what to answer
        if b.pending is not None:
            q = b.pending.payload.get("question") or b.pending.payload.get("ask")
            if q:
                print("  question: {}".format(q))
    if not gates:
        print("(no suspended gates)")


async def resume_gate(root: Dict[str, Any], base: str, baton_id: str,
                      decision: Dict[str, Any]) -> Optional[Envelope]:
    """Deliver a human decision to a parked gate and drive the rest to completion —
    possibly in a different process than the one that suspended it (the durable
    store is the rendezvous). `--resume` entrypoint."""
    harness = await _assemble_harness(root, base)
    out = await harness.resume(baton_id, Envelope(Kind.RESUME, decision))
    if isinstance(out, Suspended):  # hit the next gate
        print("GATE baton_id={} awaiting={} concerns={}".format(
            out.baton_id, out.awaiting, len(out.concerns)))
        _print_concerns(out.concerns)
    print("RESULT:", _short(out))
    return getattr(out, "output", None)


async def baton_schema(root: Dict[str, Any], base: str, baton_id: str) -> None:
    """Surface a parked baton's decision-form shape — the contract a driver
    skill composes decision.json against. Reads form/decision_schema off
    baton.pending.payload (HumanGate stamps them on the AWAIT envelope; the
    harness parks that envelope as baton.pending). Exit 1 if no such baton, or
    if the baton wasn't parked by a HumanGate (no `form` declared)."""
    from .harness.decision_forms import lookup
    bstore = BatonStore(_build_store(root.get("state"), base))
    baton = await bstore.load(baton_id)
    if baton is None:
        print("error: no baton with id {!r}".format(baton_id), file=sys.stderr)
        raise SystemExit(1)
    if baton.pending is None:
        print("error: baton {!r} has no parked envelope (not a human gate?)".format(baton_id),
              file=sys.stderr)
        raise SystemExit(1)
    form = baton.pending.payload.get("form")
    inline = baton.pending.payload.get("decision_schema")
    if form is None:
        print("error: baton {!r} parked without a declared form — add `form: \"...\"` "
              "to the human_gate node to surface its decision shape".format(baton_id),
              file=sys.stderr)
        raise SystemExit(1)
    out = lookup(form, inline_schema=inline)
    out["baton_id"] = baton_id
    out["awaiting"] = baton.awaiting
    print(json.dumps(out, indent=2))


async def clear_state(root: Dict[str, Any], base: str) -> None:
    """CLEAR the harness instead of killing the process: broadcast a `*` clear (every
    in-flight clearable node cancels, every waiting gate releases), flush the parked
    set, and drop suspended batons — a graceful reset over the SAME store/transport
    the runs use. `--clear` entrypoint."""
    harness = await _assemble_harness(root, base)
    result = await harness.clear()
    print("CLEARED:", result)


# R15: root-config validation (unknown-key, shape, enum did-you-mean, cross-field)
# lives in `yaah.validate`. Re-exported here for back-compat with `yaah.runtime`
# importers (notably tests). The keys-spec, shape table, enum tables, and
# documented surface are all in that one module — the AI skill's ground truth.
from .validate import _DEFAULTS, validate_budgets, validate_root  # noqa: E402  (re-export)
_validate_root = validate_root        # back-compat alias for older test imports


def _trace_extends_chain(root_path: str) -> list:
    """Walk a root file's `_extends` chain top-down, returning a list of
    (basename, raw_dict) starting with the user's own file and following each
    `_extends` link. Used by `explain_root` to attribute each top-level key to
    its source file (R13 provenance)."""
    chain = []
    seen = set()
    p = os.path.abspath(root_path)
    while p not in seen:
        seen.add(p)
        with open(p, "r", encoding="utf-8") as f:
            raw = json.load(f)
        chain.append((os.path.basename(p), raw))
        ext = raw.get("_extends") if isinstance(raw, dict) else None
        if not ext:
            break
        p = ext if os.path.isabs(ext) else os.path.normpath(os.path.join(os.path.dirname(p), ext))
    return chain


def explain_root(raw_user: Dict[str, Any], effective: Dict[str, Any],
                 base: str, *, root_path: str, fake: bool = False) -> None:
    """R13 — print the EFFECTIVE root config (post-`_extends` + post-`_fake` +
    defaults) with per-key provenance. The Spring `--debug` conditions report
    / `helm template` / `terraform plan` equivalent: 'what would actually load
    here?'. Used by: `yaah <root> --explain`.

    Sources:
      `(user)`             — set in the user's own root file
      `(extends:<base>)`   — inherited from an `_extends` base file
      `(fake)`             — overlaid by the `_fake` block under `--fake`
      `(default)`          — runtime default from `validate._DEFAULTS`
    Validates the effective config first (R15) — surfaces config errors with
    actionable messages before printing anything else.
    """
    validate_root(effective)
    chain = _trace_extends_chain(root_path)
    fake_keys = set((raw_user.get("_fake") or {}).keys()) if fake else set()
    user_keys = {k for k in raw_user if not k.startswith("_")}

    # Skip `_`-prefixed comment keys (`_about`, `_fake`, ...). When `--fake` was
    # given they're already merged into `effective` under their real key names;
    # when it wasn't, they're documentation and don't belong in the report.
    all_keys = sorted(k for k in (set(effective) | set(_DEFAULTS)) if not k.startswith("_"))
    sources: Dict[str, str] = {}
    for k in all_keys:
        if fake and k in fake_keys:
            sources[k] = "(fake)"   # fake overlay wins (it's why the user passed --fake)
        elif k in user_keys:
            sources[k] = "(user)"
        elif k in effective:
            label = "(?)"
            for basename, raw in chain[1:]:
                if isinstance(raw, dict) and k in raw:
                    label = "(extends:{})".format(basename)
                    break
            sources[k] = label
        else:
            sources[k] = "(default)"

    print("Effective root config (R13 explain — post-_extends + post-_fake + defaults):")
    print()
    for k in all_keys:
        val = effective[k] if k in effective else _DEFAULTS.get(k)
        s = json.dumps(val)
        if len(s) > 60:
            s = s[:57] + "..."
        print("  {:<22} {:<28} {}".format(k, sources[k], s))
    print()
    print("Effective JSON:")
    full = {k: (effective[k] if k in effective else _DEFAULTS.get(k)) for k in all_keys}
    print(json.dumps(full, indent=2, sort_keys=True))

    # Repo blast radius (BUG-693B #4): before spending tokens, show what this run
    # can DO TO A REPO — which stages run in a worktree, what commands they run,
    # what they may merge, and the declared scope contract. Derived from the merged
    # pipeline (post-`_extends`, so project facts are included). Read-only stages
    # are omitted; the point is the write/execute surface.
    if "pipeline" in effective:
        try:
            pipeline = _read_json(_rel(base, effective["pipeline"]))
        except Exception:
            pipeline = None
        if isinstance(pipeline, dict):
            _print_blast_radius(pipeline.get("nodes") or {})


def _print_blast_radius(nodes: Dict[str, Any]) -> None:
    """Per node, the file-touch / execute surface a run could exercise. Generic —
    reads node TYPES (worktree/shell/shell_check/transform) and config, names no
    app stages."""
    rows = []
    for role, spec in nodes.items():
        if not isinstance(spec, dict):
            continue
        t = spec.get("type")
        cfg = spec.get("config") or {}
        if t == "worktree":
            rows.append((role, "worktree", "repo {} (branch {}*)".format(
                spec.get("repo", "?"), spec.get("branch_prefix", "yaah/"))))
        elif t in ("shell", "shell_check"):
            cmd = spec.get("command")
            rows.append((role, t, "runs: {}".format(
                " ".join(cmd) if isinstance(cmd, list) else cmd)))
        elif t == "transform" and "merge_task_branch" in str(spec.get("target", "")):
            rows.append((role, "merge", "merges into repo {}".format(cfg.get("repo", "?"))))
        elif t == "transform" and "scope_check" in str(spec.get("target", "")):
            allowed = cfg.get("extra_allowed", [])
            scope = "test-paths only" if cfg.get("include_spec_files") is False else "spec.affected_files + " + str(allowed)
            rows.append((role, "scope-contract", "may touch: {}".format(scope)))
        elif spec.get("cwd_from"):  # an agent/node running IN the worktree
            tools = spec.get("allowed_tools")
            rows.append((role, t or "node", "runs in worktree{}".format(
                " with edit tools" if tools else "")))
    if not rows:
        print("\nRepo blast radius: none (no repo-touching stages).")
        return
    print("\nRepo blast radius (write / execute surface):")
    for role, kind, detail in rows:
        print("  {:<28} {:<14} {}".format(role, kind, detail))


_USAGE = """\
yaah <command> [args]

Commands:
  init <dir>                    scaffold a starter pipeline (runs on fake providers, no API key)
  run <root>                    run the configured pipeline (the default)
  list <root> [--json]          show parked gates (the mailbox view; --json for a parseable shape)
  resume <root> ID [FILE]       deliver a decision (optionally from FILE) to a parked gate
  clear <root>                  graceful reset: broadcast clear + flush parked + drop batons
  validate <root>               validate the config and exit (no run)
  explain <root>                print the EFFECTIVE config (post-_extends/_fake + defaults)
  trace <trace.jsonl> [PRICES]  summarize a run's trace (cost / latency / retries / model mix)
  baton-schema <root> <id>      print the JSON Schema of decision.json for one parked baton

Options (on run/list/resume/clear/validate/explain):
  --fake     merge the root's `_fake` block over the top level (sidecar fake providers/state)
  --debug    full tracebacks on errors (default: message + exit 2 config / 1 run)
  -h --help  show this message

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
_SUBCOMMANDS = ("init", "run", "list", "resume", "clear", "explain", "validate",
                "trace", "baton-schema")
_VERB_FLAG = {"list": "--list", "clear": "--clear", "explain": "--explain"}


def _parse_subcommand(argv: list) -> dict:
    verb, rest = argv[0], list(argv[1:])
    if verb == "init":
        if not rest:
            _usage_exit("init needs a target directory")
        if len(rest) > 1:
            _usage_exit("init takes one argument (the target directory)")
        return {"action": "init", "target_dir": rest[0]}
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
        files = [a for a in rest if a != "--debug"]
        if not files:
            _usage_exit("trace needs a trace.jsonl path")
        return {"action": "trace", "trace_path": files[0],
                "price_map": files[1] if len(files) > 1 else None,
                "debug": "--debug" in rest}
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
    if spec["action"] == "trace":
        # summarize a run's trace JSONL — no root config involved
        from .trace.aggregate import aggregate, load_jsonl
        records = load_jsonl(spec["trace_path"])
        price_map = _read_json(spec["price_map"]) if spec.get("price_map") else None
        print(json.dumps(aggregate(records, price_map=price_map), indent=2))
        return
    if spec["action"] == "init":
        # scaffold the embedded hello-yaah template into target_dir
        from .init_template import scaffold
        target = spec["target_dir"]
        try:
            n = scaffold(target)
        except FileExistsError as e:
            print("error: " + str(e), file=sys.stderr)
            raise SystemExit(2)
        print("Created {} files in {}/".format(n, target))
        print("Next: yaah run {}/starter.local.json".format(target))
        return
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


def main() -> None:
    argv = sys.argv[1:]
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
    except (ValueError, OSError) as e:
        # config-class errors (missing file, bad JSON, failed validation,
        # unknown type mid-build): the message IS the fix; a 40-line traceback
        # into the factory buries it (assessment DX). --debug restores it.
        if spec.get("debug"):
            raise
        print("error: {}".format(e), file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
