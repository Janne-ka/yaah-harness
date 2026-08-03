"""_target — the two narrow, validated payload→command-argument seams for shell nodes.

Used by: ShellNode and ShellCheck (the `target_from` and `interpolate_from`
config keys) and yaah.build.builders (the build-time refusals for
`interpolate_from`).
Where: yaah.nodes only (private helper, not exported).
Why: the shell nodes' COMMAND is trusted config and MUST NOT come from the
payload — a deliberate anti-injection stance. Two production footguns need a
controlled exception, and each gets its own narrow seam.

`target_from` (APPEND): a gate ran a *pre-guessed* path (e.g. a test-spec
overlay) while the agent wrote its artifact at a *different* path, so green-able
runs were sent to failure. It lets a shell node declare ONE payload key whose
value(s) are APPENDED to the trusted command as additional argument(s) — never
replacing it, never interpreted as a command — and only if every value passes
strict path-shape validation. Any invalid value ERRORS the node (loud), because
running the command WITHOUT the target (or with a wrong one) silently is the exact
bug class this fixes.

`interpolate_from` (INTERPOLATE): a command needs a per-run value in the MIDDLE
of its argv, not at the end — a connection string, a tenant id, a branch name. It
names the payload keys a `{{key}}` in the command may draw from. An undeclared
`{{key}}` is a BUILD error (never a silent literal); a declared key missing at run
time is a node ERROR (never a literal `{{key}}` riding into argv). The key is
deliberately NOT called `args_from`: the `transform` node already has an
`args_from`, and that one is ONE string naming the payload key holding a whole
args object — same words, different type/arity/semantics. `interpolate_from`
names the mechanism and sits beside `target_from`/`cwd_from`.

Contract — `target_from`:
  - unset  → old behaviour (no append).                               [opt-in]
  - key absent/None in payload → command runs UNCHANGED.              [opt-in per value]
  - value is a str → one appended argument.
  - value is a list[str] → each appended, in order.
  - ANY value failing path-shape validation → TargetError (names key + value).

Contract — `interpolate_from`:
  - unset (None) → byte-identical old behaviour: a `{{key}}` stays a LITERAL.
  - a STRING command CARRYING a `{{`-token is refused at BUILD time (the injection
    edge needs a token; a string command with no token cannot substitute at all,
    so it is legal and interpolation is simply inert).
  - `{{key}}` substitutes anywhere in an element, so `--db-url={{url}}` works.
  - undeclared `{{key}}`, or a `{{`-token that is not a `{{key}}` → error.
  - declared key absent from the payload / None / invalid value → TargetError.

Path-shape allowlist for `target_from` (STRICT): each value must be a non-empty
string of only [A-Za-z0-9._/-], no whitespace or shell metacharacters, must not
start with '-' (no option injection), must not contain '..' (no path traversal),
length-capped. `interpolate_from` values are validated separately and deliberately
more loosely — see `_validate_arg`.

Targets Python 3.9+.
"""
from __future__ import annotations

import re
import shlex
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Union

from ..templating import PLACEHOLDER, fill

# Allowlist mirrors the claude_cli provider's binary-name stance (a bare
# allowlist + no leading '-'): only characters that appear in a plain relative
# or absolute path. Anything else — spaces, $, `, ;, |, &, quotes, globs — is a
# shell metacharacter or command-structure token and is rejected.
_TARGET_ALLOWED_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
# A generous ceiling: real paths are well under this; the cap only stops a
# pathological multi-KB value from riding into an argv.
_TARGET_MAX_LEN = 4096


class TargetError(ValueError):
    """A `target_from` payload value failed strict path-shape validation, or an
    `interpolate_from` interpolation could not be completed. The shell nodes let this
    propagate (it ERRORS the node) rather than run the command without the
    intended target / with a literal `{{key}}` in argv — running the wrong/no
    target silently is the bug class these seams exist to fix. Message names the
    offending key and value. A ValueError subclass, so the builders can raise it
    as a config error at BUILD time too."""


def _validate_one(target_from: str, value: Any) -> str:
    if not isinstance(value, str):
        raise TargetError(
            "target_from {!r}: value {!r} is not a string (each value must be a "
            "path-shaped string; got {})".format(target_from, value, type(value).__name__))
    if not value:
        raise TargetError(
            "target_from {!r}: empty string is not a valid target".format(target_from))
    if len(value) > _TARGET_MAX_LEN:
        raise TargetError(
            "target_from {!r}: value exceeds {}-char cap (len {})".format(
                target_from, _TARGET_MAX_LEN, len(value)))
    if value.startswith("-"):
        raise TargetError(
            "target_from {!r}: value {!r} starts with '-' (rejected — no option "
            "injection into the trusted command)".format(target_from, value))
    if ".." in value:
        raise TargetError(
            "target_from {!r}: value {!r} contains '..' (rejected — no path "
            "traversal)".format(target_from, value))
    if not _TARGET_ALLOWED_RE.match(value):
        raise TargetError(
            "target_from {!r}: value {!r} has characters outside the path-shape "
            "allowlist [A-Za-z0-9._/-] (whitespace/shell metacharacters "
            "rejected)".format(target_from, value))
    return value


