"""yaah.shell_completion: shell completion scripts for bash + zsh.

What it proves: the rendered scripts contain every verb cli._SUBCOMMANDS
exposes (no drift between what the user can run and what tab-complete
suggests); both shells share the same archetype list; an unknown shell
raises with the supported set named; the script-level shape is wired
correctly (bash uses `complete -F`, zsh uses `#compdef`).

Run: cd yaah && PYTHONPATH=src python3 tests/test_shell_completion.py
"""
from __future__ import annotations

from yaah.cli import _SUBCOMMANDS
from yaah.shell_completion import bash_completion, render, zsh_completion


def scenario_no_verb_drift() -> None:
    """Every subcommand cli.py advertises must also appear in the completion
    scripts. Without this assertion, adding a subcommand could silently
    leave shell users unable to tab-complete it (the kind of bug that's
    invisible until someone complains)."""
    bash = bash_completion()
    zsh = zsh_completion()
    # cli.py exposes all of _SUBCOMMANDS; the completion module should too.
    for verb in _SUBCOMMANDS:
        assert verb in bash, "bash completion missing verb: {}".format(verb)
        assert verb in zsh, "zsh completion missing verb: {}".format(verb)


def scenario_bash_wire_shape() -> None:
    """Bash completion uses `complete -F _yaah_completion yaah` to register
    against the `yaah` command, and dispatches on COMP_WORDS[1]. The shape
    has to be intact for the script to actually take effect when sourced."""
    bash = bash_completion()
    assert "complete -F _yaah_completion yaah" in bash, bash[-200:]
    assert "COMP_WORDS" in bash and "compgen" in bash, bash[:500]


def scenario_zsh_wire_shape() -> None:
    """Zsh completion uses the `#compdef yaah` directive (zsh's autoload
    machinery picks this up by filename + directive). Without it, the
    script gets installed but does nothing."""
    zsh = zsh_completion()
    assert zsh.startswith("#compdef yaah"), zsh[:80]
    assert "_yaah" in zsh and "_arguments" in zsh, zsh[:500]


def scenario_archetypes_consistent() -> None:
    """Both shells expose the same archetype list — divergence would mean
    bash users and zsh users tab-complete different sets."""
    for archetype in ("linear", "branch-with-gate", "fork-fanin"):
        assert archetype in bash_completion(), archetype
        assert archetype in zsh_completion(), archetype


def scenario_unknown_shell_raises() -> None:
    """render() refuses to silently emit a wrong shell's script — the
    error message names the supported set so the user can self-correct."""
    try:
        render("fish")
    except ValueError as e:
        msg = str(e)
        assert "fish" in msg and "bash" in msg and "zsh" in msg, msg
    else:
        raise AssertionError("render('fish') should raise")


def main() -> None:
    scenario_no_verb_drift()
    scenario_bash_wire_shape()
    scenario_zsh_wire_shape()
    scenario_archetypes_consistent()
    scenario_unknown_shell_raises()
    print("ok")


if __name__ == "__main__":
    main()
