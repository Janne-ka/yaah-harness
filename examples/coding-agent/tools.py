"""Tool implementations for the coding-agent example.

Four tools, exposed to the agent via the pipeline's agent_loop config and
dispatched through call_target (`fn:tools:read_file`, etc.):

- read_file(path)   — return file contents
- edit_file(path, old_string, new_string) — exact-string replacement
- run_bash(cmd, cwd=None) — shell out, return stdout + stderr + exit code
- done(summary)     — terminator: signal to the agent loop that the task
                       is complete and carry a summary string back.

These are intentionally small/audit-friendly. Authors who want broader
capability (multi-edit, regex edit, file globbing, partial reads) can
fork this file — the dispatch contract is just `fn(args: dict) -> any`.

NOTE: `run_bash` is unsandboxed; in a real deployment scope it down via
the agent's `--allowedTools` (claude_cli) or wrap in a worktree-isolated
shell. For this example, the agent runs against fixtures/ which is a
disposable fixture directory.
"""
from __future__ import annotations

import os
import subprocess
from typing import Any, Dict


def read_file(args: Dict[str, Any]) -> str:
    path = args.get("path")
    if not path or not isinstance(path, str):
        return "error: read_file requires a 'path' string argument"
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return "error: no such file: {}".format(path)
    except IsADirectoryError:
        return "error: {} is a directory, not a file".format(path)
    except Exception as e:
        return "error: {}: {}".format(type(e).__name__, e)


def edit_file(args: Dict[str, Any]) -> str:
    # Non-atomic: a crash between read and write loses the file. Fine for an
    # example fixture; production tools would write-then-rename via tempfile.
    path = args.get("path")
    old = args.get("old_string")
    new = args.get("new_string")
    if not all(isinstance(x, str) for x in (path, old, new)):
        return "error: edit_file requires 'path', 'old_string', 'new_string' strings"
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
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
    with open(path, "w", encoding="utf-8") as f:
        f.write(new_text)
    return "ok: edited {} (1 replacement)".format(path)


def run_bash(args: Dict[str, Any]) -> Dict[str, Any]:
    cmd = args.get("cmd")
    cwd = args.get("cwd")
    timeout = args.get("timeout", 60)               # bound at 60s by default
    if not cmd or not isinstance(cmd, str):
        return {"error": "run_bash requires a 'cmd' string argument"}
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                              cwd=cwd, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        return {"error": "timeout after {}s",
                "stdout": (e.stdout.decode(errors="replace") if e.stdout else "")[-2000:],
                "stderr": (e.stderr.decode(errors="replace") if e.stderr else "")[-2000:]}
    return {"exit_code": proc.returncode,
            "stdout": (proc.stdout or "")[-4000:],   # cap to avoid token blowup
            "stderr": (proc.stderr or "")[-2000:]}


def done(args: Dict[str, Any]) -> Dict[str, Any]:
    summary = args.get("summary", "")
    return {"done": True, "summary": str(summary)}
