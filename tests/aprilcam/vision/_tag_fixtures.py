"""Synthetic tag image generation for detector tests -- no ``aprilcam taggen`` yet.

v2's CLI has no ``taggen`` subcommand (that logic exists only in
``archive/src/aprilcam/tags/taggen.py`` / ``archive/src/aprilcam/cli/
taggen_cli.py``, pending its own future ticket), so ticket 021-003's
"equivalent local helper" is this module. It renders AprilTag 36h11 /
ArUco 4x4 markers directly with ``cv2.aruco.generateImageMarker`` -- the
same primitive archive's ``taggen.render_tag`` used -- composited onto a
plain white canvas with a quiet-zone border (ArUco/AprilTag detection
needs a white margin around the marker's own black border to find the
edge) and, optionally, rotated by a known angle.

This is not a test module itself (no ``test_*`` functions; pytest will
not collect it) -- it is imported by ``test_detector.py``.
"""

from __future__ import annotations

import cv2 as cv
import numpy as np

from aprilcam.types import TagFamily

#: Same family -> dictionary mapping as ``vision/detector.py`` -- deliberately
#: re-declared rather than imported, so these tests exercise the detector
#: against markers generated independently of its own internal mapping
#: table (importing the detector's private ``_DICTIONARY_BY_FAMILY` would
#: make a bug in that table invisible to the tests meant to catch it).
DICTIONARY_BY_FAMILY = {
    TagFamily.APRILTAG: cv.aruco.DICT_APRILTAG_36h11,
    TagFamily.ARUCO: cv.aruco.DICT_4X4_50,
}


def render_marker(family: TagFamily, tag_id: int, *, size: int = 200) -> np.ndarray:
    """A single tag rendered at its canonical (unrotated) print orientation.

    *size* is the marker's own pixel width/height with no quiet zone --
    callers needing a detectable image should composite the result via
    :func:`composite_on_canvas`, which adds the required white border.
    """
    dictionary = cv.aruco.getPredefinedDictionary(DICTIONARY_BY_FAMILY[family])
    marker: np.ndarray = cv.aruco.generateImageMarker(dictionary, tag_id, size)
    return marker


def composite_on_canvas(
    marker: np.ndarray,
    *,
    canvas_size: int = 600,
    quiet_ratio: float = 0.3,
    rotation_deg: float = 0.0,
) -> np.ndarray:
    """*marker* centred on a white canvas, with a quiet-zone border and an optional rotation.

    ``rotation_deg`` uses ``cv2.getRotationMatrix2D``'s convention
    (positive = counter-clockwise as the image is displayed -- verified
    directly during this ticket's corner-order investigation by
    transforming a known point and checking which way it moved) applied
    around the canvas centre. This is what lets the corner-order tests
    apply a *known* physical rotation to a tag and check which detected
    corner index tracks the front edge -- exactly the empirical check
    ``vision/detector.py``'s module docstring describes.
    """
    h, w = marker.shape[:2]
    pad = int(round(min(h, w) * quiet_ratio))
    bordered = np.full((h + 2 * pad, w + 2 * pad), 255, dtype=np.uint8)
    bordered[pad : pad + h, pad : pad + w] = marker

    bh, bw = bordered.shape
    canvas = np.full((canvas_size, canvas_size), 255, dtype=np.uint8)
    y0, x0 = (canvas_size - bh) // 2, (canvas_size - bw) // 2
    canvas[y0 : y0 + bh, x0 : x0 + bw] = bordered

    if rotation_deg == 0.0:
        return canvas
    center = (canvas_size / 2, canvas_size / 2)
    matrix = cv.getRotationMatrix2D(center, rotation_deg, 1.0)
    rotated: np.ndarray = cv.warpAffine(
        canvas, matrix, (canvas_size, canvas_size), borderValue=255
    )
    return rotated


def synthetic_tag_image(
    family: TagFamily,
    tag_id: int,
    *,
    rotation_deg: float = 0.0,
    size: int = 200,
    canvas_size: int = 600,
) -> np.ndarray:
    """One convenience call: render + composite + (optionally) rotate.

    Returns a grayscale ``uint8`` image, ready to pass straight to
    :meth:`aprilcam.vision.detector.TagDetector.detect` (no BGR
    conversion needed -- the canvas is synthesised directly in grayscale).
    """
    return composite_on_canvas(
        render_marker(family, tag_id, size=size),
        canvas_size=canvas_size,
        rotation_deg=rotation_deg,
    )
