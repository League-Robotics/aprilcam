"""Composite: cross-camera homography for multi-camera setups."""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from typing import Optional

import cv2
import numpy as np


@dataclass
class Composite:
    """A registered composite linking two cameras via a homography."""

    composite_id: str
    primary_camera_id: str
    secondary_camera_id: str
    homography: np.ndarray  # 3x3, maps secondary -> primary
    reprojection_error: float
    playfield_id: Optional[str] = None


class CompositeManager:
    """Manages Composite entries keyed by composite_id."""

    def __init__(self) -> None:
        self._composites: dict[str, Composite] = {}

    def create(
        self,
        primary_camera_id: str,
        secondary_camera_id: str,
        homography: np.ndarray,
        reprojection_error: float,
        playfield_id: Optional[str] = None,
    ) -> Composite:
        """Create and register a new Composite, returning it."""
        composite_id = str(uuid.uuid4())
        comp = Composite(
            composite_id=composite_id,
            primary_camera_id=primary_camera_id,
            secondary_camera_id=secondary_camera_id,
            homography=homography,
            reprojection_error=reprojection_error,
            playfield_id=playfield_id or None,
        )
        self._composites[composite_id] = comp
        return comp

    def get(self, composite_id: str) -> Composite:
        """Return the Composite for *composite_id* or raise ``KeyError``."""
        return self._composites[composite_id]

    def destroy(self, composite_id: str) -> None:
        """Remove a composite. Raises ``KeyError`` if not found."""
        del self._composites[composite_id]

    def list(self) -> list[str]:
        """Return all registered composite IDs."""
        return list(self._composites.keys())


def compute_cross_camera_homography(
    primary_points: np.ndarray,
    secondary_points: np.ndarray,
) -> tuple[np.ndarray, float]:
    """Compute a homography mapping secondary camera coords to primary camera coords.

    Args:
        primary_points: Nx2 array of (x, y) in the primary camera.
        secondary_points: Nx2 array of (x, y) in the secondary camera.

    Returns:
        (H, rms_error) where H is 3x3 float64 and rms_error is the RMS
        reprojection error in primary-camera pixels.

    Raises:
        ValueError: if fewer than 4 point pairs or if the homography is
            degenerate (cv2.findHomography returns None).
    """
    primary_points = np.asarray(primary_points, dtype=np.float64).reshape(-1, 2)
    secondary_points = np.asarray(secondary_points, dtype=np.float64).reshape(-1, 2)

    if len(primary_points) < 4 or len(secondary_points) < 4:
        raise ValueError("At least 4 point correspondences are required")
    if len(primary_points) != len(secondary_points):
        raise ValueError("primary_points and secondary_points must have the same length")

    H, mask = cv2.findHomography(secondary_points, primary_points, method=0)
    if H is None:
        raise ValueError("Homography computation failed (degenerate point configuration)")

    # Compute RMS reprojection error
    sec_h = np.hstack([secondary_points, np.ones((len(secondary_points), 1))])
    projected = (H @ sec_h.T).T  # Nx3
    projected_xy = projected[:, :2] / projected[:, 2:3]
    errors = np.linalg.norm(projected_xy - primary_points, axis=1)
    rms_error = float(np.sqrt(np.mean(errors ** 2)))

    return H, rms_error


def map_tags_to_primary(
    detections: list[tuple[np.ndarray, np.ndarray, int]],
    homography: np.ndarray,
) -> list[dict]:
    """Map tag detections from secondary camera coords to primary camera coords.

    Args:
        detections: list of (corners_4x2, raw_image_unused, tag_id) tuples.
            ``corners_4x2`` is a 4x2 float array of the tag corner pixels
            in the secondary camera frame.
        homography: 3x3 ndarray mapping secondary -> primary.

    Returns:
        List of dicts, each with keys: id, center_px, corners_px, orientation_yaw.
    """
    results: list[dict] = []
    for corners, _raw, tag_id in detections:
        pts = np.asarray(corners, dtype=np.float32).reshape(-1, 1, 2)
        mapped = cv2.perspectiveTransform(pts, homography.astype(np.float64))
        mapped_2d = mapped.reshape(-1, 2)

        center = mapped_2d.mean(axis=0)

        # Orientation: direction from center to midpoint of first edge (p0-p1)
        p0, p1 = mapped_2d[0], mapped_2d[1]
        top_mid = (p0 + p1) / 2.0
        d = top_mid - center
        # (d) is a WORLD direction from an A1-centred, +y-north homography —
        # already y-up, so take the angle directly (no Y flip). 0°=+X, CCW.
        yaw = float(math.atan2(float(d[1]), float(d[0])))

        results.append({
            "id": int(tag_id),
            "center_px": [float(center[0]), float(center[1])],
            "corners_px": mapped_2d.tolist(),
            "orientation_yaw": yaw,
        })

    return results
