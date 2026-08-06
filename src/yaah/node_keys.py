"""node_keys — the legal spec keys per node TYPE, as data.

Used by: `validate.validate_pipeline` (rejects an unknown key on a node spec).
Where: load time, on the MERGED spec — after `_extends` / overlay expansion, so
what is checked is what `build` will actually read.
Why: a key no builder reads is SILENTLY DROPPED. `docs/shape-grammar.md` has
promised "unknown keys are rejected by validate_pipeline" for as long as it has
existed and nothing enforced it, which is how a pipeline carried `target_from` /
`interpolate_from` for five weeks against an engine that had never heard of
either — the run looked healthy and the feature was simply not there. This table
is the enforcement: one entry per builder in `build/builders.py`, so a new
`spec.get("...")` in a builder lands here in the same change.

The drift guard behind that promise is `test_validate.py::
test_node_key_table_covers_every_spec_read_in_builders`, and its reach is worth
stating precisely rather than claiming totality: it regex-scans `build/*.py`,
`runtime.py`, `validate.py` and `replay.py` for LITERAL `spec.get("k")` /
`spec["k"]` reads, either quote style. A key reached some other way — a computed
name, a different variable, a module outside that set — is not seen by it. So the
table is enforced against the way node keys are actually read today, not against
every conceivable read.

It lives here rather than in `build/builders.py` for the reason
`node_contract.BUILTIN_CONTRACTS` does: `validate` must stay cheap to import
(validators run in CI sandboxes), and importing the builders pulls the agents,
nodes and provider stack. Same discipline as the contract table — node knowledge
as DATA, domain-free (ADR-0001): it names node TYPES and the ENGINE keys those
built-in types read, never an app concept.

A type absent from `BUILTIN_NODE_KEYS` is a CUSTOM type registered by an
embedding app; its keys are unknown to the engine and are not checked (the same
sound skip an unregistered type gets from the data-flow contract).

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# Keys every node spec may carry, whatever its type. `_node_config` reads the
# NodeConfig scalars off ANY spec; `_wrap_node` reads `idempotent`; `runtime`'s
# serve selector reads `placement`; the data-flow lint reads `provides` /
# `consumes`; `rollback` is the per-node undo (ADR-0008 D1); `note` is the
# config-comment convention (as are all `_`-prefixed keys, legal everywhere).
COMMON_NODE_KEYS = frozenset({
    "type",
    "model", "effort", "temperature", "timeout", "retries", "idempotency_key", "config",
    "idempotent", "placement",
    "provides", "consumes", "rollback", "note",
})

# type name -> the keys THAT type's builder reads, beyond COMMON_NODE_KEYS. One
# row per `r.register(...)` in `build/builders.py:default_registry`; each entry is
# the set of `spec.get(...)` / `spec[...]` reads in that builder (plus the reads
# the contract/replay/blast-radius passes make off the same spec).
BUILTIN_NODE_KEYS: Dict[str, frozenset] = {
    "agent": frozenset({
        "template", "prompt", "stage", "cwd_from", "carry",
        "tools", "allowed_tools", "permission_mode", "mcp",
        "events_topic", "expose", "filters", "max_chars", "broker",
        "parse", "strict_render", "output_schema", "escalate_model", "attach",
    }),
    "agent_loop": frozenset({"tools", "max_turns", "system_prompt"}),
    "json_object": frozenset({"required", "key"}),
    "json_schema": frozenset({"schema", "key"}),
    "expect_field": frozenset({"key", "equals"}),
    "human_gate": frozenset({"ask", "awaiting", "form", "decision_schema", "allow_untrusted"}),
    "shell": frozenset({"command", "cwd", "cwd_from", "shell", "tail", "tail_only",
                        "carry", "target_from", "interpolate_from"}),
    "shell_check": frozenset({"command", "cwd", "cwd_from", "shell", "tail",
                              "expect_exit", "expect_nonzero",
                              "target_from", "interpolate_from"}),
    "get": frozenset({"source", "into", "cwd_from", "context", "paths"}),
    "post": frozenset({"sink", "field", "into", "cwd_from"}),
    "transform": frozenset({"target", "args_from", "into", "call"}),
    "render": frozenset({"template_file", "template_text", "out",
                         "allow_unfilled", "allow_untrusted"}),
    "worktree": frozenset({"repo", "base", "root", "branch_prefix", "op", "task_key",
                           "carry", "force"}),
}


def legal_keys(ntype: Any) -> Optional[frozenset]:
    """Every key a node of this type may carry, or None for a type the engine does
    not build (a custom/registered type — not checkable here)."""
    if not isinstance(ntype, str):
        return None
    own = BUILTIN_NODE_KEYS.get(ntype)
    return None if own is None else (own | COMMON_NODE_KEYS)


def unknown_node_keys(ntype: Any, spec: Any) -> List[str]:
    """The spec's keys that no builder of this type reads, sorted. Empty for a
    custom type, a malformed spec, or a clean node. `_`-prefixed keys are the
    documentation-by-convention channel (`_about`, `_comment`, `_role`) and are
    legal on every node."""
    legal = legal_keys(ntype)
    if legal is None or not isinstance(spec, dict):
        return []
    return sorted(k for k in spec
                  if isinstance(k, str) and not k.startswith("_") and k not in legal)
