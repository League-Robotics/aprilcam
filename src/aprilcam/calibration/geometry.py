"""Pure-geometry helpers for homography-derived deskew.

This module is a leaf: no I/O, no detection, no OpenCV detector objects —
only NumPy linear algebra plus :func:`cv2.getPerspectiveTransform`. It
provides the two pieces needed to deskew a playfield from *saved* geometry
(homography + physical dimensions) without any live ArUco corner detection:

- :func:`corner_pixels_from_homography` recovers the four playfield-corner
  pixel positions from a homography and the physical playfield size, by
  mapping the world corners ``(0,0),(W,0),(W,H),(0,H)`` back through ``H⁻¹``.
- :func:`metric_deskew_matrix` builds the perspective warp that maps a source
  polygon to a **metric** top-down rectangle of size
  ``(round(W·px_per_cm), round(H·px_per_cm))``.

The metric warp is single-sourced here so that ``PlayfieldBoundary.deskew`` /
``PlayfieldBoundary.get_deskew_matrix`` and ``PlayfieldDisplay._update_deskew``
all share identical math.

Conventions
-----------
- Polygon corner order is **UL, UR, LR, LL** (clockwise from the upper-left),
  matching ``PlayfieldBoundary``.
- World corners map to that order as
  ``UL=(0,0)``, ``UR=(W,0)``, ``LR=(W,H)``, ``LL=(0,H)`` (cm).
- A *homography* ``H`` maps source pixels to world cm:
  ``[x, y, w]ᵀ = H · [u, v, 1]ᵀ`` with ``(x/w, y/w)`` the world point.
  Its inverse ``H⁻¹`` maps world cm back to source pixels.
"""

from __future__ import annotations

from typing import Tuple

import numpy as np

try:  # OpenCV is required for the warp matrix; import lazily-tolerant.
    import cv2 as cv
except Exception:  # pragma: no cover - exercised only when cv2 is absent
    cv = None  # type: ignore

# Default deskew resolution, in output pixels per centimetre of playfield.
#
# Chosen so a typical playfield (~100 cm wide) deskews to roughly the source
# resolution (~960-1920 px wide): 100 cm * 10 px/cm = 1000 px. This keeps the
# metric top-down view close to the native frame size without up- or
# down-sampling aggressively. Override per call (or via config) as needed.
DEFAULT_PX_PER_CM: float = 10.0

# Default movement-invalidation threshold, in source pixels.
#
# When a static reference marker (ArUco corner or AprilTag 1) is detected live
# but its pixel position has drifted from the stored calibration-time position
# by more than this many pixels, the camera is assumed to have moved and the
# static-deskew assumption is invalidated.  Chosen large enough to absorb
# per-frame detection jitter / sub-pixel wobble, small enough to catch a real
# bump.  Override per :class:`PlayfieldBoundary` (or via config) as needed.
DEFAULT_MOVEMENT_THRESHOLD_PX: float = 25.0


def _world_corners(width_cm: float, height_cm: float) -> np.ndarray:
    """Return the four world corners (cm) in UL, UR, LR, LL order."""
    W = float(width_cm)
    H = float(height_cm)
    return np.array(
        [[0.0, 0.0], [W, 0.0], [W, H], [0.0, H]],
        dtype=np.float64,
    )


def corner_pixels_from_homography(
    H: np.ndarray,
    width_cm: float,
    height_cm: float,
) -> np.ndarray:
    """Map the four playfield world corners back to source pixels via ``H⁻¹``.

    Args:
        H: 3x3 homography mapping source pixels -> world cm.
        width_cm: Playfield width in cm (world X extent).
        height_cm: Playfield height in cm (world Y extent).

    Returns:
        A ``(4, 2)`` float32 array of pixel positions in UL, UR, LR, LL order,
        corresponding to world corners ``(0,0),(W,0),(W,H),(0,H)``.

    Raises:
        numpy.linalg.LinAlgError: if ``H`` is singular.
    """
    Hm = np.asarray(H, dtype=np.float64).reshape(3, 3)
    H_inv = np.linalg.inv(Hm)
    world = _world_corners(width_cm, height_cm)
    out = np.empty((4, 2), dtype=np.float32)
    for i, (wx, wy) in enumerate(world):
        vec = H_inv @ np.array([wx, wy, 1.0], dtype=np.float64)
        out[i, 0] = vec[0] / vec[2]
        out[i, 1] = vec[1] / vec[2]
    return out


def metric_deskew_matrix(
    poly: np.ndarray,
    width_cm: float,
    height_cm: float,
    px_per_cm: float = DEFAULT_PX_PER_CM,
) -> Tuple[np.ndarray, Tuple[int, int]]:
    """Build the warp mapping *poly* to a metric top-down rectangle.

    The destination is a rectangle whose size is a deterministic function of
    the physical playfield dimensions and ``px_per_cm`` — **not** of the source
    polygon's edge lengths. The four source corners (UL, UR, LR, LL) map to::

        (0, 0), (W·s, 0), (W·s, H·s), (0, H·s)

    where ``s = px_per_cm`` and ``(W, H)`` are ``(width_cm, height_cm)``.

    Args:
        poly: Source polygon, shape ``(4, 2)``, in UL, UR, LR, LL order.
        width_cm: Playfield width in cm.
        height_cm: Playfield height in cm.
        px_per_cm: Output resolution in pixels per cm (default
            :data:`DEFAULT_PX_PER_CM`).

    Returns:
        ``(M, (out_w, out_h))`` where ``M`` is the 3x3 perspective transform
        and ``(out_w, out_h) == (round(W·px_per_cm), round(H·px_per_cm))``.

    Raises:
        RuntimeError: if OpenCV is unavailable.
        ValueError: if the derived output size is degenerate.
    """
    if cv is None:  # pragma: no cover - exercised only when cv2 is absent
        raise RuntimeError("metric_deskew_matrix requires opencv (cv2)")

    s = float(px_per_cm)
    out_w = int(round(float(width_cm) * s))
    out_h = int(round(float(height_cm) * s))
    if out_w < 1 or out_h < 1:
        raise ValueError(
            f"metric deskew size degenerate: ({out_w}, {out_h}) from "
            f"W={width_cm} H={height_cm} px_per_cm={px_per_cm}"
        )

    src = np.asarray(poly, dtype=np.float32).reshape(4, 2)
    dst = np.array(
        [[0.0, 0.0], [out_w, 0.0], [out_w, out_h], [0.0, out_h]],
        dtype=np.float32,
    )
    M = cv.getPerspectiveTransform(src, dst)
    return M, (out_w, out_h)
