"""Langfuse adapters against the REAL langfuse SDK surface.

What it proves: the call the adapters actually make is accepted by the installed
client — for LangfuseTraceSink, either the v4 OTEL surface
(`start_observation(as_type=..., trace_context=..., model=..., usage_details=...)`)
or the legacy v2 surface (`.trace`/`.generation`/`.span`); for
LangfusePromptSource, `.get_prompt`. The Langfuse client API churned across
majors (v2 manual -> v4 OTEL); this fails loudly if the installed SDK exposes
NEITHER surface, or if v4 dropped a keyword the sink passes — instead of
AttributeError/TypeError-ing at runtime on the first span. Self-skips when
langfuse isn't installed (the default dev env).

It asserts the SURFACE only (no live client is constructed) — emitting would spin
up the SDK's background OTLP exporter. The mapping logic is covered offline by
scenario_langfuse_v4_mapping / scenario_langfuse_sink_mapping in test_trace.py.

Run: cd yaah && PYTHONPATH=src python3 tests/test_langfuse_integration.py

Targets Python 3.9+.
"""
from __future__ import annotations

import inspect


def main() -> None:
    try:
        from langfuse import Langfuse
    except ImportError:
        print("skip: langfuse not installed")
        return

    if hasattr(Langfuse, "start_observation"):                 # v4 (OpenTelemetry)
        params = set(inspect.signature(Langfuse.start_observation).parameters)
        need = {"name", "as_type", "trace_context", "metadata", "model", "usage_details"}
        missing = need - params
        assert not missing, (
            "LangfuseTraceSink's v4 path passes {} to start_observation but this "
            "SDK rejects them — the OTEL surface changed again.".format(sorted(missing)))
    elif all(hasattr(Langfuse, m) for m in ("trace", "generation", "span")):  # v2
        pass
    else:
        raise AssertionError(
            "installed langfuse exposes neither the v4 (.start_observation) nor the "
            "v2 (.trace/.generation/.span) surface — LangfuseTraceSink supports neither")

    assert hasattr(Langfuse, "get_prompt"), "LangfusePromptSource needs Langfuse.get_prompt"
    print("PASS")


if __name__ == "__main__":
    main()
