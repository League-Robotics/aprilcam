"""Camera calibration types and workflow functions.

Contains :class:`FieldSpec`, :class:`CameraCalibration`, and all
functions that drive the calibration workflow.  Pure homography math
lives in :mod:`aprilcam.calibration.homography`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2 as cv
import numpy as np

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PlayfieldConfigError(Exception):
    """Raised when a camera has no playfield configured or the def is missing."""


# The default set of static (fixed) reference markers for a playfield.
# Stakeholder-defined rule: static = all ArUco corner markers + AprilTag 1;
# every other AprilTag is dynamic.  ``aruco_corners`` is a sentinel meaning
# "all detected ArUco corners"; ``apriltag:N`` names a specific AprilTag id.
# Recorded per-camera so each playfield can override the set.
DEFAULT_STATIC_MARKER_IDS: List[str] = ["aruco_corners", "apriltag:1"]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class FieldSpec:
    width_in: float  # left->right
    height_in: float  # upper->lower
    units: str  # "inch" or "cm"

    @property
    def width_cm(self) -> float:
        if self.units == "inch":
            return self.width_in * 2.54
        return self.width_in

    @property
    def height_cm(self) -> float:
        if self.units == "inch":
            return self.height_in * 2.54
        return self.height_in


@dataclass
class CameraPosition:
    """Camera mounting position relative to the playfield.

    All coordinates are in cm.  The origin is the playfield center.
    Positive *x_offset* is to the right; positive *y_offset* is
    forward/up (away from the near edge).  *height* is the vertical
    distance above the playfield surface — used for parallax correction.
    """

    x_offset: float = 0.0  # cm from field center, positive = right
    y_offset: float = 0.0  # cm from field center, positive = up/forward
    height: float = 0.0    # cm above playfield


@dataclass
class CameraCalibration:
    """Stores everything needed to undistort + homography-transform a frame.

    For cameras without barrel distortion, *camera_matrix* and
    *dist_coeffs* are ``None`` and ``undistort()`` is a no-op.

    The optional *settings* dict stores hardware control values to apply
    when the camera is opened.  Expected shape::

        {
            "program": "uvc-util",
            "controls": {"exposure-time-abs": "10", "gain": "1", ...}
        }
    """

    device_name: str
    resolution: Tuple[int, int]  # (width, height)
    homography: np.ndarray  # 3x3 pixel->world
    camera_matrix: Optional[np.ndarray] = None  # 3x3 intrinsics
    dist_coeffs: Optional[np.ndarray] = None  # (k1,k2,p1,p2,k3)
    tags_used: int = 0
    rms_error: float = 0.0
    settings: Optional[Dict] = None  # hardware control settings
    pipeline: Optional[Dict] = None  # DetectorConfig overrides
    playfield_width_cm: float = 0.0
    playfield_height_cm: float = 0.0
    camera_position: Optional[CameraPosition] = None
    corner_pixels: Optional[List[List[float]]] = None
    """The four calibration-time ArUco corner pixel positions.

    Ordered ``[UL, UR, LR, LL]`` as ``[[u, v], ...]``.  Captured at
    calibration time and consumed by the static-camera deskew path
    (sprint 011, ticket 005) to seed the deskew source polygon without
    live corner detection.  ``None`` for records written before this
    field existed.
    """
    static_markers: Optional[Dict[str, Dict[str, List[float]]]] = None
    """Per-id pixel + world positions of the fixed reference markers.

    Maps a string tag id to ``{"pixel": [u, v], "world": [x, y]}``.  The
    static set is the ArUco corner markers plus AprilTag 1 (see
    ``static_marker_ids``).  World positions are in real-world cm.  These
    feed static-marker fill-in / movement-invalidation (sprint 011,
    ticket 006).  ``None`` for legacy records.
    """
    static_marker_ids: Optional[List[str]] = None
    """The configurable static-marker set recorded with this camera.

    Defaults to ``aruco_corners + apriltag:1`` (see
    :data:`DEFAULT_STATIC_MARKER_IDS`).  ``aruco_corners`` is a sentinel
    meaning "all detected ArUco corner markers"; ``apriltag:N`` names a
    specific AprilTag id.  ``None`` for legacy records.
    """
    calibrated_playfield: Optional[str] = None
    """The playfield slug that was active when this calibration was recorded.

    ``None`` for legacy records written before provenance tracking.
    Used by :func:`load_calibration_from_camera_dir` to detect stale
    calibrations when the camera's linked playfield has changed.
    """
    calibrated_camera: Optional[str] = None
    """The camera slug that was active when this calibration was recorded.

    ``None`` for legacy records written before provenance tracking.
    """

    def correct_world_for_height(
        self, wx: float, wy: float, tag_height_cm: float
    ) -> tuple:
        """Apply parallax correction for a tag elevated above the playfield.

        ``wx`` and ``wy`` must already be in the **A1-centred** coordinate
        system (origin at AprilTag 1, x right, y up).  ``x_offset`` and
        ``y_offset`` in *camera_position* are the camera's horizontal
        displacement from being directly above A1; ``0, 0`` means centred.

            wx_corrected = wx + (h/H) * (x_offset - wx)
            wy_corrected = wy + (h/H) * (y_offset - wy)

        Returns ``(wx, wy)`` unchanged when:
        - *camera_position* is ``None``, or
        - *camera_position.height* is ``0.0``, or
        - *tag_height_cm* is ``0.0``.
        """
        if self.camera_position is None or self.camera_position.height == 0.0:
            return wx, wy
        if tag_height_cm == 0.0:
            return wx, wy
        H = self.camera_position.height
        cx = self.camera_position.x_offset
        cy = self.camera_position.y_offset
        r = tag_height_cm / H
        return (wx + r * (cx - wx), wy + r * (cy - wy))

    def undistort(self, frame: np.ndarray) -> np.ndarray:
        """Remove barrel distortion if calibration data is available."""
        if self.camera_matrix is not None and self.dist_coeffs is not None:
            return cv.undistort(frame, self.camera_matrix, self.dist_coeffs)
        return frame

    def pixel_to_world(self, u: float, v: float) -> Tuple[float, float]:
        """Map a pixel coordinate to world (cm) coordinates."""
        vec = self.homography @ np.array([u, v, 1.0])
        return (float(vec[0] / vec[2]), float(vec[1] / vec[2]))

    def to_dict(self) -> dict:
        """Serialize to a JSON-compatible dict."""
        d: dict = {
            "device_name": self.device_name,
            "resolution": list(self.resolution),
            "homography": self.homography.tolist(),
            "tags_used": self.tags_used,
            "rms_error": self.rms_error,
        }
        if self.camera_matrix is not None:
            d["camera_matrix"] = self.camera_matrix.tolist()
        if self.dist_coeffs is not None:
            d["dist_coeffs"] = self.dist_coeffs.tolist()
        if self.settings is not None:
            d["settings"] = self.settings
        if self.pipeline is not None:
            d["pipeline"] = self.pipeline
        # Playfield real-world dimensions (cm).  These are the metric W×H the
        # static-camera deskew warp consumes, so they must round-trip as cm.
        if self.playfield_width_cm or self.playfield_height_cm:
            d["playfield"] = {
                "width": self.playfield_width_cm,
                "height": self.playfield_height_cm,
            }
        if self.corner_pixels is not None:
            d["corner_pixels"] = [list(p) for p in self.corner_pixels]
        if self.static_markers is not None:
            d["static_markers"] = {
                str(tid): {
                    "pixel": list(m["pixel"]),
                    "world": list(m["world"]),
                }
                for tid, m in self.static_markers.items()
            }
        if self.static_marker_ids is not None:
            d["static_marker_ids"] = list(self.static_marker_ids)
        if self.calibrated_playfield is not None:
            d["calibrated_playfield"] = self.calibrated_playfield
        if self.calibrated_camera is not None:
            d["calibrated_camera"] = self.calibrated_camera
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "CameraCalibration":
        """Deserialize from a JSON-compatible dict.

        New geometry fields (``playfield``, ``corner_pixels``,
        ``static_markers``, ``static_marker_ids``) are optional; records
        written before they existed load with these fields defaulting to
        ``0.0`` / ``None``.
        """
        cm = np.array(d["camera_matrix"], dtype=float) if "camera_matrix" in d else None
        dc = np.array(d["dist_coeffs"], dtype=float) if "dist_coeffs" in d else None

        # Playfield block round-trips as real-world centimetres.  Fall back to
        # legacy top-level field_*_cm keys when the playfield block is absent.
        pf = d.get("playfield", {}) or {}
        pw = float(pf.get("width") if pf.get("width") is not None
                   else d.get("field_width_cm") or 0.0)
        ph = float(pf.get("height") if pf.get("height") is not None
                   else d.get("field_height_cm") or 0.0)

        corner_pixels = d.get("corner_pixels")
        if corner_pixels is not None:
            corner_pixels = [[float(c) for c in p] for p in corner_pixels]

        static_markers_raw = d.get("static_markers")
        static_markers = None
        if static_markers_raw is not None:
            static_markers = {
                str(tid): {
                    "pixel": [float(c) for c in m["pixel"]],
                    "world": [float(c) for c in m["world"]],
                }
                for tid, m in static_markers_raw.items()
            }

        static_marker_ids = d.get("static_marker_ids")
        if static_marker_ids is not None:
            static_marker_ids = [str(s) for s in static_marker_ids]

        return cls(
            device_name=d["device_name"],
            resolution=tuple(d["resolution"]),
            homography=np.array(d["homography"], dtype=float),
            camera_matrix=cm,
            dist_coeffs=dc,
            tags_used=d.get("tags_used", 0),
            rms_error=d.get("rms_error", 0.0),
            settings=d.get("settings"),
            pipeline=d.get("pipeline"),
            playfield_width_cm=pw,
            playfield_height_cm=ph,
            corner_pixels=corner_pixels,
            static_markers=static_markers,
            static_marker_ids=static_marker_ids,
            calibrated_playfield=d.get("calibrated_playfield"),
            calibrated_camera=d.get("calibrated_camera"),
        )


# ---------------------------------------------------------------------------
# Per-camera calibration directory helpers (new scheme)
# ---------------------------------------------------------------------------


def device_name_slug(device_name: str) -> str:
    """Slugify a camera device name for use as a filename component.

    ``"Arducam OV9782 USB Camera"`` → ``"arducam-ov9782-usb-camera"``
    """
    import re
    return re.sub(r"[^a-z0-9]+", "-", device_name.lower()).strip("-")


def calibration_file_for_camera(
    calibration_dir: str | Path,
    device_name: str,
) -> Path:
    """Return the path to the per-camera calibration file.

    The filename is ``<device-slug>.json`` inside *calibration_dir*.
    """
    return Path(calibration_dir) / f"{device_name_slug(device_name)}.json"


def load_calibration_from_dir(
    device_name: str,
    calibration_dir: str | Path,
) -> Optional["CameraCalibration"]:
    """Load calibration for *device_name* from a per-camera directory.

    Reads ``<calibration_dir>/<device-slug>.json``.  Returns ``None``
    if the file does not exist or cannot be parsed.

    The file must contain at minimum ``"homography"``.  Optional fields
    ``"field_width_cm"`` and ``"field_height_cm"`` are ignored here but
    present in the file for operator reference.
    """
    cal_file = calibration_file_for_camera(calibration_dir, device_name)
    if not cal_file.exists():
        return None
    try:
        data = json.loads(cal_file.read_text())
        # Per-camera file has the camera data at the top level (no "cameras" nesting).
        data.setdefault("device_name", device_name)
        return CameraCalibration.from_dict(data)
    except Exception:
        return None


def load_calibration_from_camera_dir(
    camera_dir: str | Path,
    camera_config: "dict | None" = None,
    playfield_def: "object | None" = None,
) -> Optional["CameraCalibration"]:
    """Load calibration from ``<camera_dir>/calibration.json``.

    Returns ``None`` if the file does not exist or cannot be parsed.

    New fields populated from the JSON (all optional / backward-compatible):

    - ``playfield_width_cm`` / ``playfield_height_cm``: read from
      ``data["playfield"]["width"]`` / ``data["playfield"]["height"]``.
      Falls back to top-level ``field_width_cm`` / ``field_height_cm``
      for files written in the old format.
    - ``camera_position``: constructed from ``data["camera_position"]``
      dict when present; ``None`` otherwise.

    Optional mismatch detection (sprint 012):

    When both *camera_config* and *playfield_def* are provided, the loaded
    calibration is compared against the current camera config and playfield
    geometry.  If any mismatch is detected (mismatched slug, width, or
    height), or if the record has no ``calibrated_playfield`` (legacy), the
    transient attribute ``calibration_stale = True`` is set on the returned
    object and a warning is logged.

    ``calibration_stale`` is **not** a dataclass field; it is never
    serialised.  Callers check ``getattr(cal, "calibration_stale", False)``.

    Parameters
    ----------
    camera_dir:
        Directory containing ``calibration.json``.
    camera_config:
        Optional dict from ``load_camera_config(camera_dir)``.  Expected to
        have a ``"playfield"`` key naming the linked playfield slug.
    playfield_def:
        Optional :class:`~aprilcam.core.playfield_def.PlayfieldDefinition`.
        Must expose ``width_cm`` and ``height_cm`` attributes.
    """
    cal_file = Path(camera_dir) / "calibration.json"
    if not cal_file.exists():
        return None
    try:
        data = json.loads(cal_file.read_text())
        pf = data.get("playfield", {})
        pw = float(pf.get("width") or data.get("field_width_cm") or 0.0)
        ph = float(pf.get("height") or data.get("field_height_cm") or 0.0)
        cp_dict = data.get("camera_position")
        camera_position = CameraPosition(**cp_dict) if cp_dict else None
        cal = CameraCalibration.from_dict(data)
        cal.playfield_width_cm = pw
        cal.playfield_height_cm = ph
        cal.camera_position = camera_position

        # Mismatch detection — only when both optional params are provided.
        if camera_config is not None and playfield_def is not None:
            stale = False
            reason_parts: list[str] = []

            # Legacy record: no provenance field at all.
            if cal.calibrated_playfield is None:
                stale = True
                reason_parts.append("legacy record (no calibrated_playfield)")
            else:
                # Slug mismatch.
                expected_slug = camera_config.get("playfield")
                if expected_slug and cal.calibrated_playfield != expected_slug:
                    stale = True
                    reason_parts.append(
                        f"slug mismatch ({cal.calibrated_playfield!r} != {expected_slug!r})"
                    )

                # Dimension mismatch.
                _TOL = 0.01  # cm
                if abs(cal.playfield_width_cm - playfield_def.width_cm) > _TOL:
                    stale = True
                    reason_parts.append(
                        f"width mismatch ({cal.playfield_width_cm} != {playfield_def.width_cm})"
                    )
                if abs(cal.playfield_height_cm - playfield_def.height_cm) > _TOL:
                    stale = True
                    reason_parts.append(
                        f"height mismatch ({cal.playfield_height_cm} != {playfield_def.height_cm})"
                    )

            if stale:
                _log.warning(
                    "Calibration in %s is stale: %s",
                    camera_dir,
                    "; ".join(reason_parts),
                )
                cal.calibration_stale = True

        return cal
    except Exception:
        return None


def save_calibration_to_camera_dir(
    cal: "CameraCalibration",
    camera_dir: str | Path,
    field_width_cm: float,
    field_height_cm: float,
    detection_fps: int = 10,
) -> Path:
    """Write calibration to ``<camera_dir>/calibration.json``.

    Creates the directory if needed.  Returns the path written.

    *detection_fps* is written only when the file does not already
    contain a ``"detection_fps"`` key, preserving per-camera overrides.

    Playfield dimensions are written under ``playfield: {width, height}``
    (new format).  The old top-level ``field_width_cm`` / ``field_height_cm``
    keys are no longer written.  When present in an existing file they are
    treated as owned (not user-managed) so they are dropped on the next save.

    ``camera_position`` from *cal* is written when present; any
    user-managed keys not in the owned set are preserved.
    """
    camera_dir = Path(camera_dir)
    camera_dir.mkdir(parents=True, exist_ok=True)
    cal_file = camera_dir / "calibration.json"

    # Keys written by calibration — anything else in the existing file is
    # user-managed (e.g. UVC settings) and must be preserved.
    _CALIBRATION_KEYS = {
        "device_name", "resolution", "homography", "tags_used", "rms_error",
        "camera_matrix", "dist_coeffs",
        "field_width_cm", "field_height_cm",  # old keys — owned so they are dropped
        "playfield", "camera_position",
        "corner_pixels", "static_markers", "static_marker_ids",
        "detection_fps",
        "calibrated_playfield", "calibrated_camera",  # provenance (sprint 012)
    }
    preserved: dict = {}
    existing_camera_position: Optional[dict] = None
    if cal_file.exists():
        try:
            existing_data = json.loads(cal_file.read_text())
            preserved = {k: v for k, v in existing_data.items() if k not in _CALIBRATION_KEYS}
            if "detection_fps" in existing_data:
                detection_fps = int(existing_data["detection_fps"])
            # Preserve camera_position from existing file if the new cal doesn't have one
            if existing_data.get("camera_position"):
                existing_camera_position = existing_data["camera_position"]
        except Exception:
            pass

    data = cal.to_dict()
    data["playfield"] = {"width": field_width_cm, "height": field_height_cm}
    data["detection_fps"] = detection_fps
    if cal.camera_position is not None:
        data["camera_position"] = {
            "x_offset": cal.camera_position.x_offset,
            "y_offset": cal.camera_position.y_offset,
            "height": cal.camera_position.height,
        }
    elif existing_camera_position is not None:
        data["camera_position"] = existing_camera_position
    data.update(preserved)
    cal_file.write_text(json.dumps(data, indent=2))
    return cal_file


def load_field_dimensions_from_camera_dir(
    camera_dir: str | Path,
) -> Optional[tuple]:
    """Return ``(width_cm, height_cm)`` from ``<camera_dir>/calibration.json``.

    Reads new ``playfield: {width, height}`` format first; falls back to
    top-level ``field_width_cm`` / ``field_height_cm`` for old files.
    """
    cal_file = Path(camera_dir) / "calibration.json"
    if not cal_file.exists():
        return None
    try:
        data = json.loads(cal_file.read_text())
        pf = data.get("playfield", {})
        w = pf.get("width") or data.get("field_width_cm")
        h = pf.get("height") or data.get("field_height_cm")
        if w is not None and h is not None:
            return (float(w), float(h))
    except Exception:
        pass
    return None


def save_calibration_for_camera(
    cal: "CameraCalibration",
    calibration_dir: str | Path,
    field_width_cm: float,
    field_height_cm: float,
) -> Path:
    """Write a per-camera calibration file to *calibration_dir*.

    Creates ``<calibration_dir>/<device-slug>.json`` with:
    - ``field_width_cm``, ``field_height_cm`` — playfield dimensions
    - all fields from *cal* (homography, resolution, camera_matrix, etc.)

    Returns the path written.
    """
    cal_file = calibration_file_for_camera(calibration_dir, cal.device_name)
    cal_file.parent.mkdir(parents=True, exist_ok=True)
    data = cal.to_dict()
    data["field_width_cm"] = field_width_cm
    data["field_height_cm"] = field_height_cm
    cal_file.write_text(json.dumps(data, indent=2))
    return cal_file


def load_field_dimensions_from_dir(
    device_name: str,
    calibration_dir: str | Path,
) -> Optional[tuple]:
    """Return ``(width_cm, height_cm)`` from a per-camera calibration file.

    Returns ``None`` if the file is missing or dimensions are absent.
    """
    cal_file = calibration_file_for_camera(calibration_dir, device_name)
    if not cal_file.exists():
        return None
    try:
        data = json.loads(cal_file.read_text())
        w = data.get("field_width_cm")
        h = data.get("field_height_cm")
        if w is not None and h is not None:
            return (float(w), float(h))
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# File paths (legacy unified-file scheme)
# ---------------------------------------------------------------------------


def calibration_path(data_dir: str | Path = "data") -> Path:
    """Return the path to the unified calibration file."""
    return Path(data_dir) / "calibration.json"


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------


def load_field_dimensions(
    data_dir: str | Path = "data",
) -> Optional[tuple[float, float]]:
    """Return ``(width_cm, height_cm)`` from the top-level calibration file.

    Returns ``None`` if the file is missing or the keys are absent.
    """
    cal_file = calibration_path(data_dir)
    if not cal_file.exists():
        return None
    try:
        data = json.loads(cal_file.read_text())
        w = data.get("field_width_cm")
        h = data.get("field_height_cm")
        if w is not None and h is not None:
            return (float(w), float(h))
    except Exception:
        pass
    return None


def load_calibration_for_camera(
    device_name: str,
    data_dir: str | Path = "data",
) -> Optional[CameraCalibration]:
    """Load calibration for a specific camera from the unified file.

    Looks up by device_name in ``data/calibration.json``.  Returns
    ``None`` if the file doesn't exist or the camera isn't in it.
    """
    cal_file = calibration_path(data_dir)
    if not cal_file.exists():
        return None
    try:
        data = json.loads(cal_file.read_text())
        cameras = data.get("cameras", {})
        for _key, cam_data in cameras.items():
            if cam_data.get("device_name") == device_name:
                return CameraCalibration.from_dict(cam_data)
    except Exception:
        pass
    return None


def save_calibration(
    calibrations: List[CameraCalibration],
    data_dir: str | Path = "data",
    field_width_cm: float = 101.0,
    field_height_cm: float = 89.0,
) -> Path:
    """Save calibration for all cameras to ``data/calibration.json``.

    Each camera is keyed by its device_name.  Overwrites any existing
    file.  Returns the path written.
    """
    cameras = {}
    for cal in calibrations:
        cameras[cal.device_name] = cal.to_dict()

    data = {
        "type": "playfield",
        "field_width_cm": field_width_cm,
        "field_height_cm": field_height_cm,
        "cameras": cameras,
    }
    path = calibration_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    return path


def load_calibration(
    data_dir: str | Path = "data",
) -> Dict[str, CameraCalibration]:
    """Load all camera calibrations from ``data/calibration.json``.

    Returns:
        Dict mapping device_name -> CameraCalibration.
    """
    path = calibration_path(data_dir)
    data = json.loads(path.read_text())
    return {
        name: CameraCalibration.from_dict(cam_data)
        for name, cam_data in data.get("cameras", {}).items()
    }


# ---------------------------------------------------------------------------
# Calibration workflow
# ---------------------------------------------------------------------------


def _assign_corners_by_position(
    tags: dict,
    field_width_cm: float,
    field_height_cm: float,
) -> tuple[list, list]:
    """Assign ArUco boundary markers to world positions.

    **ID-based assignment (preferred, multi-camera consistent):** when a
    recognised canonical ArUco layout is detected, marker IDs are used
    directly to map pixel→world.  This guarantees every camera maps the
    same physical marker to the same world coordinate regardless of angle.

    Supported canonical layouts (ArUco ID N → tid -(N+1)):

    4-marker corners-only (IDs 0–3):
        ArUco 0 (tid -1) → (0,       0)
        ArUco 1 (tid -2) → (W,       0)
        ArUco 2 (tid -3) → (0,       H)
        ArUco 3 (tid -4) → (W,       H)

    8-marker perimeter, clockwise from upper-left (IDs 1–8):
        ArUco 1 (tid -2) → (0,       0)   upper-left
        ArUco 2 (tid -3) → (W/2,     0)   top-mid
        ArUco 3 (tid -4) → (W,       0)   upper-right
        ArUco 4 (tid -5) → (W,       H/2) right-mid
        ArUco 5 (tid -6) → (W,       H)   lower-right
        ArUco 6 (tid -7) → (W/2,     H)   bottom-mid
        ArUco 7 (tid -8) → (0,       H)   lower-left
        ArUco 8 (tid -9) → (0,       H/2) left-mid

    **Pixel-position fallback:** when no canonical layout matches, corners
    are sorted clockwise from upper-left by pixel coordinates.

    Returns (pixel_list, world_list) as paired lists.
    Raises RuntimeError if fewer than 4 ArUco markers are detected.
    """
    import math

    W, H = field_width_cm, field_height_cm

    # 8-marker perimeter layout: ArUco IDs 1–8 (tids -2 to -9), clockwise from UL.
    _CANONICAL_8_TIDS = (-2, -3, -4, -5, -6, -7, -8, -9)
    _CANONICAL_8_WORLD = [
        (0.0,  0.0),   (W/2,  0.0),   (W,    0.0),   (W,    H/2),
        (W,    H),     (W/2,  H),     (0.0,  H),     (0.0,  H/2),
    ]
    if all(tid in tags for tid in _CANONICAL_8_TIDS):
        pixel_list = [tags[tid] for tid in _CANONICAL_8_TIDS]
        return pixel_list, _CANONICAL_8_WORLD

    # 4-corner layout: ArUco IDs 0–3 (tids -1 to -4).
    _CANONICAL_4_TIDS = (-1, -2, -3, -4)
    _CANONICAL_4_WORLD = [(0.0, 0.0), (W, 0.0), (0.0, H), (W, H)]
    if all(tid in tags for tid in _CANONICAL_4_TIDS):
        pixel_list = [tags[tid] for tid in _CANONICAL_4_TIDS]
        return pixel_list, _CANONICAL_4_WORLD

    # --- Pixel-position fallback (any ArUco IDs) ---
    aruco_pts = [px for tid, px in tags.items() if tid < 0]
    n = len(aruco_pts)
    if n < 4:
        raise RuntimeError(
            f"Camera: only {n} ArUco corners found, need 4"
        )

    W, H = field_width_cm, field_height_cm
    world_by_n = {
        4: [(0, 0), (W, 0), (W, H), (0, H)],
        8: [(0, 0), (W/2, 0), (W, 0), (W, H/2),
            (W, H), (W/2, H), (0, H), (0, H/2)],
    }
    if n not in world_by_n and n > 8:
        ul = min(aruco_pts, key=lambda p:  p[0] + p[1])
        lr = max(aruco_pts, key=lambda p:  p[0] + p[1])
        ur = max(aruco_pts, key=lambda p:  p[0] - p[1])
        ll = min(aruco_pts, key=lambda p:  p[0] - p[1])
        top_mid  = min(aruco_pts, key=lambda p: p[1])
        bot_mid  = max(aruco_pts, key=lambda p: p[1])
        left_mid = min(aruco_pts, key=lambda p: p[0])
        rgt_mid  = max(aruco_pts, key=lambda p: p[0])
        aruco_pts = [ul, ur, lr, ll, top_mid, bot_mid, left_mid, rgt_mid]
        n = 8
    elif n not in world_by_n:
        ul = min(aruco_pts, key=lambda p:  p[0] + p[1])
        lr = max(aruco_pts, key=lambda p:  p[0] + p[1])
        ur = max(aruco_pts, key=lambda p:  p[0] - p[1])
        ll = min(aruco_pts, key=lambda p:  p[0] - p[1])
        aruco_pts = [ul, ur, lr, ll]
        n = 4

    world_positions = world_by_n[n]
    cx = sum(p[0] for p in aruco_pts) / n
    cy = sum(p[1] for p in aruco_pts) / n
    sorted_pts = sorted(aruco_pts, key=lambda p: math.atan2(p[1] - cy, p[0] - cx))
    ul_idx = min(range(n), key=lambda i: sorted_pts[i][0] + sorted_pts[i][1])
    sorted_pts = sorted_pts[ul_idx:] + sorted_pts[:ul_idx]

    return sorted_pts, world_positions


def _reprojection_rms(
    H: np.ndarray, pixel_pts: np.ndarray, world_pts: np.ndarray
) -> float:
    """Compute RMS reprojection error for a homography."""
    errors = []
    for px, wp in zip(pixel_pts, world_pts):
        vec = H @ np.array([px[0], px[1], 1.0])
        pred = np.array([vec[0] / vec[2], vec[1] / vec[2]])
        errors.append(np.linalg.norm(pred - wp))
    return float(np.sqrt(np.mean(np.array(errors) ** 2)))


def calibrate_from_corners(
    pixel_corners: Dict[str, Tuple[float, float]],
    field_spec: FieldSpec,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Compute homography from four corner positions and a field spec.

    Args:
        pixel_corners: Dict with keys 'upper_left', 'upper_right',
            'lower_left', 'lower_right', each a (x, y) tuple.
        field_spec: FieldSpec with physical dimensions.

    Returns:
        Tuple of (H, pixel_pts, world_pts_cm) where H is the 3x3
        homography matrix mapping [u,v,1] pixels to [X,Y,W] world cm,
        pixel_pts is the 4x2 array of pixel coordinates, and
        world_pts_cm is the 4x2 array of world coordinates in cm.
    """
    # Import compute_homography lazily to avoid circular import at module load.
    # homography.py re-exports from this module; this module calls into
    # homography.py only at runtime.
    from .homography import compute_homography

    world_pts_cm = np.array([
        [0.0, 0.0],
        [field_spec.width_cm, 0.0],
        [0.0, field_spec.height_cm],
        [field_spec.width_cm, field_spec.height_cm],
    ], dtype=np.float32)
    pixel_pts = np.array([
        pixel_corners["upper_left"],
        pixel_corners["upper_right"],
        pixel_corners["lower_left"],
        pixel_corners["lower_right"],
    ], dtype=np.float32)
    H = compute_homography(pixel_pts, world_pts_cm)
    return H, pixel_pts, world_pts_cm


