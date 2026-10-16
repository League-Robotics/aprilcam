"""Daemon-only view collector: which detected boards become kept views.

Pure computation per frame (cv2 only for IPPE pose and the Laplacian); no
I/O, no threads, no module-level state. All running state (previous-frame
corners, the camera's best sharpness, kept views) lives on the instance, so
a new collector is a fresh calibration session. Sessions keep per-view
corner arrays only, never frames.

The thresholds are named constants rather than request parameters; their
rationale is in docs/knowledge/2026-10-15-charuco-intrinsics-calibration-
lessons.md, "Detection gotchas" 4-6.
"""
from __future__ import annotations

import math
from typing import Optional, Sequence

import cv2
import numpy as np

from aprilcam.intrinsics.interfaces import (
    BoardDetection,
    BoardSpec,
    CoverageSummary,
    KeptView,
    RejectReason,
    ViewVerdict,
)

#: Fewer detected corners than this cannot constrain a view at all.
MIN_CORNERS = 12
#: Minimum views before a calibration solve is worthwhile (used by sessions).
MIN_VIEWS = 12
#: Tilt/azimuth are trusted only with at least this many corners...
POSE_MIN_CORNERS = 18
#: ...and only when the runner-up IPPE solution is clearly worse than the
#: best. Near fronto-parallel the two planar solutions are a mirror pair with
#: near-equal error, so the reported tilt would be a coin flip (gotcha 4).
POSE_AMBIGUITY_RATIO = 1.3
#: Mean corner displacement vs the previous frame above which the board is
#: moving. 2.5 px was far too strict on real cameras; 12 px works (gotcha 6).
MAX_MOTION_PX = 12.0
#: Absolute Laplacian variance does not transfer between sensors, so blur is
#: judged against this fraction of the camera's own running best (gotcha 5).
BLUR_FRACTION = 0.45
#: Novel when mean corner shift vs every kept view is at least this...
MIN_SHIFT_PX = 25.0
#: ...or the (trusted) tilt differs from every kept view by at least this.
MIN_TILT_DELTA_DEG = 10.0
#: Views at least this tilted count toward tilt-direction coverage.
TILTED_MIN_DEG = 15.0
#: Outer bands are the outer 40 % of each axis (centre band 20 %); coverage is
#: by board extent -- every region any corner reaches -- not the centroid.
EDGE_FRACTION = 0.4
#: Margin (px) around the board for the sharpness ROI.
ROI_PAD_PX = 10
#: Common corners needed to compare two observations of the same board.
MIN_COMMON = 4

REGION_NAMES = ("NW", "N", "NE", "W", "C", "E", "SW", "S", "SE")


def _region_index(x: float, y: float, w: int, h: int) -> int:
    col = 0 if x < EDGE_FRACTION * w else (2 if x > (1 - EDGE_FRACTION) * w else 1)
    row = 0 if y < EDGE_FRACTION * h else (2 if y > (1 - EDGE_FRACTION) * h else 1)
    return row * 3 + col


def _direction(azimuth_deg: float) -> str:
    """Image-plane direction (y down) the board normal leans toward."""
    a = azimuth_deg % 360.0
    if a < 45 or a >= 315:
        return "E"
    if a < 135:
        return "S"
    if a < 225:
        return "W"
    return "N"


def _mean_shift(a: dict, b: dict) -> Optional[float]:
    common = [i for i in a if i in b]
    if len(common) < MIN_COMMON:
        return None
    return float(np.mean([math.hypot(a[i][0] - b[i][0], a[i][1] - b[i][1])
                          for i in common]))


