"""Node builders + the default registry (functions, not classes).

Used by: build() / serve_from_config() via the Registry.
Where: the bridge from a config node spec to a constructed Node.
Why: one builder per built-in node type (agent, json_object, json_schema,
human_gate, shell, shell_check, expect_field, worktree, get, post, transform,
render), plus
default_registry() wiring them up and _node_config() turning a spec into a
NodeConfig.

Targets Python 3.9+.
"""
from __future__ import annotations

import json as _json
import os as _os
from typing import Any, Dict

from ..agents import Agent, Tool
from ..core import Node, NodeConfig
from ..nodes import (
    AgentLoopNode,
    GetNode,
    OnceNode,
    PostNode,
    RenderNode,
    ShellCheck,
    ShellNode,
    TransformNode,
    WorktreeNode,
)
from ..validators import ExpectField, JsonObjectValidator, JsonSchemaValidator
from .build_context import BuildContext
from .human_gate import HumanGate
from .registry import Registry


def _build_agent(spec: Dict[str, Any], ctx: BuildContext) -> Node:
    if ctx.backend is None:
        raise ValueError("an 'agent' node needs a model backend; pass backend= to build()")
    template = spec.get("template")
    prompt_key = spec.get("prompt")  # e.g. "file:eval" — resolved via the prompt source
    if template is None and prompt_key is None:
        raise ValueError("an 'agent' node needs 'template' or 'prompt' in its config")
    if prompt_key is not None and ctx.prompt_source is None:
        raise ValueError("node uses 'prompt' but no prompt_source passed to build()")
    def _expand(s: str) -> str:
        # `{base_dir}` -> the config file's dir (absolute). Tool scripts ship
        # beside the config, but a repo-bound agent runs with cwd in the task
        # worktree — the path must be absolute at runtime yet stay relocatable
        # in the file.
        if "{base_dir}" not in s:
            return s
        if not ctx.base_dir:
            raise ValueError("agent config uses {base_dir} but no base_dir was passed to build()")
        return s.replace("{base_dir}", _os.path.abspath(ctx.base_dir))

    tools = [Tool.from_dict(dict(t, usage=_expand(t["usage"])) if t.get("usage") else t)
             for t in spec.get("tools", [])]  # model-initiated capabilities
    allowed_tools = spec.get("allowed_tools")
    if allowed_tools:
        allowed_tools = [_expand(a) for a in allowed_tools]
    filters_spec = spec.get("filters") or {}  # R10: name -> {type, ...args}
    envelope_filters = None
    if filters_spec:
        from ..filter_factories import build_filter
        envelope_filters = {name: build_filter(s, comms=ctx.comms)
                            for name, s in filters_spec.items()}
    agent = Agent(
        ctx.backend,
        template=template,
        prompt_source=ctx.prompt_source,
        prompt_key=prompt_key,
        events=ctx.comms,
        events_topic=spec.get("events_topic", "events"),
        stage=spec.get("stage", spec.get("_role", "agent")),
        cwd_from=spec.get("cwd_from"),  # repo-bound agent: run in the task's worktree
        tools=tools,
        allowed_tools=allowed_tools,                  # claude-native per-agent tool perms
        permission_mode=spec.get("permission_mode"),
        mcp=spec.get("mcp"),                          # inline servers OR a 'source:key' ref
        mcp_source=ctx.mcp_source,
        carry=spec.get("carry"),                      # payload keys to forward (dialogue state)
        tracer=ctx.tracer,                            # model_call / tool_call spans
        expose=spec.get("expose"),                    # R9: envelope_get allow-list {payload:[...],header:[...]}
        max_chars=int(spec.get("max_chars", 20000)),  # hard cap on an envelope_get pull
        broker=spec.get("broker"),                    # R12: node role for the fuzzy context broker
        envelope_filters=envelope_filters,             # R10: name->Filter, available via envelope_get's `filter:` arg
        parse=bool(spec.get("parse", True)),          # ADR-0004: parse-by-default; opt out with parse:false
    )
    # ADR-0003: opt-in `attach: [...]` wraps the agent so attachers can merge
    # post-invoke data (tokens/usage/etc.) from the tracer's last span onto
    # the output payload. Engine ships zero built-ins; items are `fn:` refs.
    attach_spec = spec.get("attach")
    if attach_spec:
        from ..agents.attaching_agent import AttachingAgent
        from ..agents.attacher import Attacher
        from ..external_call import import_callable
        attachers = []
        for i, item in enumerate(attach_spec):
            if not isinstance(item, str) or not item.startswith("fn:"):
                raise ValueError(
                    "agent 'attach[{}]' must be a 'fn:module:func' string "
                    "(got {!r}); attachers ship in consumer code per ADR-0003".format(
                        i, item))
            cls = import_callable(item[len("fn:"):])
            if not (isinstance(cls, type) and issubclass(cls, Attacher)):
                raise ValueError(
                    "agent 'attach[{}]' = {!r} resolved to {!r}, expected a "
                    "subclass of yaah.agents.attacher.Attacher".format(
                        i, item, cls))
            attachers.append(cls())
        # capture-check: any required tracer capture missing → reject at LOAD
        # with the exact `trace:` snippet to add. NullTracer has empty
        # captures, so this also rejects "attach without tracing" without a
        # separate presence check.
        captures = set(getattr(ctx.tracer, "captures", ()) or ())
        missing: Dict[str, list] = {}
        for a in attachers:
            for cap in a.requires_capture:
                if cap not in captures:
                    missing.setdefault(cap, []).append(a.name or type(a).__name__)
        if missing:
            needed = sorted(missing)
            raise ValueError(
                "agent uses attach: [...] but the tracer is missing required "
                "captures {!r} for attachers {!r}. Add to your root config: "
                '"trace": {{"mode": "tracer", "capture": {}, '
                '"sinks": [{{"type": "console"}}]}}'.format(
                    needed, {c: missing[c] for c in needed},
                    sorted(captures | set(needed))))
        return AttachingAgent(agent, attachers, ctx.tracer)
    return agent


