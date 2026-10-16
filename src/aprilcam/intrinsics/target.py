"""ChArUco calibration-target observer (daemon-only; imports cv2).

Turns a grayscale frame plus the playfield definition's
:class:`~aprilcam.config.interfaces.CalibrationTarget` into field-frame
correspondences ``[FieldCorner(field_cm, pixel)]`` for the planar-pose solve
(sprint 045). It reuses sprint 044's :class:`BoardDetector` (2x upscale,
layout lock) and :func:`collector.object_points` (the one id -> lattice
mapping), then applies the declared placement.

The ``Calibrator`` never imports this module: ``DaemonService`` constructs a
:class:`ChArUcoTargetObserver` and injects it (sprint.md Decision 6). It
returns :class:`TargetCorner`, shape-compatible with ``calibration.pose.
FieldCorner`` (``field_cm`` and ``pixel``), which ``Calibrator`` reads by
attribute -- so neither package imports the other.

Placement convention (the single y flip lives in :func:`board_to_field_cm`):
the board lies flat on the field. ``rotation_deg`` 0 is upright -- its top
edge toward north, lattice rows increasing southward, columns eastward -- and
a positive rotation turns it counter-clockwise seen from above (the same sense
as ``calibration.pose``'s marker quarter turns). The board *centre* is at
``(centre_x_cm, centre_y_cm)``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

import numpy as np

from aprilcam.config.interfaces import CalibrationTarget
from aprilcam.intrinsics.collector import object_points
from aprilcam.intrinsics.detector import BoardDetector
from aprilcam.intrinsics.interfaces import BoardSpec
from aprilcam.types import PixelPoint


@dataclass(frozen=True, kw_only=True)
class TargetCorner:
    """One board corner: its field position (cm) and its raw pixel."""

    field_cm: Tuple[float, float]
    pixel: PixelPoint


def board_to_field_cm(
    spec: BoardSpec, ids: Sequence[int], target: CalibrationTarget
) -> np.ndarray:
    """(N, 2) field-frame cm of ChArUco corner *ids* under *target*'s placement.

    ``object_points`` is board metres with the origin at the board's top-left
    and y increasing down the lattice. Re-centre on the board, flip y (down ->
    north), rotate counter-clockwise by ``rotation_deg``, translate to the
    declared centre.
    """
    pts = object_points(spec, ids)[:, :2] * 100.0  # metres -> cm
    half = np.array([spec.cols * spec.square_mm / 20.0, spec.rows * spec.square_mm / 20.0])
    local = pts - half
    local[:, 1] = -local[:, 1]  # the single board-y-down -> field-y-north flip
    t = math.radians(target.rotation_deg)
    c, s = math.cos(t), math.sin(t)
    rot = np.array([[c, -s], [s, c]])
    out = local @ rot.T
    out[:, 0] += target.centre_x_cm
    out[:, 1] += target.centre_y_cm
    return out


class ChArUcoTargetObserver:
    """Callable ``(gray_frame, CalibrationTarget) -> [TargetCorner]``."""

    def __init__(self) -> None:
        self._detectors: Dict[str, BoardDetector] = {}

    def _detector(self, spec: BoardSpec) -> BoardDetector:
        key = str(spec)
        det = self._detectors.get(key)
        if det is None:
            det = self._detectors[key] = BoardDetector(spec)
        return det

    def __call__(self, gray: np.ndarray, target: CalibrationTarget) -> List[TargetCorner]:
        if target.kind != "charuco":
            raise ValueError(f"unsupported calibration target kind {target.kind!r}")
        spec = BoardSpec.parse(target.board)
        boards = self._detector(spec).detect(gray)
        if not boards:
            return []
        best = max(boards, key=lambda b: len(b.ids))
        field = board_to_field_cm(spec, best.ids, target)
        return [
            TargetCorner(
                field_cm=(float(f[0]), float(f[1])),
                pixel=PixelPoint(x=float(p[0]), y=float(p[1])),
            )
            for f, p in zip(field, best.corners)
        ]
