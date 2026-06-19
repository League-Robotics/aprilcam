"""Object detection dataclasses and square detector for non-tag objects."""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, replace
from typing import Optional, Tuple

import cv2 as cv
import numpy as np


@dataclass(frozen=True)
class ObjectRecord:
    """A detected non-tag object (e.g. a colored cube)."""

    center_px: Tuple[float, float]
    bbox: Tuple[int, int, int, int]  # x, y, w, h
    area_px: float
    world_xy: Optional[Tuple[float, float]] = None
    color: str = "unknown"
    object_type: str = "cube"
    confidence: float = 1.0


class FrameResult(list):
    """Result of processing a single frame.

    Subclasses ``list`` so that ``isinstance(result, list)`` is ``True``
    and all standard list operations (iteration, ``len()``, indexing)
    work directly on the tags.  Extra attributes expose object detections
    and frame metadata.
    """

    def __init__(self, tags, objects=None, timestamp=0.0, frame_index=0):
        super().__init__(tags)
        self.tags = tags
        self.objects = objects or []
        self.timestamp = timestamp
        self.frame_index = frame_index


class SquareDetector:
    """Detect square-ish bright contours on a dark playfield.

    Applies a high-pass filter to remove low-frequency illumination
    gradients (glare, uneven lighting), then binary-thresholds the
    result.  Filters by area, aspect ratio, solidity, and optional
    playfield polygon containment.
    """

    def __init__(
        self,
        min_area: int = 200,
        max_area: int = 800,
        threshold: int = 155,
        tag_margin: float = 1.8,
        border_margin: int = 50,
        highpass_ksize: int = 51,
    ):
        self.min_area = min_area
        self.max_area = max_area
        self.threshold = threshold
        self.tag_margin = tag_margin  # expand tag exclusion zones by this factor
        self.border_margin = border_margin  # pixels inset from playfield edge
        self.highpass_ksize = highpass_ksize
        # Persistent tag positions for exclusion across frames
        self._known_tag_corners: dict[int, np.ndarray] = {}

    def update_known_tags(self, tag_records: list) -> None:
        """Update persistent tag corner cache from current detections.

        Call this every frame so that tags which flicker out still
        have their regions excluded from object detection.
        """
        for tr in tag_records:
            self._known_tag_corners[tr.id] = np.array(
                tr.corners_px, dtype=np.float32
            )

    def detect(
        self,
        gray: np.ndarray,
        homography: np.ndarray | None = None,
        tag_corners: list[np.ndarray] | None = None,
        exclusion_point: Tuple[float, float] | None = None,
        exclusion_radius: float = 50,
        playfield_polygon: np.ndarray | None = None,
    ) -> list[ObjectRecord]:
        """Detect square objects in a grayscale image.

        Args:
            gray: Grayscale uint8 image.
            homography: Optional 3x3 homography matrix for world coords.
            tag_corners: List of Nx2 arrays of tag corner polygons to exclude.
            exclusion_point: Optional (x, y) point; detections within
                *exclusion_radius* of this point are excluded.
            exclusion_radius: Radius around *exclusion_point* to exclude.
            playfield_polygon: Optional Nx2 polygon; detections outside
                this polygon are discarded.

        Returns:
            List of :class:`ObjectRecord` for each detected square-like
            contour.
        """
        # Illumination flattening: estimate the low-frequency illumination
        # field and divide it out.  Because illumination is multiplicative,
        # division recovers the reflectance — a cube in a bright zone and
        # one in a dark zone both produce the same output level.
        k = self.highpass_ksize
        illum = cv.GaussianBlur(gray, (k, k), 0).astype(np.float32)
        illum = np.maximum(illum, 1.0)
        flat = np.clip((gray.astype(np.float32) / illum) * 128.0, 0, 255).astype(np.uint8)
        _, thresh = cv.threshold(flat, self.threshold, 255, cv.THRESH_BINARY)
        # Light morphological open to remove specks.
        kernel = cv.getStructuringElement(cv.MORPH_RECT, (3, 3))
        thresh = cv.morphologyEx(thresh, cv.MORPH_OPEN, kernel)
        contours, _ = cv.findContours(
            thresh, cv.RETR_EXTERNAL, cv.CHAIN_APPROX_SIMPLE
        )

        # Build expanded tag exclusion from current + cached corners.
        all_corners: list[np.ndarray] = list(tag_corners or [])
        # Merge persistent cache (covers tags not detected this frame)
        seen_centers = set()
        for c in all_corners:
            center = tuple(c.reshape(-1, 2).mean(axis=0).astype(int))
            seen_centers.add(center)
        for _tid, cached_c in self._known_tag_corners.items():
            center = tuple(cached_c.reshape(-1, 2).mean(axis=0).astype(int))
            if center not in seen_centers:
                all_corners.append(cached_c)

        tag_polys: list[np.ndarray] = []
        for corners in all_corners:
            pts = corners.reshape(-1, 2).astype(np.float32)
            center = pts.mean(axis=0)
            expanded = center + (pts - center) * self.tag_margin
            tag_polys.append(expanded)

        results: list[ObjectRecord] = []
        for cnt in contours:
            area = cv.contourArea(cnt)
            if area < self.min_area or area > self.max_area:
                continue

            x, y, w, h = cv.boundingRect(cnt)
            aspect = max(w, h) / max(min(w, h), 1)
            if aspect >= 2.0:
                continue

            hull = cv.convexHull(cnt)
            hull_area = cv.contourArea(hull)
            if hull_area < 1:
                continue
            solidity = area / hull_area
            if solidity <= 0.7:
                continue

            cx = x + w / 2.0
            cy = y + h / 2.0

            # Only keep detections inside the playfield polygon,
            # inset by border_margin to exclude edge markers.
            if playfield_polygon is not None:
                pf = playfield_polygon.reshape(-1, 2).astype(np.float32)
                pf_center = pf.mean(axis=0)
                # Shrink polygon toward center by border_margin pixels
                directions = pf - pf_center
                lengths = np.linalg.norm(directions, axis=1, keepdims=True)
                lengths = np.maximum(lengths, 1e-6)
                shrunk = pf - directions / lengths * self.border_margin
                shrunk_poly = shrunk.reshape(-1, 1, 2).astype(np.float32)
                if cv.pointPolygonTest(shrunk_poly, (cx, cy), False) < 0:
                    continue

            # Exclude centers inside expanded tag corner polygons.
            if tag_polys:
                inside_tag = False
                for poly in tag_polys:
                    p = poly.reshape(-1, 1, 2).astype(np.float32)
                    if cv.pointPolygonTest(p, (cx, cy), False) >= 0:
                        inside_tag = True
                        break
                if inside_tag:
                    continue

            # Exclude centers near the exclusion point.
            if exclusion_point is not None:
                dx = cx - exclusion_point[0]
                dy = cy - exclusion_point[1]
                if math.hypot(dx, dy) <= exclusion_radius:
                    continue

            world_xy: Tuple[float, float] | None = None
            if homography is not None:
                pt = homography @ np.array([cx, cy, 1.0])
                if abs(pt[2]) > 1e-9:
                    world_xy = (float(pt[0] / pt[2]), float(pt[1] / pt[2]))

            results.append(
                ObjectRecord(
                    center_px=(cx, cy),
                    bbox=(x, y, w, h),
                    area_px=float(area),
                    world_xy=world_xy,
                )
            )

        return results


