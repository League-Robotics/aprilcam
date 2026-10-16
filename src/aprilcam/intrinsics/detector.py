"""Daemon-only multi-board ChArUco detector (imports cv2).

Markers are detected on a 2x upscale with loosened candidate filters (the
cameras render markers near the detector's resolution limit), grouped into
per-board clusters, then ChArUco corners are interpolated on the *native*
image so returned coordinates are native pixels. The only state is the
board-layout lock, held per instance.
"""
from __future__ import annotations

import cv2
import numpy as np

from aprilcam.intrinsics.interfaces import (
    BoardDetection,
    BoardDetectorBase,
    BoardLayout,
    BoardSpec,
)

DETECT_SCALE = 2.0
LINK_FACTOR = 2.5          # single-linkage radius, in marker sides
MIN_CLUSTER_MARKERS = 4
#: A cluster this big is trusted enough to lock the layout.
MIN_LOCK_MARKERS = 6


def resolve_dictionary(dict_name: str):
    """Map a ``DICT_*`` name to an OpenCV aruco dictionary (daemon-side only)."""
    const = getattr(cv2.aruco, dict_name, None)
    if not isinstance(const, int):
        raise ValueError(f"unknown ArUco dictionary {dict_name!r}")
    return cv2.aruco.getPredefinedDictionary(const)


def tuned_parameters() -> "cv2.aruco.DetectorParameters":
    p = cv2.aruco.DetectorParameters()
    p.minMarkerPerimeterRate = 0.01
    p.adaptiveThreshWinSizeMin = 3
    p.adaptiveThreshWinSizeMax = 23
    p.adaptiveThreshWinSizeStep = 4
    p.polygonalApproxAccuracyRate = 0.05
    p.perspectiveRemovePixelPerCell = 8
    p.perspectiveRemoveIgnoredMarginPerCell = 0.2
    return p


def marker_clusters(corners, ids, link_factor: float = LINK_FACTOR):
    """Single-linkage groups of markers within ``link_factor`` marker sides.

    Returns ``[(corner_list, ids(n,1) int32)]`` for groups of at least
    ``MIN_CLUSTER_MARKERS`` markers with no duplicate id.
    """
    if ids is None or len(ids) == 0:
        return []
    flat_ids = np.asarray(ids).ravel()
    pts = np.array([c.reshape(4, 2) for c in corners], dtype=np.float64)
    centers = pts.mean(axis=1)
    side = float(np.median(np.linalg.norm(pts[:, 0] - pts[:, 1], axis=1)))
    thr = link_factor * side
    n = len(centers)
    label = -np.ones(n, dtype=int)
    groups = 0
    for i in range(n):
        if label[i] >= 0:
            continue
        label[i] = groups
        stack = [i]
        while stack:
            j = stack.pop()
            near = np.where((label < 0) & (np.linalg.norm(centers - centers[j], axis=1) < thr))[0]
            for m in near:
                label[m] = groups
                stack.append(int(m))
        groups += 1
    out = []
    for g in range(groups):
        idx = [i for i in range(n) if label[i] == g]
        if len(idx) < MIN_CLUSTER_MARKERS:
            continue
        gids = [int(flat_ids[i]) for i in idx]
        if len(set(gids)) != len(gids):
            continue
        out.append(([corners[i] for i in idx], np.array(gids, dtype=np.int32).reshape(-1, 1)))
    return out


class BoardDetector(BoardDetectorBase):
    """Finds every ChArUco board of one ``BoardSpec`` in a grayscale frame."""

    def __init__(self, spec: BoardSpec, *, min_corners: int = 4) -> None:
        self._spec = spec
        self._min_corners = min_corners
        dictionary = resolve_dictionary(spec.dict_name)
        self._aruco = cv2.aruco.ArucoDetector(dictionary, tuned_parameters())
        self._charuco = {
            BoardLayout.CURRENT: self._build(dictionary, legacy=False),
            BoardLayout.LEGACY: self._build(dictionary, legacy=True),
        }
        self._layout = BoardLayout.UNKNOWN

    def _build(self, dictionary, *, legacy: bool):
        board = cv2.aruco.CharucoBoard(
            (self._spec.cols, self._spec.rows),
            self._spec.square_mm / 1000.0,
            self._spec.marker_mm / 1000.0,
            dictionary,
        )
        board.setLegacyPattern(legacy)
        return cv2.aruco.CharucoDetector(board)

    @property
    def spec(self) -> BoardSpec:
        return self._spec

    @property
    def layout(self) -> BoardLayout:
        return self._layout

    def _interpolate(self, layout: BoardLayout, gray, marker_corners, marker_ids):
        c, i, _, _ = self._charuco[layout].detectBoard(
            gray, markerCorners=marker_corners, markerIds=marker_ids)
        n = 0 if i is None else len(i)
        return n, c, i

    def detect(self, gray) -> list[BoardDetection]:
        up = cv2.resize(gray, None, fx=DETECT_SCALE, fy=DETECT_SCALE,
                        interpolation=cv2.INTER_CUBIC)
        mc, mids, _ = self._aruco.detectMarkers(up)
        mc = [c / DETECT_SCALE for c in mc]
        found: list[BoardDetection] = []
        for cc, ci in marker_clusters(mc, mids):
            if self._layout is BoardLayout.UNKNOWN:
                order = [BoardLayout.CURRENT, BoardLayout.LEGACY]
            else:
                order = [self._layout]
            for layout in order:
                n, chc, chi = self._interpolate(layout, gray, cc, ci)
                if n >= self._min_corners:
                    if self._layout is BoardLayout.UNKNOWN and len(ci) >= MIN_LOCK_MARKERS:
                        self._layout = layout
                    found.append(BoardDetection(
                        corners=tuple((float(x), float(y)) for x, y in chc.reshape(-1, 2)),
                        ids=tuple(int(k) for k in chi.ravel()),
                        marker_count=len(ci),
                        layout=layout,
                    ))
                    break
        return found
