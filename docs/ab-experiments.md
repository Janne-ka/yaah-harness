# A/B experiments — measure config variants, promote the winner

**Runnable example:** [`examples/hello-yaah/experiment.json`](../examples/hello-yaah/README.md)
— two summarizer variants, offline on fakes: campaign → matrix → rescore in
three commands.

The product loop `yaah ab` serves: you need to balance **cost vs performance**
(model choice, prompts, retry knobs, topology) with data, not opinion. Run
variants — on real providers when the campaign says so — while the engine
**reliably collects one row per run**, compare, edit the setup, collect more,
and when the data confirms a winner, **promote it by merging its overlay into
production**. The experiment artifact and the production artifact are the same
species: JSON configs.

## The shape

A variant is a normal yaah root config — usually a small `_extends` overlay
pair off your production config:

<!-- doc-snippet: skip (fragment pair, shown for shape) -->
```jsonc
// variants/b.root.json — the B variant's root
{ "_extends": "../prod.local.json", "pipeline": "pipe-b.json" }
// variants/pipe-b.json — one node's model changed, siblings untouched
{ "_extends": "../prod-pipeline.json",
  "nodes": { "role:judge": { "model": "claude:claude-sonnet-4-6" } } }
```

The experiment names the variants and the campaign matrix:

<!-- doc-snippet: skip (experiment config, not a root/pipeline) -->
```jsonc
{
  "id": "judge-model",
  "variants": { "A": "prod.local.json", "B": "variants/b.root.json" },
  "inputs": [ "fixtures/case1.json", { "request": "inline works too" } ],
  "repetitions": 20,
  "price_map": { "claude:claude-haiku-4-5": { "input": 0.001, "output": 0.005 },
                 "claude:claude-sonnet-4-6": { "input": 0.003, "output": 0.015 } },
  "store": { "dir": ".ab" }
}
```

`price_map` uses the SAME rate-card shape as `yaah trace` (`{model:
{input, output}}` in $ per 1k tokens) — one dialect everywhere, so the same
map prices both the campaign report and ad-hoc trace inspection.

`yaah ab judge-experiment.json` runs variants × inputs × repetitions and
appends one row per run to the experiment store (JSONL per experiment under
`store.dir`; a database adapter is the planned production substrate).

Fingerprint honesty: the row fingerprint hashes the effective configs plus
file-sourced prompt/template BYTES. It does NOT hash remote prompt contents
(http/langfuse — pin them for campaigns), fake-provider fixture files, or
plugin/`fn:` transform code — edits to those files' contents don't split
populations; config changes to them do.

## The reliability contract (what a row is)

Every run lands a row — **done, suspended (parked at a gate), failed (with
the verdict codes), and errored alike**; failures are data and the campaign
continues. Each row carries the variant's **config fingerprint** — a hash over
the effective root, the effective pipeline, and the BYTES of every referenced
prompt/template file — so editing a prompt mid-campaign splits the population
instead of silently mixing old-B with new-B. Rows are append-only and flushed
per write; a corrupt line is a loud error naming the spot, never a silent
undercount.

Pre-flight aborts before any model call: variants must validate, must not use
`live_config` (per-invocation re-reads would make the fingerprint a lie),
every model-calling node needs an explicit `model`, and every model (including
`escalate_model` rungs) must be in `price_map` — a matrix that silently reads
$0.00 is worse than no matrix.

Pre-flight also runs the experiment-level CONTRACT checks, with the engine's
two-severity honesty split: a knowable input that PROVABLY can't drive a
variant (a render key certain to be absent) or a metric path provably never
produced ABORTS, naming variant + key — money never burns on garbage; a
declared-but-unproven metric prints `[ab: metric-unproven]` to stderr and an
input that provably forces a branch to its default prints
`[ab: branch-default-only]` — both warnings, not failures (declare the metric
key in the producing agent's `output_schema` to silence the former). The
hello-yaah example deliberately shows two such warnings.

Cost capture is FORCED during a campaign: the variant's own `trace` config is
replaced with a cost-capturing file sink into the campaign's trace file
(`<store.dir>/<id>.trace.jsonl`); the report joins rows to cost by
correlation id. Suspended rows carry `baton_id` but no cost attribution for
the post-park leg (v1 limit).

## The comparison matrix

`yaah ab judge-experiment.json --report` (add `--json` for machines) reduces
the collected rows + trace into cells — one per **(variant, fingerprint)
population**, because a mid-campaign edit splits a variant into two
populations and mixing them is how a "winner" gets crowned on stale data.
Each cell: N, outcome counts, cost (mean/min/max/stdev, with unpriced rows
COUNTED, never silently $0), duration, and your declared `metrics` (dotted
payload paths, numeric leaves). Statistical honesty is enforced, not advised:
there is **no winner column** — the matrix presents, you decide; N<2 cells
are flagged INSUFFICIENT (convention: N=20/cell); and comparisons are
run-level only — variants may differ in agent count and prompts, so per-stage
cross-variant numbers would be meaningless and the report never emits them.

## Rescore — iterate on the contract for free

`yaah ab judge-experiment.json --rescore new-contract.json` re-scores every
collected row's RAW output against a candidate schema — **zero model calls**,
deterministic, safe mid-campaign. Per population you get the parse tiers
(strict JSON / recovered by the engine's own `extract_json` — the same
fence-tolerant + weak-executor recovery the runtime uses / reject) and the
conform gate (`check_schema` pass/fail with the top mismatch errors). The
measurement-gated contract change: tighten the schema only when the rescore
shows no healthy row newly rejected. Rows without a raw output (suspended /
error rows) are counted `no_raw`, never guessed.

Two scoping facts: a multi-agent pipeline's final payload carries the LAST
agent's `raw` (each agent overwrites it) — the rescore scores that one; and
if your pipeline relocates the raw text under another key, the programmatic
seam `rescore_rows(..., raw_key="your_key")` follows it (not exposed as a CLI
flag yet). Tier shifts vs collection time measure the CONTRACT's effect on
old outputs, not the model's quality — recovery is deliberately anchored on
the CANDIDATE schema's required keys, exactly as the runtime would anchor it
once that schema ships.

## Promotion

When the matrix says B wins: point production at B's files, or merge B's
overlay into the base and delete the overlay. `yaah explain` shows the
effective config either way; `yaah validate --strict` gates the change in CI.
