"""Build-time path macros: `{base_dir}` and `{run_dir}` in a node spec.

Expanded in Registry.build — the one seam every node construction shares — so the
macros work on EVERY node type, not just an agent's tool strings (the `{base_dir}`
expansion used to be a closure inside `_build_agent`; lifting it is why a shell
node's `command` and a render's `out` can now use it too).

Covers: expansion in a shell command, an agent config and a render `out`;
`{run_dir}` with no root key is a build ERROR naming the key (no default, ever);
`{base_dir}` on a NON-agent node now works; and no collision with `{{key}}`
run-time interpolation — a string carrying both expands the single-brace macro at
build and leaves the double-brace placeholder for the envelope.

Run: cd yaah && PYTHONPATH=src python3 tests/test_macros.py
"""
from __future__ import annotations

import os
import tempfile

from yaah.agents.fake_provider import FakeProvider
from yaah.build.build_context import BuildContext
from yaah.build.builders import default_registry
from yaah.build.macros import expand_macros, resolve_run_dir
from yaah.comms import InProcessComms


def _ctx(base="rel/dir", run=None) -> BuildContext:
    return BuildContext(comms=InProcessComms(),
                        backend=FakeProvider(responses=["ok"]),
                        base_dir=base, run_dir=run)


def scenario_expansion_reaches_every_string_leaf() -> None:
    ctx = _ctx(run="/runs/task-7")
    spec = {"type": "shell",
            "command": ["bash", "{base_dir}/tools/x.sh", "--out", "{run_dir}/log.txt"],
            "cwd": "{run_dir}",
            "config": {"nested": {"deep": ["{run_dir}/a", 7, None]}},
            "model": "haiku",
            "timeout": 30}
    out = expand_macros(spec, ctx)
    base = os.path.abspath("rel/dir")
    assert out["command"] == ["bash", base + "/tools/x.sh", "--out",
                             "/runs/task-7/log.txt"], out["command"]
    assert out["cwd"] == "/runs/task-7", out["cwd"]
    assert out["config"]["nested"]["deep"] == ["/runs/task-7/a", 7, None], out["config"]
    assert out["model"] == "haiku" and out["timeout"] == 30, out
    # the caller's config object must be untouched — a spec is read again by
    # _node_config, by the lint, and by a live re-read
    assert spec["cwd"] == "{run_dir}", "expand_macros must not mutate its input"
    print("PASS macros expand every string leaf (lists, nested dicts) without mutating")


def scenario_shell_command_and_render_out() -> None:
    """Through the real builders, on node types that never had macro support."""
    with tempfile.TemporaryDirectory() as d:
        run = os.path.join(d, "run-1")
        ctx = _ctx(run=run)
        shell = default_registry().build(
            {"type": "shell", "command": ["echo", "{run_dir}/artifact.json"]}, ctx)
        assert os.path.join(run, "artifact.json") in " ".join(shell._command), shell._command

        render = default_registry().build(
            {"type": "render", "template_text": "hi", "out": "{run_dir}/report.html"}, ctx)
        assert render._out_path == os.path.join(run, "report.html"), render._out_path
    print("PASS `{run_dir}` expands in a shell command and a render `out`")


def scenario_agent_tool_strings_still_expand() -> None:
    """The behaviour that used to live in `_build_agent` — unchanged from the
    outside, now served by the shared walk."""
    spec = {"type": "agent", "template": "x",
            "allowed_tools": ["Bash(bash {base_dir}/tools/fetch.sh*)"],
            "tools": [{"name": "fetch", "impl": "fn:json:loads",
                       "usage": "Run `bash {run_dir}/fetch.sh`"}]}
    agent = default_registry().build(spec, _ctx(run="/runs/z"))
    want = os.path.abspath("rel/dir")
    assert agent._allowed_tools == ["Bash(bash {}/tools/fetch.sh*)".format(want)], \
        agent._allowed_tools
    assert agent._tools[0].usage == "Run `bash /runs/z/fetch.sh`", agent._tools[0].usage
    print("PASS agent tool strings expand both macros through the shared walk")


