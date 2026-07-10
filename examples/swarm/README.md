# swarm — dynamic per-item fan-out (`foreach`, ADR-0007)

The shape that static graphs can't express: **one worker per element of a list
whose size is only known at runtime**, with a concurrency cap.

Here: an extractor produces N requirements (N is the model's call, not the
author's) → one cold-read skeptic runs **per requirement**, max 2 in flight →
a report renders the merged findings.

## Run it (offline, no API key)

```
$ PYTHONPATH=../../src python3 -m yaah.runtime swarm.local.json
[trace] stage extract ok
[trace] stage swarm ok
[trace] stage report ok
```

The merged swarm output on the final payload:

```json
"results": [
  {"item_index": 0, "payload": {"raw": "Refuted: MFA scope excludes service accounts."}},
  {"item_index": 1, "payload": {"raw": "Holds: retention is explicit in §4."}},
  {"item_index": 2, "payload": {"raw": "Refuted: encryption covers primary store only."}}
],
"failed_items": []
```

## The one stage that matters

```json
"swarm": {"node": "skeptic",
          "foreach": {"items": "requirements", "max_concurrent": 2},
          "then": "report"}
```

- `items` names the payload key holding the runtime list (the lint checks an
  upstream stage actually provides it).
- Each item's input is **`{item, item_index}` + whatever you `carry`** — never a
  copy of the whole inbound payload, so a big document only rides along (and
  costs tokens per item) if you say so: `"carry": ["doc"]`.
- `results` comes back as `{item_index, payload}` pairs in item order — indexes
  stay honest even when some items fail (`failed_items` names them, and
  `min_success: k` tolerates up to N−k losses).

v1 limits (documented in ADR-0007): an item that suspends parks the whole
stage; a stage retry re-runs all items; execution is in-memory — use
`fork`/`fanin` when you need a durable join.