def _build_json_object(spec: Dict[str, Any], ctx: BuildContext) -> Node:
    return JsonObjectValidator(spec.get("required"), key=spec.get("key", "raw"))


def _build_json_schema(spec: Dict[str, Any], ctx: BuildContext) -> Node:
    if "schema" not in spec:
        raise ValueError("a 'json_schema' node needs a 'schema' (a JSON-Schema-subset object)")
    return JsonSchemaValidator(spec["schema"], key=spec.get("key", "raw"))


def _build_human_gate(spec: Dict[str, Any], ctx: BuildContext) -> Node:
    # `form` declares a generic decision shape from harness.decision_forms; the
    # CLI's `yaah baton-schema` surfaces it to driver skills. Validate at LOAD —
    # an unknown form or a misconfigured json_schema escape hatch is a config
    # bug, not a run-time event. (Both belong in this builder because validate.py
    # checks ROOT config only; per-node spec validation lives where the node is
    # built — same pattern as agent/shell/render.)
    from ..harness.decision_forms import FORMS
    form = spec.get("form")
    decision_schema = spec.get("decision_schema")
    if form is not None and form not in FORMS:
        raise ValueError(
            "human_gate 'form' = {!r} is not a known decision form; known: {}".format(
                form, sorted(FORMS)))
    if form == "json_schema" and decision_schema is None:
        raise ValueError(
            "human_gate form 'json_schema' requires an inline 'decision_schema' "
            "(the escape hatch's payload)")
    if form != "json_schema" and decision_schema is not None:
        raise ValueError(
            "human_gate 'decision_schema' is only allowed when form == 'json_schema' "
            "(got form={!r}) — for a built-in form, omit decision_schema".format(form))
    return HumanGate(ask=spec.get("ask", ""), awaiting=spec.get("awaiting"),
                     form=form, decision_schema=decision_schema)


