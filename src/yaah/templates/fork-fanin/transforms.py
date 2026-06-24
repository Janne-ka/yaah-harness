"""The fan-in reduce for the fork-fanin starter.

A `reduce` target gets the arrived branches as `{branch_id: payload}`.
Each lens is an agent with parse=True (the default, ADR-0004) so its
output JSON has already been merged onto the per-branch payload — the
reducer reads `finding` directly. The engine never learns the data
shape; the reduce owns it (that is why it is an `fn:` target, not
engine code).
"""


def merge(arrived):
    lines = []
    for branch in sorted(arrived):
        finding = arrived[branch].get("finding", "(unreadable)")
        lines.append("- [{}] {}".format(branch, finding))
    return {"report": "\n".join(lines), "count": len(lines)}
