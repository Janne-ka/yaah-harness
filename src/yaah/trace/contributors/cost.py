"""CostContributor — the token/model capture (the cost dimension).

Used by: a Tracer's contributor set when `cost` is enabled (`capture: [phase,
cost]`); pairs with the R4 on_usage bridge that fills a model_call span's tokens.
Where: the engine's bundled contributors (pure projection).
Why: the raw material for the continuous-improvement payoff — tokens in/out and
the model, per call. Orthogonal to phase: enable cost without tools, or tools
without cost. Dollar pricing is NOT here — that's a config price-map applied by
the aggregator (R8), so history can be re-priced without touching the engine.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, Dict

from ..span import Span


class CostContributor:
    name = "cost"

    def contribute(self, span: Span) -> Dict[str, Any]:
        # only model calls carry token cost; keep other records lean
        if span.name != "model_call":
            return {}
        return {"tokens_in": span.tokens_in, "tokens_out": span.tokens_out,
                "model": span.model}