def _build_static_markers(
    tags: dict,
    corner_pixels: list,
    corner_worlds: list,
    H: np.ndarray,
    static_marker_ids: List[str],
) -> Dict[str, Dict[str, List[float]]]:
    """Build the per-id static-marker pixel+world record.

    The static set is resolved from *static_marker_ids* (default
    ``aruco_corners + apriltag:1``):

    - ``aruco_corners`` — the four ArUco corner markers, recorded by their
      *world* corner key (``"corner:UL"`` … ``"corner:LL"``) with the
      calibration-time corner pixels and known world cm positions.
    - ``apriltag:N`` — AprilTag id ``N`` (positive id), recorded with its
      measured pixel position and the world position derived from *H*.
      Skipped silently if that tag was not detected.

    Returns ``{id_str: {"pixel": [u, v], "world": [x, y]}}``.  World
    coordinates are real-world centimetres.
    """
    _CORNER_KEYS = ["corner:UL", "corner:UR", "corner:LR", "corner:LL"]
    static_markers: Dict[str, Dict[str, List[float]]] = {}

    for marker_id in static_marker_ids:
        if marker_id == "aruco_corners":
            for key, px, wp in zip(_CORNER_KEYS, corner_pixels, corner_worlds):
                static_markers[key] = {
                    "pixel": [float(px[0]), float(px[1])],
                    "world": [float(wp[0]), float(wp[1])],
                }
        elif marker_id.startswith("apriltag:"):
            try:
                tid = int(marker_id.split(":", 1)[1])
            except ValueError:
                continue
            if tid in tags:
                px = tags[tid]
                vec = H @ np.array([px[0], px[1], 1.0])
                wx, wy = float(vec[0] / vec[2]), float(vec[1] / vec[2])
                static_markers[marker_id] = {
                    "pixel": [float(px[0]), float(px[1])],
                    "world": [wx, wy],
                }
    return static_markers


