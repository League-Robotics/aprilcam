"""Planar pose of the camera from marker corners (sprint 045 ticket 004).

**Daemon-only (cv2).** The numerical heart of pose-based calibration: given
surveyed marker centres and sizes plus the marker corners seen in the frame,
recover the camera's metric pose over the field plane, derive the
pixel -> field homography from it, and expose the checks ``gates.py`` decides on.

Conventions (guilty until proven innocent -- every one is pinned by a test):

* **Field frame**: x east, y north, z up, cm; the field plane is z = 0. A
  camera looking straight down with north at the top of the image has the
  rotation ``diag(1, -1, -1)`` (field -> camera): no mirror.
* **Marker corner order** is the detector's own, TL, TR, BR, BL of the marker's
  *own* frame (clockwise seen from above, from the printed top-left).
  ``quarter_turns`` rotates the printed marker **counter-clockwise seen from
  above** (north-up). Upright (0): top edge toward north, so
  TL = centre + (-s/2, +s/2). One quarter turn: TL moves to the south-west
  and the marker's top edge faces west.
* **Orientation is detected, never declared** (sprint 045 Decision 3):
  :func:`detect_marker_rotations` picks, per marker, the quarter turn whose
  predicted corners best match the detected ones.
* ``homography_from_pose`` maps an **undistorted** pixel (what
  ``distortion.undistort_point`` returns) to field cm -- exactly what
  ``Calibration.to_world`` consumes via ``geometry.pixel_to_world``.

Relationship to ``intrinsics/collector.py``: its ``_pose`` also calls
``solvePnPGeneric`` (IPPE) and keeps the error ratio, but from a guessed K for
tilt/coverage gating only. This module is the metric solve with a measured
lens. They are deliberately separate (sprint 045 Decision 1: sharing would
close a package cycle). If you change the IPPE handling here, look there too.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import cv2
import numpy as np

from aprilcam.calibration.interfaces import MarkerObservation
from aprilcam.types import PixelPoint, WorldPoint

__all__ = [
    "PlanarPose", "SurveyedMarker", "MarkerRotation", "FieldCorner", "TargetObserver",
    "marker_object_points", "undistort_points", "viewing_rays",
    "detect_marker_rotation", "detect_marker_rotations", "correspondences",
    "solve_planar_pose", "homography_from_pose", "camera_position_from_pose",
    "reprojection_error_cm", "marker_distances", "leave_one_out_residuals",
]

_EPS_PX = 1e-3
_EPS_CM = 1e-4


@dataclass(frozen=True, kw_only=True)
class PlanarPose:
    """Camera pose over the field plane (``rvec``/``tvec`` map field cm -> camera)."""

    rvec: np.ndarray
    tvec: np.ndarray
    residuals_px: np.ndarray
    """Per-corner raw-pixel residual of the refined solve, shape (N,)."""
    rms_px: float
    ambiguity_ratio: float
    """IPPE runner-up error / best error (undistorted px). ~1 means two
    poses explain the corners equally (near-edge-on single marker)."""
    n_points: int


@dataclass(frozen=True, kw_only=True)
class SurveyedMarker:
    """What the playfield definition says about one marker."""

    tag_id: int
    centre_cm: Tuple[float, float]
    size_mm: float


@dataclass(frozen=True, kw_only=True)
class MarkerRotation:
    """Detected in-plane rotation of one marker."""

    tag_id: int
    quarter_turns: int
    """0..3, counter-clockwise seen from above (see module docstring)."""
    residuals_cm: Tuple[float, float, float, float]
    """RMS corner error in cm for each candidate quarter turn 0..3."""
    ratio: float
    """Runner-up / winner residual (>= 1; large = unambiguous)."""


@dataclass(frozen=True, kw_only=True)
class FieldCorner:
    """A field-frame correspondence from a calibration-target observer."""

    field_cm: Tuple[float, float]
    pixel: PixelPoint


#: ``(frame, calibration_target) -> field-frame corner correspondences``
#: (sprint 045 Decision 6). Injected into ``Calibrator``; the target type is
#: left as ``Any`` here because ``CalibrationTarget`` arrives with ticket 011.
TargetObserver = Callable[[np.ndarray, Any], Sequence[FieldCorner]]


# ---- geometry primitives --------------------------------------------------


def marker_object_points(
    centre_cm: Tuple[float, float], size_mm: float, quarter_turns: int = 0
) -> np.ndarray:
    """(4, 3) field-frame corners (cm, z = 0) in TL, TR, BR, BL order."""
    h = float(size_mm) / 20.0
    xy = np.array([[-h, h], [h, h], [h, -h], [-h, -h]], dtype=np.float64)
    for _ in range(int(quarter_turns) % 4):
        xy = np.column_stack([-xy[:, 1], xy[:, 0]])  # CCW quarter turn
    pts = np.zeros((4, 3), dtype=np.float64)
    pts[:, 0] = xy[:, 0] + float(centre_cm[0])
    pts[:, 1] = xy[:, 1] + float(centre_cm[1])
    return pts


def _dist(dist_coeffs: Optional[np.ndarray]) -> np.ndarray:
    if dist_coeffs is None:
        return np.zeros(5, dtype=np.float64)
    return np.asarray(dist_coeffs, dtype=np.float64).reshape(-1)


def undistort_points(
    pixels: np.ndarray, camera_matrix: np.ndarray, dist_coeffs: Optional[np.ndarray]
) -> np.ndarray:
    """(N, 2) raw pixels -> (N, 2) undistorted pixels (``P = K``)."""
    src = np.asarray(pixels, dtype=np.float64).reshape(-1, 1, 2)
    out = cv2.undistortPoints(src, camera_matrix, _dist(dist_coeffs), P=camera_matrix)
    return out.reshape(-1, 2)


def viewing_rays(
    pixels: np.ndarray, camera_matrix: np.ndarray, dist_coeffs: Optional[np.ndarray]
) -> np.ndarray:
    """(N, 3) unit viewing rays through raw *pixels*, for ``gates.angular_spread_deg``."""
    src = np.asarray(pixels, dtype=np.float64).reshape(-1, 1, 2)
    xy = cv2.undistortPoints(src, camera_matrix, _dist(dist_coeffs)).reshape(-1, 2)
    rays = np.column_stack([xy, np.ones(len(xy))])
    return rays / np.linalg.norm(rays, axis=1, keepdims=True)


def _apply_h(h: np.ndarray, pts: np.ndarray) -> np.ndarray:
    p = np.column_stack([pts, np.ones(len(pts))]) @ h.T
    return p[:, :2] / p[:, 2:3]


def _corners_array(obs: MarkerObservation) -> np.ndarray:
    return np.array([[c.x, c.y] for c in obs.corners], dtype=np.float64)


# ---- pose solve -----------------------------------------------------------


def _rms_raw(obj, img, rvec, tvec, k, dist) -> Tuple[float, np.ndarray]:
    proj, _ = cv2.projectPoints(obj, rvec, tvec, k, dist)
    res = np.linalg.norm(proj.reshape(-1, 2) - img, axis=1)
    return float(np.sqrt(np.mean(res ** 2))), res


def solve_planar_pose(
    object_pts: np.ndarray,
    image_pts_raw: np.ndarray,
    camera_matrix: np.ndarray,
    dist_coeffs: Optional[np.ndarray],
) -> PlanarPose:
    """Camera pose from >= 4 coplanar (z = 0) field points and their raw pixels.

    Undistorts to ``P = K``, runs IPPE (both solutions kept), refines
    the better one (camera above the field plane) with Levenberg-Marquardt
    against the *raw* points through the distortion model, and reports the
    runner-up/best IPPE error ratio as the ambiguity ratio.
    """
    obj = np.asarray(object_pts, dtype=np.float64).reshape(-1, 3)
    img = np.asarray(image_pts_raw, dtype=np.float64).reshape(-1, 2)
    if len(obj) < 4 or len(obj) != len(img):
        raise ValueError(f"need >= 4 matching points, got {len(obj)} / {len(img)}")
    if np.abs(obj[:, 2]).max() > 1e-9:
        raise ValueError("object points must lie on the field plane (z = 0)")
    k = np.asarray(camera_matrix, dtype=np.float64)
    dist = _dist(dist_coeffs)
    und = undistort_points(img, k, dist)
    n_sol, rvecs, tvecs, _errs = cv2.solvePnPGeneric(
        obj, und, k, np.zeros(5), flags=cv2.SOLVEPNP_IPPE)
    if n_sol < 1:
        raise ValueError("IPPE found no solution")
    # IPPE returns solutions sorted by error. Ratio of the two errors is the
    # ambiguity measure (as intrinsics/collector.py does); it must come from
    # IPPE's own errors, since LM refinement drags the runner-up onto the same
    # minimum and would erase the ambiguity.
    errs = np.asarray(_errs, dtype=np.float64).reshape(-1)
    order = [i for i in range(n_sol) if _camera_height(rvecs[i], tvecs[i]) > 0] or list(range(n_sol))
    pick = order[0]
    ratio = math.inf
    if n_sol > 1:
        other = 1 - pick if n_sol == 2 else order[1] if len(order) > 1 else None
        if other is not None:
            ratio = float(errs[other]) / max(float(errs[pick]), _EPS_PX)
    rv = np.array(rvecs[pick], dtype=np.float64).reshape(3, 1)
    tv = np.array(tvecs[pick], dtype=np.float64).reshape(3, 1)
    cv2.solvePnPRefineLM(obj, img, k, dist, rv, tv)
    rms, res = _rms_raw(obj, img, rv, tv, k, dist)
    return PlanarPose(rvec=rv.reshape(3), tvec=tv.reshape(3), residuals_px=res,
                      rms_px=rms, ambiguity_ratio=float(ratio), n_points=len(obj))


def _camera_height(rvec, tvec) -> float:
    rot, _ = cv2.Rodrigues(np.asarray(rvec, dtype=np.float64).reshape(3, 1))
    return float((-rot.T @ np.asarray(tvec, dtype=np.float64).reshape(3, 1))[2, 0])


def homography_from_pose(pose: PlanarPose, camera_matrix: np.ndarray) -> np.ndarray:
    """3x3 undistorted pixel -> field cm homography (inverse of ``K [r1 r2 t]``)."""
    rot, _ = cv2.Rodrigues(pose.rvec.reshape(3, 1))
    h_world_to_pixel = np.asarray(camera_matrix, dtype=np.float64) @ np.column_stack(
        [rot[:, 0], rot[:, 1], pose.tvec.reshape(3)])
    h = np.linalg.inv(h_world_to_pixel)
    return h / h[2, 2]


def camera_position_from_pose(pose: PlanarPose) -> Tuple[float, WorldPoint, float]:
    """``(height_cm, nadir, tilt_deg)`` from ``C = -R^T t``.

    Tilt is the angle between the optical axis and straight down.
    """
    rot, _ = cv2.Rodrigues(pose.rvec.reshape(3, 1))
    c = -rot.T @ pose.tvec.reshape(3)
    tilt = math.degrees(math.acos(max(-1.0, min(1.0, -float(rot[2, 2])))))
    return float(c[2]), WorldPoint(x=float(c[0]), y=float(c[1])), tilt


def reprojection_error_cm(
    pose: PlanarPose,
    camera_matrix: np.ndarray,
    dist_coeffs: Optional[np.ndarray],
    object_pts: np.ndarray,
    image_pts_raw: np.ndarray,
) -> np.ndarray:
    """(N,) distance in cm between each corner's surveyed position and where the
    derived homography puts its detected pixel."""
    obj = np.asarray(object_pts, dtype=np.float64).reshape(-1, 3)
    und = undistort_points(image_pts_raw, camera_matrix, dist_coeffs)
    world = _apply_h(homography_from_pose(pose, camera_matrix), und)
    return np.linalg.norm(world - obj[:, :2], axis=1)


# ---- rotation detection ---------------------------------------------------


def detect_marker_rotation(
    tag_id: int,
    corners_cm: np.ndarray,
    centre_cm: Tuple[float, float],
    size_mm: float,
) -> MarkerRotation:
    """Best of the 4 quarter turns for one marker, given its detected corners
    already mapped into field cm (by any trusted provisional homography)."""
    c = np.asarray(corners_cm, dtype=np.float64).reshape(4, 2)
    res = []
    for q in range(4):
        pred = marker_object_points(centre_cm, size_mm, q)[:, :2]
        res.append(float(np.sqrt(np.mean(np.sum((pred - c) ** 2, axis=1)))))
    order = sorted(range(4), key=lambda i: res[i])
    best, second = order[0], order[1]
    ratio = res[second] / max(res[best], _EPS_CM)
    return MarkerRotation(tag_id=tag_id, quarter_turns=best,
                          residuals_cm=tuple(res), ratio=float(ratio))  # type: ignore[arg-type]


def correspondences(
    observations: Sequence[MarkerObservation],
    surveyed: Mapping[int, SurveyedMarker],
    quarter_turns: Mapping[int, int],
) -> Tuple[np.ndarray, np.ndarray]:
    """Stack ((4n, 3) object points, (4n, 2) raw pixels) over matched markers."""
    obj, img = [], []
    for o in observations:
        s = surveyed[o.tag_id]
        obj.append(marker_object_points(s.centre_cm, s.size_mm, quarter_turns[o.tag_id]))
        img.append(_corners_array(o))
    return np.vstack(obj), np.vstack(img)


def detect_marker_rotations(
    observations: Sequence[MarkerObservation],
    surveyed: Mapping[int, SurveyedMarker],
    camera_matrix: np.ndarray,
    dist_coeffs: Optional[np.ndarray],
) -> Dict[int, MarkerRotation]:
    """Detect every marker's quarter turn from the corner order alone.

    With >= 4 markers: seed a homography from the detected centres to the
    surveyed centres, score each marker's 4 candidates, refit the pose with
    the winners, and score once more through the refit pose. With fewer
    (a centre homography is under-determined): enumerate every combination
    of quarter turns (<= 4^3) and take the best pose fit; a marker's ratio
    is then best-with-another-turn / best. One marker is inherently
    ambiguous (ratio ~ 1, turn 0) -- flagged by its ratio.
    """
    obs = [o for o in observations if o.tag_id in surveyed]
    if not obs:
        return {}
    k = np.asarray(camera_matrix, dtype=np.float64)
    if len(obs) < 4:
        return _rotations_by_enumeration(obs, surveyed, k, dist_coeffs)

    centres_px = undistort_points(np.array([[o.centre.x, o.centre.y] for o in obs]), k, dist_coeffs)
    centres_cm = np.array([surveyed[o.tag_id].centre_cm for o in obs], dtype=np.float64)
    h, _ = cv2.findHomography(centres_px, centres_cm, 0)
    if h is None:
        return _rotations_by_enumeration(obs[:3], surveyed, k, dist_coeffs)

    def score(h_px_to_cm: np.ndarray) -> Dict[int, MarkerRotation]:
        out = {}
        for o in obs:
            s = surveyed[o.tag_id]
            cm = _apply_h(h_px_to_cm, undistort_points(_corners_array(o), k, dist_coeffs))
            out[o.tag_id] = detect_marker_rotation(o.tag_id, cm, s.centre_cm, s.size_mm)
        return out

    rotations = score(h)
    for _ in range(2):  # refit through the pose and rescore; stops when stable
        turns = {i: r.quarter_turns for i, r in rotations.items()}
        obj, img = correspondences(obs, surveyed, turns)
        pose = solve_planar_pose(obj, img, k, dist_coeffs)
        refined = score(homography_from_pose(pose, k))
        stable = all(refined[i].quarter_turns == turns[i] for i in turns)
        rotations = refined
        if stable:
            break
    return rotations


def _rotations_by_enumeration(obs, surveyed, k, dist_coeffs) -> Dict[int, MarkerRotation]:
    n = len(obs)
    fits: Dict[Tuple[int, ...], float] = {}
    for combo in itertools.product(range(4), repeat=n):
        turns = {o.tag_id: q for o, q in zip(obs, combo)}
        obj, img = correspondences(obs, surveyed, turns)
        try:
            fits[combo] = solve_planar_pose(obj, img, k, dist_coeffs).rms_px
        except ValueError:
            fits[combo] = math.inf
    best_combo = min(fits, key=lambda c: (fits[c], c))
    best = fits[best_combo]
    out = {}
    for i, o in enumerate(obs):
        per_turn = [min(v for c, v in fits.items() if c[i] == q) for q in range(4)]
        others = [per_turn[q] for q in range(4) if q != best_combo[i]]
        ratio = min(others) / max(best, _EPS_PX)
        out[o.tag_id] = MarkerRotation(
            tag_id=o.tag_id, quarter_turns=best_combo[i],
            residuals_cm=tuple(per_turn), ratio=float(ratio))  # type: ignore[arg-type]
    return out


# ---- checks for gates.py --------------------------------------------------


def marker_distances(
    observations: Sequence[MarkerObservation],
    surveyed: Mapping[int, SurveyedMarker],
    pose: PlanarPose,
    camera_matrix: np.ndarray,
    dist_coeffs: Optional[np.ndarray],
) -> Tuple[List[float], List[float]]:
    """``(local, global)`` camera-to-marker-centre distances in cm, one per marker.

    *local*: that marker alone, IPPE with its own print size (so it knows only
    the marker's apparent size). *global*: from the all-marker pose and the
    surveyed centre. Their median ratio is ``gates.scale_agreement``.
    """
    k = np.asarray(camera_matrix, dtype=np.float64)
    rot, _ = cv2.Rodrigues(pose.rvec.reshape(3, 1))
    cam = -rot.T @ pose.tvec.reshape(3)
    local, glob = [], []
    for o in observations:
        s = surveyed[o.tag_id]
        und = undistort_points(_corners_array(o), k, dist_coeffs)
        own = marker_object_points((0.0, 0.0), s.size_mm, 0)
        n_sol, _rv, tvecs, _e = cv2.solvePnPGeneric(own, und, k, np.zeros(5), flags=cv2.SOLVEPNP_IPPE)
        if n_sol < 1:
            continue
        local.append(float(np.linalg.norm(np.asarray(tvecs[0], dtype=np.float64))))
        glob.append(float(np.linalg.norm(cam - np.array([s.centre_cm[0], s.centre_cm[1], 0.0]))))
    return local, glob


def leave_one_out_residuals(
    observations: Sequence[MarkerObservation],
    surveyed: Mapping[int, SurveyedMarker],
    quarter_turns: Mapping[int, int],
    camera_matrix: np.ndarray,
    dist_coeffs: Optional[np.ndarray],
) -> Dict[int, float]:
    """Per-marker leave-one-out: id -> cm error of that marker's centre as
    predicted by the pose fit to all the *other* markers. Empty below 3 markers."""
    obs = [o for o in observations if o.tag_id in surveyed]
    if len(obs) < 3:
        return {}
    k = np.asarray(camera_matrix, dtype=np.float64)
    out = {}
    for held in obs:
        rest = [o for o in obs if o.tag_id != held.tag_id]
        obj, img = correspondences(rest, surveyed, quarter_turns)
        pose = solve_planar_pose(obj, img, k, dist_coeffs)
        h = homography_from_pose(pose, k)
        c = undistort_points(np.array([[held.centre.x, held.centre.y]]), k, dist_coeffs)
        pred = _apply_h(h, c)[0]
        truth = np.array(surveyed[held.tag_id].centre_cm, dtype=np.float64)
        out[held.tag_id] = float(np.linalg.norm(pred - truth))
    return out
