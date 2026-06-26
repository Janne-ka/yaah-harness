You turn a code snapshot into ONE Mermaid `flowchart` of the architecture. You
are good at copying a worked example — so COPY THE SHAPE of the example below
(same layering, same descriptive labels, same kind of edges), using the packages
from THIS snapshot.

# Repo snapshot

{{snapshot}}

# Prior human feedback (may be empty on the first pass)

{{feedback}}

# The OUTPUT shape I want — copy this exactly, swap in the snapshot's packages

flowchart TD
  subgraph kernel["core kernel"]
    envelope["envelope — the one message shape"]
    node["node — invoke(input, config)"]
    comms["comms — request / publish / subscribe"]
  end
  runtime["runtime — wires adapters to the kernel"]
  subgraph adapters["adapters"]
    backends["backends — claude_cli, litellm, fake"]
    data["data — sources & sinks"]
    prompts["prompts — file, http"]
    stores["stores — file store"]
    trace["trace — sinks"]
  end
  runtime --> kernel
  runtime --> adapters
  backends --> node
  data --> node
  stores --> comms
  trace --> comms

# Your task

Do the same for the snapshot:

1. **Group packages into a small "kernel" subgraph and an "adapters" subgraph**
   (rename them if the snapshot's roles differ). 6 to 10 boxes total.
2. **Give every box a label `name — short description`** (2–5 words), taken from
   the package's module docstring in the snapshot. Not just the bare name.
3. **Draw edges to the SPECIFIC kernel box** each adapter group connects to, using
   the snapshot's `imports:` lines — e.g. an adapter that imports the node
   interface gets `that_adapter --> node`. Plus the wiring node `--> kernel` and
   `--> adapters`. Show the real seams, not one generic arrow.
4. **Exactly ONE level of grouping. NEVER put a subgraph inside a subgraph.**

# Rules (do not violate)

- `flowchart TD` only. No styling/`classDef`, no `%%` comments, no nested
  subgraphs, no prose outside the JSON.
- If feedback is non-empty, change exactly what it asks and nothing else.

# Reply

ONE JSON object. No prose around it. No ``` fence. Double-quote every key and
string value (use `\n` for newlines inside the mermaid string).

{"mermaid": "flowchart TD\n  subgraph kernel[\"core kernel\"]\n    envelope[\"envelope — the message shape\"]\n    node[\"node — invoke(input, config)\"]\n  end\n  subgraph adapters[\"adapters\"]\n    backends[\"backends — claude_cli, litellm\"]\n    stores[\"stores — file store\"]\n  end\n  backends --> node\n  stores --> node", "notes": "one sentence on the layering and the main seams"}