def calibrate_single(
    cap: cv.VideoCapture,
    field_width_cm: float = 101.0,
    field_height_cm: float = 89.0,
    num_frames: int = 30,
    correct_distortion: bool = True,
    camera_index: int = 0,
) -> CameraCalibration:
    """Calibrate a single camera using ArUco corners and AprilTags.

    Detects ArUco 4x4 corner markers (known world positions) and all
    AprilTags visible in the frame.  Computes homography from the 4
    ArUco corners, then refines using all detected tags.  Optionally
    estimates lens distortion if enough points are available.

    Args:
        cap: Open VideoCapture for the camera.
        field_width_cm: Playfield width between ArUco corners in cm.
        field_height_cm: Playfield height between ArUco corners in cm.
        num_frames: Frames to accumulate for tag detection.
        correct_distortion: Attempt barrel distortion correction.
        camera_index: Camera index (for device name lookup).

    Returns:
        CameraCalibration for the camera.
    """
    from .homography import compute_homography, detect_all_tags

    tags = detect_all_tags(cap, num_frames)

    cam_w = int(cap.get(cv.CAP_PROP_FRAME_WIDTH))
    cam_h = int(cap.get(cv.CAP_PROP_FRAME_HEIGHT))

    # Assign corners by pixel position (any 4 ArUco IDs accepted)
    corner_pixels, corner_worlds = _assign_corners_by_position(
        tags, field_width_cm, field_height_cm
    )

    pixel_pts = np.array(corner_pixels, dtype=np.float32)
    world_pts = np.array(corner_worlds, dtype=np.float32)
    H = compute_homography(pixel_pts, world_pts)

    # Compute world positions for AprilTags using corner-based homography
    all_px = list(corner_pixels)
    all_wp = list(corner_worlds)
    for tid, px in tags.items():
        if tid > 0:  # AprilTag (positive ID)
            vec = H @ np.array([px[0], px[1], 1.0])
            world_xy = (float(vec[0] / vec[2]), float(vec[1] / vec[2]))
            all_px.append(px)
            all_wp.append(world_xy)

    all_pixel = np.array(all_px, dtype=np.float32)
    all_world = np.array(all_wp, dtype=np.float32)
    n_pts = len(all_px)

    # Optionally correct barrel distortion
    camera_matrix = None
    dist_coeffs = None
    pts_for_rms = all_pixel

    if correct_distortion and n_pts >= 6:
        obj_pts_3d = np.zeros((n_pts, 1, 3), dtype=np.float32)
        obj_pts_3d[:, 0, :2] = all_world
        img_pts = all_pixel.reshape(n_pts, 1, 2)

        _rms, camera_matrix, dist_coeffs, _rvecs, _tvecs = cv.calibrateCamera(
            [obj_pts_3d], [img_pts], (cam_w, cam_h), None, None
        )
        dist_coeffs = dist_coeffs.flatten()

        undist_pts = cv.undistortPoints(
            all_pixel.reshape(-1, 1, 2), camera_matrix, dist_coeffs, P=camera_matrix
        ).reshape(-1, 2)
        H = compute_homography(undist_pts, all_world)
        pts_for_rms = undist_pts  # RMS must use same (undistorted) points as H
    elif n_pts > 4:
        # Recompute with all points for better accuracy
        H = compute_homography(all_pixel, all_world)

    rms = _reprojection_rms(H, pts_for_rms, all_world)

    # Capture the reference geometry the static-camera deskew path needs.
    # corner_pixels: the four calibration-time ArUco corner positions
    # (UL, UR, LR, LL) — exactly the pixel_pts used to seed the homography.
    corner_pixels = [[float(p[0]), float(p[1])] for p in corner_pixels]

    # static_markers: the fixed reference set (ArUco corners + AprilTag 1)
    # with their measured pixel positions and known/derived world positions.
    static_markers = _build_static_markers(
        tags, corner_pixels, corner_worlds, H, DEFAULT_STATIC_MARKER_IDS
    )

    from ..camera.camutil import get_device_name

    return CameraCalibration(
        device_name=get_device_name(camera_index),
        resolution=(cam_w, cam_h),
        homography=H,
        camera_matrix=camera_matrix,
        dist_coeffs=dist_coeffs,
        tags_used=n_pts,
        rms_error=rms,
        playfield_width_cm=field_width_cm,
        playfield_height_cm=field_height_cm,
        corner_pixels=corner_pixels,
        static_markers=static_markers,
        static_marker_ids=list(DEFAULT_STATIC_MARKER_IDS),
    )


