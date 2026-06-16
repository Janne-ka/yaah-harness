"""yaah.adapters — THE SWAP LAYER. Every implementation that binds yaah to an
outside system lives here, grouped by the port it fulfils:

  transports/  Comms      — local_bus, nats_comms        (the engine ships InProcessComms)
  backends/    ModelBackend — claude_cli, litellm         (the engine ships Fake/Scripted/Routing)
  prompts/     PromptSource — file, http, langfuse         (the engine ships Static/Routing)
  data/        DataSource/Sink — file, git_diff, file_sink (the engine ships Routing)
  mcp/         McpSource    — file                          (the engine ships Static/Routing)
  stores/      Store        — file                          (the engine ships MemoryStore)

The contract (the Protocol / port) lives WITH the engine next to its zero-config
default; this package holds only the swap-ins. Dependency direction is strict:
adapters import from the engine's ports + core, NEVER the reverse — nothing in
core/comms/harness/agents/nodes imports from here. Only build/ and runtime.py
(the assembly layer) wire these in, selected by config. To add or replace a
provider, add a module here and register it in the matching runtime factory map;
the engine and the pipeline graphs are untouched.
"""
