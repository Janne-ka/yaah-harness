"""The fan-in reduce for the fork-join review pipeline.

A `reduce` target gets the arrived branches as `{branch_id: payload}` — here each
lens left its JSON finding in `payload["raw"]`. We merge them into one report.
The engine never learns the data shape; the reduce owns it (that's why it's an
`fn:` target, not engine code).
"""
from yaah.jsonio import extract_json


def merge(arrived):
    # extract_json — sonnet/haiku fence their JSON; strict json.loads would break.
    lines = []
    for branch in sorted(arrived):
        raw = arrived[branch].get("raw", "{}")
        try:
            finding = extract_json(raw).get("finding", "")
        except Exception:
            finding = "(unreadable)"
        lines.append("- [{}] {}".format(branch, finding))
    return {"report": "\n".join(lines), "count": len(lines)}
