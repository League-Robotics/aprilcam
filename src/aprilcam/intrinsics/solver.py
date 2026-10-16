"""Daemon-side lens solver, good-enough metric, and the next-step hint.

Pure functions over :class:`KeptView` arrays; no I/O, no threads, no state
beyond the image size a :class:`LensSolver` was told about. The metric is
described in docs/knowledge/2026-10-15-charuco-intrinsics-calibration-
lessons.md ("Good enough"): solve on everything and on random halves,
undistort a grid over the central part of the frame, absorb any projective
difference with a homography (a planar field would), and take the worst
remaining disagreement.
"""
from __future__ import annotations

import math
import random
from typing import Mapping, Optional, Sequence

import cv2
import numpy as np

from aprilcam.intrinsics.collector import MIN_VIEWS, REGION_NAMES, object_points
from aprilcam.intrinsics.interfaces import (
    BoardSpec,
    KeptView,
    LensEvaluation,
    LensSolution,
)

#: Held-out mapping residual below this (px) is good enough...
GOOD_RESIDUAL_PX = 4.0
#: ...as is a reprojection rms below this (px).
GOOD_RMS_PX = 0.6
#: Residual/correction are measured over the central fraction of the frame.
FIELD_FRACTION = 0.85
#: Fewest usable views a solve is attempted with.
MIN_SOLVE_VIEWS = 6
#: Corners a view needs to be usable for a solve.
MIN_VIEW_CORNERS = 6
#: Random half-sample solves used for the residual.
HALF_DRAWS = 3
#: Plausible focal length as a multiple of frame width (loose: ~19-118 deg hfov).
FX_RATIO_MIN = 0.3
FX_RATIO_MAX = 3.0
#: Residual reported when a half-sample solve fails outright.
FAILED_RESIDUAL_PX = 99.0

CELL_WORDS = {"NW": "TOP-LEFT", "N": "TOP", "NE": "TOP-RIGHT", "W": "LEFT",
              "C": "CENTER", "E": "RIGHT", "SW": "BOTTOM-LEFT", "S": "BOTTOM",
              "SE": "BOTTOM-RIGHT"}
DIR_WORDS = {"N": "bottom edge up", "E": "left edge up",
             "S": "top edge up", "W": "right edge up"}


def check_plausible(camera_matrix: np.ndarray, image_size: tuple[int, int]) -> Optional[str]:
    """Loose sanity gate on a full solve; ``None`` when plausible.

    Deliberately about focal length against frame width only -- not the
    single-view |k1| bounds, which reject legitimate wide-angle lenses.
    """
    w, h = image_size
    fx, fy = float(camera_matrix[0, 0]), float(camera_matrix[1, 1])
    if not (math.isfinite(fx) and math.isfinite(fy)):
        return "non-finite focal length"
    if not (FX_RATIO_MIN * w <= fx <= FX_RATIO_MAX * w):
        return (f"implausible focal length fx={fx:.0f}px for a {w}px-wide frame "
                f"(expected {FX_RATIO_MIN * w:.0f}-{FX_RATIO_MAX * w:.0f})")
    if fy <= 0 or not (0.5 <= fy / fx <= 2.0):
        return f"implausible fx/fy ratio ({fx:.0f}/{fy:.0f})"
    return None


