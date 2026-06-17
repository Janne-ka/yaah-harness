"""The parse transform for the hello-yaah pipeline.

A `transform` node with `call: "envelope"` is `fn(envelope, config) -> dict`; the
returned dict SPREADS over the payload top-level. That is how `summary` becomes a
real payload key the `render` stage can interpolate — an agent's output arrives as
a STRING in `payload["raw"]`, and nothing merges it until a parse step like this.
"""
import json


def parse(envelope, config):
    return json.loads(envelope.payload.get("raw", "{}"))