def _build_shell(spec: Dict[str, Any], ctx: BuildContext) -> Node:
    if "command" not in spec:
        raise ValueError("a 'shell' node needs 'command' (host-fact missing — is an overlay supposed to supply it?)")
    return ShellNode(spec["command"], cwd=spec.get("cwd"), cwd_from=spec.get("cwd_from"),
                     timeout=spec.get("timeout"), shell=bool(spec.get("shell", False)),
                     tail_only=bool(spec.get("tail_only", False)), carry=spec.get("carry"))


def _build_shell_check(spec: Dict[str, Any], ctx: BuildContext) -> Node:
    if "command" not in spec:
        raise ValueError("a 'shell_check' node needs 'command' (host-fact missing — is an overlay supposed to supply it?)")
    return ShellCheck(spec["command"], expect_exit=int(spec.get("expect_exit", 0)),
                      expect_nonzero=bool(spec.get("expect_nonzero", False)),
                      cwd=spec.get("cwd"), cwd_from=spec.get("cwd_from"),
                      timeout=spec.get("timeout"), shell=bool(spec.get("shell", False)))


def _build_expect_field(spec: Dict[str, Any], ctx: BuildContext) -> Node:
    if "key" not in spec or "equals" not in spec:
        raise ValueError("an 'expect_field' node needs 'key' and 'equals'")
    return ExpectField(spec["key"], spec["equals"])


def _build_get(spec: Dict[str, Any], ctx: BuildContext) -> Node:
    if ctx.data_source is None:
        raise ValueError("a 'get' node needs a data source; pass data_source= to build()")
    key = spec.get("source")
    if key is None:
        raise ValueError("a 'get' node needs 'source' (e.g. 'git:' or 'file:path')")
    return GetNode(ctx.data_source, key, into=spec.get("into", "data"),
                   cwd_from=spec.get("cwd_from"), context=spec.get("context"),
                   paths=spec.get("paths"))


def _build_post(spec: Dict[str, Any], ctx: BuildContext) -> Node:
    if ctx.data_sink is None:
        raise ValueError("a 'post' node needs a data sink; pass data_sink= to build()")
    key = spec.get("sink")
    if key is None:
        raise ValueError("a 'post' node needs 'sink' (e.g. 'file:out/report.html')")
    return PostNode(ctx.data_sink, key, field=spec.get("field", "data"),
                    into=spec.get("into", "stored"), cwd_from=spec.get("cwd_from"))


def _build_transform(spec: Dict[str, Any], ctx: BuildContext) -> Node:
    target = spec.get("target")
    if not target:
        raise ValueError("a 'transform' node needs 'target' (e.g. 'fn:mod:func' or 'node:role')")
    return TransformNode(target, comms=ctx.comms, args_from=spec.get("args_from"),
                         into=spec.get("into", "result"), call=spec.get("call", "args"))


def _build_agent_loop(spec: Dict[str, Any], ctx: BuildContext) -> Node:
    if ctx.backend is None:
        raise ValueError("an 'agent_loop' node needs a model backend; pass backend= to build()")
    if not hasattr(ctx.backend, "turn"):
        raise ValueError(
            "the configured backend ({!r}) only implements .complete() — agent_loop "
            "needs a ToolBackend with .turn(messages, tools). Either swap the backend "
            "or use a plain 'agent' node for one-shot stages."
            .format(type(ctx.backend).__name__))
    tools = spec.get("tools")
    if not isinstance(tools, dict) or not tools:
        raise ValueError(
            "an 'agent_loop' node needs a non-empty 'tools' dict — {tool_name: "
            "{description, input_schema, dispatch}}. Got {!r}.".format(tools))
    # Validate each tool spec eagerly so authoring mistakes surface at build, not turn N.
    for name, t in tools.items():
        if not isinstance(t, dict):
            raise ValueError("tool {!r} must be a dict, got {!r}".format(name, type(t).__name__))
        if "dispatch" not in t:
            raise ValueError(
                "tool {!r} is missing 'dispatch' (e.g. 'fn:mymod:myfunc', "
                "'node:my_role', or 'http://...')".format(name))
    # The system prompt is passed through verbatim (a literal string), or as a
    # 'file:key' reference that the node resolves on first invoke via its
    # prompt_source. We don't resolve here because the prompt-source `.get` is
    # async — punt to invoke time. (Same pattern Agent uses.)
    return AgentLoopNode(
        backend=ctx.backend,
        tools=tools,
        comms=ctx.comms,
        prompt_source=ctx.prompt_source,
        max_turns=int(spec.get("max_turns", 10)),
        system_prompt=spec.get("system_prompt"),
        model=spec.get("model"),
    )


