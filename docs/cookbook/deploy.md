# Deploying YAAH to production

Patterns for getting a pipeline off your laptop and into a service or a CI
worker. The engine ships zero deployment surface — these are conventions you
adopt in *your* project, not bindings the engine forces.

The four moving parts:

1. **Transport** — in-process for one binary, NATS for many.
2. **State store** — memory for tests, file for one box, durable backend
   for a service that survives restart.
3. **Secrets** — API keys come from env vars, never from config files.
4. **Observability** — trace JSONL on disk, optional Langfuse sink.

See also [offline-runs.md](offline-runs.md) for offline / CI patterns —
deploy.md is the real-mode complement.

## 1 — Single-binary container

The simplest production shape: one Docker image, one process, your pipeline
runs against a real provider. State is local to the container (lost on
restart unless you mount a volume).

```dockerfile
# Dockerfile
FROM python:3.12-slim AS base

# Engine core is zero-deps; extras pull only what you use.
# Pin the version explicitly in your build (this snippet uses `latest`).
RUN pip install --no-cache-dir 'yaah-harness[litellm,langfuse]'

# Your pipeline ships beside the image so the configs ride with the binary.
COPY my-pipeline/ /app/my-pipeline/
WORKDIR /app

# Pass secrets via env. NEVER bake them into the image or the config.
# yaah propagates the standard provider env vars to the backend's child
# process (claude_cli) or to litellm.
ENV YAAH_TRACE_PATH=/var/lib/yaah/trace.jsonl

CMD ["yaah", "run", "my-pipeline/root.json"]
```

Build and run:

```bash
docker build -t my-yaah .
docker run --rm \
  -e ANTHROPIC_API_KEY=sk-... \
  -v $PWD/state:/var/lib/yaah \
  my-yaah
```

**State mount** — without `-v`, the container's `state.dir` lives inside
the layer and dies on restart. Mount a host path or a named volume for
anything past a smoke test. `state.dir` in your `root.json` should match
the container path you mount (`/var/lib/yaah` in the example).

## 2 — Distributed: orchestrator + worker fleet over NATS

When work is heavy or you want different machines for different stages,
switch transport to NATS and split the run into one orchestrator + many
workers (each serving a subset of roles).

The engine ships a base config you can extend:

```json
{
  "_about": "production root extending the packaged NATS base",
  "_extends": "yaah:bases/nats.base.json",

  "transport": {"type": "nats", "url": "nats://nats.internal:4222",
                 "request_timeout": 300.0},
  "state": {"type": "file", "dir": "/var/lib/yaah/state"},

  "providers": {"claude": {"type": "claude_cli", "binary": "claude"}},
  "default_provider": "claude",

  "pipeline": "my-pipeline.json",
  "input": "fixtures/input.json",
  "serve": "all"
}
```

Each worker host runs the same image with a different `serve` value
naming the roles it claims:

```bash
# orchestrator (drives the graph, parks gates, writes state)
yaah run root.json   # serve: ["__driver__"] or "all" in dev

# workers (one per role; scale horizontally as needed)
YAAH_SERVE=role:summarize  yaah run root.json
YAAH_SERVE=role:verify     yaah run root.json
```

(Set `"serve"` in the root config or via the `YAAH_SERVE` env if your shell
prefers — see [root-config-reference.md](../root-config-reference.md) for
the full grammar.)

**Backpressure + timeouts** — NATS request timeouts are set in
`transport.request_timeout`. Per-stage timeouts go in the pipeline. Pick
both deliberately: the transport ceiling must exceed any stage timeout it
carries, or the stage gets killed before it can finish. `validate_budgets`
runs at assemble time to catch the inverted case.

**TLS + auth** — NATS subject ACLs are how the engine expects auth to be
done. The transport accepts the standard nats-py kwargs (`tls`,
`user_credentials`, `nkeys_seed`) — see `src/yaah/adapters/transports/nats_comms.py`
for what's wired today.

## 3 — State store choices

| Choice | When |
|---|---|
| `{"type": "memory"}` | Tests + smoke runs only. Lost on restart. |
| `{"type": "file", "dir": "..."}` | Single-host service. Mount the dir as a volume. Resumes on restart. |
| Custom durable store | Multi-host: you implement `StoreBackend` against Redis / Postgres / DynamoDB / etc. and register it. The protocol is in `src/yaah/store/store.py`. |

The same store backs four things: parked-baton (suspend/resume),
idempotency (execute-once across retries), envelope buffering
(fanin barrier arrivals), and any working memory your transforms keep.
One store, four uses — sized accordingly.

## 4 — Secrets: env, not config

**Never** put API keys, tokens, or signing secrets in `root.json` /
`pipeline.json` / overlays. The provider adapters (`claude_cli`,
`litellm`) read keys from the standard env vars (`ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, etc.). Mount keys via your secret manager (Vault,
AWS Secrets Manager, K8s Secret) and inject them as env at container
start.

Config files belong in version control; secrets do not. The split is
mechanical: env vars never appear in a yaah config; config values never
appear in env. (`{base_dir}` is the only templating the engine does, and
it's resolved at build time from the config file's location, not from
env.)

## 5 — Tracing in production

The engine writes a JSONL trace by default. Two configurations matter:

```json
"trace": {
  "mode": "tracer",
  "capture": ["phase", "cost"],
  "sinks": [
    {"type": "file", "path": "/var/lib/yaah/trace.jsonl"},
    {"type": "langfuse"}
  ]
}
```

- **`capture`** — `phase` (cheap, default), `cost` (tokens + model),
  `tools` (tool-call detail). Each is independent; pick what your
  observability stack actually consumes.
- **Sinks compose** — file is local + replayable; Langfuse is hosted +
  searchable. Both run at once when both are listed.
- **Reading the trace** — `yaah trace <jsonl> --pretty` for one-run debug;
  `yaah trace <jsonl> --cost prices.json` for the spend rollup;
  `yaah trace <jsonl> --errors-only` as a CI guard (exits non-zero on any
  error-status span).

For Langfuse v4 / OTEL setup see `src/yaah/adapters/trace/` and pyproject
extras (`pip install 'yaah-harness[langfuse]'`).

## 6 — Health check before you ship

After installing the wheel into the production image, run `yaah doctor`
as a `HEALTHCHECK` or as an init step:

```dockerfile
HEALTHCHECK --interval=30s --timeout=5s CMD yaah doctor || exit 1
```

`yaah doctor` exits 1 if Python is too old or the packaged base configs
didn't ship (the wheel was built wrong) — both are install-time bugs
worth catching before a real run.

## Not covered yet

- **Output back to a calling application** — the embedding story.
  Today the harness runs as a CLI; if your service wants the result
  back in-process, you call `yaah.harness.Harness.run()` directly
  (see `src/yaah/runtime.py:_assemble_harness`).
- **Auto-scaling worker count** — NATS subscriber groups distribute
  work, but the engine doesn't manage replica count. Use your
  orchestrator (K8s HPA, AWS ECS, etc.) on standard metrics
  (CPU / NATS queue depth).
- **Cross-run cost dashboards** — `yaah trace --cost` rolls up one
  trace; a multi-trace rollup is shell + `jq` for now.
