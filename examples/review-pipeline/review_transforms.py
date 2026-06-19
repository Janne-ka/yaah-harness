"""Parse the draft agent's JSON string into payload keys (see hello-yaah for the
data-flow contract: an agent's output is a STRING in payload["raw"] until a
transform like this merges it)."""
from yaah.jsonio import extract_json


def parse(envelope, config):
    # extract_json — sonnet/haiku fence their JSON; strict json.loads would break.
    return extract_json(envelope.payload.get("raw", "{}"))
