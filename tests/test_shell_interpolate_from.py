"""`interpolate_from` — the validated payload→argv INTERPOLATION seam on shell nodes.

What it proves: a shell/shell_check node may substitute declared payload values
into `{{key}}` placeholders in its trusted argv-LIST command (the per-run value
that belongs in the MIDDLE of the argv, not appended at the end like
`target_from`), and that every way this could go wrong is loud:

  - whole-element `{{key}}` and embedded `--flag={{key}}` both substitute;
  - `interpolate_from` UNSET → byte-identical old behaviour (a `{{x}}` stays a literal);
  - a declared key absent/None/invalid at run time → TargetError (node ERRORS —
    never exec a command with a literal `{{key}}` in argv);
  - an UNDECLARED `{{key}}`, a malformed `{{`-token, or a STRING command CARRYING
    a `{{`-token → BUILD error (the tokened-string refusal is what makes the seam
    safe by construction: interpolation only ever happens into ONE argv element);
  - a STRING command with NO `{{`-token + a declared `interpolate_from` is LEGAL —
    it builds and runs unchanged, interpolation simply inert. (Relaxed in the M29
    review round, HIGH-1: the old blanket string refusal broke every consumer
    whose overlay supplies its RED/GREEN runner as a shell string. Without a
    token, substitution cannot occur, so the by-construction argument holds.)
  - a value containing a space stays ONE argument, and `;` / `$(echo pwned)`
    reach the child as inert literal text — under `shell: false` AND `shell: true`;
  - C0 control characters other than TAB are rejected (LOW-6: these values ride
    back out through `stdout_tail` into ANSI operator terminals);
  - `interpolate_from` composes with `target_from`: placeholders keep their
    position, targets still land last.

Also pins the `tail` regression: `_build_shell`/`_build_shell_check` never read
`spec["tail"]`, so a node configured with `"tail": 20000` was silently truncated
at the 2000 default.

Run: cd yaah && PYTHONPATH=src python3 tests/test_shell_interpolate_from.py

Targets Python 3.9+.
"""
from __future__ import annotations

import asyncio

from yaah import Envelope, Kind, NodeConfig, Verdict
from yaah.nodes import ShellCheck, ShellNode
from yaah.nodes._target import TargetError, check_command, render_command
from yaah.build.builders import _build_shell, _build_shell_check
from yaah.build.build_context import BuildContext
from yaah.comms import InProcessComms

def _ctx() -> BuildContext:
    return BuildContext(comms=InProcessComms())

def _build_raises(spec: dict, needle: str) -> None:
    """BOTH shell builders must refuse `spec`, with `needle` in the message."""
    for builder in (_build_shell, _build_shell_check):
        raised = False
        try:
            builder(dict(spec), _ctx())
        except ValueError as e:            # TargetError is a ValueError
            raised = True
            assert needle in str(e), (builder.__name__, str(e))
        assert raised, "{} must refuse {!r}".format(builder.__name__, spec)