def resolve_target_args(payload: Dict[str, Any], target_from: Optional[str]) -> List[str]:
    """Return the validated extra argument(s) to append to a shell command.

    `target_from` unset → []. Key absent or None in `payload` → [] (command
    unchanged; opt-in per value presence). A str value → [value]; a list value →
    each element (must be a str). Empty list → [] (nothing to append). Any value
    failing path-shape validation raises `TargetError`."""
    if not target_from:
        return []
    if target_from not in payload:
        return []
    value = payload[target_from]
    if value is None:
        return []
    if isinstance(value, str):
        values: List[Any] = [value]
    elif isinstance(value, list):
        values = value
    else:
        raise TargetError(
            "target_from {!r}: value {!r} must be a string or a list of strings "
            "(got {})".format(target_from, value, type(value).__name__))
    return [_validate_one(target_from, v) for v in values]


def append_targets(command: Union[str, List[str]], values: List[str]) -> Union[str, List[str]]:
    """Append validated `values` to a trusted `command` as ADDITIONAL arguments.
    Never replaces the base command — only extends it. Matches the two execution
    shapes `_run` accepts: a shell string gets each value `shlex.quote`d and space-
    joined on; an argv list gets each value appended as its own element."""
    if not values:
        return command
    if isinstance(command, str):
        return command + "".join(" " + shlex.quote(v) for v in values)
    return list(command) + list(values)


# ---------------------------------------------------------- interpolate_from --

# Deliberately LOOSER than `_TARGET_ALLOWED_RE`: an interpolated value lands
# inside ONE argv element and is never parsed as shell syntax. Either the command
# is exec'd directly (`create_subprocess_exec` — no shell exists), or `shell: true`
# runs each element through `shlex.quote` before joining (_shell.py). So a value
# containing a space, a `;`, or a `$(...)` reaches the child as inert literal
# text; there is no code path in which it becomes command structure. That is why
# a connection string or a commit message may pass here while `target_from` (whose
# values may ride a shell-STRING command) stays path-shaped.
#
# What IS rejected is what corrupts something OTHER than the shell:
#   - an empty argument (argv slot with no content) and anything past the cap;
#   - a NUL — argv is NUL-terminated, so the rest of the value is silently lost;
#   - every other C0 control except TAB. Newline/CR are the loud cases (an
#     argument spanning lines is a log/parse hazard and near-always injected
#     content), but the rest of C0 is rejected for the same reason one level up:
#     these values ride back out through `stdout_tail` into ANSI operator
#     terminals and rendered reports, where ESC (0x1B) rewrites the screen and
#     BS/CR erase what was already printed. Tab is the one control that is
#     ordinary in real command arguments, so it is allowed.
_ARG_MAX_LEN = _TARGET_MAX_LEN
# C0 minus TAB. NUL and newline/CR keep their own, more specific messages below;
# this catches the remainder (ESC, BEL, BS, VT, FF, SO/SI, the C1-introducers...).
_ARG_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f]")


def _validate_arg(key: str, value: Any) -> str:
    if value is None:
        raise TargetError(
            "interpolate_from {0!r}: payload value is None — the command has a "
            "{{{{{0}}}}} placeholder with nothing to fill it".format(key))
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise TargetError(
            "interpolate_from {!r}: value {!r} is not a string/int/float (got {}) — a "
            "command argument must be a scalar".format(key, value, type(value).__name__))
    text = value if isinstance(value, str) else str(value)
    if not text:
        raise TargetError(
            "interpolate_from {!r}: empty string is not a valid command argument".format(key))
    if len(text) > _ARG_MAX_LEN:
        raise TargetError(
            "interpolate_from {!r}: value exceeds {}-char cap (len {})".format(
                key, _ARG_MAX_LEN, len(text)))
    if "\x00" in text:
        raise TargetError(
            "interpolate_from {!r}: value contains a NUL byte (argv is NUL-terminated — "
            "the rest of the value would be silently dropped)".format(key))
    if "\n" in text or "\r" in text:
        raise TargetError(
            "interpolate_from {!r}: value {!r} contains a newline (rejected — a command "
            "argument spanning lines is near-always injected content)".format(key, text))
    ctrl = _ARG_CONTROL_RE.search(text)
    if ctrl is not None:
        raise TargetError(
            "interpolate_from {!r}: value {!r} contains the control character {!r} "
            "(rejected — C0 controls other than TAB corrupt the argv's readers: "
            "they ride back out through stdout_tail into ANSI terminals and "
            "reports)".format(key, text, ctrl.group(0)))
    return text


