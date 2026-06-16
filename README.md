# YAAH 🥱

Yet Another Agentic Harness. A generic, distributed runtime for orchestrating agentic workers.

YAAH treats agents as workers, not first-class citizens. The harness owns routing and control; a worker does one job and is interchangeable. (In our harness, agents are first-class citizens only in their dreams, after they clock off.)

The system has three concepts:

- **Envelope** — one message shape, used everywhere.
- **Node** — `invoke(input, config) → output`. Every worker, including agents.
- **Comms** — `request` / `publish` / `subscribe`. The only thing the harness calls.

Workers do not address each other; the harness routes between them. Parts are loosely coupled and replaceable. YAAH runs locally, in the cloud, or split across both; placement is configuration, not code.

## Status

**Working and proven**, pre-1.0 (error handling is deliberately not hardened yet). What exists today:

- **Harness** — graph runner with a validator retry-with-feedback loop, fan-out, conditional branching, and durable **suspend/resume** for human gates. Baton lifecycle is bounded (evict on terminal outcome + TTL sweep).
- **Pluggable layers, all on one `source:key` pattern** — prompts, data **get/post**, **MCP**, and a model-backend router (`provider:model`).
- **Node library** — `agent`, `get`, `post`, `transform` (fn/node/http), plus `shell`, `shell_check`, `render`, `python`, `worktree`, `once`, and validators.
- **Agent capability triad** — prompt + tools (LiteLLM loop & Claude-native) + MCP.
- **Distribution** — multi-process over NATS with auth + TLS + subject scoping + queue-group shared pools. `LocalBus` is the offline, wire-faithful proof; `InProcessComms` is the zero-infra default.
- Real end-to-end runs with `claude -p`, including a repo-bound worktree→RED→code→GREEN→diff→review pipeline.

Test suite is green (in-process/local) plus real-NATS tests under a venv. See [`docs/ROADMAP.md`](docs/ROADMAP.md) for what's next.

## Layout

The folder structure makes the swap layer visible (see [`docs/design.md`](docs/design.md) §2):

- **Engine** — `core/ comms/ harness/ agents/ nodes/ build/ runtime.py`, plus the port + zero-config default for each pluggable layer (`prompts/ data/ mcp/ store/`).
- **Adapters** (`adapters/{transports,backends,prompts,data,mcp,stores}/`) — every implementation that binds to an outside system (NATS, `claude`/LiteLLM, file/HTTP/Langfuse, git, file store). Adapters import the engine, never the reverse.

## Run

```bash
# Run a pipeline from a root/deployment config (transport, providers, prompt
# sources, which pipeline, which roles this host serves, optional input):
python -m yaah.runtime <root-config.json>

# Inspect parked human gates, and deliver a decision to resume a suspended run:
python -m yaah.runtime <root-config.json> --list
python -m yaah.runtime <root-config.json> --resume <baton_id> [decision.json]
```

Targets Python 3.9+. See `examples/` for runnable configs and `docs/` for the deeper references:
[`architecture.md`](docs/architecture.md) (layers + node mapping), [`node-reference.md`](docs/node-reference.md) + [`root-config-reference.md`](docs/root-config-reference.md) (config keys), [`design.md`](docs/design.md) (rationale/decisions), [`agent-tools.md`](docs/agent-tools.md) (tools/MCP), [`durable-state.md`](docs/durable-state.md) (baton/idempotency/memory).

## Environments & dependency hardening (pixi)

The **core has zero runtime dependencies**; every third-party library is an
opt-in adapter, declared as a PEP 621 extra in `pyproject.toml`
(`pip install yaah[litellm,nats,langfuse,http]`, or `[all]`). That metadata is
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
