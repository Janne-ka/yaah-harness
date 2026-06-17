# YAAH docs

Start with the [project README](../README.md) for the pitch and a 5-minute run.
This folder is the deeper reference.

## Start here

| | |
|---|---|
| [quickstart.md](quickstart.md) | run a pipeline in 5 minutes (fake backend, no API key) |
| [tutorial.md](tutorial.md) | build your own — a 5-step recipe, then each concept in a runnable example |
| [../examples/](../examples/) | `hello-yaah` (linear), `review-pipeline` (branch + human gate), `fork-join` (parallel) |
| [../AGENTS.md](../AGENTS.md) | point an AI assistant at this to author pipelines for you |

## Understand it

| | |
|---|---|
| [why-yaah.md](why-yaah.md) | what it's for, and when to reach for it |
| [design.md](design.md) | the rationale and the decisions behind the shape |
| [architecture.md](architecture.md) | the layers + how concepts map to code |
| [envelope-by-example.md](envelope-by-example.md) | the one message shape, shown at each hop of a real run |

## Reference

| | |
|---|---|
| [node-reference.md](node-reference.md) | every node type and its config |
| [root-config-reference.md](root-config-reference.md) | every root / deployment-config key |
| [module-catalog.md](module-catalog.md) | every node / port / adapter / sink (auto-generated from the code) |

## Going further

| | |
|---|---|
| [agent-tools.md](agent-tools.md) | giving an agent tools + MCP |
| [durable-state.md](durable-state.md) | batons, idempotency, working memory, suspend/resume across restarts |

## Project

| | |
|---|---|
| [ROADMAP.md](ROADMAP.md) | what's next (engine, fault tolerance, onboarding) |
| [requirements.md](requirements.md) | requirements + how YAAH compares to other frameworks |