def scenario_base_dir_on_a_non_agent_node() -> None:
    """The intentional WIDENING: `{base_dir}` was agent-only because its expansion
    lived in the agent builder, not because the need was agent-specific."""
    node = default_registry().build(
        {"type": "shell", "command": ["bash", "{base_dir}/deploy.sh"]}, _ctx())
    assert os.path.abspath("rel/dir") + "/deploy.sh" in node._command, node._command
    print("PASS `{base_dir}` now works on a non-agent node (shell)")


def scenario_missing_run_dir_key_is_a_build_error() -> None:
    """NO engine default. Falling back to cwd (or to base_dir) would write a run's
    artifacts into whatever tree the launcher happened to be in — the silent
    misroute class this codebase fails loud on."""
    failed = None
    try:
        default_registry().build(
            {"type": "shell", "command": ["echo", "{run_dir}/x"]}, _ctx(run=None))
    except ValueError as e:
        failed = e
    assert failed is not None, "{run_dir} without the root key must be a build error"
    assert "run_dir" in str(failed), failed
    assert "no default" in str(failed), failed

    # ...and the same discipline for {base_dir} with no base.
    failed = None
    try:
        default_registry().build(
            {"type": "shell", "command": ["echo", "{base_dir}/x"]},
            BuildContext(comms=InProcessComms(), backend=FakeProvider(responses=["ok"])))
    except ValueError as e:
        failed = e
    assert failed is not None and "base_dir" in str(failed), failed
    print("PASS `{run_dir}` with no root key is a build error naming the key")


def scenario_no_collision_with_runtime_interpolation() -> None:
    """`{run_dir}` is single-brace and expands at BUILD; `{{db_url}}` is double-brace
    and is filled per invocation from the envelope. One string may carry both."""
    ctx = _ctx(run="/runs/q")
    out = expand_macros({"type": "shell",
                         "command": ["psql", "{{db_url}}", "-o", "{run_dir}/dump.sql"],
                         "template_text": "wrote {{count}} rows to {run_dir}"}, ctx)
    assert out["command"] == ["psql", "{{db_url}}", "-o", "/runs/q/dump.sql"], out["command"]
    assert out["template_text"] == "wrote {{count}} rows to /runs/q", out["template_text"]

    # THE NAME COLLISION: a run-time placeholder that happens to be CALLED
    # `{{run_dir}}`/`{{base_dir}}` contains the macro token as a substring. A plain
    # replace rewrote it to `{/runs/q}` at build — a silently corrupted template at
    # exit 0, with the run-time fill it was waiting for never happening.
    both = expand_macros({"template_text": "{{run_dir}}/x and {run_dir}/y",
                          "out": "{{base_dir}}/a and {base_dir}/b"}, ctx)
    assert both["template_text"] == "{{run_dir}}/x and /runs/q/y", both["template_text"]
    base = os.path.abspath("rel/dir")
    assert both["out"] == "{{base_dir}}/a and " + base + "/b", both["out"]

    # ...and a spec whose ONLY use is the double-brace placeholder must not even
    # RESOLVE the macro — no `run_dir` root key, no error, because no macro is used.
    placeholder_only = expand_macros({"template_text": "{{run_dir}}/x"}, _ctx(run=None))
    assert placeholder_only["template_text"] == "{{run_dir}}/x", placeholder_only

    # THE ADJACENCY EDGES, pinned because they are documented as accepted limits
    # (templating.macro_matcher): a LOPSIDED brace is neither dialect and is left
    # alone, silently — guessing which one it meant is worse. And a triple brace
    # composes: the build skips the brace-wrapped token, the run-time fill takes the
    # inner pair, and the outer braces stay literal.
    edges = expand_macros(["{{run_dir}", "{run_dir}}", "{{{run_dir}}}"], ctx)
    assert edges == ["{{run_dir}", "{run_dir}}", "{{{run_dir}}}"], edges

    # a string with no brace at all short-circuits untouched
    assert expand_macros("plain/path", ctx) == "plain/path"
    print("PASS `{run_dir}` and `{{key}}` coexist: build-time vs run-time, no collision")


