"""LiteLLMBackend against the REAL litellm SDK — offline, via mock_response.

What it proves: the backend's response extraction works on a genuine litellm
`ModelResponse` (whose `choices` items are objects, not dicts), not just on the
plain-dict stubs in test_adapters_external.py. Regression guard for the
silent-empty-content bug where `complete()` returned "" through the real SDK
because `isinstance(choice, dict)` was False.

Uses litellm's `mock_response` — no network, no API key, deterministic. Self-skips
when litellm isn't installed (the default zero-dep dev env), like the NATS tests.

Run: cd yaah && PYTHONPATH=src python3 tests/test_litellm_integration.py

Targets Python 3.9+.
"""
from __future__ import annotations

import asyncio
import sys

from yaah.agents import api_provider as _ap


def main() -> None:
    try:
        import litellm  # noqa: F401
    except ImportError:
        print("skip: litellm not installed")
        return
    asyncio.run(_run())
    print("PASS")


async def _run() -> None:
    from yaah.adapters.backends import LiteLLMBackend

    usage = {}
    be = LiteLLMBackend()  # no stub -> real litellm.acompletion (lazy import)

    out = await _ap.complete(be, 
        "ping", model="gpt-4o-mini",
        mock_response="pong",
        on_usage=lambda u: usage.update(u),
    )
    assert out == "pong", "complete() lost content through the real ModelResponse: {!r}".format(out)
    assert usage.get("tokens_out", 0) > 0, "usage not read from the real response: {}".format(usage)
    assert usage.get("model"), "model not reported: {}".format(usage)

    turn = await be.turn(
        [{"role": "user", "content": "hi"}], [],
        model="gpt-4o-mini", mock_response="final",
    )
    assert turn == {"text": "final"}, "turn() lost text through the real ModelResponse: {!r}".format(turn)


if __name__ == "__main__":
    main()
