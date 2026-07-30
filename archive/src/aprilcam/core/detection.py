"""Immutable record types for tag detection results.

TagRecord and FrameRecord are frozen dataclasses used by the detection
loop and ring buffer to store per-frame tag observations.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional, List

if TYPE_CHECKING:
    from aprilcam.core.aprilcam import AprilCam

from aprilcam.core.models import AprilTag


@dataclass(frozen=True)
class TagRecord:
    """Immutable snapshot of a single detected tag in one frame."""

    id: int
    center_px: tuple[float, float]
    corners_px: list[list[float]]  # 4x2 as plain lists
    orientation_yaw: float
    world_xy: tuple[float, float] | None
    in_playfield: bool
    vel_px: tuple[float, float] | None
    speed_px: float | None
    vel_world: tuple[float, float] | None
    speed_world: float | None
    heading_rad: float | None
    timestamp: float
    frame_index: int
    age: float = 0.0  # seconds since last detected (0 = seen this frame)

    def to_dict(self) -> dict:
        """Return a plain dict with all JSON-serializable values."""
        return {
            "id": self.id,
            "center_px": list(self.center_px),
            "corners_px": [list(c) for c in self.corners_px],
            "orientation_yaw": self.orientation_yaw,
            "world_xy": list(self.world_xy) if self.world_xy is not None else None,
            "in_playfield": self.in_playfield,
            "vel_px": list(self.vel_px) if self.vel_px is not None else None,
            "speed_px": self.speed_px,
            "vel_world": list(self.vel_world) if self.vel_world is not None else None,
            "speed_world": self.speed_world,
            "heading_rad": self.heading_rad,
            "timestamp": self.timestamp,
            "frame_index": self.frame_index,
            "age": self.age,
        }

    def estimate(self, t: float | None = None) -> TagRecord:
        """Return a new TagRecord with position extrapolated to time *t*.

        Uses linear extrapolation from the stored velocity.  When *t* is
        ``None``, defaults to ``time.monotonic()``.

        Args:
            t: Target monotonic time.  Defaults to now.

        Returns:
            New ``TagRecord`` with ``center_px``, ``corners_px``, and
            ``world_xy`` shifted by velocity * dt.  ``timestamp`` is set
            to *t* and ``age`` is increased by dt.  All other fields are
            preserved unchanged.
        """
        if t is None:
            t = time.monotonic()
        dt = t - self.timestamp

        new_center = self.center_px
        new_corners = self.corners_px
        if self.vel_px is not None:
            dx = self.vel_px[0] * dt
            dy = self.vel_px[1] * dt
            new_center = (self.center_px[0] + dx, self.center_px[1] + dy)
            new_corners = [[c[0] + dx, c[1] + dy] for c in self.corners_px]

        new_world = self.world_xy
        if self.world_xy is not None and self.vel_world is not None:
            new_world = (
                self.world_xy[0] + self.vel_world[0] * dt,
                self.world_xy[1] + self.vel_world[1] * dt,
            )

        return TagRecord(
            id=self.id,
            center_px=new_center,
            corners_px=new_corners,
            orientation_yaw=self.orientation_yaw,
            world_xy=new_world,
            in_playfield=self.in_playfield,
            vel_px=self.vel_px,
            speed_px=self.speed_px,
            vel_world=self.vel_world,
            speed_world=self.speed_world,
            heading_rad=self.heading_rad,
            timestamp=t,
            frame_index=self.frame_index,
            age=self.age + dt,
        )

    @classmethod
    def from_apriltag(
        cls,
        tag: AprilTag,
        *,
        vel_px: tuple[float, float] | None = None,
        speed_px: float | None = None,
        vel_world: tuple[float, float] | None = None,
        speed_world: float | None = None,
        heading_rad: float | None = None,
        timestamp: float,
        frame_index: int,
        age: float = 0.0,
    ) -> TagRecord:
        """Create a TagRecord from an existing AprilTag model instance.

        Converts numpy arrays to plain Python lists so the record is
        fully serializable without numpy.
        """
        corners_as_lists = [
            [float(x) for x in row] for row in tag.corners_px.tolist()
        ]
        return cls(
            id=tag.id,
            center_px=tag.center_px,
            corners_px=corners_as_lists,
            orientation_yaw=tag.orientation_yaw,
            world_xy=tag.world_xy,
            in_playfield=tag.in_playfield,
            vel_px=vel_px,
            speed_px=speed_px,
            vel_world=vel_world,
            speed_world=speed_world,
            heading_rad=heading_rad,
            timestamp=timestamp,
            frame_index=frame_index,
            age=age,
        )


@dataclass(frozen=True)
class FrameRecord:
    """Immutable snapshot of all tag detections for a single frame."""

    timestamp: float
    frame_index: int
    tags: list[TagRecord]

    def to_dict(self) -> dict:
        """Return a plain dict with tags as list of tag dicts."""
        return {
            "timestamp": self.timestamp,
            "frame_index": self.frame_index,
            "tags": [t.to_dict() for t in self.tags],
        }


class RingBuffer:
    """Thread-safe fixed-size buffer of FrameRecords.

    Uses a collections.deque with a maximum length so that old frames
    are automatically discarded when the buffer is full.  All public
    methods are guarded by a threading.Lock for safe concurrent access.
    """

    def __init__(self, maxlen: int = 300) -> None:
        self._buf: deque[FrameRecord] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def append(self, record: FrameRecord) -> None:
        with self._lock:
            self._buf.append(record)

    def get_latest(self) -> FrameRecord | None:
        with self._lock:
            return self._buf[-1] if self._buf else None

    def get_last_n(self, n: int) -> list[FrameRecord]:
        with self._lock:
            if n <= 0:
                return []
            items = list(self._buf)
            return items[-n:]

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._buf)


class DetectionLoop:
    """Runs tag detection in a background thread, writing results to a RingBuffer."""

    def __init__(
        self,
        source: Any,
        aprilcam: Any,
        ring_buffer: RingBuffer,
        coord_transform: Any = None,
    ) -> None:
        self._source = source
        self._cam = aprilcam
        self._buf = ring_buffer
        self._coord_transform = coord_transform
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._frame_count = 0
        self._error: Exception | None = None
        self._max_consecutive_failures = 10
        self._last_frame: Any = None  # latest raw BGR frame (numpy array)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("DetectionLoop is already running")
        self._stop_event.clear()
        self._frame_count = 0
        self._error = None
        self._cam.reset_state()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    @property
    def is_running(self) -> bool:
        return (self._thread is not None and self._thread.is_alive()
                and not self._stop_event.is_set())

    @property
    def frame_count(self) -> int:
        return self._frame_count

    @property
    def error(self) -> Exception | None:
        return self._error

    @property
    def last_frame(self) -> Any:
        """The most recently captured raw BGR frame, or ``None``."""
        return self._last_frame

    def _run(self) -> None:
        consecutive_failures = 0
        while not self._stop_event.is_set():
            try:
                ret, frame = self._source.read()
                if not ret:
                    consecutive_failures += 1
                    if consecutive_failures >= self._max_consecutive_failures:
                        break
                    continue
                ts = time.monotonic()
                self._last_frame = frame
                tag_records = self._cam.process_frame(frame, ts)
                if self._coord_transform is not None:
                    tag_records = self._coord_transform(tag_records)
                frame_record = FrameRecord(
                    timestamp=ts,
                    frame_index=self._frame_count,
                    tags=tag_records,
                )
                self._buf.append(frame_record)
                self._frame_count += 1
                consecutive_failures = 0
            except Exception as exc:
                self._error = exc
                consecutive_failures += 1
                if consecutive_failures >= self._max_consecutive_failures:
                    break
