# Pipeline reference — templates + fake-mode shapes

Lookup tables for `yaah-pipeline-authoring`. Load when you need a concrete shape
to adapt; the authoring *judgment* (when to use which) stays in `SKILL.md`.

## Quick reference

**Smallest viable pipeline** (linear, one agent, one validator, one parse
transform — same shape as the QUICK START; the parse stage is not optional, see
the data-flow contract in `quickstart.md`):
```json
{
  "nodes": {
    "role:think":  {"type": "agent", "prompt": "file:my-prompt", "model": "claude:claude-sonnet-4-6", "stage": "think"},
    "role:check":  {"type": "json_object"},
    "role:parse":  {"type": "transform", "target": "fn:my_transforms:parse", "call": "envelope"}
  },
  "graph": {
    "start": "think",
    "stages": {
      "think": {"node": "role:think", "validators": ["role:check"], "max_attempts": 3, "feedback": true, "then": "parse"},
      "parse": {"node": "role:parse", "then": null}
    }
  }
}
```
(`my_transforms.py` lives next to the root config and is imported from the run
dir; `fn:` targets in config are trusted code — never point one at anything
payload-derived.)

**Fork (asymmetric A/B)** — a `fork` stage spreads the envelope to successor
**stage** names; the `fanin` `expect:` lists the fork targets and `reduce:` is the
comparison fn. (See the fork/fan-in cases in `tests/test_fork_join.py`.)

**Fanout barrier** (same input → N parallel roles → merge) — a stage with
`fanout:` listing **role** names runs them in parallel; downstream reads
`{results, roles}`.

**Root config** template (the real shape — every field is a typed block, not a
bare string):
```json
{
  "transport": {"type": "inproc"},
  "providers": {"claude": {"type": "claude_cli"}},
  "default_provider": "claude",
  "prompt_sources": {"file": {"type": "file", "dir": "prompts"}},
  "default_prompt_source": "file",
  "state": {"type": "memory"},
  "pipeline": "my-pipeline.json",
  "input": "fixtures/my-input.json",
  "run": true
}
```
Add `data_sources` (e.g. `git_diff`), a `trace` block (`mode`/`capture`/`sink:[…]`),
and `decisions:{…}` (auto-approve gates) when you need them. See
[`docs/root-config-reference.md`](../../../docs/root-config-reference.md) for every
root key.

## Fake-mode shapes — pick the smallest one that fits

The harness gives you three escalating ways to swap in fakes. **Default to the
cheapest** that covers what differs.

| Shape | When | What it looks like |
|---|---|---|
| **`--fake` flag + inline `_fake` block** | Only the root differs (providers/state). Pipeline is shared. *Most trivial cases.* | One root file. Add `"_fake": {"providers": {...fake_scripted...}, "default_provider": "..."}` to the root, then `yaah <root> --fake` swaps the matching top-level keys in. The `_`-prefix means the block is ignored without the flag. |
| **`.fake.json` pipeline overlay (`_extends`)** | The pipeline ALSO differs (model swaps per role) — most non-trivial pipelines. | `<name>.fake.json` is `{"_extends": "<name>.json", "nodes": {"role:x": {"model": "fake:..."}}}`. Reference it from a fake root. A fake overlay is typically a fraction of the canonical's size — overlay only the deltas. |
| **Two root files (`.claude.local.json` + `.fake.local.json` with `_extends`)** | Trace sinks / state / multiple host-specific knobs also differ. | The fake root `_extends` the claude root and overrides as needed. Use only when the other two shapes don't reach. |

**Drift surface** — adding `max_attempts: N` to a canonical stage doesn't
auto-propagate to the overlay; verify the overlay doesn't redefine that key.
