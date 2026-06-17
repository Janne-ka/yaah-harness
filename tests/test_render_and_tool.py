"""Unit tests for RenderNode (file I/O via tmp dirs) and Tool (pure).

RenderNode reads a template (inline or file, mtime-cached) and optionally writes
the rendered output to disk — exercised here against real tmp files, no mocking
needed. Tool is pure config parsing.

Run: cd yaah && PYTHONPATH=src python3 tests/test_render_and_tool.py
"""
from __future__ import annotations

import asyncio
import os
import tempfile

from yaah.agents import Tool
from yaah.core import Envelope, Kind, NodeConfig
from yaah.nodes.render_node import RenderNode

CFG = NodeConfig()


def _env(**payload):
    return Envelope(Kind.TASK, payload)


# ---- RenderNode -------------------------------------------------------------

async def render_inline_fills_known_and_leaves_unknown() -> None:
    # allow_unfilled opts into the degrade path: known keys filled (non-str coerced
    # via str()), unknown placeholder left literal. Without the flag this render
    # FAILS (covered in test_silent_wrong.py) — the footgun is closed by default.
    node = RenderNode(template="Hi {{name}}, you owe {{amount}} — {{missing}}",
                      allow_unfilled=True)
    out = await node.invoke(_env(name="Ada", amount=42), CFG)
    assert out.payload["output"] == "Hi Ada, you owe 42 — {{missing}}"
    assert out.payload["path"] is None


async def render_from_file_with_mtime_cache() -> None:
    with tempfile.TemporaryDirectory() as d:
        tpl = os.path.join(d, "t.tpl")
        with open(tpl, "w") as f:
            f.write("v1 {{x}}")
        node = RenderNode(template_file=tpl)

        out1 = await node.invoke(_env(x="A"), CFG)
        assert out1.payload["output"] == "v1 A"
        # second call hits the mtime cache (same file, unchanged) -> same template
        out2 = await node.invoke(_env(x="B"), CFG)
        assert out2.payload["output"] == "v1 B"

        # changing the file (and bumping mtime) busts the cache -> new template read
        with open(tpl, "w") as f:
            f.write("v2 {{x}}")
        os.utime(tpl, (10 ** 9 + 50, 10 ** 9 + 50))
        out3 = await node.invoke(_env(x="C"), CFG)
        assert out3.payload["output"] == "v2 C"


async def render_writes_out_file_and_makes_dirs() -> None:
    with tempfile.TemporaryDirectory() as d:
        out_path = os.path.join(d, "nested", "report.txt")  # dir does not exist yet
        node = RenderNode(template="REPORT: {{body}}", out_path=out_path)
        out = await node.invoke(_env(body="ok"), CFG)
        assert out.payload["path"] == out_path
        assert os.path.isfile(out_path)
        with open(out_path) as f:
            assert f.read() == "REPORT: ok"


def render_requires_a_template() -> None:
    try:
        RenderNode()
        raise AssertionError("RenderNode with neither template nor template_file must fail")
    except ValueError:
        pass


# ---- Tool -------------------------------------------------------------------

def tool_from_dict_valid_and_invalid() -> None:
    t = Tool.from_dict({"name": "lookup", "impl": "fn:mod:f", "description": "d",
                        "schema": {"type": "object"}})
    assert t.name == "lookup" and t.impl == "fn:mod:f" and t.description == "d"

    for bad in ({"name": "x"}, {"impl": "fn:y"}, {}):
        try:
            Tool.from_dict(bad)
            raise AssertionError("missing name/impl must raise: {!r}".format(bad))
        except ValueError:
            pass


def tool_function_schema_defaults_when_no_schema() -> None:
    with_schema = Tool("n", "fn:m:f", schema={"type": "object", "properties": {"a": {}}})
    assert with_schema.to_function_schema()["function"]["parameters"]["properties"] == {"a": {}}

    no_schema = Tool("n", "fn:m:f")
    params = no_schema.to_function_schema()["function"]["parameters"]
    assert params == {"type": "object", "properties": {}}  # the fallback shape


async def main() -> None:
    await render_inline_fills_known_and_leaves_unknown()
    await render_from_file_with_mtime_cache()
    await render_writes_out_file_and_makes_dirs()
    render_requires_a_template()
    tool_from_dict_valid_and_invalid()
    tool_function_schema_defaults_when_no_schema()
    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
