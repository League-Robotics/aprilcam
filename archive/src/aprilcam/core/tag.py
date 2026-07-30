"""User-facing Tag handle with flat properties and live update."""

from __future__ import annotations

import math
import threading
import time
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .pipeline import DetectionPipeline

from .detection import TagRecord


class Tag:
    """Live tag handle obtained from a Playfield.

    Properties use flat names (``cx``, ``cy``, ``wx``, ``wy``) for
    concise access.  Call :meth:`update` to pull the latest data from
    the detection pipeline.
    """

    def __init__(self, tag_id: int, pipeline: DetectionPipeline) -> None:
        self._id = tag_id
        self._pipeline = pipeline
        self._snapshot: Optional[TagRecord] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Core properties
    # ------------------------------------------------------------------

    @property
    def id(self) -> int:
        return self._id

    @property
    def cx(self) -> float:
        """Pixel center X."""
        s = self._snapshot
        return s.center_px[0] if s else 0.0

    @property
    def cy(self) -> float:
        """Pixel center Y."""
        s = self._snapshot
        return s.center_px[1] if s else 0.0

    @property
    def wx(self) -> Optional[float]:
        """World center X (cm), or None if not calibrated."""
        s = self._snapshot
        return s.world_xy[0] if s and s.world_xy else None

    @property
    def wy(self) -> Optional[float]:
        """World center Y (cm), or None if not calibrated."""
        s = self._snapshot
        return s.world_xy[1] if s and s.world_xy else None

    @property
    def orientation(self) -> float:
        """Yaw angle in radians."""
        s = self._snapshot
        return s.orientation_yaw if s else 0.0

    @property
    def velocity(self) -> tuple[float, float]:
        """Pixel velocity (vx, vy) in px/s."""
        s = self._snapshot
        return s.vel_px if s and s.vel_px else (0.0, 0.0)

    @property
    def speed(self) -> float:
        """Pixel speed in px/s."""
        s = self._snapshot
        return s.speed_px if s and s.speed_px else 0.0

    @property
    def heading(self) -> Optional[float]:
        """Heading in radians (direction of motion), or None."""
        s = self._snapshot
        return s.heading_rad if s else None

    @property
    def rotation_rate(self) -> float:
        """Yaw rotation rate in rad/s (not yet computed — placeholder)."""
        return 0.0

    @property
    def timestamp(self) -> float:
        """Monotonic time when this observation was captured."""
        s = self._snapshot
        return s.timestamp if s else 0.0

    @property
    def age(self) -> float:
        """Seconds since last detection (0 = seen this frame)."""
        s = self._snapshot
        return s.age if s else float('inf')

    @property
    def is_visible(self) -> bool:
        """True if the tag was seen in the most recent frame."""
        s = self._snapshot
        if s is None:
            return False
        return s.age < 0.01  # effectively zero

    # ------------------------------------------------------------------
    # Methods
    # ------------------------------------------------------------------

    def update(self) -> Tag:
        """Pull the latest snapshot from the pipeline for this tag.

        Returns self for chaining.
        """
        latest = self._pipeline.ring_buffer.get_latest()
        if latest is not None:
            for tr in latest.tags:
                if tr.id == self._id:
                    with self._lock:
                        self._snapshot = tr
                    break
        return self

    def position_at(self, t: Optional[float] = None) -> tuple[float, float]:
        """Extrapolate pixel position to time *t*.

        Uses linear extrapolation from the stored velocity.  Defaults
        to ``time.monotonic()`` if *t* is ``None``.
        """
        if t is None:
            t = time.monotonic()
        s = self._snapshot
        if s is None:
            return (0.0, 0.0)
        dt = t - s.timestamp
        vx, vy = s.vel_px if s.vel_px else (0.0, 0.0)
        return (s.center_px[0] + vx * dt, s.center_px[1] + vy * dt)

    def world_position_at(self, t: Optional[float] = None) -> Optional[tuple[float, float]]:
        """Extrapolate world position to time *t*.

        Returns None if world coordinates are not available.
        """
        if t is None:
            t = time.monotonic()
        s = self._snapshot
        if s is None or s.world_xy is None:
            return None
        dt = t - s.timestamp
        if s.vel_world:
            return (s.world_xy[0] + s.vel_world[0] * dt,
                    s.world_xy[1] + s.vel_world[1] * dt)
        return s.world_xy

    def to_dict(self) -> dict:
        """Return a flat dict of all properties."""
        return {
            "id": self._id,
            "cx": self.cx,
            "cy": self.cy,
            "wx": self.wx,
            "wy": self.wy,
            "orientation": self.orientation,
            "velocity": self.velocity,
            "speed": self.speed,
            "heading": self.heading,
            "rotation_rate": self.rotation_rate,
            "timestamp": self.timestamp,
            "age": self.age,
            "is_visible": self.is_visible,
        }

    def __repr__(self) -> str:
        vis = "visible" if self.is_visible else "stale"
        return f"Tag(id={self._id}, cx={self.cx:.0f}, cy={self.cy:.0f}, {vis})"
