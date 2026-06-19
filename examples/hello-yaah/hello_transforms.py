"""The parse transform for the hello-yaah pipeline.

A `transform` node with `call: "envelope"` is `fn(envelope, config) -> dict`; the
returned dict SPREADS over the payload top-level. That is how `summary` becomes a
real payload key the `render` stage can interpolate — an agent's output arrives as
a STRING in `payload["raw"]`, and nothing merges it until a parse step like this.
"""
from yaah.jsonio import extract_json


def parse(envelope, config):
    # extract_json (not json.loads) — sonnet/haiku wrap JSON in markdown
    # fences; strict json.loads breaks on real-model runs. opus is the
    # only model that reliably emits bare JSON.
    return extract_json(envelope.payload.get("raw", "{}"))
