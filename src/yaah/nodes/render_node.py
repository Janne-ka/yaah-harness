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


def _fill(template: str, payload: dict) -> str:
    def sub(m: "re.Match") -> str:
        k = m.group(1)
        if k not in payload:
            return m.group(0)
        v = payload[k]
        return v if isinstance(v, str) else str(v)

    return _PLACEHOLDER.sub(sub, template)


class RenderNode:
    def __init__(self, *, template: Optional[str] = None, template_file: Optional[str] = None,
                 out_path: Optional[str] = None) -> None:
        if template is None and template_file is None:
            raise ValueError("RenderNode needs template= or template_file=")
        self._template = template
        self._template_file = template_file
        self._out_path = out_path
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
        output = _fill(tpl, input.payload)
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
        return input.reply_with(Kind.RESULT,
                                {**input.payload, "output": output, "path": self._out_path})

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
