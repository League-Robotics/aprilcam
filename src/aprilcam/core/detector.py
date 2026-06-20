"""Pure stateless AprilTag / ArUco detection engine.

This module contains no camera access, no threading, and no ring
buffers.  ``TagDetector`` accepts a BGR frame, runs preprocessing and
detection, and returns a list of :class:`Detection` objects.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import cv2 as cv
import numpy as np

from ..vision.aruco_compat import make_aruco_detector


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Detection:
    """Raw detection result for a single tag in a single frame.

    Attributes:
        id: Tag identifier as reported by the detector.
        center: Sub-pixel center ``(x, y)`` in the original frame's pixel
            space (accounting for any downscaling applied during detection).
        corners: ``(4, 2)`` float32 array of corner coordinates in the
            original frame's pixel space, ordered top-left → top-right →
            bottom-right → bottom-left.
        family: Dictionary family name, e.g. ``"36h11"`` or ``"aruco_4x4"``.
    """

    id: int
    center: tuple[float, float]
    corners: np.ndarray  # shape (4, 2), float32
    family: str


@dataclass
class DetectorConfig:
    """Configuration for :class:`TagDetector`.

    All fields have sensible defaults so ``DetectorConfig()`` works
    out-of-the-box.

    Attributes:
        family: AprilTag family or ``"all"`` to run all four families
            (16h5, 25h9, 36h10, 36h11) in sequence.
        proc_width: If > 0 the input frame is downscaled so that its width
            equals *proc_width* before detection; results are scaled back.
            0 disables downscaling.
        quad_decimate: AprilTag quad decimation factor (≥ 1; higher is
            faster but coarser).
        quad_sigma: Gaussian blur sigma applied during quad detection.
        corner_refine: Corner refinement mode — ``"none"``, ``"contour"``,
            or ``"subpix"``.
        detect_inverted: Also detect white-on-black (inverted) markers.
        detect_aruco_4x4: Additionally run a 4x4 ArUco detector.
        use_highpass: Apply illumination flattening (highpass filter) before
            detection.  Recommended; on by default.
        highpass_ksize: Kernel size for the Gaussian used in highpass
            filtering.  Must be odd; automatically incremented if even.
        use_clahe: Apply CLAHE histogram equalisation before detection.
        use_sharpen: Apply a light unsharp-mask sharpening kernel before
            detection.
        april_min_wb_diff: Minimum white/black intensity difference for the
            AprilTag quad detector.
        april_min_cluster_pixels: Minimum cluster size (pixels) for the
            AprilTag quad detector.
        april_max_line_fit_mse: Maximum line-fit mean-squared error for the
            AprilTag quad detector.
    """

    family: str = "36h11"
    proc_width: int = 0
    quad_decimate: float = 1.0
    quad_sigma: float = 0.0
    corner_refine: str = "subpix"
    detect_inverted: bool = True
    detect_aruco_4x4: bool = False
    use_highpass: bool = True
    highpass_ksize: int = 51
    use_clahe: bool = False
    use_sharpen: bool = False
    april_min_wb_diff: float = 3.0
    april_min_cluster_pixels: int = 5
    april_max_line_fit_mse: float = 20.0


# ---------------------------------------------------------------------------
# TagDetector
# ---------------------------------------------------------------------------

class TagDetector:
    """Stateless AprilTag / ArUco detection engine.

    ``TagDetector`` is constructed once (which builds the detector objects)
    and then :meth:`detect` is called per-frame.  The instance holds no
    per-frame state — calling :meth:`detect` twice with the same frame
    returns equivalent results.

    Example::

        detector = TagDetector()
        detections = detector.detect(frame_bgr)
        for d in detections:
            print(d.id, d.center, d.family)
    """

    def __init__(self, config: Optional[DetectorConfig] = None) -> None:
        """Build detector objects from *config*.

        Args:
            config: Detection configuration.  ``None`` uses
                :class:`DetectorConfig` defaults.
        """
        self._config: DetectorConfig = config if config is not None else DetectorConfig()
        # Pre-build ArucoDetector objects once to avoid per-frame overhead.
        self._detectors: list[tuple[cv.aruco.ArucoDetector, str]] = (
            self._build_detectors()
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def detect(
        self,
        frame_bgr: np.ndarray,
        *,
        gray: Optional[np.ndarray] = None,
    ) -> list[Detection]:
        """Detect tags in a BGR frame.

        The method is stateless: the same *frame_bgr* always produces the
        same output regardless of call history.

        Args:
            frame_bgr: Input colour frame in BGR channel order.
            gray: Optional pre-computed single-channel grayscale image
                matching *frame_bgr*.  When supplied, the BGR-to-gray
                conversion step is skipped.

        Returns:
            A list of :class:`Detection` objects, one per detected tag.
            Empty list when no tags are found.
        """
        cfg = self._config
        h, w = frame_bgr.shape[:2]

        # Convert to grayscale
        if gray is None:
            gray_img = cv.cvtColor(frame_bgr, cv.COLOR_BGR2GRAY)
        else:
            gray_img = gray

        # Compute downscale factor from proc_width
        scale = 1.0
        if cfg.proc_width > 0 and w > 0:
            scale = min(1.0, float(cfg.proc_width) / float(w))

        if scale < 1.0:
            new_w = max(1, int(w * scale))
            new_h = max(1, int(h * scale))
            gray_img = cv.resize(gray_img, (new_w, new_h), interpolation=cv.INTER_AREA)

        # Apply preprocessing (highpass, CLAHE, sharpening)
        gray_img = self._preprocess(
            gray_img,
            use_highpass=cfg.use_highpass,
            highpass_ksize=cfg.highpass_ksize,
            use_clahe=cfg.use_clahe,
            use_sharpen=cfg.use_sharpen,
        )

        results: list[Detection] = []
        for detector, fam in self._detectors:
            corners, ids, _rej = detector.detectMarkers(gray_img)
            if ids is None:
                continue
            for c, idv in zip(corners, ids.flatten().tolist()):
                pts = c.reshape(-1, 2).astype(np.float32)
                if scale < 1.0:
                    pts = pts / float(scale)
                cx = float(pts[:, 0].mean())
                cy = float(pts[:, 1].mean())
                results.append(Detection(
                    id=int(idv),
                    center=(cx, cy),
                    corners=pts,
                    family=fam,
                ))
        return results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _preprocess(
        gray: np.ndarray,
        *,
        use_highpass: bool = True,
        highpass_ksize: int = 51,
        use_clahe: bool = False,
        use_sharpen: bool = False,
    ) -> np.ndarray:
        """Apply optional preprocessing to a grayscale image.

        Illumination flattening (highpass) is on by default.  It estimates
        the low-frequency illumination field via a large Gaussian blur and
        divides it out so that tag squares under varying illumination are
        equalised.

        Args:
            gray: Single-channel uint8 grayscale image.
            use_highpass: Apply illumination-flattening highpass filter.
            highpass_ksize: Gaussian kernel size for illumination estimate.
            use_clahe: Apply CLAHE histogram equalisation.
            use_sharpen: Apply a 3×3 unsharp-mask sharpening kernel.

        Returns:
            Preprocessed single-channel uint8 image (same size as input).
        """
        out = gray
        if use_highpass:
            k = highpass_ksize
            if k % 2 == 0:
                k += 1
            illum = cv.GaussianBlur(out, (k, k), 0).astype(np.float32)
            illum = np.maximum(illum, 1.0)
            flat = (out.astype(np.float32) / illum) * 128.0
            out = np.clip(flat, 0, 255).astype(np.uint8)
        if use_clahe:
            clahe = cv.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
            out = clahe.apply(out)
        if use_sharpen:
            k_sharp = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]], dtype=np.float32)
            out = cv.filter2D(out, -1, k_sharp)
        return out

    def _build_detectors(self) -> list[tuple[cv.aruco.ArucoDetector, str]]:
        """Construct per-family :class:`cv.aruco.ArucoDetector` objects.

        Returns a list of ``(ArucoDetector, family_name)`` pairs — one per
        configured family.  The list is built once in ``__init__`` and
        reused for every :meth:`detect` call.
        """
        cfg = self._config
        fams: list[str]
        if cfg.family == "all":
            fams = ["16h5", "25h9", "36h10", "36h11"]
        else:
            fams = [cfg.family]
        if cfg.detect_aruco_4x4:
            fams.append("aruco_4x4")

        detectors: list[tuple[cv.aruco.ArucoDetector, str]] = []
        for fam in fams:
            d = cv.aruco.getPredefinedDictionary(self._get_dict_by_family(fam))
            p = cv.aruco.DetectorParameters()
            # Wider adaptive threshold range with smaller steps for robust
            # detection under uneven illumination / glare.
            p.adaptiveThreshWinSizeMin = 3
            p.adaptiveThreshWinSizeMax = 53
            p.adaptiveThreshWinSizeStep = 4
            p.cornerRefinementMethod = {
                "none": cv.aruco.CORNER_REFINE_NONE,
                "contour": cv.aruco.CORNER_REFINE_CONTOUR,
                "subpix": cv.aruco.CORNER_REFINE_SUBPIX,
            }.get(cfg.corner_refine, cv.aruco.CORNER_REFINE_SUBPIX)
            p.aprilTagQuadDecimate = float(max(1.0, cfg.quad_decimate))
            p.aprilTagQuadSigma = float(max(0.0, cfg.quad_sigma))
            if hasattr(p, "aprilTagMinWhiteBlackDiff"):
                try:
                    p.aprilTagMinWhiteBlackDiff = int(
                        max(0, int(round(cfg.april_min_wb_diff)))
                    )
                except Exception:
                    p.aprilTagMinWhiteBlackDiff = 3
            if hasattr(p, "aprilTagMinClusterPixels"):
                try:
                    p.aprilTagMinClusterPixels = int(
                        max(1, int(cfg.april_min_cluster_pixels))
                    )
                except Exception:
                    p.aprilTagMinClusterPixels = 5
            if hasattr(p, "aprilTagMaxLineFitMse"):
                try:
                    p.aprilTagMaxLineFitMse = float(
                        max(1.0, float(cfg.april_max_line_fit_mse))
                    )
                except Exception:
                    p.aprilTagMaxLineFitMse = 20.0
            p.detectInvertedMarker = bool(cfg.detect_inverted)
            detectors.append((make_aruco_detector(d, p), fam))
        return detectors

    @staticmethod
    def _get_dict_by_family(name: str) -> int:
        """Map a family name string to an OpenCV ArUco predefined dictionary ID.

        Args:
            name: Family name — one of ``"16h5"``, ``"25h9"``, ``"36h10"``,
                ``"36h11"``, or ``"aruco_4x4"``.  Unknown names fall back to
                ``DICT_APRILTAG_36h11``.

        Returns:
            An integer constant suitable for ``cv.aruco.getPredefinedDictionary``.
        """
        mapping = {
            "16h5": cv.aruco.DICT_APRILTAG_16h5,
            "25h9": cv.aruco.DICT_APRILTAG_25h9,
            "36h10": cv.aruco.DICT_APRILTAG_36h10,
            "36h11": cv.aruco.DICT_APRILTAG_36h11,
            "aruco_4x4": cv.aruco.DICT_4X4_50,
        }
        return mapping.get(name, cv.aruco.DICT_APRILTAG_36h11)
