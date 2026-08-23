from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Tuple, Deque
from collections import deque

import numpy as np


def world_yaw(dx_raw: float, dy_raw: float) -> float:
    """Yaw of a raw (y-down) world/pixel direction in the reported ENU frame.

    Reported frame: x right, y up, origin at A1, 0°=+X, counter-clockwise
    positive (ROS REP-103 "math angles"). Raw coordinates straight out of the
    homography — and pixel coordinates — have Y pointing *down*; negating Y
    yields the y-up reporting frame so that ``forward = (cos yaw, sin yaw)``
    points along the tag's heading in reported world coordinates.
    """
    return math.atan2(-dy_raw, dx_raw)


def _project(
    homography: np.ndarray, px: float, py: float
) -> Optional[Tuple[float, float]]:
    """Map a pixel point through ``homography`` to raw world coords, or None."""
    v = homography @ np.array([float(px), float(py), 1.0], dtype=float)
    w = float(v[2])
    if abs(w) < 1e-9:
        return None
    return (float(v[0]) / w, float(v[1]) / w)


def _yaw_and_world(
    center: Tuple[float, float],
    top_mid: Tuple[float, float],
    n_unit: Tuple[float, float],
    homography: Optional[np.ndarray],
) -> Tuple[float, Optional[Tuple[float, float]]]:
    """Return ``(orientation_yaw, world_xy)`` in the reported ENU frame.

    With a homography the heading is measured in *world* space (center →
    top-mid transformed through the homography), which correctly accounts for
    camera rotation relative to the field. Without one — or if the world
    heading is degenerate — fall back to the pixel-space top direction
    (treated as raw y-down). See :func:`world_yaw` for the frame convention.
    """
    if homography is not None and homography.size == 9:
        cw = _project(homography, center[0], center[1])
        if cw is not None:
            tw = _project(homography, top_mid[0], top_mid[1])
            if tw is not None:
                dx, dy = tw[0] - cw[0], tw[1] - cw[1]
                if dx * dx + dy * dy > 1e-12:
                    # (dx, dy) are WORLD deltas from an A1-centred, +y-north
                    # homography — already in the reported y-up frame, so take
                    # the angle directly (do NOT negate y; world_yaw is only for
                    # the raw y-down pixel fallback below).
                    return math.atan2(dy, dx), cw
            return world_yaw(n_unit[0], n_unit[1]), cw
    return world_yaw(n_unit[0], n_unit[1]), None


@dataclass
class AprilTag:
    """Represents a detected AprilTag and its tracked state.

    - id: tag ID
    - family: AprilTag family name (e.g. "36h11", "25h9")
    - corners_px: 4x2 pixel coordinates (order as returned by detector)
    - center_px: pixel center (computed)
    - top_dir_px: unit vector from center toward the top edge midpoint (image coords)
    - world_xy: optional (X,Y) in world units (via homography)
    - orientation_yaw: yaw in radians in the reported ENU frame (x right,
      y up, origin at A1), 0°=+X, counter-clockwise positive (ROS REP-103
      "math angles"). 0 → top edge faces world +X; +pi/2 → faces world +Y.
      forward = (cos yaw, sin yaw) in reported world coordinates.
    - last_ts: timestamp of last update
    - frame: video frame index when measured
    - in_playfield: whether the tag center is within the playfield polygon
    """

    id: int
    family: str
    corners_px: np.ndarray
    center_px: Tuple[float, float]
    top_dir_px: Tuple[float, float]
    orientation_yaw: float
    world_xy: Optional[Tuple[float, float]] = None
    last_ts: Optional[float] = None
    frame: int = 0
    in_playfield: bool = False

    @staticmethod
    def from_corners(
        tag_id: int,
        corners_px: np.ndarray,
        homography: Optional[np.ndarray] = None,
    timestamp: Optional[float] = None,
    frame: int = 0,
    family: str = "36h11",
    ) -> "AprilTag":
        ptsf = corners_px.astype(np.float32)
        c = ptsf.mean(axis=0)
        p0, p1 = ptsf[0], ptsf[1]
        top_mid = (p0 + p1) / 2.0
        n = top_mid - c
        n_norm = float(np.linalg.norm(n))
        if n_norm > 1e-6:
            n_unit = (float(n[0]) / n_norm, float(n[1]) / n_norm)
        else:
            # Fallback: perpendicular to first edge
            e = p1 - p0
            perp = np.array([-e[1], e[0]], dtype=np.float32)
            denom = float(np.linalg.norm(perp)) or 1.0
            n_unit = (float(perp[0]) / denom, float(perp[1]) / denom)
        # Orientation + world position in the reported ENU frame
        # (x right, y up, 0°=+X, CCW). See world_yaw / _yaw_and_world.
        yaw, world_xy = _yaw_and_world(
            (float(c[0]), float(c[1])),
            (float(top_mid[0]), float(top_mid[1])),
            n_unit,
            homography,
        )
        return AprilTag(
            id=int(tag_id),
            family=family,
            corners_px=ptsf.copy(),
            center_px=(float(c[0]), float(c[1])),
            top_dir_px=n_unit,
            orientation_yaw=float(yaw),
            world_xy=world_xy,
            last_ts=timestamp,
            frame=int(frame),
        )

    def update(self, corners_px: np.ndarray, timestamp: float, homography: Optional[np.ndarray] = None) -> None:
        ptsf = corners_px.astype(np.float32)
        c = ptsf.mean(axis=0)
        p0, p1 = ptsf[0], ptsf[1]
        top_mid = (p0 + p1) / 2.0
        n = top_mid - c
        n_norm = float(np.linalg.norm(n))
        if n_norm > 1e-6:
            n_unit = (float(n[0]) / n_norm, float(n[1]) / n_norm)
        else:
            e = p1 - p0
            perp = np.array([-e[1], e[0]], dtype=np.float32)
            denom = float(np.linalg.norm(perp)) or 1.0
            n_unit = (float(perp[0]) / denom, float(perp[1]) / denom)
        # Orientation + world position in the reported ENU frame
        # (x right, y up, 0°=+X, CCW). See world_yaw / _yaw_and_world.
        yaw, world_xy = _yaw_and_world(
            (float(c[0]), float(c[1])),
            (float(top_mid[0]), float(top_mid[1])),
            n_unit,
            homography,
        )
        self.corners_px = ptsf.copy()
        self.center_px = (float(c[0]), float(c[1]))
        self.top_dir_px = n_unit
        self.orientation_yaw = float(yaw)
        if world_xy is not None:
            self.world_xy = world_xy
        self.last_ts = float(timestamp)

    def clone(self) -> "AprilTag":
        """Return a deep-ish copy suitable for historical storage in flows."""
        return AprilTag(
            id=int(self.id),
            family=self.family,
            corners_px=self.corners_px.copy(),
            center_px=(float(self.center_px[0]), float(self.center_px[1])),
            top_dir_px=(float(self.top_dir_px[0]), float(self.top_dir_px[1])),
            orientation_yaw=float(self.orientation_yaw),
            world_xy=(None if self.world_xy is None else (float(self.world_xy[0]), float(self.world_xy[1]))),
            last_ts=(None if self.last_ts is None else float(self.last_ts)),
            frame=int(self.frame),
            in_playfield=bool(self.in_playfield),
        )