class LensSolver:
    """Solve intrinsics from kept views of one board spec."""

    def __init__(self, spec: BoardSpec, image_size: Optional[tuple[int, int]] = None) -> None:
        self._spec = spec
        self._image_size = image_size

    # ---- public -------------------------------------------------------------

    def solve(self, views: Sequence[KeptView],
              image_size: Optional[tuple[int, int]] = None) -> Optional[LensSolution]:
        """Full solve (k3 fixed); ``None`` until >= 6 usable views."""
        size = self._size(image_size)
        objs, imgs = self._arrays(views)
        if len(objs) < MIN_SOLVE_VIEWS:
            return None
        fit = self._calibrate(objs, imgs, size)
        if fit is None:
            return None
        rms, k, d = fit
        fx = float(k[0, 0])
        w = size[0]
        return LensSolution(
            camera_matrix=tuple(tuple(float(v) for v in row) for row in k),
            dist_coeffs=tuple(float(v) for v in d.ravel()),
            image_size=(int(size[0]), int(size[1])),
            rms_px=float(rms), fx=fx,
            hfov_deg=math.degrees(2.0 * math.atan2(w / 2.0, fx)) if fx > 0 else 0.0,
            view_count=len(objs),
            plausibility_error=check_plausible(k, size))

    def evaluate(self, views: Sequence[KeptView],
                 image_size: Optional[tuple[int, int]] = None) -> Optional[LensEvaluation]:
        """Good-enough verdict; ``None`` if the full solve is not possible."""
        size = self._size(image_size)
        objs, imgs = self._arrays(views)
        n = len(objs)
        if n < MIN_SOLVE_VIEWS:
            return None
        sol = self.solve(views, size)
        if sol is None:
            return None
        k = np.array(sol.camera_matrix)
        d = np.array(sol.dist_coeffs)
        grid = _field_grid(size)
        ref = cv2.undistortPoints(grid, k, d, P=k).reshape(-1, 2)
        correction = _homography_residual(grid.reshape(-1, 2), ref)
        residual: Optional[float] = None
        if n >= MIN_VIEWS:
            rng = random.Random(n)
            residual = 0.0
            for _ in range(HALF_DRAWS):
                sel = rng.sample(range(n), n // 2)
                fit = self._calibrate([objs[i] for i in sel], [imgs[i] for i in sel], size)
                if fit is None:
                    residual = FAILED_RESIDUAL_PX
                    continue
                _, k2, d2 = fit
                alt = cv2.undistortPoints(grid, k2, d2, P=k2).reshape(-1, 2)
                residual = max(residual, _homography_residual(alt, ref))
        good = (residual is not None and sol.rms_px < GOOD_RMS_PX
                and residual < GOOD_RESIDUAL_PX and sol.plausibility_error is None)
        return LensEvaluation(solution=sol, rms_px=sol.rms_px, residual_px=residual,
                              correction_px=correction, good=bool(good))

    # ---- internals ----------------------------------------------------------

    def _size(self, image_size) -> tuple[int, int]:
        size = image_size or self._image_size
        if size is None:
            raise ValueError("image_size is required (none given to solver or call)")
        self._image_size = (int(size[0]), int(size[1]))
        return self._image_size

    def _arrays(self, views: Sequence[KeptView]):
        objs, imgs = [], []
        for v in views:
            if len(v.ids) < MIN_VIEW_CORNERS:
                continue
            objs.append(object_points(self._spec, v.ids).astype(np.float32))
            imgs.append(np.array(v.corners, dtype=np.float32).reshape(-1, 1, 2))
        return objs, imgs

    @staticmethod
    def _calibrate(objs, imgs, size):
        try:
            rms, k, d, _, _ = cv2.calibrateCamera(
                objs, imgs, size, None, None, flags=cv2.CALIB_FIX_K3)
        except cv2.error:
            return None
        return float(rms), k, d


def _field_grid(size: tuple[int, int]) -> np.ndarray:
    w, h = size
    m = (1 - FIELD_FRACTION) / 2
    gx, gy = np.meshgrid(np.linspace(m * w, (1 - m) * w, 7),
                         np.linspace(m * h, (1 - m) * h, 5))
    return np.stack([gx.ravel(), gy.ravel()], axis=1).astype(np.float64).reshape(-1, 1, 2)


def _homography_residual(src: np.ndarray, dst: np.ndarray) -> float:
    """Worst remaining distance after the best homography src -> dst."""
    src = np.asarray(src, dtype=np.float64).reshape(-1, 2)
    h, _ = cv2.findHomography(src, dst, 0)
    if h is None:
        return FAILED_RESIDUAL_PX
    proj = cv2.perspectiveTransform(src.reshape(-1, 1, 2), h).reshape(-1, 2)
    return float(np.max(np.linalg.norm(proj - dst, axis=1)))


def next_hint(coverage, tilt_dirs: Mapping[str, int], tilted: int, view_count: int) -> str:
    """What the person holding the board should do next.

    *coverage* is a 9-sequence in NW..SE order or a mapping keyed by region.
    """
    cells = (dict(coverage) if isinstance(coverage, Mapping)
             else dict(zip(REGION_NAMES, coverage)))
    if view_count < 4:
        return "HOLD THE BOARD UP HIGH, TILTED, OVER THE CENTER"
    corners = [c for c in ("NW", "NE", "SW", "SE") if cells.get(c, 0) < 2]
    edges = [c for c in ("N", "E", "S", "W") if cells.get(c, 0) < 2]
    missing = [d for d in "NESW" if tilt_dirs.get(d, 0) == 0]
    if corners:
        d = missing[0] if missing else "NESW"[len(corners) % 4]
        return f"{CELL_WORDS[corners[0]]} corner, tilt it ({DIR_WORDS[d]})"
    if missing:
        return f"tilt more: {DIR_WORDS[missing[0]]}, anywhere"
    if edges:
        return f"{CELL_WORDS[edges[0]]} edge, tilted"
    if tilted < 10:
        return "a few more TILTED views, anywhere, 30-40 degrees"
    return "keep sweeping slowly, high and tilted"
