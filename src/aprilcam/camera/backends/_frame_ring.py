"""Shared-memory frame ring: the pixel-transport half of the per-camera
capture worker's IPC (sprint 038 ticket 003).

**Why shared memory, not a pipe.** A 1920x1080 BGR ``uint8`` frame is
~6 MB. At 15-30 Hz that is 90-180 MB/s of pixel data crossing a process
boundary. A pipe would either block the writer on a slow parent read or
force a full-frame copy through kernel pipe buffers on every tick; a
shared-memory ring lets the parent read the newest frame with at most
one copy, independent of how promptly it polls. See the sprint's
``design/camera-DESIGN.md`` overlay, "A pipe instead of shared memory
for frame transport" in its Rejected/changed alternatives.

**Ownership model.** Whichever side calls :meth:`FrameRing.create`
allocates and owns the ``multiprocessing.shared_memory.SharedMemory``
segment and is responsible for :meth:`FrameRing.close` unlinking it;
the other side calls :meth:`FrameRing.attach` to open the same segment
by name without owning it (its own ``close()`` detaches only). This
ticket does not commit to *which* process is the owner in the eventual
worker architecture (ticket 004's ``WorkerBackend``/``capture_worker.py``
decide that) -- the API supports either side creating and the other
attaching, and both `create`/`attach` read the shared geometry
(``shape``/``dtype``/``num_slots``) from their caller rather than
inferring it, so a mismatched attach is caught as a loud
:class:`FrameRingError`, never a silent misread.

**Torn-frame safety: a seqlock, not a mutex.** The header's ``sequence``
field is a classic seqlock counter -- even means "stable, data ready",
odd means "writer is mid-publish, do not trust this slot yet". The
writer bumps it odd *before* touching a slot and even again only after
the slot and metadata are fully written; the reader retries whenever it
observes an odd sequence, or when the sequence changed between its first
and second header read (the writer lapped the reader during its copy).
This gives a reader that never blocks a real-time writer, and a writer
that never blocks the reader nor needs a lock at all -- exactly the "one
writer, one reader" case this ring targets (a mutex would let a slow
reader stall frame capture, unacceptable for a capture worker).

**Ring depth.** :data:`DEFAULT_RING_SLOTS` is a handful of slots (3): the
seqlock already makes a torn read impossible regardless of depth --
depth exists only so the writer does not overwrite a slot the reader
is still copying out of *so often that read_latest() has to retry
often*. A reader's copy of a ~6 MB frame is a memcpy, several orders of
magnitude faster than one capture interval at 15-30 Hz, so 3 slots is
generous headroom, not a tuned minimum.
"""

from __future__ import annotations

import struct
import time
import uuid
from dataclasses import dataclass
from multiprocessing import shared_memory
from typing import NamedTuple, Optional, Tuple

import numpy as np
import numpy.typing as npt

__all__ = [
    "DEFAULT_RING_SLOTS",
    "FrameRecord",
    "FrameRing",
    "FrameRingError",
]

#: See module docstring, "Ring depth".
DEFAULT_RING_SLOTS = 3

_MAGIC = b"FRNG"
_HEADER_VERSION = 1
_DTYPE_STR_BYTES = 16

# Fixed layout, little-endian, no native padding (the leading "<"):
#   magic(4s) version(I) num_slots(I) write_index(I) sequence(Q)
#   timestamp(d) shape[3](iii) dtype(16s)
_HEADER_FORMAT = f"<4sIIIQd3i{_DTYPE_STR_BYTES}s"
_HEADER_SIZE = struct.calcsize(_HEADER_FORMAT)


class FrameRingError(RuntimeError):
    """A FrameRing usage or geometry error -- a caller bug (mismatched
    shape/dtype/num_slots, a frame that doesn't match the ring's
    declared geometry, attaching to a segment that isn't a FrameRing).

    Never raised for an ordinary "no fresh frame yet" condition -- that
    is a ``None`` return from :meth:`FrameRing.read_latest`, exactly
    like every other backend's ``read()`` never raising for an ordinary
    failure (``camera/backends/__init__.py``'s ``CaptureBackend.read``
    docstring).
    """