class AprilTagFlow:
    """Fixed-size history of AprilTag observations with convenient properties.

    Exposes the same attribute interface as AprilTag, returning values from the
    most recent AprilTag in the deque. Velocity is set externally by the
    Playfield via :meth:`set_velocity` (EMA + dead-band smoothing).
    """

    def __init__(self, maxlen: int = 5) -> None:
        self._deque: Deque[AprilTag] = deque(maxlen=maxlen)
        self._id: Optional[int] = None
        self._vel_px: Tuple[float, float] = (0.0, 0.0)
        self._speed_px: float = 0.0
        self._vel_world: Optional[Tuple[float, float]] = None
        self._speed_world: Optional[float] = None
        self._heading_rad: Optional[float] = None

    def add_tag(self, tag: AprilTag) -> None:
        if self._id is None:
            self._id = int(tag.id)
        self._deque.append(tag)

    def set_velocity(self, vel_px: Tuple[float, float], speed_px: float) -> None:
        """Set the EMA-smoothed velocity for this flow.

        Called by Playfield.add_tag() after computing the EMA + dead-band
        velocity from successive tag observations.
        """
        self._vel_px = vel_px
        self._speed_px = speed_px

    def set_world_velocity(
        self,
        vel_world: Tuple[float, float],
        speed_world: float,
        heading_rad: float,
    ) -> None:
        """Set the world-space velocity for this flow.

        Called by Playfield.add_tag() after transforming the pixel velocity
        through the homography matrix.
        """
        self._vel_world = vel_world
        self._speed_world = speed_world
        self._heading_rad = heading_rad

    # --- core accessors mirroring AprilTag ---
    @property
    def id(self) -> int:
        return int(self._id) if self._id is not None else -1

    def _last(self) -> Optional[AprilTag]:
        return self._deque[-1] if self._deque else None

    @property
    def corners_px(self) -> np.ndarray:
        t = self._last()
        return t.corners_px if t is not None else np.zeros((4, 2), dtype=np.float32)

    @property
    def center_px(self) -> Tuple[float, float]:
        t = self._last()
        return t.center_px if t is not None else (0.0, 0.0)

    @property
    def top_dir_px(self) -> Tuple[float, float]:
        t = self._last()
        return t.top_dir_px if t is not None else (1.0, 0.0)

    @property
    def orientation_yaw(self) -> float:
        t = self._last()
        return t.orientation_yaw if t is not None else 0.0

    @property
    def world_xy(self) -> Optional[Tuple[float, float]]:
        t = self._last()
        return t.world_xy if t is not None else None

    @property
    def last_ts(self) -> Optional[float]:
        t = self._last()
        return t.last_ts if t is not None else None

    @property
    def frame(self) -> int:
        t = self._last()
        return t.frame if t is not None else 0

    @property
    def in_playfield(self) -> bool:
        t = self._last()
        return bool(t.in_playfield) if t is not None else False

    # --- derived motion (externally set) ---
    @property
    def vel_px(self) -> Tuple[float, float]:
        return self._vel_px

    @property
    def speed_px(self) -> float:
        return self._speed_px

    @property
    def vel_world(self) -> Optional[Tuple[float, float]]:
        return self._vel_world

    @property
    def speed_world(self) -> Optional[float]:
        return self._speed_world

    @property
    def heading_rad(self) -> Optional[float]:
        return self._heading_rad
