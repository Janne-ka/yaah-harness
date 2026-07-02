# 0007 — Declared ports: every shipped impl names its port; @abstractmethod enforces it

**Status:** Accepted (implemented 2026-07-02)
**Date:** 2026-07-02

## Context

Every seam in yaah is a `Protocol` port (Node, ApiProvider, StoreBackend tiers, Comms,
Tracer, PromptSource, …), but until this decision **no shipped impl declared its port**
— conformance was purely structural (duck-typed). The maintainer's verdict: *"Inheritance
must be clear, because the contracts need to be clear. If there are no visible contracts,
then extensibility is limited."* A reader opening `MemoryBackend` could not see which
port it implements without diffing method names against a Protocol in another file, and
nothing failed when an impl drifted from its port until some call site broke at runtime.

## Decision

1. **Ports stay `@runtime_checkable Protocol`s and gain `@abstractmethod` on every
   method.** A class that DECLARES a port (`class MemoryBackend(StoreBackend, Scannable,
   CompareAndSet)`) cannot instantiate with a method missing (runtime `TypeError` naming
   the gap), and mypy checks its signatures against the port.
2. **Every shipped impl declares its port in the class header.** Routers declare both
   the router base and the port (`class RoutingProvider(PrefixRouter[Any], ApiProvider,
   SupportsTurn)`). Optional capabilities are their own small port (`SupportsTurn`) so
   absence is meaningful (claude_cli deliberately does not declare/implement it).
3. **Structural conformance still works for non-declaring impls** — external extenders
   and test doubles are not forced to import yaah. Declaring is the *shipped-code*
   convention (AGENTS.md "Declare your port"), not a runtime requirement.
4. **The convention is frozen by `tests/test_ports.py`** — one table (port → impls),
   checked via `Port in cls.__mro__`. NOT isinstance: for a runtime-checkable Protocol
   isinstance is structural, so it passes for anything of the right shape and proves
   nothing about the declaration.
5. **Enforcement is a mypy RATCHET, wired into `scripts/run_tests.py`**: the error count
   must not exceed `scripts/mypy_baseline.txt`. Lower the baseline as the legacy
   `Any`-seam tail is paid down; never raise it. (A zero-error gate on day one was
   impossible; an unwired gate was ceremony — the ratchet is real today.)

## Caveats (know these before extending)

- **Protocol attributes are NOT enforced at instantiation.** `Tracer.captures` /
  `is_carriage` / `TraceContributor.name` are checked by mypy only; a declaring subclass
  missing them instantiates fine and `isinstance` still returns True (nominal inheritance
  short-circuits the structural check). Forgetting one fails at first attribute read.
- **The tier a facade needs is validated at construction** (`StoreBackedFacade.REQUIRES`
  + isinstance) — the one place declaration alone wasn't enough, because the facade's
  requirement is about the *backend it wraps*, not itself.

## Portability

Nothing here locks the engine into Python: `Protocol + @abstractmethod + __mro__ test`
is the Python rendering of "interface + nominal declaration + a conformance check" —
Rust traits, Go interfaces with static assertions, and TS `implements` are direct
equivalents. Orthogonal to ADR-0006: 0006 governs *data-flow key* contracts (as data);
this governs *call-signature* contracts (as types). Do not unify them.

## Consequences

- A new impl = declare the port + add one row in `tests/test_ports.py`.
- mypy now sees through the previously-`Any` consumer seams (wrapper `inner: Node`,
  `Agent.backend: ApiProvider`), so an engine change that would call an extender wrongly
  is caught by the ratchet, not by the extender's users.
- Registration of new *types* (config `type:` strings → factories) is a separate,
  still-closed seam — tracked as the plugins/registration decision, not covered here.
