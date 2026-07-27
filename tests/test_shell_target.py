"""`target_from` — the validated payload→command-argument seam on shell nodes.

What it proves: a shell/shell_check node may APPEND a payload value to its trusted
command as an additional argument (M17: the test gate must run against the path
the agent actually wrote, not a pre-guessed one), and ONLY when the value passes
strict path-shape validation. Covers: valid single string appended; valid list
appended in order; absent key → command unchanged; every rejection class
(whitespace, shell metachar, leading dash, '..', non-string, empty string) →
node ERROR naming the value; shell-string vs argv appending; and the load-bearing
invariant that the payload can NEVER replace the base command, only append.

Run: cd yaah && PYTHONPATH=src python3 tests/test_shell_target.py

Targets Python 3.9+.
"""
from __future__ import annotations

import asyncio

from yaah import Envelope, Kind, NodeConfig, Verdict
from yaah.nodes import ShellCheck, ShellNode
from yaah.nodes._target import TargetError, append_targets, resolve_target_args
from yaah.build.builders import _build_shell, _build_shell_check
from yaah.build.build_context import BuildContext
from yaah.comms import InProcessComms


def _ctx() -> BuildContext:
    return BuildContext(comms=InProcessComms())


async def main() -> None:
    cfg = NodeConfig()

    # --- valid single string is appended as an argument (argv command) ---
    env = Envelope(Kind.TASK, {"spec": "tests/test_x.py"})
    out = await ShellNode(["echo", "base"], target_from="spec").invoke(env, cfg)
    assert out.payload["stdout"].split() == ["base", "tests/test_x.py"], out.payload["stdout"]

    # --- valid list appended in order ---
    env = Envelope(Kind.TASK, {"specs": ["a/x.py", "b/y.py"]})
    out = await ShellNode(["echo", "base"], target_from="specs").invoke(env, cfg)
    assert out.payload["stdout"].split() == ["base", "a/x.py", "b/y.py"], out.payload["stdout"]

    # --- absent key → command runs UNCHANGED (opt-in per value presence) ---
    env = Envelope(Kind.TASK, {"other": "z"})
    out = await ShellNode(["echo", "base"], target_from="spec").invoke(env, cfg)
    assert out.payload["stdout"].strip() == "base", out.payload["stdout"]

    # --- None value → unchanged; empty list → unchanged (nothing to append) ---
    out = await ShellNode(["echo", "base"], target_from="spec").invoke(
        Envelope(Kind.TASK, {"spec": None}), cfg)
    assert out.payload["stdout"].strip() == "base", out.payload["stdout"]
    out = await ShellNode(["echo", "base"], target_from="spec").invoke(
        Envelope(Kind.TASK, {"spec": []}), cfg)
    assert out.payload["stdout"].strip() == "base", out.payload["stdout"]

    # --- target_from unset → old behaviour, payload ignored entirely ---
    out = await ShellNode(["echo", "base"]).invoke(
        Envelope(Kind.TASK, {"spec": "tests/x.py"}), cfg)
    assert out.payload["stdout"].strip() == "base", out.payload["stdout"]

    # --- every rejection class ERRORS the node, naming key + value ---
    rejects = {
        "whitespace": "tests/ a.py",
        "metachar_semicolon": "a.py;rm",
        "metachar_dollar": "a$(x).py",
        "metachar_pipe": "a.py|b",
        "leading_dash": "-rf",
        "traversal": "../../etc/passwd",
        "empty_string": "",
    }
    for name, bad in rejects.items():
        env = Envelope(Kind.TASK, {"spec": bad})
        raised = False
        try:
            await ShellNode(["echo", "base"], target_from="spec").invoke(env, cfg)
        except TargetError as e:
            raised = True
            assert "spec" in str(e), (name, str(e))
            if bad:  # empty-string message names the class, not the value
                assert repr(bad) in str(e) or bad in str(e), (name, str(e))
        assert raised, "expected TargetError for {} ({!r})".format(name, bad)

    # non-string value in a list, and a non-string/non-list value, both ERROR
    for bad_payload in ({"spec": ["ok.py", 5]}, {"spec": {"a": 1}}, {"spec": 42}):
        raised = False
        try:
            await ShellNode(["echo", "base"], target_from="spec").invoke(
                Envelope(Kind.TASK, bad_payload), cfg)
        except TargetError as e:
            raised = True
            assert "spec" in str(e), str(e)
        assert raised, "expected TargetError for {!r}".format(bad_payload)

    # --- shell-STRING command: the value is shell-quoted and appended ---
    env = Envelope(Kind.TASK, {"spec": "tests/test_x.py"})
    out = await ShellNode("echo base", shell=True, target_from="spec").invoke(env, cfg)
    assert out.payload["stdout"].split() == ["base", "tests/test_x.py"], out.payload["stdout"]

    # --- shell_check honours target_from too (RED/GREEN gate against the real path) ---
    # `test -e <existing file>` → exit 0; feed this very test file's dir marker.
    v = Verdict.from_envelope(await ShellCheck(["test", "-e"], target_from="spec").invoke(
        Envelope(Kind.TASK, {"spec": "tests/test_shell_target.py"}), cfg))
    assert v.ok, "shell_check ran `test -e tests/test_shell_target.py` and passed"
    # a bad value ERRORS the gate rather than running it without the target
    raised = False
    try:
        await ShellCheck(["test", "-e"], target_from="spec").invoke(
            Envelope(Kind.TASK, {"spec": "a;b"}), cfg)
    except TargetError:
        raised = True
    assert raised, "shell_check must ERROR on an invalid target, not run the gate blind"

    # --- INVARIANT: the payload can NEVER replace the base command, only append.
    # Even a perfectly valid value lands as a trailing argument; the base command
    # tokens are always present and first. `printf '%s\n'` echoes each arg on its
    # own line, so we can see the exact argv the node ran.
    out = await ShellNode(["printf", "%s\\n", "SENTINEL"], target_from="spec").invoke(
        Envelope(Kind.TASK, {"spec": "appended.py"}), cfg)
    lines = out.payload["stdout"].split()
    assert lines[0] == "SENTINEL" and "appended.py" in lines, out.payload["stdout"]
    assert lines.index("SENTINEL") < lines.index("appended.py"), "target only ever appends"

    # --- unit-level: append_targets never mutates the base, only extends ---
    base_list = ["cmd", "a"]
    assert append_targets(base_list, []) == base_list          # no values → unchanged
    assert append_targets(base_list, ["b"]) == ["cmd", "a", "b"]
    assert base_list == ["cmd", "a"], "append_targets must not mutate its input list"
    assert append_targets("cmd a", ["b"]) == "cmd a b"
    assert resolve_target_args({}, None) == []                 # feature off
    assert resolve_target_args({}, "spec") == []               # absent key

    # --- config-validation seam: target_from must be a string at BUILD time ---
    n = _build_shell({"command": ["echo"], "target_from": "spec"}, _ctx())
    assert isinstance(n, ShellNode)
    for bad_tf in (["a", "b"], {"k": "v"}, 3):
        for builder, kw in ((_build_shell, {}),
                            (_build_shell_check, {})):
            raised = False
            try:
                builder({"command": ["echo"], "target_from": bad_tf}, _ctx())
            except ValueError as e:
                raised = True
                assert "target_from" in str(e), str(e)
            assert raised, "build must reject non-string target_from {!r}".format(bad_tf)
    # a shell_check with a string target_from builds fine
    assert isinstance(_build_shell_check(
        {"command": ["true"], "target_from": "spec"}, _ctx()), ShellCheck)

    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
