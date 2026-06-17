"""Parse the draft agent's JSON string into payload keys (see hello-yaah for the
data-flow contract: an agent's output is a STRING in payload["raw"] until a
transform like this merges it)."""
import json


def parse(envelope, config):
    return json.loads(envelope.payload.get("raw", "{}"))
