"""NatsComms.connect must fail in BOUNDED time if the broker is unreachable.

Pre-fix, nats-py retried the initial connection with no upper bound, so a
down broker at startup hung the caller (~20s+) — and hung the whole test
suite via test_nats_integration. This pins the contract: connect() honors
a connect_timeout and raises promptly. Runs in the normal suite WITHOUT a
server (it connects to an unroutable address on purpose); self-skips only
if nats-py isn't installed.

Run: cd yaah && PYTHONPATH=src python3 tests/test_nats_connect_timeout.py
"""
from __future__ import annotations

import asyncio
import time

try:
    import nats  # noqa: F401
except Exception:
    print("skip: nats-py not installed")
    raise SystemExit(0)

from yaah.adapters.transports import NatsComms


async def _scenario() -> None:
    # 192.0.2.1 is TEST-NET-1 (RFC 5737) — guaranteed unroutable, so the
    # connection hangs until the connect_timeout fires (vs a refused-fast
    # localhost port, which wouldn't exercise the timeout path).
    be = NatsComms("nats://192.0.2.1:4222", connect_timeout=0.5)
    t0 = time.monotonic()
    raised = False
    try:
        await be.connect()
    except Exception:
        raised = True
    elapsed = time.monotonic() - t0
    assert raised, "connect() to an unreachable broker must raise, not hang"
    # bounded: a 0.5s connect_timeout should surface well under 3s, not ~20s
    assert elapsed < 3.0, "connect() took {:.1f}s — connect_timeout not honored".format(elapsed)


def main() -> None:
    # Outer guard: if connect() is NOT bounded (the bug), this fails at 5s
    # instead of hanging the suite forever.
    asyncio.run(asyncio.wait_for(_scenario(), timeout=5.0))
    print("ok")


if __name__ == "__main__":
    main()
