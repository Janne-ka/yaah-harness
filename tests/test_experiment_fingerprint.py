"""config_fingerprint — the variant-identity hash behind experiment rows (AB-1).

The population-integrity contract under test (the design eval's D): a
fingerprint must change when ANY behavior-bearing config changes — the merged
root, the merged pipeline (file or inline), a file-sourced PROMPT's bytes, a
render's template_file bytes — and must NOT change on per-run data (the
`input` key) or unrelated files. Prompts live in files (project position), so
hashing only the JSON would silently mix populations when a prompt is edited
mid-campaign — the exact failure the fingerprint exists to prevent.

Run: cd yaah && PYTHONPATH=src python3 tests/test_experiment_fingerprint.py

Targets Python 3.9+.
"""
from __future__ import annotations

import json
import os
import tempfile

from yaah.experiment import config_fingerprint

PIPELINE = {
    "nodes": {
        "role:draft": {"type": "agent", "prompt": "file:draft", "model": "fake:m"},
        "role:render": {"type": "render", "template_file": "report.tmpl",
                        "then": None},
    },
    "graph": {"start": "s", "stages": {
        "s": {"node": "role:draft", "then": "r"},
        "r": {"node": "role:render"},
    }},
}


def _setup(d: str) -> dict:
    os.makedirs(os.path.join(d, "prompts"), exist_ok=True)
    with open(os.path.join(d, "prompts", "draft.md"), "w") as f:
        f.write("draft the thing\n")
    with open(os.path.join(d, "report.tmpl"), "w") as f:
        f.write("{{raw}}\n")
    with open(os.path.join(d, "pipe.json"), "w") as f:
        json.dump(PIPELINE, f)
    return {
        "transport": {"type": "inproc"},
        "providers": {"fake": {"type": "fake", "default": "x"}},
        "default_provider": "fake",
        "prompt_sources": {"file": {"type": "file", "dir": "prompts"}},
        "default_prompt_source": "file",
        "pipeline": "pipe.json",
        "input": {"request": "one"},
        "run": True,
    }


def main() -> None:
    with tempfile.TemporaryDirectory() as d:
        root = _setup(d)
        fp0 = config_fingerprint(root, d)
        assert isinstance(fp0, str) and len(fp0) >= 16, fp0
        assert config_fingerprint(root, d) == fp0     # deterministic

        # per-run data (`input`) is NOT identity — a campaign varies inputs per row
        assert config_fingerprint(dict(root, input={"request": "two"}), d) == fp0

        # root config change IS identity
        assert config_fingerprint(dict(root, default_provider="fake2",
                                       providers={"fake2": {"type": "fake"}}), d) != fp0

        # a PROMPT FILE edit changes the fingerprint (the population-mixing trap)
        with open(os.path.join(d, "prompts", "draft.md"), "a") as f:
            f.write("be terse\n")
        fp1 = config_fingerprint(root, d)
        assert fp1 != fp0, "prompt-file edit must change the fingerprint"

        # a template_file edit changes it too
        with open(os.path.join(d, "report.tmpl"), "a") as f:
            f.write("---\n")
        fp2 = config_fingerprint(root, d)
        assert fp2 != fp1

        # an UNRELATED file does not
        with open(os.path.join(d, "notes.txt"), "w") as f:
            f.write("irrelevant")
        assert config_fingerprint(root, d) == fp2

        # a pipeline change (file) is identity
        p2 = dict(PIPELINE)
        p2 = json.loads(json.dumps(PIPELINE))
        p2["nodes"]["role:draft"]["model"] = "fake:other"
        with open(os.path.join(d, "pipe.json"), "w") as f:
            json.dump(p2, f)
        assert config_fingerprint(root, d) != fp2

        # inline pipeline dicts fingerprint fine (no file needed)
        inline = dict(root, pipeline=json.loads(json.dumps(PIPELINE)))
        del inline["pipeline"]["nodes"]["role:render"]     # avoid template_file
        del inline["pipeline"]["graph"]["stages"]["r"]
        inline["pipeline"]["graph"]["stages"]["s"]["then"] = None
        fp_inline = config_fingerprint(inline, d)
        inline2 = json.loads(json.dumps(inline))
        inline2["pipeline"]["nodes"]["role:draft"]["model"] = "fake:z"
        assert config_fingerprint(inline2, d) != fp_inline

        # DEFAULTED prompt source dir must hash the same files the RUNTIME
        # reads (factory default: dir="prompts") — a diverging default here
        # made prompt edits invisible to the fingerprint (eval catch F1)
        root_defaulted = json.loads(json.dumps(root))
        root_defaulted["prompt_sources"] = {"file": {"type": "file"}}  # no dir
        fp_d0 = config_fingerprint(root_defaulted, d)
        with open(os.path.join(d, "prompts", "draft.md"), "a") as f:
            f.write("even terser\n")
        assert config_fingerprint(root_defaulted, d) != fp_d0, \
            "prompt edit must be visible under the DEFAULT prompt dir"

    print("PASS config_fingerprint: root+pipeline+prompt-bytes in; input + unrelated files out")


if __name__ == "__main__":
    main()
