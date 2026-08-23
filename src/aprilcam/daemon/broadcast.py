"""``Broadcaster`` -- a generic bounded-queue fan-out primitive.

Sprint 027 ticket 001. This is the mechanism that makes "two subscribers,
one computation" true by construction: every camera product this sprint
adds (deskewed image, tag stream) will have exactly one producer thread
publishing into exactly one ``Broadcaster``, so N subscribers reading the
*same* product never cause N computations -- there is only ever one thing
to compute per publish, and this module's only job is handing a copy of
it to every current subscriber's own queue.

**No camera, vision, or proto knowledge.** ``Broadcaster`` is generic
over the published item type (``bytes``, a dataclass instance, anything)
-- ``daemon/streaming.py`` (tickets 003/004) is what specializes this for
``TagFrame``/``ImageFrame``. Keeping this module free of that knowledge
is what makes it testable with synthetic data, no camera or detector
double required (sprint.md Design Rationale 1: production is split from
transport, and this primitive sits below both).

**Backpressure is silent drop, by construction.** Each subscriber gets
its own bounded ``queue.Queue``; :meth:`Broadcaster.publish` pushes with
``put_nowait`` and swallows ``queue.Full`` -- a slow subscriber loses the
newest item rather than blocking the publisher or any other subscriber
(``docs/design/design.md`` §5, ``docs/design/specification.md`` §4.1).
Adapted from -- not copied wholesale from -- v1's ``_BaseProducer``
(``archive/src/aprilcam/daemon/stream.py``): its per-connection
``queue.Queue(maxsize=_MAX_SENDER_QUEUE)`` / ``put_nowait`` /
``queue.Full`` silent-drop pattern and its ``_MAX_SENDER_QUEUE = 2``
value are the earned starting point (:data:`DEFAULT_SUBSCRIBER_QUEUE_DEPTH`
below), stripped of everything socket-related -- v1 coupled fan-out to a
live ``socket.socket`` per subscriber; this class hands back and takes in
plain Python objects only.

**Subscriber-count transitions are the idle-gating hook.** Ticket
003/004's producers do not exist as a thread with zero subscribers
(sprint.md Design Rationale 2: "the producer thread does not exist," not
"exists but throttles"). This class does not itself start or stop
anything -- it has no notion of a producer -- but it is *where* the 0->1
and 1->0 transitions are known first, so a caller passes ``on_active``/
``on_idle`` callbacks at construction time and gets told exactly when to
start/stop its own background work. :attr:`Broadcaster.subscriber_count`
is also available as a plain observable count for anything that only
needs to poll rather than react.
"""

from __future__ import annotations

import queue
import threading
from typing import Callable, Generic, List, Optional, TypeVar

T = TypeVar("T")

#: Per-subscriber queue depth used when a caller does not override it.
#: Carried forward from v1's ``_MAX_SENDER_QUEUE`` (``archive/src/
#: aprilcam/daemon/stream.py``) -- an earned constant, not a guess:
#: deep enough that a momentary stall does not drop a frame that would
#: otherwise have been delivered, shallow enough that a genuinely slow
#: subscriber falls behind and starts dropping almost immediately rather
#: than building an ever-growing backlog of stale frames.
DEFAULT_SUBSCRIBER_QUEUE_DEPTH = 2


class Broadcaster(Generic[T]):
    """Fan out published items to every current subscriber, independently.

    Usage::

        broadcaster: Broadcaster[bytes] = Broadcaster(
            on_active=start_producer, on_idle=stop_producer
        )
        q = broadcaster.subscribe()
        ...
        broadcaster.publish(some_item)
        ...
        item = q.get(timeout=1.0)
        ...
        broadcaster.unsubscribe(q)

    Not a ``typing.Protocol`` and has none -- a plain, concrete class, per
    ``daemon/interfaces.py``'s module docstring design rule (this class
    has no need for an interface/implementation split at all: there is
    exactly one implementation of "fan out to bounded queues").
    """

    def __init__(
        self,
        *,
        queue_depth: int = DEFAULT_SUBSCRIBER_QUEUE_DEPTH,
        on_active: Optional[Callable[[], None]] = None,
        on_idle: Optional[Callable[[], None]] = None,
    ) -> None:
        """
        Args:
            queue_depth: Bounded size of each subscriber's own queue.
                Must be >= 1 -- a zero-depth queue could never deliver
                anything, which is never a caller's actual intent.
            on_active: Called with no arguments exactly when the
                subscriber count transitions 0 -> 1 (the first
                ``subscribe()`` after none existed). This is the
                idle-gating hook: a producer starts its background
                thread here, not on every ``subscribe()`` call.
            on_idle: Called with no arguments exactly when the
                subscriber count transitions 1 -> 0 (the last
                ``unsubscribe()``). A producer tears its background
                thread down here.
        """
        if queue_depth < 1:
            raise ValueError(f"queue_depth must be >= 1, got {queue_depth}")
        self._queue_depth = queue_depth
        self._on_active = on_active
        self._on_idle = on_idle
        self._lock = threading.Lock()
        self._subscribers: List["queue.Queue[T]"] = []

    @property
    def subscriber_count(self) -> int:
        """The current number of live subscribers -- an observable count,
        the non-callback half of the 0<->1 transition hook this class
        offers (see the class docstring)."""
        with self._lock:
            return len(self._subscribers)

    def subscribe(self) -> "queue.Queue[T]":
        """Register a new subscriber and return its own bounded queue.

        Every call returns a fresh queue -- there is no way to share one
        queue across two subscribers, which is what keeps "unsubscribe
        this one without affecting any other" true without any extra
        bookkeeping on the caller's part.
        """
        new_queue: "queue.Queue[T]" = queue.Queue(maxsize=self._queue_depth)
        became_active = False
        with self._lock:
            self._subscribers.append(new_queue)
            if len(self._subscribers) == 1:
                became_active = True
        # Fire outside the lock: on_active may itself call back into this
        # Broadcaster (e.g. a producer's first tick publishing
        # immediately), which would deadlock against self._lock if held.
        if became_active and self._on_active is not None:
            self._on_active()
        return new_queue

    def unsubscribe(self, subscriber_queue: "queue.Queue[T]") -> None:
        """Stop delivering to *subscriber_queue*. A no-op if it is not
        (or is no longer) a live subscriber -- unsubscribing twice, or a
        queue this ``Broadcaster`` never issued, never raises."""
        became_idle = False
        with self._lock:
            try:
                self._subscribers.remove(subscriber_queue)
            except ValueError:
                return
            if len(self._subscribers) == 0:
                became_idle = True
        if became_idle and self._on_idle is not None:
            self._on_idle()

    def publish(self, item: T) -> None:
        """Deliver *item* to every subscriber current at call time.

        A subscriber whose queue is full silently drops *item* --
        ``queue.Full`` is caught and swallowed, never raised to the
        caller, and never blocks delivery to any other subscriber. Taking
        a snapshot of the subscriber list before publishing (rather than
        holding the lock across every ``put_nowait``) keeps a slow
        ``put_nowait`` from blocking a concurrent ``subscribe()``/
        ``unsubscribe()`` -- ``put_nowait`` itself never blocks either
        way, but this keeps the lock's held duration independent of
        subscriber count.
        """
        with self._lock:
            subscribers = list(self._subscribers)
        for subscriber_queue in subscribers:
            try:
                subscriber_queue.put_nowait(item)
            except queue.Full:
                pass  # slow subscriber -- silent drop, by design