def calibrate_from_playfield_def(
    cap: "cv.VideoCapture",
    camera_dir: "Path | str",
    camera_slug: str,
    playfield_def_registry: "object",
    num_frames: int = 30,
    correct_distortion: bool = True,
    camera_position: "CameraPosition | None" = None,
) -> CameraCalibration:
    """Calibrate a camera using a named playfield definition as the source of truth.

    This is the single shared calibration helper used by both the MCP
    ``calibrate_playfield`` tool and the ``aprilcam calibrate`` CLI.  It
    reads the camera's ``config.json`` to find the linked playfield, loads
    corner geometry from the :class:`PlayfieldDefinition`, detects the
    corresponding ArUco markers in the live feed, computes the homography,
    and writes ``calibration.json`` with provenance fields.

    Parameters
    ----------
    cap:
        Open VideoCapture-compatible object (must support ``read()`` and
        ``get(cv.CAP_PROP_FRAME_WIDTH/HEIGHT)``).
    camera_dir:
        Per-camera directory containing ``config.json`` and where
        ``calibration.json`` will be written.
    camera_slug:
        Human-readable camera identifier used in error messages and the
        ``calibrated_camera`` provenance field.
    playfield_def_registry:
        A :class:`~aprilcam.core.playfield_def.PlayfieldDefinitionRegistry`
        instance.  Must expose a ``get(name)`` method and a ``list()``
        method.
    num_frames:
        Number of frames to accumulate for tag detection.
    correct_distortion:
        Whether to attempt barrel-distortion calibration when enough points
        are available.
    camera_position:
        Optional camera-mounting offset used for parallax correction; stored
        in the calibration but not used during the calibration computation.

    Returns
    -------
    CameraCalibration
        The freshly computed calibration, already saved to *camera_dir*.

    Raises
    ------
    PlayfieldConfigError
        If ``config.json`` is absent, or if the playfield slug it names is
        not found in *playfield_def_registry*.
    RuntimeError
        If fewer than 4 of the expected corner ArUco markers are detected.
    """
    from .homography import compute_homography, detect_all_tags
    from ..camera.camera_config import load_camera_config

    camera_dir = Path(camera_dir)

    # Step 1: load config.json to find the linked playfield.
    config = load_camera_config(camera_dir)
    if config is None:
        available = ", ".join(playfield_def_registry.list()) or "(none)"
        raise PlayfieldConfigError(
            f"Camera '{camera_slug}' has no playfield configured. "
            f"Create data/aprilcam/cameras/{camera_slug}/config.json with "
            f'{{\"playfield\": \"<name>\"}}. '
            f"Available playfields: [{available}]"
        )

    playfield_slug = config.get("playfield") or ""
    if not playfield_slug:
        available = ", ".join(playfield_def_registry.list()) or "(none)"
        raise PlayfieldConfigError(
            f"Camera '{camera_slug}' config.json has no 'playfield' key. "
            f"Available playfields: [{available}]"
        )

    # Step 2: fetch the PlayfieldDefinition.
    try:
        pf_def = playfield_def_registry.get(playfield_slug)
    except KeyError:
        available = ", ".join(playfield_def_registry.list()) or "(none)"
        raise PlayfieldConfigError(
            f"Playfield '{playfield_slug}' not found in registry. "
            f"Available playfields: [{available}]"
        )

    # Step 3: get corner ArUco IDs and world coordinates from the def.
    # corner_aruco_ids() returns positive ints (e.g. 1/3/5/7).
    # In the detection dict, ArUco ID N is stored as tid = -(N+1).
    corner_ids_positive = pf_def.corner_aruco_ids()   # e.g. [1, 3, 5, 7]
    corner_world_coords = pf_def.corner_world_coords() # e.g. [(-67,44.65),...]
    corner_tids = [-(cid + 1) for cid in corner_ids_positive]  # e.g. [-2,-4,-6,-8]

    # Step 4: detect all tags over num_frames.
    tags = detect_all_tags(cap, num_frames)

    cam_w = int(cap.get(cv.CAP_PROP_FRAME_WIDTH))
    cam_h = int(cap.get(cv.CAP_PROP_FRAME_HEIGHT))

    # Step 5: match detected ArUco tags to the def's corner IDs.
    found_tids = [tid for tid in corner_tids if tid in tags]
    if len(found_tids) < 4:
        expected_ids_str = ", ".join(
            f"ArUco {cid} (tid {tid})"
            for cid, tid in zip(corner_ids_positive, corner_tids)
        )
        found_ids_str = ", ".join(
            f"ArUco {-(tid+1)} (tid {tid})"
            for tid in sorted(t for t in tags if t < 0)
        ) or "(none)"
        raise RuntimeError(
            f"Camera '{camera_slug}': only {len(found_tids)} of 4 expected corner "
            f"ArUco markers detected.\n"
            f"  Expected: {expected_ids_str}\n"
            f"  Found ArUco tids: {found_ids_str}"
        )

    # Build pixel/world pairs in the same order as the def's corner list.
    pixel_corners = [tags[tid] for tid in corner_tids]
    world_corners = list(corner_world_coords)

    pixel_pts = np.array(pixel_corners, dtype=np.float32)
    world_pts = np.array(world_corners, dtype=np.float32)

    # Step 6: initial homography from corner correspondence.
    H = compute_homography(pixel_pts, world_pts)

    # Step 7: augment with AprilTags for a more accurate homography.
    all_px = list(pixel_corners)
    all_wp = list(world_corners)
    for tid, px in tags.items():
        if tid > 0:  # AprilTag (positive ID)
            vec = H @ np.array([px[0], px[1], 1.0])
            world_xy = (float(vec[0] / vec[2]), float(vec[1] / vec[2]))
            all_px.append(px)
            all_wp.append(world_xy)

    all_pixel = np.array(all_px, dtype=np.float32)
    all_world = np.array(all_wp, dtype=np.float32)
    n_pts = len(all_px)

    # Step 8: optional barrel-distortion correction.
    camera_matrix = None
    dist_coeffs = None
    pts_for_rms = all_pixel

    if correct_distortion and n_pts >= 6:
        obj_pts_3d = np.zeros((n_pts, 1, 3), dtype=np.float32)
        obj_pts_3d[:, 0, :2] = all_world
        img_pts = all_pixel.reshape(n_pts, 1, 2)
        _rms, camera_matrix, dist_coeffs, _rvecs, _tvecs = cv.calibrateCamera(
            [obj_pts_3d], [img_pts], (cam_w, cam_h), None, None
        )
        dist_coeffs = dist_coeffs.flatten()
        undist_pts = cv.undistortPoints(
            all_pixel.reshape(-1, 1, 2), camera_matrix, dist_coeffs, P=camera_matrix
        ).reshape(-1, 2)
        H = compute_homography(undist_pts, all_world)
        pts_for_rms = undist_pts
    elif n_pts > 4:
        H = compute_homography(all_pixel, all_world)

    rms = _reprojection_rms(H, pts_for_rms, all_world)

    # Step 9: record calibration-time corner pixel positions and static markers.
    corner_pixels_rec = [[float(p[0]), float(p[1])] for p in pixel_corners]
    static_markers = _build_static_markers(
        tags, corner_pixels_rec, world_corners, H, DEFAULT_STATIC_MARKER_IDS
    )

    # Step 10: construct CameraCalibration with provenance fields.
    cal = CameraCalibration(
        device_name=camera_slug,
        resolution=(cam_w, cam_h),
        homography=H,
        camera_matrix=camera_matrix,
        dist_coeffs=dist_coeffs,
        tags_used=n_pts,
        rms_error=rms,
        playfield_width_cm=pf_def.width_cm,
        playfield_height_cm=pf_def.height_cm,
        camera_position=camera_position,
        corner_pixels=corner_pixels_rec,
        static_markers=static_markers,
        static_marker_ids=list(DEFAULT_STATIC_MARKER_IDS),
        calibrated_playfield=playfield_slug,
        calibrated_camera=camera_slug,
    )

    # Step 11: persist.
    save_calibration_to_camera_dir(cal, camera_dir, pf_def.width_cm, pf_def.height_cm)

    return cal


