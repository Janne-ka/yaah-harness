# YAAH 🥱

Yet Another Agentic Harness. A generic, distributed runtime for orchestrating agentic workers.

YAAH treats agents as workers, not first-class citizens. The harness owns routing and control; a worker does one job and is interchangeable. (In our harness, agents are first-class citizens only in their dreams, after they clock off.)

The system has three concepts:

- **Envelope** — one message shape, used everywhere.
- **Node** — `invoke(input, config) → output`. Every worker, including agents.
- **Comms** — `request` / `publish` / `subscribe`. The only thing the harness calls.

Workers do not address each other; the harness routes between them. Parts are loosely coupled and replaceable. YAAH runs locally, in the cloud, or split across both; placement is configuration, not code.

YAAH is a coding harness used **on top of** coding — the process layer around the inner loop. Tools like Cline / Aider / Cursor are great at "write the code"; the wider process (evals, multi-stage review, human gates, parallel branches, durable state, cost/usage instrumentation) is what YAAH covers and what most coding harnesses do lightly or partially.

## Getting started

```bash
pip install -e .                                       # zero-dep core (add ".[all]" for every adapter)
yaah scaffold linear my-pipeline                       # write a runnable starter
cd my-pipeline && yaah run starter.local.json          # 2-stage agent → render on a fake backend; no API key
```

The scaffold writes a 2-stage `agent → render` pipeline (parse-by-default per [ADR-0004](docs/decisions/0004-parse-by-default.md); the explicit validate+parse middle stages are no longer needed). Then:

- **[docs/archetypes.md](docs/archetypes.md)** — the five pipeline shapes; pick the nearest one to your problem and scaffold it (`yaah scaffold <archetype> <dir>`).
- **[docs/quickstart.md](docs/quickstart.md)** — the 5-minute version, explained.
- **[docs/tutorial.md](docs/tutorial.md)** — **how to build a pipeline for your own task** (a 5-step recipe up front), then each concept explained: validators & retry, branching, a human gate with durable suspend/resume, fork/fan-in, going real, tracing.
- **[examples/](examples/)** — runnable: `hello-yaah` (linear), `review-pipeline` (branch + human gate), `fork-join` (parallel + reduce), `arch-drift` (instrumented, A/B + cost attacher), `config-flow` (meta-tool — visualize any YAAH config).

**Building with an AI assistant?** This repo ships agent helpers — point your tool
(Codex, Cursor, Copilot, Claude Code) at **[AGENTS.md](AGENTS.md)** and describe the
pipeline you want in plain language; it knows the node types, the data-flow
contract, and the guardrails, and will draft the JSON for you to verify offline.

## Status

**Working and proven**, pre-1.0 (error handling is deliberately not hardened yet). What exists today:

- **Harness** — graph runner with a validator retry-with-feedback loop, fan-out, conditional branching, and durable **suspend/resume** for human gates. Baton lifecycle is bounded (evict on terminal outcome + TTL sweep).
- **Parse-by-default agents** ([ADR-0004](docs/decisions/0004-parse-by-default.md)) — agents auto-merge their JSON output onto the payload; the data-flow contract is enforced at load time.
- **Pluggable layers, all on one `source:key` pattern** — prompts, data **get/post**, **MCP**, and a model-backend router (`provider:model`).
- **Node library** — `agent`, `get`, `post`, `transform` (fn/node/http), plus `shell`, `shell_check`, `render`, `worktree`, and validators.
- **Agent capability triad** — prompt + tools (LiteLLM loop & Claude-native) + MCP. Plus the [attacher port](docs/decisions/0003-attacher-port.md) for surfacing per-call cost/usage onto the payload.
- **Distribution** — multi-process over NATS with auth + TLS + subject scoping + queue-group shared pools. `LocalBus` is the offline, wire-faithful proof; `InProcessComms` is the zero-infra default.
- **Form + ergonomics** — five named [pipeline archetypes](docs/archetypes.md) + `yaah scaffold <archetype>` CLI + one-page [shape-grammar card](docs/shape-grammar.md) + IDE-ready [JSON Schemas](schemas/) + copy-paste [cookbook recipes](docs/cookbook/).
- **AI-assistant skills** — five structured [skills](.claude/skills/) for authoring (`yaah-pipeline-authoring`), extending the engine (`yaah-extending`), operating a running pipeline (`yaah-driving`), reviewing (`yaah-reviewing`), and pre-submission self-review (`yaah-review-my-pr`).
- Real end-to-end runs with `claude -p`, including a repo-bound worktree→RED→code→GREEN→diff→review pipeline.

