"""ExpectField — structured counterpart to ShellCheck for payload fields.

Used by: the `expect_field` node type (`build/builders.py:_build_expect_field`).
Where: in a stage's `validators:` list when the upstream node already
produced a structured outcome whose key must equal a specific value, with
no need to re-run a command to verify.
Why: a RED gate needs to require the test run reported failure (`ok ==
False`) without re-running the suite; an integration check needs to
require an external job returned the expected status. Asserts a field the
previous node produced — cheaper than running a check command.

Targets Python 3.9+.
"""
from __future__ import annotations

from ..core import Node, Envelope, Failure, NodeConfig, Verdict


class ExpectField(Node):
    """Passes if the prior node's output payload[key] equals an expected value."""

    def __init__(self, key: str, equals: object) -> None:
        self._key = key
        self._equals = equals

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        actual = input.payload.get(self._key)
        if actual == self._equals:
            return Verdict.passed().to_envelope(input)
        return Verdict.failed(Failure(
            "field_mismatch",
            "{} is {!r}, expected {!r}".format(self._key, actual, self._equals),
            "produce {}={!r}".format(self._key, self._equals),
        )).to_envelope(input)