def calibrate_secondary(
    secondary_cap: cv.VideoCapture,
    primary_cal: CameraCalibration,
    primary_cap: cv.VideoCapture,
    num_frames: int = 30,
    correct_distortion: bool = True,
    secondary_index: int = 3,
) -> CameraCalibration:
    """Calibrate a secondary camera using a fully-calibrated primary camera.

    The primary camera has already been calibrated (homography + optional
    distortion).  Its homography is used to project the world positions of
    any AprilTags both cameras see, giving the secondary camera a set of
    pixel→world correspondences without needing ArUco corner markers.

    Args:
        secondary_cap: Open VideoCapture for the camera to calibrate.
        primary_cal: Completed CameraCalibration for the primary camera.
        primary_cap: Open VideoCapture for the primary camera (for
            simultaneous tag detection).
        num_frames: Frames to accumulate for tag detection.
        correct_distortion: Attempt barrel distortion correction if ≥6
            shared points are found.
        secondary_index: Camera index for device name lookup.

    Returns:
        CameraCalibration for the secondary camera.
    """
    from .homography import compute_homography, detect_all_tags

    # Detect tags on both cameras simultaneously
    primary_tags  = detect_all_tags(primary_cap,   num_frames)
    secondary_tags = detect_all_tags(secondary_cap, num_frames)

    sec_w = int(secondary_cap.get(cv.CAP_PROP_FRAME_WIDTH))
    sec_h = int(secondary_cap.get(cv.CAP_PROP_FRAME_HEIGHT))

    # Build world positions for all tags seen by the primary camera.
    # Include both AprilTags (positive IDs) and ArUco corners (negative IDs)
    # so the secondary calibration works even when there are few/no AprilTags.
    primary_world: Dict[int, Tuple[float, float]] = {}
    H_pri = primary_cal.homography
    cm = primary_cal.camera_matrix
    dc = primary_cal.dist_coeffs
    for tid, px in primary_tags.items():
        # Undistort the pixel point if primary has distortion calibration
        if cm is not None and dc is not None:
            pt = cv.undistortPoints(
                px.reshape(1, 1, 2).astype(np.float32), cm, dc, P=cm
            ).reshape(2)
        else:
            pt = px
        vec = H_pri @ np.array([pt[0], pt[1], 1.0])
        primary_world[tid] = (float(vec[0] / vec[2]), float(vec[1] / vec[2]))

    # Find all tags visible in both cameras (ArUco corners + AprilTags)
    sec_px_list, world_list = [], []
    for tid, world_xy in primary_world.items():
        if tid in secondary_tags:
            sec_px_list.append(secondary_tags[tid])
            world_list.append(world_xy)

    n_pts = len(sec_px_list)
    if n_pts < 4:
        raise RuntimeError(
            f"Secondary camera: only {n_pts} shared tags found (need ≥4). "
            f"Primary sees {len(primary_world)} tags; secondary sees "
            f"{len(secondary_tags)} tags."
        )

    sec_px = np.array(sec_px_list, dtype=np.float32)
    world  = np.array(world_list,  dtype=np.float32)

    camera_matrix = None
    dist_coeffs   = None
    rms           = 0.0

    if correct_distortion and n_pts >= 6:
        obj_pts_3d = np.zeros((n_pts, 1, 3), dtype=np.float32)
        obj_pts_3d[:, 0, :2] = world
        img_pts = sec_px.reshape(n_pts, 1, 2)
        _cv_rms, camera_matrix, dist_coeffs, _rv, _tv = cv.calibrateCamera(
            [obj_pts_3d], [img_pts], (sec_w, sec_h), None, None
        )
        dist_coeffs = dist_coeffs.flatten()

    # Always compute H from raw pixel coordinates.  Every caller applies H
    # directly to raw (non-undistorted) pixels, so H must be built the same way.
    H = compute_homography(sec_px, world)
    rms = _reprojection_rms(H, sec_px, world)

    from ..camera.camutil import get_device_name

    return CameraCalibration(
        device_name=get_device_name(secondary_index),
        resolution=(sec_w, sec_h),
        homography=H,
        camera_matrix=camera_matrix,
        dist_coeffs=dist_coeffs,
        tags_used=n_pts,
        rms_error=rms,
    )


