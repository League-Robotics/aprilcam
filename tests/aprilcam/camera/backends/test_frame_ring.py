"""Tests for the shared-memory frame ring (sprint 038 ticket 003) --
publish/read-latest round trips, ring wrap-around, the torn-frame
seqlock guarantee under real concurrent writes, cross-process usage,
and shared-memory cleanup. No real camera or ``cv2.VideoCapture``
anywhere in this file -- every frame is synthetic, numpy-only.
"""

from __future__ import annotations

import multiprocessing
import threading
import time
from multiprocessing import shared_memory

import numpy as np
import pytest

from aprilcam.camera.backends._frame_ring import (
    DEFAULT_RING_SLOTS,
    FrameRing,
    FrameRingError,
)


# ---------------------------------------------------------------------------
# publish / read_latest round trip, varying shapes and dtypes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "shape,dtype",
    [
        ((240, 320, 3), np.uint8),
        ((480, 640), np.uint8),  # grayscale, no channel axis
        ((1080, 1920, 3), np.uint8),  # a real 1080p BGR frame's shape
        ((100, 100), np.float32),
    ],
)
def test_publish_and_read_latest_round_trip(shape, dtype):
    writer = FrameRing.create(shape, dtype)
    reader = FrameRing.attach(writer.name, shape, dtype)
    try:
        assert reader.read_latest() is None  # nothing published yet

        frame1 = np.full(shape, 7, dtype=dtype)
        seq1 = writer.publish(frame1, timestamp=100.0)
        record1 = reader.read_latest()
        assert record1 is not None
        assert record1.sequence == seq1
        assert record1.timestamp == 100.0
        np.testing.assert_array_equal(record1.frame, frame1)

        # sequence hasn't advanced -- reader must see "no new frame",
        # never a stale copy of frame1 again.
        assert reader.read_latest() is None

        frame2 = np.full(shape, 9, dtype=dtype)
        seq2 = writer.publish(frame2, timestamp=200.0)
        assert seq2 > seq1
        record2 = reader.read_latest()
        assert record2 is not None
        assert record2.sequence == seq2
        np.testing.assert_array_equal(record2.frame, frame2)
    finally:
        writer.close()
        reader.close()


def test_sequence_is_monotonically_increasing():
    shape, dtype = (8, 8), np.uint8
    writer = FrameRing.create(shape, dtype)
    try:
        sequences = [writer.publish(np.full(shape, i, dtype=dtype)) for i in range(10)]
        assert sequences == sorted(sequences)
        assert len(set(sequences)) == len(sequences)
        # every publish bumps the seqlock odd-then-even, so consecutive
        # published sequences differ by exactly 2.
        assert all(b - a == 2 for a, b in zip(sequences, sequences[1:]))
    finally:
        writer.close()


def test_publish_rejects_shape_mismatch():
    writer = FrameRing.create((4, 4, 3), np.uint8)
    try:
        with pytest.raises(FrameRingError):
            writer.publish(np.zeros((4, 4), dtype=np.uint8))
    finally:
        writer.close()


def test_publish_rejects_dtype_mismatch():
    writer = FrameRing.create((4, 4), np.uint8)
    try:
        with pytest.raises(FrameRingError):
            writer.publish(np.zeros((4, 4), dtype=np.float32))
    finally:
        writer.close()


def test_attach_rejects_shape_mismatch():
    writer = FrameRing.create((4, 4, 3), np.uint8)
    try:
        with pytest.raises(FrameRingError):
            FrameRing.attach(writer.name, (4, 4), np.uint8)
    finally:
        writer.close()


def test_attach_rejects_dtype_mismatch():
    writer = FrameRing.create((4, 4), np.uint8)
    try:
        with pytest.raises(FrameRingError):
            FrameRing.attach(writer.name, (4, 4), np.float32)
    finally:
        writer.close()


def test_attach_rejects_num_slots_mismatch():
    writer = FrameRing.create((4, 4), np.uint8, num_slots=3)
    try:
        with pytest.raises(FrameRingError):
            FrameRing.attach(writer.name, (4, 4), np.uint8, num_slots=5)
    finally:
        writer.close()


def test_attach_rejects_non_frame_ring_segment():
    plain = shared_memory.SharedMemory(create=True, size=256)
    try:
        with pytest.raises(FrameRingError):
            FrameRing.attach(plain.name, (4, 4), np.uint8)
    finally:
        plain.close()
        plain.unlink()


# ---------------------------------------------------------------------------
# ring wrap-around
# ---------------------------------------------------------------------------


def test_ring_wrap_around_reuses_slots_and_keeps_newest():
    shape, dtype = (4, 4), np.uint8
    writer = FrameRing.create(shape, dtype, num_slots=3)
    reader = FrameRing.attach(writer.name, shape, dtype, num_slots=3)
    try:
        published = []
        for i in range(7):  # more than num_slots -- forces wrap-around
            value = i + 1
            seq = writer.publish(np.full(shape, value, dtype=dtype))
            published.append((seq, value))

        record = reader.read_latest()
        assert record is not None
        last_seq, last_value = published[-1]
        assert record.sequence == last_seq
        assert np.all(record.frame == last_value)
    finally:
        writer.close()
        reader.close()


# ---------------------------------------------------------------------------
# torn-frame impossibility under real concurrent writes
# ---------------------------------------------------------------------------


