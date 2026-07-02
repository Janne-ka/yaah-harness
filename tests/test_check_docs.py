"""check_docs — the runnable-docs checker's own mechanics, on tempdir fixtures.

Proves the checker's contract: a valid pipeline/root block PASSES through the
real validators; an invalid one FAILS with the real validator message; fragments
are skipped BY SHAPE (unparseable, or too few root keys); the explicit
`<!-- doc-snippet: skip -->` marker opts a block out; ```jsonc comment stripping
works without mangling `//` inside string values (tls:// URLs). Ends by running
the checker on the REAL docs — the tree must be green.

Run: cd yaah && PYTHONPATH=src python3 tests/test_check_docs.py
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "check_docs.py")

_spec = importlib.util.spec_from_file_location("check_docs", SCRIPT)
check_docs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_docs)

TMP = tempfile.mkdtemp(prefix="check-docs-test-")

VALID_PIPELINE = """\
{"nodes": {"summarize": {"type": "agent", "prompt": "Summarize.", "model": "fake:ok"}},
 "graph": {"start": "summarize", "stages": {"summarize": {"node": "summarize"}}}}
"""

# `then` names an undeclared stage — the real validate_pipeline error
INVALID_PIPELINE = VALID_PIPELINE.replace(
    '{"node": "summarize"}', '{"node": "summarize", "then": "publish"}')


def _results(md_text: str):
    path = os.path.join(TMP, "fixture.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(md_text)
    return check_docs.check_files([path], root=TMP)


def _fenced(body: str, lang: str = "json", above: str = "") -> str:
    return "# Doc\n\n{}```{}\n{}```\n".format(above, lang, body)


def valid_pipeline_block_passes() -> None:
    (r,) = _results(_fenced(VALID_PIPELINE))
    assert r.status == "pass" and r.detail == "pipeline", r


def invalid_pipeline_fails_with_real_message() -> None:
    (r,) = _results(_fenced(INVALID_PIPELINE))
    assert r.status == "fail", r
    assert "then 'publish' is not a stage" in r.detail, r.detail


def invalid_root_fails_despite_one_typo_key() -> None:
    # 3 of 4 keys are root keys -> still classified as root (majority rule),
    # so the typo'd 'provider' FAILS with the engine's did-you-mean message
    # instead of demoting the block to "fragment".
    (r,) = _results(_fenced(
        '{"provider": {}, "pipeline": "p.json", "input": {}, "run": true}\n'))
    assert r.status == "fail", r
    assert "provider" in r.detail and "did you mean" in r.detail, r.detail


def valid_root_block_passes() -> None:
    (r,) = _results(_fenced(
        '{"providers": {"fake": {"type": "fake", "default": "ok"}},\n'
        ' "default_provider": "fake", "pipeline": "p.json", "run": true}\n'))
    assert r.status == "pass" and r.detail == "root", r


def payload_fragment_skipped_by_shape() -> None:
    (r,) = _results(_fenced('{"summary": "hello", "decision": "approve"}\n'))
    assert r.status == "skip" and "fragment by shape" in r.detail, r


def unparseable_fragment_skipped() -> None:
    # the docs' common shape: a bare `"stage": {...}` excerpt
    (r,) = _results(_fenced('"summarize": {"node": "role:summarize"},\n'))
    assert r.status == "skip" and "not valid JSON" in r.detail, r


def skip_marker_opts_out() -> None:
    for marker in ("skip", "example-only"):
        (r,) = _results(_fenced(
            INVALID_PIPELINE, above="<!-- doc-snippet: {} -->\n".format(marker)))
        assert r.status == "skip" and r.detail == "doc-snippet marker", (marker, r)


def marker_must_be_immediately_before() -> None:
    (r,) = _results(_fenced(
        INVALID_PIPELINE, above="<!-- doc-snippet: skip -->\n\n"))
    assert r.status == "fail", r  # a blank line breaks the marker's adjacency


def jsonc_comments_stripped() -> None:
    body = (
        "{\n"
        '  // what runs\n'
        '  "providers": {"fake": {"type": "fake"}},  // trailing comment\n'
        '  "transport": {"type": "nats", "url": "tls://nats.example:4222"},\n'
        '  "pipeline": "p.json", "run": true\n'
        "}\n")
    (r,) = _results(_fenced(body, lang="jsonc"))
    assert r.status == "pass" and r.detail == "root", r  # tls:// survived


def json_fence_gets_no_comment_stripping() -> None:
    (r,) = _results(_fenced('// comment\n' + VALID_PIPELINE))
    assert r.status == "skip" and "not valid JSON" in r.detail, r


def indented_fence_is_seen() -> None:
    indented = "1. item:\n   ```json\n" + \
        "".join("   " + ln + "\n" for ln in INVALID_PIPELINE.splitlines()) + \
        "   ```\n"
    (r,) = _results(indented)
    assert r.status == "fail", r


def real_docs_are_green() -> None:
    proc = subprocess.run(
        [sys.executable, SCRIPT], cwd=REPO,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out = proc.stdout.decode("utf-8", "replace")
    assert proc.returncode == 0, out
    assert "0 FAIL" in out, out


def main() -> None:
    try:
        valid_pipeline_block_passes()
        invalid_pipeline_fails_with_real_message()
        invalid_root_fails_despite_one_typo_key()
        valid_root_block_passes()
        payload_fragment_skipped_by_shape()
        unparseable_fragment_skipped()
        skip_marker_opts_out()
        marker_must_be_immediately_before()
        jsonc_comments_stripped()
        json_fence_gets_no_comment_stripping()
        indented_fence_is_seen()
        real_docs_are_green()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print("ok")


if __name__ == "__main__":
    main()