def calibrate_joint(
    bw_cap: cv.VideoCapture,
    color_cap: cv.VideoCapture,
    field_width_cm: float = 101.0,
    field_height_cm: float = 89.0,
    num_frames: int = 30,
    correct_distortion: bool = True,
    bw_index: int = 3,
    color_index: int = 2,
) -> Tuple[CameraCalibration, CameraCalibration]:
    """Run joint multi-tag calibration on two cameras.

    Uses ArUco 4x4 corner markers (known world positions) and AprilTags
    (world positions computed from the B&W camera's homography) as
    shared reference points.  When *correct_distortion* is True and
    enough points are available (>=6), estimates lens distortion
    coefficients for the color camera.

    Args:
        bw_cap: Open VideoCapture for the B&W (primary) camera.
        color_cap: Open VideoCapture for the color (secondary) camera.
        field_width_cm: Playfield width between ArUco corners in cm.
        field_height_cm: Playfield height between ArUco corners in cm.
        num_frames: Frames to accumulate for tag detection.
        correct_distortion: Attempt barrel distortion correction on color.

    Returns:
        Tuple of (bw_calibration, color_calibration).
    """
    from .homography import compute_homography, detect_all_tags

    # Step 1: Detect tags on both cameras
    bw_tags = detect_all_tags(bw_cap, num_frames)
    color_tags = detect_all_tags(color_cap, num_frames)

    bw_w = int(bw_cap.get(cv.CAP_PROP_FRAME_WIDTH))
    bw_h = int(bw_cap.get(cv.CAP_PROP_FRAME_HEIGHT))
    color_w = int(color_cap.get(cv.CAP_PROP_FRAME_WIDTH))
    color_h = int(color_cap.get(cv.CAP_PROP_FRAME_HEIGHT))

    # Step 2: B&W camera homography — assign corners by pixel position
    bw_corner_pixels, bw_corner_world = _assign_corners_by_position(
        bw_tags, field_width_cm, field_height_cm
    )

    bw_pixel_pts = np.array(bw_corner_pixels, dtype=np.float32)
    bw_world_pts = np.array(bw_corner_world, dtype=np.float32)
    bw_H = compute_homography(bw_pixel_pts, bw_world_pts)

    # Step 3: Compute world positions for ALL tags using B&W homography.
    # Seed with ArUco corners (positionally assigned) so color camera can
    # use them as correspondence points even if no AprilTags overlap.
    tag_world_positions: Dict[int, Tuple[float, float]] = {}
    aruco_ids = sorted(tid for tid in bw_tags if tid < 0)
    # Map each ArUco negative ID to its assigned world position in order
    # (same sort order as _assign_corners_by_position: top-left, top-right,
    #  bottom-left, bottom-right sorted by y then x)
    by_y = sorted(((tid, bw_tags[tid]) for tid in aruco_ids), key=lambda t: t[1][1])
    top_row = sorted(by_y[:2], key=lambda t: t[1][0])
    bot_row = sorted(by_y[2:], key=lambda t: t[1][0])
    corner_id_order = [top_row[0][0], top_row[1][0], bot_row[0][0], bot_row[1][0]]
    for tid, world_xy in zip(corner_id_order, bw_corner_world):
        tag_world_positions[tid] = world_xy

    for tid, px in bw_tags.items():
        if tid > 0:  # AprilTag (positive ID)
            vec = bw_H @ np.array([px[0], px[1], 1.0])
            tag_world_positions[tid] = (float(vec[0] / vec[2]), float(vec[1] / vec[2]))

    # Step 4: Build color camera correspondences from shared tags
    color_pixel_pts = []
    color_world_pts = []
    for tid, world_xy in tag_world_positions.items():
        if tid in color_tags:
            color_pixel_pts.append(color_tags[tid])
            color_world_pts.append(world_xy)

    n_color_pts = len(color_pixel_pts)
    if n_color_pts < 4:
        raise RuntimeError(
            f"Color camera: only {n_color_pts} shared tags found, need >= 4"
        )

    color_px = np.array(color_pixel_pts, dtype=np.float32)
    color_wp = np.array(color_world_pts, dtype=np.float32)

    # Step 5: Color camera calibration
    color_cm = None
    color_dc = None
    color_rms = 0.0

    if correct_distortion and n_color_pts >= 6:
        # Use cv.calibrateCamera for distortion + intrinsics.
        # It needs 3D object points (add z=0 for planar).
        obj_pts_3d = np.zeros((n_color_pts, 1, 3), dtype=np.float32)
        obj_pts_3d[:, 0, :2] = color_wp
        img_pts = color_px.reshape(n_color_pts, 1, 2)

        color_rms, color_cm, color_dc, _rvecs, _tvecs = cv.calibrateCamera(
            [obj_pts_3d], [img_pts], (color_w, color_h), None, None
        )
        color_dc = color_dc.flatten()

        # Undistort the pixel points and recompute homography
        undist_pts = cv.undistortPoints(
            color_px.reshape(-1, 1, 2), color_cm, color_dc, P=color_cm
        ).reshape(-1, 2)
        color_H = compute_homography(undist_pts, color_wp)
    else:
        # Not enough points for distortion -- plain homography
        color_H = compute_homography(color_px, color_wp)

    # Compute B&W RMS error
    bw_all_px = []
    bw_all_wp = []
    for tid, world_xy in tag_world_positions.items():
        if tid in bw_tags:
            bw_all_px.append(bw_tags[tid])
            bw_all_wp.append(world_xy)
    if len(bw_all_px) > 4:
        # Recompute B&W homography with ALL points for better accuracy
        bw_all_pixel = np.array(bw_all_px, dtype=np.float32)
        bw_all_world = np.array(bw_all_wp, dtype=np.float32)
        bw_H = compute_homography(bw_all_pixel, bw_all_world)

    # Compute RMS reprojection errors
    bw_rms = _reprojection_rms(bw_H, bw_all_pixel, bw_all_world)

    from ..camera.camutil import get_device_name

    bw_cal = CameraCalibration(
        device_name=get_device_name(bw_index),
        resolution=(bw_w, bw_h),
        homography=bw_H,
        tags_used=len(bw_all_px),
        rms_error=bw_rms,
    )
    color_cal = CameraCalibration(
        device_name=get_device_name(color_index),
        resolution=(color_w, color_h),
        homography=color_H,
        camera_matrix=color_cm,
        dist_coeffs=color_dc,
        tags_used=n_color_pts,
        rms_error=color_rms,
    )
    return bw_cal, color_cal