def scenario_resolve_run_dir_creates_it() -> None:
    with tempfile.TemporaryDirectory() as d:
        got = resolve_run_dir("artifacts/run-9", d)
        assert got == os.path.join(d, "artifacts/run-9"), got
        assert os.path.isdir(got), "the run dir must exist for nodes writing into it"
        assert resolve_run_dir(got, "/elsewhere") == got, "absolute is kept"
        assert resolve_run_dir(None, d) is None, "absent key stays None"

        # ...but NOT for a teardown verb. `yaah clear` assembles a harness only to
        # reach the clear primitives; creating a directory on the way to deleting
        # state is wrong, so `create=False` resolves the path and leaves the disk
        # alone. (The runtime end of this is
        # test_runtime_actions::scenario_clear_does_not_create_the_run_dir.)
        lazy = resolve_run_dir("artifacts/never-made", d, create=False)
        assert lazy == os.path.join(d, "artifacts/never-made"), lazy
        assert not os.path.exists(lazy), "create=False must not touch the disk"
    print("PASS resolve_run_dir absolutizes against the base and creates the directory")


def scenario_config_extras_expand_through_the_build_path() -> None:
    """The seam the builder alone does NOT cover. `registry.build` expands a COPY of
    the spec, so a node's `config` extras — read by `_node_config` off the ORIGINAL —
    used to reach `NodeConfig.extras` with the macro still literal, and a node reading
    `config.extras["repo_root"]` got the string `{run_dir}/repo`. Expansion now happens
    once in `build._built_nodes`, upstream of the builder AND of `_node_config`.

    Asserted through the real build path (both `_built_nodes` and the `build()` that
    registers on the comms), not through `expand_macros` — the bug was never in the
    expansion, it was in which spec the readers saw."""
    from yaah.build.build import _built_nodes, build

    ctx = _ctx(run="/runs/t1")
    config = {"nodes": {"role:x": {"type": "shell", "command": ["echo", "hi"],
                                   "config": {"repo_root": "{run_dir}/repo",
                                              "base": "{base_dir}/pkg",
                                              "budget": 5}}},
              "graph": {"start": "s", "stages": {"s": {"node": "role:x"}}}}
    (_role, _node, cfg), = _built_nodes(config, default_registry(), ctx, None)
    assert cfg.extras["repo_root"] == "/runs/t1/repo", cfg.extras
    assert cfg.extras["base"] == os.path.abspath("rel/dir") + "/pkg", cfg.extras
    assert cfg.extras["budget"] == 5, cfg.extras

    # ...and end-to-end through build(), which is what a real run registers.
    comms = InProcessComms()
    build(config, comms=comms, backend=FakeProvider(responses=["ok"]),
          base_dir="rel/dir", run_dir="/runs/t1")
    registered = comms._nodes["role:x"][1]                      # noqa: SLF001
    assert registered.extras["repo_root"] == "/runs/t1/repo", registered.extras
    print("PASS node `config` extras expand through the build path into NodeConfig.extras")


def main() -> None:
    scenario_expansion_reaches_every_string_leaf()
    scenario_config_extras_expand_through_the_build_path()
    scenario_shell_command_and_render_out()
    scenario_agent_tool_strings_still_expand()
    scenario_base_dir_on_a_non_agent_node()
    scenario_missing_run_dir_key_is_a_build_error()
    scenario_no_collision_with_runtime_interpolation()
    scenario_resolve_run_dir_creates_it()
    print("\nALL PASS")


if __name__ == "__main__":
    main()