def object_points(spec: BoardSpec, ids: Sequence[int]) -> np.ndarray:
    """Board-plane (z = 0) positions in metres of ChArUco corner ids.

    The single id -> grid mapping shared by the collector's pose estimate and
    the solver: corner ``i`` sits at column ``i % (cols-1)``, row
    ``i // (cols-1)`` of the inner-corner lattice.
    """
    inner = spec.cols - 1
    sq = spec.square_mm / 1000.0
    return np.array([[((i % inner) + 1) * sq, ((i // inner) + 1) * sq, 0.0]
                     for i in ids], dtype=np.float64)


class ViewCollector:
    """Per-session decision maker over :class:`BoardDetection` records."""

    def __init__(self, spec: BoardSpec) -> None:
        self._spec = spec
        self._max_sharp = 0.0
        self._prev: list[tuple[np.ndarray, dict]] = []
        self._views: list[KeptView] = []
        self._view_pts: list[dict] = []
        self._image_size: Optional[tuple[int, int]] = None

    # ---- public -------------------------------------------------------------

    @property
    def views(self) -> tuple[KeptView, ...]:
        return tuple(self._views)

    def offer(self, gray, boards: Sequence[BoardDetection], ts: float) -> list[ViewVerdict]:
        """Judge each board in *gray*; one verdict per board, in order."""
        h, w = gray.shape[:2]
        self._image_size = (w, h)
        cur: list[tuple[np.ndarray, dict]] = []
        verdicts: list[ViewVerdict] = []
        for board in boards:
            pts = {int(i): (float(c[0]), float(c[1]))
                   for i, c in zip(board.ids, board.corners)}
            arr = np.array(board.corners, dtype=np.float64).reshape(-1, 2)
            if len(arr) == 0:
                verdicts.append(ViewVerdict(accepted=False,
                                            reason=RejectReason.TOO_FEW_CORNERS))
                continue
            cen = arr.mean(axis=0)
            moving = self._is_moving(cen, pts)
            sharp = self._sharpness(gray, arr)
            self._max_sharp = max(self._max_sharp, sharp)
            blurry = sharp < BLUR_FRACTION * self._max_sharp
            cur.append((cen, pts))
            verdicts.append(self._judge(gray, board, pts, arr, moving, blurry, ts))
        self._prev = cur
        return verdicts

    def coverage(self) -> CoverageSummary:
        w, h = self._image_size or (0, 0)
        regions = [0] * 9
        dirs = {"N": 0, "E": 0, "S": 0, "W": 0}
        tilted = 0
        for v in self._views:
            hit = {_region_index(x, y, w, h) for x, y in v.corners}
            for r in hit:
                regions[r] += 1
            if (v.tilt_deg is not None and v.azimuth_deg is not None
                    and v.tilt_deg >= TILTED_MIN_DEG):
                tilted += 1
                dirs[_direction(v.azimuth_deg)] += 1
        return CoverageSummary(view_count=len(self._views), region_counts=tuple(regions),
                               tilt_directions=dirs, tilted_count=tilted)

    # ---- gates --------------------------------------------------------------

    def _is_moving(self, cen: np.ndarray, pts: dict) -> bool:
        if not self._prev:
            return True  # need a previous frame to judge steadiness
        _, prev_pts = min(self._prev, key=lambda e: float(np.linalg.norm(e[0] - cen)))
        shift = _mean_shift(pts, prev_pts)
        return shift is None or shift > MAX_MOTION_PX

    @staticmethod
    def _sharpness(gray, arr: np.ndarray) -> float:
        x0, y0 = np.floor(arr.min(axis=0)).astype(int)
        x1, y1 = np.ceil(arr.max(axis=0)).astype(int)
        roi = gray[max(0, y0 - ROI_PAD_PX):y1 + ROI_PAD_PX,
                   max(0, x0 - ROI_PAD_PX):x1 + ROI_PAD_PX]
        if roi.size == 0:
            return 0.0
        return float(cv2.Laplacian(roi, cv2.CV_64F).var())

    def _judge(self, gray, board, pts, arr, moving, blurry, ts) -> ViewVerdict:
        n = len(board.ids)
        if n < MIN_CORNERS:
            return ViewVerdict(accepted=False, reason=RejectReason.TOO_FEW_CORNERS,
                               corner_count=n)
        if moving:
            return ViewVerdict(accepted=False, reason=RejectReason.MOVING, corner_count=n)
        if blurry:
            return ViewVerdict(accepted=False, reason=RejectReason.BLURRY, corner_count=n)
        h, w = gray.shape[:2]
        pose = self._pose(board, w, h)
        confident = pose is not None and pose[2]
        tilt = pose[0] if confident else None
        az = pose[1] if confident else None
        if not self._is_novel(pts, tilt):
            # Without a trusted tilt the tilt-change route to novelty is closed.
            reason = (RejectReason.AMBIGUOUS_POSE if not confident
                      else RejectReason.NOT_NOVEL)
            return ViewVerdict(accepted=False, reason=reason, corner_count=n,
                               tilt_deg=tilt, azimuth_deg=az, pose_confident=confident)
        extent = (float(arr[:, 0].min()), float(arr[:, 1].min()),
                  float(arr[:, 0].max()), float(arr[:, 1].max()))
        self._views.append(KeptView(
            corners=tuple((float(x), float(y)) for x, y in arr),
            ids=tuple(int(i) for i in board.ids),
            tilt_deg=tilt, azimuth_deg=az, extent=extent, timestamp=float(ts)))
        self._view_pts.append(pts)
        return ViewVerdict(accepted=True, corner_count=n, tilt_deg=tilt,
                           azimuth_deg=az, pose_confident=confident)

    def _is_novel(self, pts: dict, tilt: Optional[float]) -> bool:
        for kept, kpts in zip(self._views, self._view_pts):
            shift = _mean_shift(pts, kpts)
            if shift is None:
                continue
            dtilt = (abs(tilt - kept.tilt_deg)
                     if tilt is not None and kept.tilt_deg is not None else 0.0)
            if shift < MIN_SHIFT_PX and dtilt < MIN_TILT_DELTA_DEG:
                return False
        return True

    # ---- pose ---------------------------------------------------------------

    def _object_points(self, ids: Sequence[int]) -> np.ndarray:
        return object_points(self._spec, ids)

    def _pose(self, board: BoardDetection, w: int, h: int):
        """``(tilt_deg, azimuth_deg, confident)`` from IPPE with a guessed K."""
        obj = self._object_points(board.ids)
        img = np.array(board.corners, dtype=np.float64).reshape(-1, 2)
        if len(obj) < 6:
            return None
        k = np.array([[w, 0, w / 2.0], [0, w, h / 2.0], [0, 0, 1]], dtype=np.float64)
        try:
            n_sol, rvecs, _tvecs, errs = cv2.solvePnPGeneric(
                obj, img, k, np.zeros((4, 1), dtype=np.float64),
                flags=cv2.SOLVEPNP_IPPE)
        except cv2.error:
            return None
        if n_sol < 1:
            return None
        errs = np.asarray(errs).ravel()
        rot, _ = cv2.Rodrigues(rvecs[0])
        normal = rot[:, 2]
        if normal[2] > 0:  # orient toward the camera so azimuth is consistent
            normal = -normal
        tilt = math.degrees(math.acos(min(1.0, abs(float(normal[2])))))
        az = math.degrees(math.atan2(float(normal[1]), float(normal[0])))
        confident = len(obj) >= POSE_MIN_CORNERS and (
            n_sol < 2 or float(errs[1]) > POSE_AMBIGUITY_RATIO * max(float(errs[0]), 1e-6))
        return tilt, az, confident