class ObjectFuser:
    """Fuses B&W object detections with color camera classifications."""

    def __init__(self, match_radius: float = 5.0):
        self.match_radius = match_radius
        # Maps quantized (x_cm, y_cm) -> (color_name, timestamp)
        self._color_map: dict[tuple[float, float], tuple[str, float]] = {}

    @staticmethod
    def _quantize(x: float, y: float) -> tuple[float, float]:
        """Round to nearest 0.1 (1mm) for map key."""
        return (round(x, 1), round(y, 1))

    def update_colors(self, color_objects: list[ObjectRecord]) -> None:
        """Update color map from color camera detections."""
        now = time.time()
        for obj in color_objects:
            if obj.world_xy is not None and obj.color != "unknown":
                key = self._quantize(obj.world_xy[0], obj.world_xy[1])
                self._color_map[key] = (obj.color, now)

    def fuse(self, bw_objects: list[ObjectRecord]) -> list[ObjectRecord]:
        """Assign color labels to B&W objects from the color map."""
        result = []
        for obj in bw_objects:
            if obj.world_xy is None:
                result.append(obj)
                continue

            ox, oy = obj.world_xy
            best_color = "unknown"
            best_dist = self.match_radius

            for (kx, ky), (color, _ts) in self._color_map.items():
                d = math.hypot(kx - ox, ky - oy)
                if d < best_dist:
                    best_dist = d
                    best_color = color

            if best_color != obj.color:
                obj = replace(obj, color=best_color)
            result.append(obj)
        return result

    def clear_stale(self, max_age_seconds: float = 5.0) -> None:
        """Remove color map entries older than max_age_seconds."""
        cutoff = time.time() - max_age_seconds
        stale = [k for k, (_, ts) in self._color_map.items() if ts < cutoff]
        for k in stale:
            del self._color_map[k]


class ColorCameraThread:
    """DAEMON-ONLY — opens a cv2.VideoCapture device directly.

    This class is NOT reachable from the MCP server or any CLI client entry
    point.  The ``get_objects`` MCP tool reads frames from the DetectionLoop's
    ``last_frame`` cache (which is populated by DaemonCapture via gRPC) rather
    than instantiating this class.

    ColorCameraThread may only be used from daemon-internal code that already
    owns the camera hardware.  Never construct it with a device index from the
    MCP server path.
    """

    def __init__(self, camera_index, fuser, classifier, homography=None, fps=5.0):
        self._camera_index = camera_index
        self._fuser = fuser
        self._classifier = classifier
        self._homography = homography
        self._interval = 1.0 / max(0.1, fps)
        self._stop_event = threading.Event()
        self._thread = None
        self._cap = None

    def start(self):
        # DAEMON-ONLY: direct VideoCapture open — caller must own the hardware.
        self._cap = cv.VideoCapture(self._camera_index)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop_event.is_set():
            try:
                ret, frame = self._cap.read()
                if ret and frame is not None:
                    objects = self._classifier.classify(frame, self._homography)
                    self._fuser.update_colors(objects)
                    self._fuser.clear_stale()
            except Exception:
                pass
            self._stop_event.wait(self._interval)

    def stop(self):
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