def test_reader_never_sees_a_torn_frame_under_concurrent_writes():
    shape, dtype = (240, 320, 3), np.uint8
    writer = FrameRing.create(shape, dtype)
    reader = FrameRing.attach(writer.name, shape, dtype)
    stop = threading.Event()
    write_error: list = []

    def writer_loop() -> None:
        counter = 0
        try:
            while not stop.is_set():
                counter += 1
                frame = np.full(shape, counter % 256, dtype=dtype)
                writer.publish(frame)
        except Exception as exc:  # pragma: no cover -- surfaced via write_error
            write_error.append(exc)

    thread = threading.Thread(target=writer_loop, daemon=True)
    thread.start()
    try:
        torn_frames = []
        seen_sequences = []
        deadline = time.time() + 3.0
        while time.time() < deadline and len(seen_sequences) < 300:
            record = reader.read_latest()
            if record is None:
                continue
            distinct_values = np.unique(record.frame)
            if distinct_values.size != 1:
                torn_frames.append((record.sequence, distinct_values.tolist()))
            seen_sequences.append(record.sequence)
    finally:
        stop.set()
        thread.join(timeout=5.0)
        writer.close()
        reader.close()

    assert not write_error
    assert not torn_frames, f"torn frame(s) observed: {torn_frames}"
    assert len(seen_sequences) > 0
    assert seen_sequences == sorted(seen_sequences)


# ---------------------------------------------------------------------------
# cross-process usage
# ---------------------------------------------------------------------------


def _cross_process_writer(name: str, shape: tuple, dtype_str: str, count: int) -> None:
    """Top-level (picklable) child-process entry point: attaches to an
    already-created ring by name and publishes *count* distinct,
    internally-uniform frames, pacing itself so the parent has time to
    observe more than just the very last one."""
    writer = FrameRing.attach(name, shape, dtype_str)
    try:
        for i in range(count):
            frame = np.full(shape, (i + 1) % 256, dtype=dtype_str)
            writer.publish(frame)
            time.sleep(0.02)
    finally:
        writer.close()


def test_cross_process_writer_and_reader():
    shape = (1080, 1920, 3)  # real 1080p BGR shape
    dtype = np.uint8
    reader = FrameRing.create(shape, dtype)
    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(target=_cross_process_writer, args=(reader.name, shape, "uint8", 5))
    proc.start()
    try:
        collected = []
        deadline = time.time() + 15.0
        while time.time() < deadline and len(collected) < 5:
            record = reader.read_latest()
            if record is not None:
                collected.append(record)
            else:
                time.sleep(0.01)
        proc.join(timeout=10.0)
        assert proc.exitcode == 0
    finally:
        if proc.is_alive():  # pragma: no cover -- safety net only
            proc.terminate()
            proc.join()
        reader.close()

    assert len(collected) >= 1
    sequences = [r.sequence for r in collected]
    assert sequences == sorted(sequences)
    assert len(set(sequences)) == len(sequences)  # each new frame seen exactly once
    for record in collected:
        assert np.unique(record.frame).size == 1  # never a torn frame


# ---------------------------------------------------------------------------
# shared-memory cleanup / no leak
# ---------------------------------------------------------------------------


def test_close_unlinks_owned_segment():
    writer = FrameRing.create((4, 4), np.uint8)
    name = writer.name
    writer.close()
    with pytest.raises(FileNotFoundError):
        shared_memory.SharedMemory(name=name, create=False)


def test_close_on_attached_ring_does_not_unlink():
    writer = FrameRing.create((4, 4), np.uint8)
    name = writer.name
    reader = FrameRing.attach(name, (4, 4), np.uint8)
    reader.close()  # non-owner: must not unlink

    # segment must still be attachable after the non-owner closed
    reader_again = FrameRing.attach(name, (4, 4), np.uint8)
    reader_again.close()

    writer.close()  # owner: unlinks
    with pytest.raises(FileNotFoundError):
        shared_memory.SharedMemory(name=name, create=False)


def test_close_is_idempotent():
    writer = FrameRing.create((4, 4), np.uint8)
    writer.close()
    writer.close()  # must not raise


def test_context_manager_closes_and_unlinks():
    name = None
    with FrameRing.create((4, 4), np.uint8) as writer:
        name = writer.name
        writer.publish(np.zeros((4, 4), dtype=np.uint8))
    with pytest.raises(FileNotFoundError):
        shared_memory.SharedMemory(name=name, create=False)


# ---------------------------------------------------------------------------
# peek() -- cheap heartbeat-style check, no frame copy
# ---------------------------------------------------------------------------


def test_peek_reports_sequence_and_timestamp_without_reading_frame():
    writer = FrameRing.create((4, 4), np.uint8)
    try:
        seq0, ts0 = writer.peek()
        assert seq0 == 0  # no frame published yet
        assert ts0 == 0.0

        writer.publish(np.full((4, 4), 3, dtype=np.uint8), timestamp=42.5)
        seq1, ts1 = writer.peek()
        assert seq1 == 2
        assert ts1 == 42.5

        # peek() must not disturb read_latest()'s own "last seen" state
        record = writer.read_latest()
        assert record is not None
        assert record.sequence == seq1
    finally:
        writer.close()


def test_default_ring_slots_is_a_small_positive_number():
    assert DEFAULT_RING_SLOTS >= 2
