"""RenderNode — a worker that fills a {{mustache}} template with the payload.

Used by: yaah.build (the 'render' node type) for report/HTML stages.
Where: stages that turn artifacts into a rendered document.
Why: simple, dependency-free templating (template from trusted config, not the
payload); optionally write the output to a file.

Targets Python 3.9+.
"""
from __future__ import annotations

import os
import re
from typing import Optional, Tuple

from ..core import Envelope, Failure, Kind, NodeConfig, Verdict

_PLACEHOLDER = re.compile(r"{{\s*(\w+)\s*}}")


def _fill(template: str, payload: dict) -> Tuple[str, list]:
    """Fill {{mustache}} placeholders; also return the names with NO payload value.
    A missing key renders the LITERAL `{{key}}` (unchanged behaviour) — but that
    silently shipped a broken report/spec at exit 0, the worst fault class. The
    caller surfaces the unfilled set so it's observable instead of silent."""
    unfilled: list = []

    def sub(m: "re.Match") -> str:
        k = m.group(1)
        if k not in payload:
            if k not in unfilled:
                unfilled.append(k)
            return m.group(0)
        v = payload[k]
        return v if isinstance(v, str) else str(v)

    return _PLACEHOLDER.sub(sub, template), unfilled


class RenderNode:
    def __init__(self, *, template: Optional[str] = None, template_file: Optional[str] = None,
                 out_path: Optional[str] = None, allow_unfilled: bool = False) -> None:
        if template is None and template_file is None:
            raise ValueError("RenderNode needs template= or template_file=")
        self._template = template
        self._template_file = template_file
        self._out_path = out_path
        # By default an unfilled {{placeholder}} FAILS the stage — the data-flow
        # footgun (a forgotten parse step shipping a literal `{{name}}` at exit 0)
        # is the project's worst fault class, so it must be loud, not observable.
        # Set allow_unfilled=true for a template with intentionally-optional fields.
        self._allow_unfilled = allow_unfilled
        self._cache: Optional[Tuple[float, str]] = None  # (mtime, content), mtime-aware (#5)

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        tpl = self._template
        if tpl is None:
            try:
                tpl = self._read_template()
            except OSError as e:
                # Assessment cluster 4 #4: previously a missing/unreadable template
                # raised raw OSError straight up through the stage; ShellNode and
                # WorktreeNode use Verdict.failed for I/O errors. Align here so the
                # harness sees a structured failure, not an opaque exception.
                return Verdict.failed(Failure(
                    "render_template_unreadable",
                    "{}: {}".format(type(e).__name__, e),
                    "check template_file path and permissions"
                )).to_envelope(input)
        output, unfilled = _fill(tpl, input.payload)
        if unfilled and not self._allow_unfilled:
            # The footgun, closed: a placeholder with no payload value almost always
            # means a missing parse step (agent output is a string in `raw` until a
            # parse transform unpacks it). Fail loudly instead of shipping a literal
            # `{{name}}` at exit 0. Opt out with allow_unfilled for optional fields.
            return Verdict.failed(Failure(
                "render_unfilled_placeholders",
                "no payload value for: {}".format(", ".join(unfilled)),
                "add a parse step before this render, or set allow_unfilled:true "
                "if these fields are intentionally optional"
            )).to_envelope(input)
        if self._out_path:
            try:
                os.makedirs(os.path.dirname(os.path.abspath(self._out_path)), exist_ok=True)
                with open(self._out_path, "w", encoding="utf-8") as f:
                    f.write(output)
            except OSError as e:
                return Verdict.failed(Failure(
                    "render_write_failed",
                    "{}: {}".format(type(e).__name__, e),
                    "check out_path and write permissions"
                )).to_envelope(input)
        # ENRICH (don't replace): keep the payload so a MID-pipeline render (e.g. an
        # HTML spec before the approve gate, or a code-review render before the report)
        # doesn't drop downstream state; just add `output`/`path`. Terminal renders are
        # unaffected (the extra keys are harmless). reply_with = reserved-key safe.
        fields = {**input.payload, "output": output, "path": self._out_path}
        if unfilled:
            # a placeholder with no payload value rendered as a literal `{{name}}` —
            # degrade (the doc still renders) but SURFACE it so a soft gate / the
            # report can flag the silent-wrong render instead of shipping it blind.
            fields["unfilled"] = unfilled
        return input.reply_with(Kind.RESULT, fields)

    def _read_template(self) -> str:
        # mtime-aware cache: re-read only when the template file changes (#5),
        # so repeated renders don't re-read, but edits are still picked up.
        try:
            mtime = os.path.getmtime(self._template_file)
        except OSError:
            mtime = None
        if self._cache is not None and mtime is not None and self._cache[0] == mtime:
            return self._cache[1]
        with open(self._template_file, "r", encoding="utf-8") as f:
            content = f.read()
        if mtime is not None:
            self._cache = (mtime, content)
        return content
