"""yaah.comms — the Comms interface + in-process transport. One class per file;
re-exported so `from yaah.comms import Comms, Subscription, InProcessComms`
(and the top-level `from yaah import ...`) keep working.

Subscription is the PROTOCOL (the cross-transport `.cancel()` contract);
InMemorySubscription is the concrete dataclass returned by InProcessComms /
LocalBus. NatsComms wraps its native sub in an adapter that also satisfies the
Protocol — same harness teardown code works on every transport.
"""
from .comms import Comms, Handler, Subscription
from .in_process_comms import InProcessComms
from .subscription import InMemorySubscription

__all__ = ["Comms", "Handler", "Subscription", "InMemorySubscription", "InProcessComms"]