@dataclass(frozen=True, kw_only=True)
class FrameRecord:
    """One frame copied out of a :class:`FrameRing` by
    :meth:`FrameRing.read_latest` -- value-shaped, so a frozen
    dataclass declaring fields (project interface rule), not a
    behavioral class."""

    frame: np.ndarray
    sequence: int
    timestamp: float


class _HeaderView(NamedTuple):
    """Unpacked snapshot of the ring header at one point in time."""

    magic: bytes
    version: int
    num_slots: int
    write_index: int
    sequence: int
    timestamp: float
    shape: Tuple[int, int, int]
    dtype: np.dtype


def _dtype_to_bytes(dtype: np.dtype) -> bytes:
    raw = np.dtype(dtype).str.encode("ascii")
    if len(raw) > _DTYPE_STR_BYTES:
        raise FrameRingError(f"dtype descriptor {raw!r} does not fit in {_DTYPE_STR_BYTES} bytes")
    return raw.ljust(_DTYPE_STR_BYTES, b"\0")


def _bytes_to_dtype(raw: bytes) -> np.dtype:
    return np.dtype(raw.split(b"\0", 1)[0].decode("ascii"))


def _pad_shape(shape: Tuple[int, ...]) -> Tuple[int, int, int]:
    padded = tuple(shape) + (0,) * (3 - len(shape))
    return (padded[0], padded[1], padded[2])