def _build_worktree(spec: Dict[str, Any], ctx: BuildContext) -> Node:
    repo = spec.get("repo")
    if not repo:
        raise ValueError("a 'worktree' node needs 'repo' (path to the source repo)")
    if not _os.path.isabs(repo) and ctx.base_dir:
        repo = _os.path.join(ctx.base_dir, repo)
    root = spec.get("root")
    if root and not _os.path.isabs(root) and ctx.base_dir:
        root = _os.path.join(ctx.base_dir, root)
    return WorktreeNode(repo=repo, base=spec.get("base", "HEAD"), root=root,
                        branch_prefix=spec.get("branch_prefix", "yaah/"),
                        op=spec.get("op", "add"), task_key=spec.get("task_key", "task"),
                        timeout=spec.get("timeout"), carry=spec.get("carry"),
                        force=bool(spec.get("force", False)))


def _build_render(spec: Dict[str, Any], ctx: BuildContext) -> Node:
    tfile = spec.get("template_file")
    if tfile and not _os.path.isabs(tfile) and ctx.base_dir:
        tfile = _os.path.join(ctx.base_dir, tfile)
    out = spec.get("out")
    if out and not _os.path.isabs(out) and ctx.base_dir:
        out = _os.path.join(ctx.base_dir, out)
    return RenderNode(template=spec.get("template_text"), template_file=tfile, out_path=out,
                      allow_unfilled=bool(spec.get("allow_unfilled", False)))


def default_registry() -> Registry:
    r = Registry()
    r.register("agent", _build_agent)
    r.register("json_object", _build_json_object)
    r.register("json_schema", _build_json_schema)
    r.register("human_gate", _build_human_gate)
    r.register("shell", _build_shell)
    r.register("shell_check", _build_shell_check)
    r.register("expect_field", _build_expect_field)
    r.register("worktree", _build_worktree)
    r.register("get", _build_get)
    r.register("post", _build_post)
    r.register("transform", _build_transform)
    r.register("render", _build_render)
    r.register("agent_loop", _build_agent_loop)
    return r


def _wrap_node(node: Node, spec: Dict[str, Any], ctx: BuildContext) -> Node:
    """Apply config-driven node wrappers. Today: `idempotent: true` wraps a
    side-effecting node in an OnceNode so a retry/replay runs its effect once
    (needs an idempotency_store in the context); a carriage tracer (R6 envelope
    mode) wraps EVERY node in a CarriageBoundaryNode so spans drain at the serve
    boundary — once per request, for every node type, not inside Agent.invoke
    (assessment #6: the mid-stage drain lost nested-agent spans, and non-agent
    nodes never drained at all). Used by build()/serve_from_config after the
    registry constructs the raw node."""
    if spec.get("idempotent"):
        if ctx.idempotency_store is None:
            raise ValueError(
                "node {!r} is marked idempotent but no state/idempotency store is "
                "configured (set root `state:`)".format(spec.get("_role", "?")))
        node = OnceNode(node, ctx.idempotency_store)
    if getattr(ctx.tracer, "is_carriage", False):
        # outermost: even a cached OnceNode reply carries the corr's buffered spans
        from ..trace import CarriageBoundaryNode
        node = CarriageBoundaryNode(node, ctx.tracer)
    return node


def _node_config(spec: Dict[str, Any]) -> NodeConfig:
    return NodeConfig(
        model=spec.get("model"),
        effort=spec.get("effort"),
        temperature=spec.get("temperature"),
        timeout=spec.get("timeout"),
        retries=int(spec.get("retries", 0)),
        idempotency_key=spec.get("idempotency_key"),
        extras=dict(spec.get("config", {})),
    )
