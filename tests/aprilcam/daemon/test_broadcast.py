"""Tests for ``aprilcam.daemon.broadcast`` (sprint 027 ticket 001).

Pure in-process: no camera, no vision, no socket -- ``Broadcaster`` is
generic over the published item type, so every test here publishes plain
``str``/``int`` items and asserts on queue contents directly. This is
also where the 0<->1 subscriber-count transition hook is proven, ahead
of any real producer (tickets 003/004) depending on it.
"""

from __future__ import annotations

import queue
from typing import List

import pytest

from aprilcam.daemon.broadcast import DEFAULT_SUBSCRIBER_QUEUE_DEPTH, Broadcaster


def test_default_queue_depth_is_two() -> None:
    assert DEFAULT_SUBSCRIBER_QUEUE_DEPTH == 2


def test_rejects_non_positive_queue_depth() -> None:
    with pytest.raises(ValueError):
        Broadcaster(queue_depth=0)


def test_single_subscriber_receives_publish() -> None:
    broadcaster: Broadcaster[str] = Broadcaster()
    q = broadcaster.subscribe()

    broadcaster.publish("frame-1")

    assert q.get_nowait() == "frame-1"
    assert q.empty()


def test_n_subscribers_each_receive_every_publish() -> None:
    """UC-07.S-1/SUC-004's headline property: N subscribers to the same
    producer's output each get an independent copy of everything
    published, not a shared/consumed-once item."""
    broadcaster: Broadcaster[str] = Broadcaster(queue_depth=10)
    queues = [broadcaster.subscribe() for _ in range(3)]

    broadcaster.publish("a")
    broadcaster.publish("b")

    for q in queues:
        assert list(_drain(q)) == ["a", "b"]


def test_full_subscriber_queue_silently_drops_newest_item() -> None:
    """A full queue never raises to the publisher and never blocks --
    the newest item is dropped, the queue keeps whatever it already
    held (design.md section 5's backpressure contract)."""
    broadcaster: Broadcaster[int] = Broadcaster(queue_depth=2)
    q = broadcaster.subscribe()

    broadcaster.publish(1)
    broadcaster.publish(2)
    broadcaster.publish(3)  # queue is full (maxsize=2) -- dropped silently

    assert list(_drain(q)) == [1, 2]


def test_slow_subscriber_drop_does_not_affect_fast_subscriber() -> None:
    """UC-07.S-2/UC-11's backpressure requirement: one subscriber never
    draining its queue must not affect another subscriber on the same
    producer."""
    broadcaster: Broadcaster[int] = Broadcaster(queue_depth=2)
    slow = broadcaster.subscribe()  # never drained in this test
    fast = broadcaster.subscribe()

    for i in range(5):
        broadcaster.publish(i)
        # fast subscriber drains after every publish -- never fills up.
        assert fast.get_nowait() == i

    # slow subscriber only ever holds its bounded depth's worth, and
    # holds the *oldest* undelivered items (3, 4 dropped silently),
    # never an exception raised out of publish().
    assert list(_drain(slow)) == [0, 1]


def test_unsubscribe_stops_delivery_without_affecting_others() -> None:
    broadcaster: Broadcaster[str] = Broadcaster(queue_depth=10)
    q1 = broadcaster.subscribe()
    q2 = broadcaster.subscribe()

    broadcaster.publish("before")
    broadcaster.unsubscribe(q1)
    broadcaster.publish("after")

    assert list(_drain(q1)) == ["before"]
    assert list(_drain(q2)) == ["before", "after"]


def test_unsubscribe_unknown_queue_is_a_safe_no_op() -> None:
    broadcaster: Broadcaster[str] = Broadcaster()
    stray: "queue.Queue[str]" = queue.Queue()

    broadcaster.unsubscribe(stray)  # never subscribed -- must not raise

    q = broadcaster.subscribe()
    broadcaster.unsubscribe(q)
    broadcaster.unsubscribe(q)  # double-unsubscribe -- also must not raise


def test_subscriber_count_is_observable() -> None:
    broadcaster: Broadcaster[str] = Broadcaster()
    assert broadcaster.subscriber_count == 0

    q1 = broadcaster.subscribe()
    assert broadcaster.subscriber_count == 1

    q2 = broadcaster.subscribe()
    assert broadcaster.subscriber_count == 2

    broadcaster.unsubscribe(q1)
    assert broadcaster.subscriber_count == 1

    broadcaster.unsubscribe(q2)
    assert broadcaster.subscriber_count == 0


def test_on_active_fires_exactly_on_zero_to_one_transition() -> None:
    """The idle-gating hook tickets 003/004 depend on: a producer starts
    its background thread on the *first* subscriber, not on every
    subscribe() call."""
    activations: List[None] = []
    broadcaster: Broadcaster[str] = Broadcaster(on_active=lambda: activations.append(None))

    q1 = broadcaster.subscribe()
    assert len(activations) == 1

    q2 = broadcaster.subscribe()  # second subscriber -- no new activation
    assert len(activations) == 1

    broadcaster.unsubscribe(q1)
    broadcaster.unsubscribe(q2)

    broadcaster.subscribe()  # a fresh 0 -> 1 transition after going idle
    assert len(activations) == 2


def test_on_idle_fires_exactly_on_one_to_zero_transition() -> None:
    idles: List[None] = []
    broadcaster: Broadcaster[str] = Broadcaster(on_idle=lambda: idles.append(None))

    q1 = broadcaster.subscribe()
    q2 = broadcaster.subscribe()

    broadcaster.unsubscribe(q1)  # still one subscriber left -- no idle yet
    assert len(idles) == 0

    broadcaster.unsubscribe(q2)  # last subscriber -- now idle
    assert len(idles) == 1

    broadcaster.unsubscribe(q2)  # already gone -- no second idle firing
    assert len(idles) == 1


def test_on_active_and_on_idle_together_across_multiple_cycles() -> None:
    """Full 0->1->0->1->0 lifecycle, the exact shape a producer's
    start/stop pair will see in tickets 003/004."""
    events: List[str] = []
    broadcaster: Broadcaster[str] = Broadcaster(
        on_active=lambda: events.append("active"),
        on_idle=lambda: events.append("idle"),
    )

    q = broadcaster.subscribe()
    broadcaster.unsubscribe(q)
    q = broadcaster.subscribe()
    broadcaster.unsubscribe(q)

    assert events == ["active", "idle", "active", "idle"]


def _drain(q: "queue.Queue[object]") -> List[object]:
    items: List[object] = []
    while True:
        try:
            items.append(q.get_nowait())
        except queue.Empty:
            break
    return items