def _check_element(element: str, declared: FrozenSet[str]) -> None:
    """Static check of ONE argv element against the DECLARED keys — depends on the
    command and `interpolate_from` only, never on the payload. The builder runs it
    at LOAD time (so a bad pair is a config error, not a 3am node error) and
    `render_command` runs it again per run: one rule, two callers, no drift."""
    for key in PLACEHOLDER.findall(element):
        if key not in declared:
            raise TargetError(
                "command element {0!r} interpolates {{{{{1}}}}} but {1!r} is not in "
                "interpolate_from {2} — an undeclared key is never substituted and "
                "must never ride into argv as a literal".format(element, key, sorted(declared)))
    if "{{" in PLACEHOLDER.sub("", element):
        raise TargetError(
            "command element {0!r} contains a `{{{{`-token that is not a "
            "`{{{{key}}}}` placeholder — it would ride into argv as a literal "
            "(the render/gate templater has no `{{{{?key}}}}`/`{{{{!key}}}}` "
            "dialect; see yaah.templating)".format(element))


def check_command(command: Union[str, List[str]],
                  interpolate_from: Optional[Sequence[str]]) -> None:
    """BUILD-time refusal for an `interpolate_from` command. Raises TargetError (a
    ValueError, so builders surface it as a config error) when the pair can never
    render safely: a STRING command that CARRIES a `{{`-token (interpolation is
    argv-list-only — a string command goes to `create_subprocess_shell`, where
    substitution would be raw source concatenation, the injection edge this closes
    BY CONSTRUCTION), or a list element carrying a `{{key}}` that
    `interpolate_from` does not declare.

    A string command with NO `{{`-token is LEGAL: substitution needs a token, so
    without one it cannot occur and interpolation is simply inert. That matters
    because a host overlay routinely supplies its RED/GREEN runner as a shell
    string while the pipeline declares the key centrally — refusing those would
    break every such consumer to close an edge that isn't there.

    `interpolate_from` absent → no-op, so today's configs are untouched."""
    if interpolate_from is None:
        return
    if isinstance(command, str):
        if "{{" in command:
            raise TargetError(
                "interpolate_from {} is declared and the command is a STRING carrying a "
                "`{{{{`-token — interpolation is argv-LIST-only, because a string command "
                "is handed to a shell and a substituted value would become shell SOURCE "
                "rather than one argument. Rewrite the command as a list of arguments. "
                "(A string command with NO `{{{{`-token is fine: nothing can be "
                "substituted, so interpolation is inert.)".format(sorted(interpolate_from)))
        return
    declared = frozenset(interpolate_from)
    for element in command:
        if isinstance(element, str):
            _check_element(element, declared)


def render_command(command: Union[str, List[str]], payload: Dict[str, Any],
                   interpolate_from: Optional[Sequence[str]]) -> Union[str, List[str]]:
    """Substitute the declared `{{key}}` placeholders in an argv-LIST command from
    the payload, returning the rendered argv. `interpolate_from` unset (None) → the
    command is returned UNCHANGED, so a `{{key}}` in a config that never opted in
    stays the literal it is today. A declared key absent/None/invalid, or any token
    that cannot be filled, raises TargetError — the node ERRORS rather than exec a
    command with a literal `{{key}}` in it."""
    if interpolate_from is None:
        return command
    check_command(command, interpolate_from)
    if isinstance(command, str):
        # a token-free STRING command (check_command already refused a tokened
        # one): interpolation is inert, and the string must NOT fall into the
        # per-element loop below, which would iterate it CHARACTER by character.
        return command
    rendered: List[Any] = []
    for element in command:
        if not isinstance(element, str) or "{{" not in element:
            rendered.append(element)
            continue
        values: Dict[str, str] = {}
        for key in PLACEHOLDER.findall(element):
            if key in values:
                continue
            if key not in payload:
                raise TargetError(
                    "interpolate_from {!r}: key is not in the payload (have: {}) — the "
                    "command element {!r} cannot be filled".format(
                        key, sorted(payload), element))
            values[key] = _validate_arg(key, payload[key])
        rendered.append(fill(element, values)[0])  # every key present → nothing unfilled
    return rendered
