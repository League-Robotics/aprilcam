"""Unit tests for stream backpressure / drop logic.

CameraPipeline's subscriber queue fan-out has been replaced by
ImageStreamProducer / TagStreamProducer (see daemon.stream).  The
pure queue-drop tests remain as documentation of the drop policy used
inside those producers.
"""

from __future__ import annotations

import queue


# ---------------------------------------------------------------------------
# Queue drop logic (pure queue test)
# ---------------------------------------------------------------------------


def test_queue_drop_on_full():
    """A bounded queue silently drops items when full via put_nowait."""
    q: queue.Queue[int] = queue.Queue(maxsize=2)

    items = [1, 2, 3]
    for item in items:
        try:
            q.put_nowait(item)
        except queue.Full:
            pass  # silent drop — matches producer fan-out behaviour

    assert q.qsize() == 2
    assert q.get_nowait() == 1
    assert q.get_nowait() == 2
    assert q.empty()


def test_queue_drop_does_not_raise():
    """Silently swallowing queue.Full must not propagate any exception."""
    q: queue.Queue[bytes] = queue.Queue(maxsize=1)
    q.put_nowait(b"first")

    # Second put should be silently dropped, not raise
    try:
        q.put_nowait(b"second")
    except queue.Full:
        pass  # expected — swallow

    assert q.qsize() == 1
