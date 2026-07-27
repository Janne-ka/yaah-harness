"""_target — the narrow, validated payload→command-argument seam for shell nodes.

Used by: ShellNode and ShellCheck (the `target_from` config key).
Where: yaah.nodes only (private helper, not exported).
Why: the shell nodes' COMMAND is trusted config and MUST NOT come from the
payload — a deliberate anti-injection stance. But one production footgun needs a
controlled exception: a gate ran a *pre-guessed* path (e.g. a test-spec overlay)
while the agent wrote its artifact at a *different* path, so green-able runs were
sent to failure. `target_from` lets a shell node declare ONE payload key whose
value(s) are APPENDED to the trusted command as additional argument(s) — never
replacing it, never interpreted as a command — and only if every value passes
strict path-shape validation. Any invalid value ERRORS the node (loud), because
running the command WITHOUT the target (or with a wrong one) silently is the exact
bug class this fixes.

Contract:
  - `target_from` unset  → old behaviour (no append).                 [opt-in]
  - key absent/None in payload → command runs UNCHANGED.              [opt-in per value]
  - value is a str → one appended argument.
  - value is a list[str] → each appended, in order.
  - ANY value failing path-shape validation → TargetError (names key + value).

Path-shape allowlist (STRICT): each value must be a non-empty string of only
[A-Za-z0-9._/-], no whitespace or shell metacharacters, must not start with '-'
(no option injection), must not contain '..' (no path traversal), length-capped.

Targets Python 3.9+.
"""
from __future__ import annotations

import re
import shlex
from typing import Any, Dict, List, Optional, Union

# Allowlist mirrors the claude_cli provider's binary-name stance (a bare
# allowlist + no leading '-'): only characters that appear in a plain relative
# or absolute path. Anything else — spaces, $, `, ;, |, &, quotes, globs — is a
# shell metacharacter or command-structure token and is rejected.
_TARGET_ALLOWED_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
# A generous ceiling: real paths are well under this; the cap only stops a
# pathological multi-KB value from riding into an argv.
_TARGET_MAX_LEN = 4096


class TargetError(ValueError):
    """A `target_from` payload value failed strict path-shape validation. The
    shell nodes let this propagate (it ERRORS the node) rather than run the
    command without the intended target — running the wrong/no target silently
    is the bug class `target_from` exists to fix. Message names the offending
    key and value."""


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
