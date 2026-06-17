#!/usr/bin/env python3
"""Deterministic subset of YAAH's pre-submission review.

Runs the grep-shaped, mechanical checks against the working-tree diff and
prints a structured report. The semantic checks (new nouns, three lenses, the
footgun) need a human or an LLM — invoke the `yaah-review-my-pr` skill (or the
AGENTS.md section of the same name) for those.

Source of truth for the rules: docs/contributor/pre-submission-check.md.
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable, List, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
BANLIST_FILE = REPO_ROOT / "docs" / "contributor" / "banlist.txt"
# A gitignored sibling for terms that must NOT be published — a downstream app's
# or company's proper nouns. A public banlist can't guard a secret string (listing
# it leaks it), so those live here and travel only with the checkout that needs
# them. Absent in the public repo; present in the consuming project's clone / CI.
BANLIST_LOCAL_FILE = REPO_ROOT / "docs" / "contributor" / "banlist.local.txt"

CORE_ROOTS = ("src/yaah/core/", "src/yaah/harness/", "src/yaah/comms/")
ENGINE_ROOT = "src/yaah/"

ALLOWED_CORE_IMPORT_PREFIXES = ("yaah.",)
STDLIB_HINT = None  # we check membership against sys.stdlib_module_names if available


def git(*args: str) -> str:
    out = subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    return out.stdout


def changed_files() -> List[Path]:
    """All files different from HEAD (staged, unstaged, untracked)."""
    tracked = git("diff", "HEAD", "--name-only").splitlines()
    untracked = git("ls-files", "--others", "--exclude-standard").splitlines()
    seen, out = set(), []
    for p in [*tracked, *untracked]:
        if p and p not in seen:
            seen.add(p)
            out.append(Path(p))
    return out


def diff_line_counts() -> Tuple[int, int]:
    """(added, removed) line counts vs HEAD across the working tree."""
    raw = git("diff", "HEAD", "--numstat")
    added = removed = 0
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        a, r, _ = parts
        if a.isdigit():
            added += int(a)
        if r.isdigit():
            removed += int(r)
    return added, removed


def added_lines_for(path: Path) -> List[str]:
    """Lines added to `path` in the working-tree diff (no leading '+')."""
    if not (REPO_ROOT / path).exists():
        return []
    raw = git("diff", "HEAD", "--", str(path))
    out, in_hunk = [], False
    for line in raw.splitlines():
        if line.startswith("@@"):
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            out.append(line[1:])
    if not raw and (REPO_ROOT / path).is_file():
        # untracked / brand-new file: every line is "added"
        try:
            return (REPO_ROOT / path).read_text().splitlines()
        except (OSError, UnicodeDecodeError):
            return []
    return out


def is_stdlib(module_root: str) -> bool:
    stdlib = getattr(sys, "stdlib_module_names", None)
    if stdlib is not None:
        return module_root in stdlib
    # Python 3.9 fallback: a small curated allow-list. The CI environment uses
    # 3.10+ where sys.stdlib_module_names exists; this branch is just so the
    # script never crashes on an older interpreter.
    common = {
        "abc", "argparse", "ast", "asyncio", "base64", "collections", "concurrent",
        "contextlib", "copy", "dataclasses", "datetime", "enum", "functools",
        "hashlib", "http", "importlib", "inspect", "io", "ipaddress", "itertools",
        "json", "logging", "math", "os", "pathlib", "pickle", "platform", "queue",
        "random", "re", "shlex", "shutil", "signal", "socket", "ssl", "stat",
        "string", "struct", "subprocess", "sys", "tempfile", "textwrap", "threading",
        "time", "traceback", "types", "typing", "unittest", "urllib", "uuid",
        "warnings", "weakref", "xml", "zipfile",
    }
    return module_root in common


# ---------- CHECK 2 — core purity --------------------------------------------

def check_core_purity(files: Iterable[Path]) -> Tuple[str, str]:
    offenders: List[str] = []
    for f in files:
        s = str(f)
        if not any(s.startswith(r) for r in CORE_ROOTS):
            continue
        if not s.endswith(".py"):
            continue
        for line in added_lines_for(f):
            m = re.match(r"\s*(?:from\s+([\w\.]+)\s+import|import\s+([\w\.]+))", line)
            if not m:
                continue
            mod = (m.group(1) or m.group(2) or "").split(".")[0]
            if not mod:
                continue
            if mod.startswith("_"):
                continue
            if any(mod.startswith(p.rstrip(".")) for p in ALLOWED_CORE_IMPORT_PREFIXES):
                continue
            if is_stdlib(mod):
                continue
            offenders.append(f"{s}: imports '{mod}'")
    if offenders:
        return "FAIL", "third-party import inside the zero-dep core: " + "; ".join(offenders)
    return "PASS", "no new third-party imports in core/harness/comms"


# ---------- CHECK 3 — domain leakage -----------------------------------------

def load_banlist() -> List[str]:
    out = []
    for f in (BANLIST_FILE, BANLIST_LOCAL_FILE):  # public + gitignored local
        if not f.exists():
            continue
        for line in f.read_text().splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                out.append(s)
    return out


def check_domain_leakage(files: Iterable[Path]) -> Tuple[str, str]:
    words = load_banlist()
    if not words:
        return "PASS", "banlist empty (or missing) — no terms to enforce"
    pattern = re.compile(r"\b(" + "|".join(re.escape(w) for w in words) + r")\b",
                         re.IGNORECASE)
    hits: List[str] = []
    for f in files:
        s = str(f)
        if not s.startswith(ENGINE_ROOT):
            continue
        if not s.endswith(".py"):
            continue
        for i, line in enumerate(added_lines_for(f), start=1):
            m = pattern.search(line)
            if m:
                hits.append(f"{s}: banlist term '{m.group(1)}' in added line")
                break  # one hit per file is enough to report
    if hits:
        return "FAIL", "domain term leaked into engine: " + "; ".join(hits)
    return "PASS", "no banlist terms in engine changes"


# ---------- CHECK 4 — file shape (new files) ---------------------------------

def is_new_file(path: Path) -> bool:
    raw = git("log", "-1", "--format=%H", "--", str(path))
    return raw.strip() == ""


def check_file_shape(files: Iterable[Path]) -> Tuple[str, str]:
    problems: List[str] = []
    for f in files:
        s = str(f)
        if not s.startswith(ENGINE_ROOT) or not s.endswith(".py"):
            continue
        if not is_new_file(f):
            continue
        full = REPO_ROOT / f
        if not full.exists():
            continue
        try:
            tree = ast.parse(full.read_text())
        except (SyntaxError, OSError, UnicodeDecodeError):
            problems.append(f"{s}: could not parse")
            continue
        classes = [n for n in tree.body if isinstance(n, ast.ClassDef)]
        if len(classes) > 1:
            problems.append(f"{s}: {len(classes)} top-level classes (expected 1)")
        # module docstring presence ("who calls this, where, why")
        if not ast.get_docstring(tree):
            problems.append(f"{s}: missing module docstring (who calls this, where, why)")
        # class-name == filename (PascalCase ↔ snake_case)
        if len(classes) == 1:
            expected = "".join(part.capitalize() for part in full.stem.split("_"))
            actual = classes[0].name
            if actual != expected:
                problems.append(
                    f"{s}: class '{actual}' does not match filename "
                    f"(expected '{expected}')"
                )
    if problems:
        return "WARN", "; ".join(problems)
    return "PASS", "new files follow one-class-per-file + docstring convention"


# ---------- driver ------------------------------------------------------------

def main() -> int:
    if not (REPO_ROOT / ".git").exists():
        print("review_my_pr.py: not a git repo (run from inside the YAAH checkout)",
              file=sys.stderr)
        return 2
    files = changed_files()
    added, removed = diff_line_counts()
    net = added - removed

    print("YAAH pre-submission review (deterministic subset)")
    print()
    print(f"Lines: +{added} / -{removed}   net {net:+d}")
    print()

    if not files:
        print("No working-tree changes vs HEAD. Nothing to review.")
        return 0

    checks: List[Tuple[str, str, str]] = []  # (number, verdict, reason)

    v, msg = check_core_purity(files)
    checks.append(("CHECK 2 — core purity        ", v, msg))

    v, msg = check_domain_leakage(files)
    checks.append(("CHECK 3 — domain leakage     ", v, msg))

    v, msg = check_file_shape(files)
    checks.append(("CHECK 4 — file shape         ", v, msg))

    for label, verdict, reason in checks:
        print(f"{label}: {verdict:<4}  {reason}")

    print()
    print("Semantic checks (1, 5, 6, 7, 8, 9) are not run by this script.")
    print("Invoke the `yaah-review-my-pr` skill (Claude Code) or use the")
    print("'Pre-submission self-review' prompt in AGENTS.md.")
    print()

    fail = any(v == "FAIL" for _, v, _ in checks)
    warn = any(v == "WARN" for _, v, _ in checks)
    if fail:
        print("Deterministic verdict: BLOCKED  (fix FAILs before opening the PR)")
        return 1
    if warn:
        print("Deterministic verdict: NEEDS REVISION  (address WARNs)")
        return 0
    print("Deterministic verdict: PASS  (semantic checks still required)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
