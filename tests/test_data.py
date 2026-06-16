"""Data 'get' layer: GitDiffSource (changed lines ± N), FileDataSource line
range, RoutingDataSource dispatch, and GetNode enriching the payload.

Run: cd yaah && PYTHONPATH=src python3 tests/test_data.py
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile

from yaah import Envelope
from yaah.core import NodeConfig
from yaah.data import RoutingDataSource
from yaah.adapters.data import FileDataSource, GitDiffSource
from yaah.nodes import GetNode


async def scenario_git_diff(tmp: str) -> None:
    repo = os.path.join(tmp, "r")
    os.makedirs(repo)
    body = "".join("line {}\n".format(i) for i in range(1, 21))
    with open(os.path.join(repo, "f.txt"), "w") as f:
        f.write(body)
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    for cmd in (["git", "init", "-q"], ["git", "add", "."], ["git", "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=repo, env=env, check=True)
    # change one line in the middle (line 10)
    changed = body.replace("line 10\n", "line 10 CHANGED\n")
    with open(os.path.join(repo, "f.txt"), "w") as f:
        f.write(changed)

    # Only the hunk BODY lines (context + add/del), not headers/@@-section-headings.
    def body(diff):
        return {ln[1:] for ln in diff.splitlines()
                if ln[:1] in (" ", "+", "-") and not ln.startswith(("+++", "---"))}

    # context=0: only the changed lines, no surrounding context
    d0 = await GitDiffSource(context=0).fetch("", cwd=repo)
    assert "line 10 CHANGED" in body(d0) and "line 10" in body(d0)
    assert "line 9" not in body(d0) and "line 11" not in body(d0), "context=0 leaked surroundings"

    # context=2: a couple of lines either side ride along
    d2 = body(await GitDiffSource(context=2).fetch("", cwd=repo))
    assert {"line 8", "line 9", "line 11", "line 12"} <= d2
    assert "line 7" not in d2 and "line 13" not in d2

    # new untracked file only shows with intent_to_add
    with open(os.path.join(repo, "new.txt"), "w") as f:
        f.write("brand new\n")
    assert "brand new" not in await GitDiffSource(context=0).fetch("", cwd=repo)
    assert "brand new" in await GitDiffSource(context=0, intent_to_add=True).fetch("", cwd=repo)


async def scenario_file_and_routing(tmp: str) -> None:
    p = os.path.join(tmp, "big.txt")
    with open(p, "w") as f:
        f.write("".join("L{}\n".format(i) for i in range(1, 101)))
    files = FileDataSource()
    assert await files.fetch(p, start=10, end=12) == "L10\nL11\nL12\n"
    assert await files.fetch(p, center=50, radius=1) == "L49\nL50\nL51\n"
    # assessment #11: a computed center - radius near the top of the file clamps
    # to line 1 instead of tripping the explicit start<=0 rejection
    assert await files.fetch(p, center=2, radius=5) == "".join(
        "L{}\n".format(i) for i in range(1, 8))

    routing = RoutingDataSource({"file": files}, default="file")
    assert (await routing.fetch("file:" + p, start=1, end=1)) == "L1\n"

    try:
        await RoutingDataSource({}).fetch("nope:x")
        raise AssertionError("expected LookupError")
    except LookupError:
        pass


async def scenario_get_node(tmp: str) -> None:
    p = os.path.join(tmp, "g.txt")
    with open(p, "w") as f:
        f.write("alpha\nbeta\ngamma\n")
    routing = RoutingDataSource({"file": FileDataSource()}, default="file")
    node = GetNode(routing, "file:" + p, into="slice")
    out = await node.invoke(Envelope("task", {"task": "T1", "keep": "me"}), NodeConfig())
    assert out.payload["slice"] == "alpha\nbeta\ngamma\n"
    assert out.payload["keep"] == "me" and out.payload["task"] == "T1", "must carry payload forward"


async def scenario_file_data_source_fails_loud_on_half_spec_and_zero_start(tmp: str) -> None:
    # assessment cluster 5 #3: previously a half-specified center/radius silently
    # fell through and returned the entire file; start=0 was silently treated as
    # line 1. Both now raise loud.
    p = os.path.join(tmp, "validation.txt")
    with open(p, "w") as f:
        f.write("L1\nL2\nL3\n")
    files = FileDataSource()
    for kwargs in [{"center": 2}, {"radius": 1}]:
        try:
            await files.fetch(p, **kwargs)
        except ValueError:
            continue
        raise AssertionError("expected ValueError on half-spec center/radius {}".format(kwargs))
    try:
        await files.fetch(p, start=0, end=2)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError on start=0")


async def scenario_file_data_source_relative_escape_rejected(tmp: str) -> None:
    # cluster 5 security #3: a relative key like `../../etc/passwd` used to
    # resolve straight through; now safe_join contains it.
    base = os.path.join(tmp, "base")
    os.makedirs(base, exist_ok=True)
    files = FileDataSource(base_dir=base)
    try:
        await files.fetch("../escape.txt")
    except ValueError:
        return
    raise AssertionError("expected ValueError on relative escape")


async def main() -> None:
    if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
        print("skip: git not available")
        return
    with tempfile.TemporaryDirectory() as tmp:
        await scenario_git_diff(tmp)
        await scenario_file_and_routing(tmp)
        await scenario_get_node(tmp)
        await scenario_file_data_source_fails_loud_on_half_spec_and_zero_start(tmp)
        await scenario_file_data_source_relative_escape_rejected(tmp)
    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
