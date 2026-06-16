"""post (write sink) and transform (call-external) node utilities.

post: write a payload field via a DataSink (memory-write is a post).
transform: the one generic class behind tools+mcp — fn:/node: targets here.

Run: cd yaah && PYTHONPATH=src python3 tests/test_post_transform.py
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile

from yaah import Envelope, InProcessComms, Kind, NodeConfig
from yaah.data import RoutingDataSink
from yaah.adapters.data import FileSink
from yaah.nodes import PostNode, TransformNode


# target for the transform fn: scheme (resolved via importlib as this module)
def double(args: dict) -> dict:
    return {"doubled": args["n"] * 2}


class Adder:
    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        return input.reply(Kind.RESULT, sum=input.payload["a"] + input.payload["b"])


class EchoPayload:  # echoes the payload it received (uses reply_with, dict payload)
    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        return input.reply_with(Kind.RESULT, dict(input.payload))


async def scenario_post() -> None:
    with tempfile.TemporaryDirectory() as d:
        sink = RoutingDataSink({"file": FileSink(base_dir=d)}, default="file")
        node = PostNode(sink, "file:out/report.html", field="html", into="path")
        env = Envelope(Kind.TASK, {"html": "<h1>hi</h1>", "task": "T1"})
        out = await node.invoke(env, NodeConfig())

        written = os.path.join(d, "out/report.html")
        assert open(written).read() == "<h1>hi</h1>"
        assert out.payload["path"] == written          # handle returned
        assert out.payload["task"] == "T1"             # payload carried forward

        # non-str value is JSON-encoded
        node2 = PostNode(sink, "file:data.json", field="obj", into="path")
        out2 = await node2.invoke(Envelope(Kind.TASK, {"obj": {"x": 1}}), NodeConfig())
        assert json.loads(open(out2.payload["path"]).read()) == {"x": 1}


async def scenario_transform_fn() -> None:
    node = TransformNode("fn:test_post_transform:double", args_from=None, into="r")
    out = await node.invoke(Envelope(Kind.TASK, {"n": 21}), NodeConfig())
    assert out.payload["r"] == {"doubled": 42}, out.payload
    assert out.payload["n"] == 21, "input carried forward"


async def scenario_transform_node() -> None:
    """A transform whose target is another node — a tool that IS a node over Comms."""
    comms = InProcessComms()
    comms.register("role:adder", Adder())
    node = TransformNode("node:role:adder", comms=comms, into="r")
    out = await node.invoke(Envelope(Kind.TASK, {"a": 2, "b": 3}), NodeConfig())
    assert out.payload["r"]["sum"] == 5, out.payload


async def scenario_transform_node_reserved_key() -> None:
    """M4: a node: transform forwarding a payload that contains a 'sender' key must
    not raise (reply_with takes a dict, so no kwarg collision with reply(sender=))."""
    comms = InProcessComms()
    comms.register("role:echo", EchoPayload())
    node = TransformNode("node:role:echo", comms=comms, into="r")
    out = await node.invoke(Envelope(Kind.TASK, {"sender": "alice", "a": 1}), NodeConfig())
    assert out.payload["r"]["sender"] == "alice", out.payload  # forwarded, no crash


async def scenario_transform_http() -> None:
    """A transform whose target is an HTTP endpoint (a tool/MCP-style external
    call). Uses a tiny stdlib echo server on an ephemeral port."""
    import http.server
    import json as _json
    import threading

    class Echo(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length", 0))
            body = _json.loads(self.rfile.read(n) or b"{}")
            resp = _json.dumps({"echoed": body}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(resp)

        def log_message(self, *a):  # silence
            return

    srv = http.server.HTTPServer(("127.0.0.1", 0), Echo)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = "http://127.0.0.1:{}/tool".format(srv.server_address[1])
        node = TransformNode(url, into="r")
        out = await node.invoke(Envelope(Kind.TASK, {"q": "hi"}), NodeConfig(timeout=5))
        assert out.payload["r"] == {"echoed": {"q": "hi"}}, out.payload
    finally:
        srv.shutdown()


async def main() -> None:
    await scenario_post()
    await scenario_transform_fn()
    await scenario_transform_node()
    await scenario_transform_node_reserved_key()
    await scenario_transform_http()
    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
