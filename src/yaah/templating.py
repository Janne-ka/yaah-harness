"""templating — the one {{mustache}} placeholder regex + fill, shared by everything that
performs or reasons about template substitution: the render node, the human-gate prompt, and
the data-flow lint (which extracts the {{keys}} a render reads). ONE copy so the lint and the
runtime can never disagree on which tokens a template fills. Dependency-free (only `re`), cheap
to import.

Example:
    fill("hi {{name}}, {{missing}}", {"name": "Sam"})  ->  ("hi Sam, {{missing}}", ["missing"])

Targets Python 3.9+.
"""
from __future__ import annotations

import re
from typing import List, Tuple

PLACEHOLDER = re.compile(r"{{\s*(\w+)\s*}}")


def fill(template: str, payload: dict) -> Tuple[str, List[str]]:
    """Substitute {{key}} from the payload; return (filled_text, names_with_NO_value). A
    missing key renders the LITERAL `{{key}}` unchanged — that once silently shipped a broken
    report/spec at exit 0 (the worst fault class), so the caller surfaces the unfilled set to
    make it observable. Values are stringified; the template is trusted config, not payload."""
    unfilled: List[str] = []

    def sub(m: "re.Match") -> str:
        k = m.group(1)
        if k not in payload:
            if k not in unfilled:
                unfilled.append(k)
            return m.group(0)
        v = payload[k]
        return v if isinstance(v, str) else str(v)

    return PLACEHOLDER.sub(sub, template), unfilled