# ---------------------------------------------------------------------------
# Top-level convenience function
# ---------------------------------------------------------------------------


def calibrate(
    camera: "cv.VideoCapture | int",
    *,
    width_cm: float = 101.0,
    height_cm: float = 89.0,
    frames: int = 30,
    output: "str | Path | None" = None,
) -> CameraCalibration:
    """Calibrate a camera for playfield homography.

    Args:
        camera: An open :class:`cv.VideoCapture` or a camera index.
        width_cm: Playfield width between ArUco corners in cm.
        height_cm: Playfield height between ArUco corners in cm.
        frames: Frames to accumulate for tag detection.
        output: Optional path to save the calibration file.  If given,
            the result is merged into (or created as) a
            ``calibration.json`` at that path.

    Returns:
        :class:`CameraCalibration` for the camera.
    """
    own_cap = False
    if isinstance(camera, int):
        cap = cv.VideoCapture(camera)
        cam_index = camera
        own_cap = True
    else:
        cap = camera
        cam_index = 0

    try:
        cal = calibrate_single(
            cap,
            field_width_cm=width_cm,
            field_height_cm=height_cm,
            num_frames=frames,
            camera_index=cam_index,
        )
    finally:
        if own_cap:
            cap.release()

    if output is not None:
        out_path = Path(output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if out_path.exists():
            try:
                import json as _json
                cal_data = _json.loads(out_path.read_text())
            except Exception:
                cal_data = {}
        else:
            cal_data = {}
        cal_data.setdefault("type", "playfield")
        cal_data.setdefault("field_width_cm", width_cm)
        cal_data.setdefault("field_height_cm", height_cm)
        if "cameras" not in cal_data:
            cal_data["cameras"] = {}
        cal_data["cameras"][cal.device_name] = cal.to_dict()
        import json as _json
        out_path.write_text(_json.dumps(cal_data, indent=2))

    return cal


# ---------------------------------------------------------------------------
# Legacy aliases
# ---------------------------------------------------------------------------


def save_joint_calibration(
    bw_cal: CameraCalibration,
    color_cal: CameraCalibration,
    path: Path,
    field_width_cm: float = 101.0,
    field_height_cm: float = 89.0,
) -> None:
    """Save calibration (legacy -- prefers :func:`save_calibration`)."""
    save_calibration(
        [bw_cal, color_cal],
        data_dir=path.parent,
        field_width_cm=field_width_cm,
        field_height_cm=field_height_cm,
    )


def load_joint_calibration(
    path: Path,
) -> Tuple[CameraCalibration, CameraCalibration]:
    """Load calibration (legacy -- prefers :func:`load_calibration`)."""
    data = json.loads(path.read_text())
    cams = list(data.get("cameras", {}).values())
    if len(cams) < 2:
        raise ValueError(f"Expected at least 2 cameras in {path}")
    return (
        CameraCalibration.from_dict(cams[0]),
        CameraCalibration.from_dict(cams[1]),
    )