async def main() -> None:
    cfg = NodeConfig()

    # --- whole-element AND embedded placeholders both substitute ---
    env = Envelope(Kind.TASK, {"tenant": "acme", "url": "postgres://h/db"})
    out = await ShellNode(["printf", "%s\\n", "{{tenant}}", "--url={{url}}"],
                          interpolate_from=["tenant", "url"]).invoke(env, cfg)
    assert out.payload["stdout"].split() == ["acme", "--url=postgres://h/db"], out.payload["stdout"]

    # the same key twice in one element, and two keys in one element
    out = await ShellNode(["printf", "%s\\n", "{{tenant}}-{{tenant}}/{{url}}"],
                          interpolate_from=["tenant", "url"]).invoke(env, cfg)
    assert out.payload["stdout"].strip() == "acme-acme/postgres://h/db", out.payload["stdout"]

    # --- int / float values stringify ---
    out = await ShellNode(["printf", "%s\\n", "--workers={{n}}"],
                          interpolate_from=["n"]).invoke(
        Envelope(Kind.TASK, {"n": 4}), cfg)
    assert out.payload["stdout"].strip() == "--workers=4", out.payload["stdout"]

    # --- interpolate_from UNSET → byte-identical old behaviour: {{x}} is a LITERAL ---
    out = await ShellNode(["printf", "%s\\n", "{{tenant}}"]).invoke(env, cfg)
    assert out.payload["stdout"].strip() == "{{tenant}}", out.payload["stdout"]
    assert render_command(["a", "{{x}}"], {"x": "v"}, None) == ["a", "{{x}}"]
    assert render_command("echo {{x}}", {"x": "v"}, None) == "echo {{x}}"

    # --- a declared key absent / None at RUN time → TargetError (node ERRORS) ---
    for bad_payload in ({"other": "z"}, {"tenant": None}):
        raised = False
        try:
            await ShellNode(["printf", "%s\\n", "{{tenant}}"],
                            interpolate_from=["tenant"]).invoke(
                Envelope(Kind.TASK, bad_payload), cfg)
        except TargetError as e:
            raised = True
            assert "tenant" in str(e), str(e)
        assert raised, "unfilled placeholder must ERROR, not exec a literal {{tenant}}"

    # --- every value-rejection class ERRORS the node ---
    # C0 controls other than TAB are rejected (LOW-6): these values ride back out
    # through stdout_tail into ANSI operator terminals, where ESC rewrites the
    # screen and BS/CR erase what was already printed.
    rejects = {
        "empty_string": "",
        "newline": "a\nb",
        "carriage_return": "a\rb",
        "nul": "a\x00b",
        "escape": "a\x1b[2Jb",
        "bell": "a\x07b",
        "backspace": "a\x08b",
        "vertical_tab": "a\x0bb",
        "form_feed": "a\x0cb",
        "over_cap": "x" * 5000,
        "bool": True,
        "dict": {"a": 1},
        "list": ["a"],
    }
    for name, bad in rejects.items():
        raised = False
        try:
            await ShellNode(["printf", "%s\\n", "{{v}}"], interpolate_from=["v"]).invoke(
                Envelope(Kind.TASK, {"v": bad}), cfg)
        except TargetError as e:
            raised = True
            assert "v" in str(e), (name, str(e))
        assert raised, "expected TargetError for {}".format(name)

    # ...but TAB is ordinary in a real command argument and stays ALLOWED
    out = await ShellNode(["printf", "[%s]\\n", "{{v}}"], interpolate_from=["v"]).invoke(
        Envelope(Kind.TASK, {"v": "a\tb"}), cfg)
    assert out.payload["stdout"] == "[a\tb]\n", repr(out.payload["stdout"])

    # --- BUILD-time refusal: an UNDECLARED {{key}} is never a silent literal ---
    _build_raises({"command": ["run", "{{secret}}"], "interpolate_from": ["tenant"]}, "secret")
    # ...including one declared key alongside one undeclared
    _build_raises({"command": ["run", "--a={{tenant}}", "--b={{other}}"],
                   "interpolate_from": ["tenant"]}, "other")

    # --- BUILD-time refusal: a STRING command CARRYING a token (the injection edge) ---
    _build_raises({"command": "run {{tenant}}", "interpolate_from": ["tenant"]},
                  "argv-LIST-only")

    # --- ...but a STRING command with NO token + a declared key is LEGAL (HIGH-1) ---
    # A host overlay routinely supplies its RED/GREEN runner as a shell string
    # while the pipeline declares the key centrally. Nothing can be substituted
    # without a `{{`-token, so the node builds and runs BYTE-IDENTICALLY.
    for builder in (_build_shell, _build_shell_check):
        builder({"command": "run --all", "interpolate_from": ["tenant"]}, _ctx())
    check_command("run --all", ["tenant"])                     # unit level: no raise
    assert render_command("run --all", {"tenant": "acme"}, ["tenant"]) == "run --all"
    n = _build_shell({"command": "printf 'no-token\\n'", "shell": True,
                      "interpolate_from": ["tenant"]}, _ctx())
    out = await n.invoke(Envelope(Kind.TASK, {"tenant": "acme"}), cfg)
    assert out.payload["stdout"] == "no-token\n", repr(out.payload["stdout"])
    assert out.payload["ok"], out.payload

    # --- BUILD-time refusal: a `{{`-token that is not a well-formed {{key}} ---
    _build_raises({"command": ["run", "{{?tenant}}"], "interpolate_from": ["tenant"]}, "not a")
    _build_raises({"command": ["run", "{{tenant"], "interpolate_from": ["tenant"]}, "not a")

    # --- BUILD-time refusal: interpolate_from must be a list of non-empty strings ---
    for bad_af in ("tenant", 3, {"k": "v"}, ["tenant", 3], ["tenant", ""]):
        _build_raises({"command": ["run"], "interpolate_from": bad_af}, "interpolate_from")

    # --- interpolate_from ABSENT → build is untouched, {{x}} survives into the node ---
    n = _build_shell({"command": ["printf", "%s\\n", "{{x}}"]}, _ctx())
    out = await n.invoke(Envelope(Kind.TASK, {"x": "v"}), cfg)
    assert out.payload["stdout"].strip() == "{{x}}", out.payload["stdout"]

    # --- a value with a SPACE stays exactly ONE argv element ---
    # printf repeats its format per argument, so two elements would print two lines.
    out = await ShellNode(["printf", "[%s]\\n", "{{v}}"], interpolate_from=["v"]).invoke(
        Envelope(Kind.TASK, {"v": "two words"}), cfg)
    assert out.payload["stdout"] == "[two words]\n", repr(out.payload["stdout"])

    # --- shell metacharacters reach the child as INERT LITERAL TEXT ---
    hostile = "x; rm -rf /tmp/nope $(echo pwned) `echo pwned` | tee /dev/null"
    for shell_mode in (False, True):
        out = await ShellNode(["printf", "[%s]\\n", "{{v}}"], shell=shell_mode,
                              interpolate_from=["v"]).invoke(
            Envelope(Kind.TASK, {"v": hostile}), cfg)
        assert out.payload["stdout"] == "[" + hostile + "]\n", (shell_mode, repr(out.payload["stdout"]))
        assert "pwned\n" not in out.payload["stdout"], (shell_mode, out.payload["stdout"])

    # --- interpolate_from + target_from: placeholders keep POSITION, targets land LAST ---
    out = await ShellNode(["printf", "%s\\n", "--tenant={{tenant}}", "MIDDLE"],
                          interpolate_from=["tenant"], target_from="spec").invoke(
        Envelope(Kind.TASK, {"tenant": "acme", "spec": "tests/a_spec.py"}), cfg)
    assert out.payload["stdout"].split() == ["--tenant=acme", "MIDDLE", "tests/a_spec.py"], \
        out.payload["stdout"]

    # --- shell_check honours interpolate_from (the RED/GREEN gate against a per-run arg) ---
    v = Verdict.from_envelope(await ShellCheck(["test", "-e", "{{path}}"],
                                               interpolate_from=["path"]).invoke(
        Envelope(Kind.TASK, {"path": "tests/test_shell_interpolate_from.py"}), cfg))
    assert v.ok, "shell_check ran `test -e tests/test_shell_interpolate_from.py`"
    raised = False
    try:
        await ShellCheck(["test", "-e", "{{path}}"], interpolate_from=["path"]).invoke(
            Envelope(Kind.TASK, {}), cfg)
    except TargetError:
        raised = True
    assert raised, "shell_check must ERROR on an unfilled placeholder, not run the gate blind"

    # --- unit level: check_command is the ONE rule the builder and the run share ---
    check_command(["ok", "{{a}}"], ["a"])                    # no raise
    check_command("any string command", None)                # feature off → no-op
    for bad in (["x", "{{a}}"],):
        raised = False
        try:
            check_command(bad, [])                            # nothing declared
        except TargetError:
            raised = True
        assert raised, "check_command must refuse an undeclared key"

    # --- REGRESSION: `tail` was never read off the spec (silently capped at 2000) ---
    big = _build_shell({"command": ["seq", "1", "2000"], "tail": 20000}, _ctx())
    out = await big.invoke(Envelope(Kind.TASK, {}), cfg)
    assert len(out.payload["stdout_tail"]) == len(out.payload["stdout"]) > 2000, \
        len(out.payload["stdout_tail"])
    dflt = _build_shell({"command": ["seq", "1", "2000"]}, _ctx())
    out = await dflt.invoke(Envelope(Kind.TASK, {}), cfg)
    assert len(out.payload["stdout_tail"]) == 2000, len(out.payload["stdout_tail"])

    chk = _build_shell_check({"command": ["sh", "-c", "seq 1 2000; exit 3"], "tail": 20000},
                             _ctx())
    env_out = await chk.invoke(Envelope(Kind.TASK, {}), cfg)
    assert len(env_out.payload["failures"][0]["fix_hint"]) > 2000, \
        len(env_out.payload["failures"][0]["fix_hint"])

    print("ok")

if __name__ == "__main__":
    asyncio.run(main())
