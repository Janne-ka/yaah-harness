"""LangfusePromptSource — managed prompts from Langfuse (by name + label/version).

Used by: deployments using Langfuse for prompt management (the runtime's
`langfuse` source).
Where: hosts with `pip install langfuse` + credentials.
Why: versioned, labelled prompts managed outside the repo; langfuse is imported
lazily. Note: Langfuse templates use {{var}}; the Agent renders {{var}} too.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


class LangfusePromptSource:
    def __init__(self, *, label: Optional[str] = None, client: Any = None,
                 **client_opts: Any) -> None:
        # `client` is the external dependency, injected for testability: any object
        # exposing get_prompt(key, **kwargs). Defaults to a lazily-built real
        # Langfuse client (only when used). Tests pass a stub so this source runs
        # without the langfuse SDK / credentials.
        self._label = label
        self._client_opts = client_opts
        self._client: Any = client

    def _client_(self) -> Any:
        if self._client is None:  # pragma: no cover - real SDK shim (lazy, integration-only)
            from langfuse import Langfuse

            self._client = Langfuse(**self._client_opts)
        return self._client

    async def get(self, key: str, *, label: Optional[str] = None,
                  version: Optional[int] = None, **opts: Any) -> str:
        client = self._client_()
        kwargs: Dict[str, Any] = {}
        lbl = label or self._label
        if lbl is not None:
            kwargs["label"] = lbl
        if version is not None:
            kwargs["version"] = version
        prompt = client.get_prompt(key, **kwargs)
        return getattr(prompt, "prompt", None) or str(prompt)