Test suite is green (in-process/local) plus real-NATS tests under a venv. See [`docs/ROADMAP.md`](docs/ROADMAP.md) for what's next.

## Layout

The folder structure makes the swap layer visible (see [`docs/design.md`](docs/design.md) §2):

- **Engine** — `core/ comms/ harness/ agents/ nodes/ build/ runtime.py cli.py validators/`, plus the port + zero-config default for each pluggable layer (`prompts/ data/ mcp/ store/`).
- **Adapters** (`adapters/{transports,backends,prompts,data,mcp,stores}/`) — every implementation that binds to an outside system (NATS, `claude`/LiteLLM, file/HTTP/Langfuse, git, file store). Adapters import the engine, never the reverse.

## Run

```bash
yaah scaffold <archetype> <dir>       # scaffold a starter (linear / branch-with-gate / fork-fanin)
yaah run <root>                       # run the configured pipeline (the default; `yaah <root>` also works)
yaah list <root> [--json]             # mailbox view: every suspended baton
yaah baton-schema <root> <id>         # surface the parked gate's decision form
yaah resume <root> <id> [decision]    # deliver a decision + drive to next gate / completion
yaah validate <root>                  # validate + exit; no run
yaah explain <root>                   # print the EFFECTIVE config (post-_extends/_fake)
```

Full verb list + per-key reference: [`docs/shape-grammar.md`](docs/shape-grammar.md). Legacy `python3 -m yaah.runtime <root> [--list | --resume …]` still works.

Targets Python 3.9+. See `examples/` for runnable configs and **[`docs/`](docs/README.md)** for everything else —
[`archetypes.md`](docs/archetypes.md) (pipeline shapes), [`shape-grammar.md`](docs/shape-grammar.md) (the one-page card), [`architecture.md`](docs/architecture.md) (layers + node mapping), [`node-reference.md`](docs/node-reference.md) + [`root-config-reference.md`](docs/root-config-reference.md) (config keys), [`design.md`](docs/design.md) (rationale/decisions), [`agent-tools.md`](docs/agent-tools.md) (tools/MCP), [`durable-state.md`](docs/durable-state.md) (baton/idempotency/memory), [`cookbook/`](docs/cookbook/) (copy-paste recipes), [`decisions/`](docs/decisions/) (ADRs). Full index: **[`docs/README.md`](docs/README.md)**.

## Environments & dependency hardening (pixi)

The **core has zero runtime dependencies**; every third-party library is an
opt-in adapter, declared as a PEP 621 extra in `pyproject.toml`
(`pip install yaah-harness[litellm,nats,langfuse,http]`, or `[all]`; the import
name stays `yaah`). That metadata is
tool-agnostic — pip, uv, conda, and pixi all read it.

For a reproducible environment use the committed [`pixi.toml`](pixi.toml). pixi
speaks both conda packages and PyPI:

```bash
pixi install              # default env: zero-dep core
pixi run -e full test     # full env: every adapter; runs the suite
```

The resolved versions are written to **`pixi.lock` (commit it)**, so the build is
reproducible across machines. If your organization (or conda-forge) ships a conda
constraints package that uses `run_constraints` to block known-vulnerable version
ranges, add it in `pixi.toml` to harden the adapter versions — the lock then
carries those pins. `scripts/run_tests.py` runs the suite standalone (also what
CI uses).

## Applications

YAAH is the engine; applications are built on top of it. The first one is a
multi-stage code-change pipeline (a separate project). YAAH depends on no
application — if a domain term such as "spec" or "RED" appears in YAAH, an
abstraction is not yet generic.
