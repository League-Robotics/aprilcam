"""AprilTag 36h11 + ArUco 4x4 detection: pixels in, raw hits out.

Mined from v1's ``archive/src/aprilcam/core/detector.py`` (``TagDetector``/
``DetectorConfig``/``_build_detectors``) per this ticket's contract
(sprint 021-003) -- the *approach* (prebuild one ``ArucoDetector`` per
family in ``__init__``, reuse across frames; the widened
``adaptiveThreshWinSize*`` sweep and the ``april_min_wb_diff``/
``april_min_cluster_pixels``/``april_max_line_fit_mse`` earned constants
tuned against real playfield captures) is taken; the *structure* is not.
Scoped narrower than v1: exactly AprilTag 36h11 and ArUco 4x4, no
``family="all"`` option, and ``detect_inverted``/``detect_aruco_4x4`` stop
being config toggles -- inverted-marker detection is now unconditionally
on (an earned constant, not a knob) and ArUco 4x4 is always run alongside
36h11 rather than optionally.

**This module has no dependency on ``vision/pipeline.py``.** Ticket 001's
pipeline and this module's detector are two independently-testable,
independently-reusable stages (sprint.md Step 2: "changes independently").
:meth:`TagDetector.detect` takes an already-preprocessed grayscale image --
preprocessing composition is the caller's job (ticket 004's
``daemon/detection.py``, which runs ``pipeline.run_pipeline`` then this
module's ``detect``, per the sprint architecture diagram). The one piece of
information that must cross that boundary -- how much the image passed to
``detect`` was downscaled from the frame the caller actually wants pixel
coordinates in -- is a bare ``float`` (``scale``), not a ``Rescale`` import:
``pipeline.Rescale``'s own docstring states "``Rescale.scale`` divides
directly into an ``(N, 2)`` corner array for the batch case", so passing
``rescale.scale`` (or ``1.0`` when no pipeline ran) is the entire contract.
This keeps ``vision/pipeline.py`` and ``vision/detector.py`` free of any
cross-import between them.

**Raw hits only.** :meth:`TagDetector.detect` returns :class:`Detection`
objects carrying a typed :class:`~aprilcam.types.TagFamily`, the family's
native non-negative dictionary id, and the 4 corners **in the detector's
native order** -- not reordered to the design.md Sec.6 boundary/hat
contract (front-left, front-right, back-right, back-left). Building
:class:`~aprilcam.types.TagId` and any corner reordering is ticket 004's
job (``daemon/detection.py``); this module's contract stops at "pixels in,
raw hits out" (dev-plan Stage 5's "the last layer where pixels are a
legitimate output").

**No ``-(N+1)`` encoding.** v1 stored ArUco marker *N* as tid ``-(N+1)`` in
a single shared int-keyed dict to dodge colliding with AprilTag ids sharing
that dict. That hazard cannot occur here: ``Detection.family`` carries
:class:`~aprilcam.types.TagFamily` explicitly, so AprilTag 5 and ArUco 5 are
never confused, and ``Detection.number`` is always the family's native
non-negative dictionary index. Porting the encoding "to be safe" would
reintroduce exactly the leak-prone representation sprint.md's Architecture
Decision 2 replaces (see ``clasi/sprints/021-tag-detection-and-tag-queries/
sprint.md``); it is deliberately absent.

Corner-order / boundary-hat convention -- VERIFIED, not assumed
-----------------------------------------------------------------
This was sprint.md's Open Question 2 and this ticket's genuinely open
empirical item. Verification method (also exercised in
``tests/aprilcam/vision/test_detector.py``): render a synthetic tag with
``cv2.aruco.generateImageMarker`` (its canonical, un-rotated print
orientation), composite it onto a plain canvas with a quiet-zone border,
then apply a series of *known* physical rotations (0 deg, 90 deg, 180 deg,
270 deg, and a non-axis-aligned 37 deg, via ``cv2.getRotationMatrix2D`` +
``cv2.warpAffine``) before detecting.

Finding: **OpenCV's ArUco/AprilTag corner order is stable relative to the
marker's own printed pattern, not the image frame.** Detected corner index
0 is always the same physical corner of the printed marker -- its
canonical top-left corner, as drawn by ``generateImageMarker`` with zero
rotation applied -- regardless of how the marker is physically rotated in
the captured image; corners 1/2/3 are, respectively, the canonical
top-right, bottom-right, and bottom-left corners of that same fixed
pattern. This was confirmed identically for both families (36h11 and
ArUco 4x4 use the same underlying ``ArucoDetector`` machinery) across all
five tested rotations: rotating the physical tag rotates *where in the
image* corner 0 lands, never *which corner of the pattern* corner 0 is --
exactly the property pose estimation (``solvePnP``-style correspondence)
needs, and exactly what "front" tracking requires.

Neither family's bit pattern carries a marking a human would recognise as
"this edge is the front" at a glance (unlike, say, a printed arrow) --
the pattern is designed to be unambiguous to the *detector* via its
decoded bit content, not to a human eye. "Front" is therefore necessarily
a mounting convention, not an intrinsic property of the pattern, and the
only convention available without additional hardware information is the
marker's own canonical print orientation: **this module adopts "front" =
the marker's canonical top edge, i.e. the orientation ``generateImageMarker``
draws with no rotation.** Under that convention the OpenCV-native corner
order (canonical top-left, top-right, bottom-right, bottom-left) is
*already* identical, index for index, to design.md Sec.6's boundary/hat
order (front-left, front-right, back-right, back-left) -- the 0->1 edge is
the front edge in both. **No reordering is needed anywhere in this
pipeline for this convention to hold**; ticket 004's corner-to-``TagId``
composition can carry ``Detection.corners`` straight through as
``pixel_corners``.

This is a documented convention, not a law of physics: it depends on
tags actually being mounted with their canonical print orientation facing
the robot's front, which is a physical assembly choice outside this
module's control. Ticket 007's hardware pass against ``main-playfield``'s
real, physically-mounted tags is the cross-check against an assembly that
might not honour it -- this module states and verifies the convention on
synthetic ground truth; it does not and cannot verify real-world mounting.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import cv2 as cv
import numpy as np

from aprilcam.types import PixelPoint, TagFamily

from .aruco_compat import make_aruco_detector

__all__ = ["Detection", "DetectorConfig", "TagDetector"]

#: v1's widened adaptive-threshold sweep (earned against real playfield
#: captures under uneven illumination/glare) -- a wider range with a
#: smaller step than OpenCV's own defaults, unconditionally applied (not
#: a config knob; nothing in v1 or this port ever varied it).
_ADAPTIVE_THRESH_WIN_SIZE_MIN = 3
_ADAPTIVE_THRESH_WIN_SIZE_MAX = 53
_ADAPTIVE_THRESH_WIN_SIZE_STEP = 4

#: Corner-refinement mode name -> OpenCV constant. ``DetectorConfig.corner_refine``
#: unrecognised values fall back to subpixel refinement (v1's own default and
#: fallback behaviour, unchanged).
_CORNER_REFINE_MODES = {
    "none": cv.aruco.CORNER_REFINE_NONE,
    "contour": cv.aruco.CORNER_REFINE_CONTOUR,
    "subpix": cv.aruco.CORNER_REFINE_SUBPIX,
}

#: Family -> OpenCV predefined-dictionary constant. Fixed to exactly these
#: two this sprint (issue scope: "AprilTag 36h11 and ArUco 4x4, not v1's
#: full four-AprilTag-family option").
#:
#: ``tags/generator.py`` has an identical mapping, deliberately
#: duplicated rather than imported -- ``tags/`` is a standalone,
#: purely-offline utility (design.md §4.10) and must not gain an import
#: edge to this daemon-only detection module (sprint 028 sprint.md,
#: Design Rationale Step 6). If one changes, check the other.
_DICTIONARY_BY_FAMILY = {
    TagFamily.APRILTAG: cv.aruco.DICT_APRILTAG_36h11,
    TagFamily.ARUCO: cv.aruco.DICT_4X4_50,
}


@dataclass(frozen=True, kw_only=True, eq=False)
class Detection:
    """One raw tag hit, as the detector saw it -- pixel space, no calibration.

    ``eq``/``hash`` are suppressed (rather than the frozen-dataclass
    default of deriving both from field values): ``corners`` is a numpy
    array, and numpy's ``==`` returns an elementwise array rather than a
    bool, which would make an auto-generated ``__eq__`` raise on
    ``ValueError: truth value of an array is ambiguous`` the moment
    anything compared two ``Detection``\\ s. Tests compare fields
    individually (``d.number``, ``np.testing.assert_allclose(d.corners,
    ...)``) instead.

    Attributes:
        family: Which dictionary this hit was decoded from --
            :attr:`~aprilcam.types.TagFamily.APRILTAG` or
            :attr:`~aprilcam.types.TagFamily.ARUCO`. Part of the tag's
            identity, never inferred from ``number`` (two families can and
            do share numeric ids on the same playfield).
        number: The family's native, non-negative dictionary index. Never
            a v1-style ``-(N+1)`` encoding -- see the module docstring.
        corners: ``(4, 2)`` float32 array, in the caller's requested pixel
            space (see :meth:`TagDetector.detect`\\ 's ``scale``
            parameter), in the detector's **native** order -- verified
            equal to the design.md Sec.6 boundary/hat order
            (front-left, front-right, back-right, back-left) under the
            canonical-print-orientation convention documented at module
            level, but this module does not itself relabel or reorder
            them; that composition is ticket 004's.
        center: The corners' centroid, as a convenience for callers that
            just want "where is it" without averaging the quad themselves.
            Derived, not authoritative -- ``corners`` is the source of
            truth for anything geometric (drawing, orientation).
    """

    family: TagFamily
    number: int
    corners: np.ndarray
    center: PixelPoint


@dataclass(frozen=True, kw_only=True)
class DetectorConfig:
    """Tuning knobs for :class:`TagDetector`.

    Scoped to genuinely tunable parameters only -- preprocessing fields
    (highpass/CLAHE/sharpen/``proc_width``) live in
    ``vision.pipeline.PipelineConfig``, not here (sprint.md Step 2's
    "changes independently" module boundary: a new detector parameter
    must never require a pipeline change and vice versa). Compared to
    v1's ``DetectorConfig``, ``family``/``detect_inverted``/
    ``detect_aruco_4x4`` are gone -- family selection is fixed (36h11 +
    ArUco 4x4, always both) and inverted-marker detection is
    unconditionally on inside :class:`TagDetector`, not a toggle.

    Attributes:
        quad_decimate: AprilTag/ArUco quad decimation factor (>= 1; higher
            is faster but coarser). v1 default, unchanged.
        quad_sigma: Gaussian blur sigma applied during quad detection. v1
            default (0.0 -- off), unchanged.
        corner_refine: Corner refinement mode -- ``"none"``, ``"contour"``,
            or ``"subpix"``. v1 default (``"subpix"``), unchanged.
        april_min_wb_diff: Minimum white/black intensity difference for the
            AprilTag quad detector. Earned constant from v1's real-playfield
            tuning; only applied where the installed OpenCV exposes the
            attribute (``aprilTagMinWhiteBlackDiff`` is not on every build).
        april_min_cluster_pixels: Minimum cluster size (pixels) for the
            AprilTag quad detector. Earned constant, same caveat.
        april_max_line_fit_mse: Maximum line-fit mean-squared error for the
            AprilTag quad detector. Earned constant, same caveat.
        detect_inverted: Also detect inverted (light-on-dark) markers. On by
            default (an earned constant for the live detection path), but
            with it on OpenCV reports a marker's corners biased inward by
            roughly 0.4 of a marker cell and without subpixel refinement
            (measured on synthetic high-contrast markers, sprint 045 ticket
            003: ~8 px error on a 120 px marker versus ~0.06 px with it
            off). Centres are barely affected; corner-consuming callers
            (the calibration pose solve) must set this to False.
    """

    quad_decimate: float = 1.0
    quad_sigma: float = 0.0
    corner_refine: str = "subpix"
    april_min_wb_diff: float = 3.0
    april_min_cluster_pixels: int = 5
    april_max_line_fit_mse: float = 20.0
    detect_inverted: bool = True


class TagDetector:
    """Stateless AprilTag 36h11 + ArUco 4x4 detector.

    Constructed once per :class:`DetectorConfig` (which builds both
    per-family ``ArucoDetector`` objects up front, mirroring v1's own
    "avoid per-frame overhead" comment -- building an ``ArucoDetector`` is
    not free, and ticket 004's daemon holds one ``TagDetector`` per camera
    across many detection bursts, not one per burst). :meth:`detect` then
    holds no per-call state: the same grayscale image always produces the
    same output, independent of call history (this ticket's determinism
    acceptance criterion).
    """

    def __init__(self, config: Optional[DetectorConfig] = None) -> None:
        self._config: DetectorConfig = config if config is not None else DetectorConfig()
        # Value type is genuinely `cv.aruco.ArucoDetector | _LegacyArucoDetector`
        # (aruco_compat.make_aruco_detector's return type), both of which are
        # opaque to mypy since cv2 ships no stubs (pyproject.toml's
        # `ignore_missing_imports` override) -- `Any` reflects that honestly
        # rather than asserting a shape mypy cannot check.
        self._detectors: dict[TagFamily, Any] = {
            family: self._build_family_detector(dict_id)
            for family, dict_id in _DICTIONARY_BY_FAMILY.items()
        }

    def detect(self, gray: np.ndarray, *, scale: float = 1.0) -> list[Detection]:
        """Detect AprilTag 36h11 and ArUco 4x4 markers in a preprocessed grayscale image.

        Args:
            gray: Single-channel grayscale image, already run through
                ``vision.pipeline.run_pipeline`` (or equivalent) by the
                caller -- this method does no preprocessing and no BGR
                conversion of its own (see the module docstring's
                no-cross-import-with-``pipeline.py`` decision).
            scale: The ratio (width of *gray*) / (width of the pixel space
                the caller wants corners reported in) -- pass
                ``rescale.scale`` from ``pipeline.run_pipeline``'s
                returned :class:`~aprilcam.vision.pipeline.Rescale`
                unchanged (its own docstring sanctions dividing an
                ``(N, 2)`` corner array by ``.scale`` directly, which is
                exactly what this does), or leave the default ``1.0`` when
                *gray* is already in the caller's target pixel space (no
                downscaling ran).

        Returns:
            One :class:`Detection` per tag found, empty when nothing was.
            Order is family-then-detection-order (all AprilTag hits, then
            all ArUco hits) -- callers needing a different order sort
            themselves; no ordering guarantee beyond "stable for identical
            input" is made.
        """
        results: list[Detection] = []
        for family, detector in self._detectors.items():
            corners, ids, _rejected = detector.detectMarkers(gray)
            if ids is None:
                continue
            for raw_corners, raw_id in zip(corners, ids.flatten().tolist()):
                pts = raw_corners.reshape(-1, 2).astype(np.float32)
                if scale != 1.0:
                    pts = pts / np.float32(scale)
                center = PixelPoint(x=float(pts[:, 0].mean()), y=float(pts[:, 1].mean()))
                results.append(
                    Detection(family=family, number=int(raw_id), corners=pts, center=center)
                )
        return results

    def _build_family_detector(self, dict_id: int) -> Any:
        """Construct one prebuilt, tuned ``ArucoDetector`` (or 4.6 shim) for *dict_id*."""
        cfg = self._config
        dictionary = cv.aruco.getPredefinedDictionary(dict_id)
        params = cv.aruco.DetectorParameters()

        params.adaptiveThreshWinSizeMin = _ADAPTIVE_THRESH_WIN_SIZE_MIN
        params.adaptiveThreshWinSizeMax = _ADAPTIVE_THRESH_WIN_SIZE_MAX
        params.adaptiveThreshWinSizeStep = _ADAPTIVE_THRESH_WIN_SIZE_STEP
        params.cornerRefinementMethod = _CORNER_REFINE_MODES.get(
            cfg.corner_refine, cv.aruco.CORNER_REFINE_SUBPIX
        )
        params.aprilTagQuadDecimate = float(max(1.0, cfg.quad_decimate))
        params.aprilTagQuadSigma = float(max(0.0, cfg.quad_sigma))
        # Not every OpenCV build exposes these three AprilTag quad-detector
        # knobs (archive's own hasattr guard, kept -- 4.6 in particular
        # predates some of them); skip silently where absent rather than
        # raising on an otherwise-working build.
        if hasattr(params, "aprilTagMinWhiteBlackDiff"):
            params.aprilTagMinWhiteBlackDiff = int(max(0, round(cfg.april_min_wb_diff)))
        if hasattr(params, "aprilTagMinClusterPixels"):
            params.aprilTagMinClusterPixels = int(max(1, cfg.april_min_cluster_pixels))
        if hasattr(params, "aprilTagMaxLineFitMse"):
            params.aprilTagMaxLineFitMse = float(max(1.0, cfg.april_max_line_fit_mse))
        # Earned constant, unconditionally on (not a config toggle -- see
        # the module and DetectorConfig docstrings).
        # Default on (see DetectorConfig.detect_inverted); calibration turns
        # it off because inverted detection reports corners biased ~0.4 cell
        # inward and unrefined.
        params.detectInvertedMarker = bool(cfg.detect_inverted)

        return make_aruco_detector(dictionary, params)