class FrameRing:
    """One writer, one reader, shared-memory ring of raw frame buffers.

    Construct with :meth:`create` (allocates and owns the segment) or
    :meth:`attach` (opens an existing segment by name, does not own
    it). Both require the same *shape*/*dtype*/*num_slots* the creator
    used -- geometry is a construction-time contract between the two
    sides, not something this class infers, so a mismatch is a loud
    :class:`FrameRingError` at attach time rather than a silently wrong
    read later.

    *shape* is a camera frame's shape: ``(height, width)`` for
    grayscale or ``(height, width, channels)`` for color -- higher-rank
    arrays are out of scope for a camera frame ring.
    """

    def __init__(
        self,
        *,
        shm: shared_memory.SharedMemory,
        shape: Tuple[int, ...],
        dtype: np.dtype,
        num_slots: int,
        owns_segment: bool,
    ) -> None:
        if len(shape) not in (2, 3):
            raise FrameRingError(f"FrameRing shape must be 2D or 3D, got {shape!r}")
        self._shm = shm
        self._shape: Tuple[int, ...] = tuple(int(d) for d in shape)
        self._dtype = np.dtype(dtype)
        self._num_slots = int(num_slots)
        self._owns_segment = owns_segment
        self._slot_bytes = int(np.prod(self._shape)) * self._dtype.itemsize
        self._last_seen_sequence = 0
        self._closed = False
        if owns_segment:
            self._init_header()
        else:
            self._validate_layout()

    # -- construction ------------------------------------------------

    @classmethod
    def create(
        cls,
        shape: Tuple[int, ...],
        dtype: npt.DTypeLike,
        *,
        name: Optional[str] = None,
        num_slots: int = DEFAULT_RING_SLOTS,
    ) -> "FrameRing":
        """Allocate a new shared-memory segment sized for *shape*/*dtype*
        and *num_slots* slots, and take ownership of it (this side's
        ``close()`` unlinks by default)."""
        shape = tuple(int(d) for d in shape)
        np_dtype = np.dtype(dtype)
        slot_bytes = int(np.prod(shape)) * np_dtype.itemsize
        size = _HEADER_SIZE + num_slots * slot_bytes
        # Short prefix + a short hex suffix: macOS's POSIX shared-memory
        # names are capped at PSHMNAMLEN (31 bytes, including the
        # leading "/" the platform adds) -- "aprilcam-frame-ring-<32
        # hex chars>" blew past that with an OSError("File name too
        # long"). "afr-<12 hex chars>" (16 bytes) stays well under it
        # on every platform while remaining unique enough for a
        # per-process, per-camera ring name.
        seg_name = name or f"afr-{uuid.uuid4().hex[:12]}"
        shm = shared_memory.SharedMemory(name=seg_name, create=True, size=size)
        return cls(shm=shm, shape=shape, dtype=np_dtype, num_slots=num_slots, owns_segment=True)

    @classmethod
    def attach(
        cls,
        name: str,
        shape: Tuple[int, ...],
        dtype: npt.DTypeLike,
        *,
        num_slots: int = DEFAULT_RING_SLOTS,
    ) -> "FrameRing":
        """Open an existing segment created by :meth:`create` elsewhere.
        Does not own the segment -- this side's ``close()`` detaches
        only, never unlinks, unless overridden."""
        shape = tuple(int(d) for d in shape)
        np_dtype = np.dtype(dtype)
        shm = shared_memory.SharedMemory(name=name, create=False)
        return cls(shm=shm, shape=shape, dtype=np_dtype, num_slots=num_slots, owns_segment=False)

    # -- properties ----------------------------------------------------

    @property
    def name(self) -> str:
        return self._shm.name

    @property
    def shape(self) -> Tuple[int, ...]:
        return self._shape

    @property
    def dtype(self) -> np.dtype:
        return self._dtype

    @property
    def num_slots(self) -> int:
        return self._num_slots

    # -- writer side -----------------------------------------------------

    def publish(self, frame: np.ndarray, *, timestamp: Optional[float] = None) -> int:
        """Publish *frame* as the newest complete frame. Returns the new
        (even) sequence number.

        Raises :class:`FrameRingError` if *frame*'s shape/dtype does not
        match this ring's declared geometry -- a caller bug, not an
        ordinary runtime condition.

        Single writer only: concurrent ``publish()`` calls from more than
        one writer are not supported and will corrupt the ring (the
        seqlock protects one writer against any number of readers, not
        multiple writers against each other).
        """
        self._check_open()
        if frame.shape != self._shape or frame.dtype != self._dtype:
            raise FrameRingError(
                f"frame shape/dtype {frame.shape}/{frame.dtype} does not match ring "
                f"geometry {self._shape}/{self._dtype}"
            )
        header = self._read_header()
        next_index = (header.write_index + 1) % self._num_slots

        # Odd sequence: "write in progress" -- any reader that observes
        # this must retry rather than trust write_index/slot contents.
        in_progress_sequence = header.sequence + 1
        self._write_header(sequence=in_progress_sequence, write_index=header.write_index, timestamp=header.timestamp)

        self._write_slot(next_index, frame)

        # Even sequence: "stable" -- published last, after the slot and
        # metadata are fully written, per the class docstring's seqlock
        # discipline.
        ts = time.time() if timestamp is None else timestamp
        stable_sequence = in_progress_sequence + 1
        self._write_header(sequence=stable_sequence, write_index=next_index, timestamp=ts)
        return stable_sequence

    # -- reader side -----------------------------------------------------

    def read_latest(self, *, max_retries: int = 50) -> Optional[FrameRecord]:
        """The newest complete frame, copied out once, or ``None`` if
        nothing new has been published since the last call (or, in the
        vanishingly rare case *max_retries* is exhausted by a writer
        that keeps lapping this read -- never a crash, never a stale/
        torn copy, just "nothing to report this cycle")."""
        self._check_open()
        for _ in range(max_retries):
            header = self._read_header()
            if header.sequence % 2 == 1:
                continue  # writer is mid-publish
            if header.sequence == self._last_seen_sequence:
                return None  # no fresh frame since our last read
            frame = self._read_slot(header.write_index)
            confirm = self._read_header()
            if confirm.sequence != header.sequence:
                continue  # writer lapped us mid-copy -- torn, retry
            self._last_seen_sequence = header.sequence
            return FrameRecord(frame=frame, sequence=header.sequence, timestamp=header.timestamp)
        return None

    def peek(self) -> Tuple[int, float]:
        """Cheap ``(sequence, timestamp)`` snapshot with no frame copy --
        for a heartbeat/staleness check that doesn't want the cost of a
        full frame read. Does not affect :meth:`read_latest`'s "seen"
        state."""
        header = self._read_header()
        return header.sequence, header.timestamp

    # -- lifecycle -----------------------------------------------------

    def close(self, *, unlink: Optional[bool] = None) -> None:
        """Detach from the segment; unlink it too if this side owns it
        (or if *unlink* explicitly says so). Safe to call more than
        once."""
        if self._closed:
            return
        self._closed = True
        self._shm.close()
        should_unlink = self._owns_segment if unlink is None else unlink
        if should_unlink:
            try:
                self._shm.unlink()
            except FileNotFoundError:
                pass

    def __enter__(self) -> "FrameRing":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- internals -----------------------------------------------------

    def _check_open(self) -> None:
        if self._closed:
            raise FrameRingError("FrameRing is closed")

    @property
    def _buf(self) -> memoryview:
        # ``SharedMemory.buf`` is typed ``Optional`` because it becomes
        # ``None`` after ``close()`` -- ``_check_open()`` (called by
        # every public method that reaches here) already guarantees
        # that hasn't happened, so this is a type-narrowing assertion,
        # not a real runtime possibility.
        buf = self._shm.buf
        assert buf is not None, "FrameRing: shared memory buffer is unexpectedly closed"
        return buf

    def _slot_offset(self, index: int) -> int:
        return _HEADER_SIZE + index * self._slot_bytes

    def _write_slot(self, index: int, frame: np.ndarray) -> None:
        offset = self._slot_offset(index)
        view = np.ndarray(self._shape, dtype=self._dtype, buffer=self._buf, offset=offset)
        view[...] = frame

    def _read_slot(self, index: int) -> np.ndarray:
        offset = self._slot_offset(index)
        view = np.ndarray(self._shape, dtype=self._dtype, buffer=self._buf, offset=offset)
        return view.copy()

    def _write_header(self, *, sequence: int, write_index: int, timestamp: float) -> None:
        packed = struct.pack(
            _HEADER_FORMAT,
            _MAGIC,
            _HEADER_VERSION,
            self._num_slots,
            write_index,
            sequence,
            timestamp,
            *_pad_shape(self._shape),
            _dtype_to_bytes(self._dtype),
        )
        self._buf[:_HEADER_SIZE] = packed

    def _read_header(self) -> _HeaderView:
        magic, version, num_slots, write_index, sequence, timestamp, d0, d1, d2, dtype_raw = struct.unpack(
            _HEADER_FORMAT, bytes(self._buf[:_HEADER_SIZE])
        )
        return _HeaderView(
            magic=magic,
            version=version,
            num_slots=num_slots,
            write_index=write_index,
            sequence=sequence,
            timestamp=timestamp,
            shape=(d0, d1, d2),
            dtype=_bytes_to_dtype(dtype_raw),
        )

    def _init_header(self) -> None:
        # write_index starts one before slot 0 so the first publish()'s
        # (write_index + 1) % num_slots lands on slot 0; sequence starts
        # at 0 (even/stable, "no frame published yet").
        self._write_header(sequence=0, write_index=self._num_slots - 1, timestamp=0.0)

    def _validate_layout(self) -> None:
        # Check magic/version off the raw bytes first, before decoding
        # the dtype field -- a segment that isn't a FrameRing at all
        # (arbitrary/zeroed bytes) can carry a dtype string that
        # np.dtype() itself rejects (e.g. an empty string), which must
        # surface as "not a recognized FrameRing", not an unrelated
        # TypeError escaping from numpy.
        raw = bytes(self._buf[:_HEADER_SIZE])
        magic, version = struct.unpack_from("<4sI", raw)
        if magic != _MAGIC or version != _HEADER_VERSION:
            raise FrameRingError(
                f"shared memory segment {self._shm.name!r} is not a recognized FrameRing "
                f"(magic/version mismatch)"
            )
        header = self._read_header()
        if header.num_slots != self._num_slots:
            raise FrameRingError(
                f"FrameRing num_slots mismatch attaching to {self._shm.name!r}: "
                f"segment has {header.num_slots}, attach() asked for {self._num_slots}"
            )
        expected_shape = _pad_shape(self._shape)
        if header.shape != expected_shape:
            raise FrameRingError(
                f"FrameRing shape mismatch attaching to {self._shm.name!r}: "
                f"segment has {header.shape}, attach() asked for {expected_shape}"
            )
        if header.dtype != self._dtype:
            raise FrameRingError(
                f"FrameRing dtype mismatch attaching to {self._shm.name!r}: "
                f"segment has {header.dtype}, attach() asked for {self._dtype}"
            )
