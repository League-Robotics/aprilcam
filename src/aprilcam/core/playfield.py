from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Tuple, List, Dict

import cv2 as cv
import numpy as np
from .models import AprilTag, AprilTagFlow


@dataclass
class PlayfieldBoundary:
    """ArUco corner detection and polygon management for the playfield.

    This is the internal geometry class. The user-facing :class:`Playfield`
    composes this as ``_boundary``.

    Caches 4x4 ArUco corner detections and exposes a stable playfield polygon.
    Corner IDs are expected to be 0=UL, 1=UR, 2=LL, 3=LR.
    We compute a consistent UL,UR,LR,LL order by geometry to tolerate any ID swaps.
    """

    proc_width: int = 960
    detect_inverted: bool = False
    polygon: Optional[np.ndarray] = None  # shape (4,2) float32, UL/UR/LR/LL
    ema_alpha: float = 0.3
    deadband_threshold: float = 50.0
    corner_detect_interval: int = 30

    # Saved-geometry (static-camera) seeding inputs. When a homography and
    # physical dimensions are supplied, the polygon is seeded up front from
    # H⁻¹-derived corners (no live ArUco detection required) and the metric
    # deskew rectangle is sized from W×H × px_per_cm.
    homography: Optional[np.ndarray] = None
    width_cm: float = 0.0
    height_cm: float = 0.0
    px_per_cm: float = 0.0  # 0 → use geometry.DEFAULT_PX_PER_CM

    _poly: Optional[np.ndarray] = field(default=None, init=False, repr=False)
    _flows: Dict[int, AprilTagFlow] = field(default_factory=dict, init=False, repr=False)
    _vel_ema: Dict[int, float] = field(default_factory=dict, init=False, repr=False)
    _last_seen: Dict[int, Tuple[float, float, float]] = field(default_factory=dict, init=False, repr=False)
    _aruco_detector: Optional[cv.aruco.ArucoDetector] = field(default=None, init=False, repr=False)
    _corner_frame_count: int = field(default=0, init=False, repr=False)

    def __post_init__(self):
        if self.polygon is not None:
            self._poly = np.asarray(self.polygon, dtype=np.float32).reshape(4, 2)
        elif self.homography is not None and self.width_cm > 0 and self.height_cm > 0:
            # Static-camera path: seed the polygon from saved geometry so
            # get_polygon() is non-None before any live frame is processed.
            from ..calibration.geometry import corner_pixels_from_homography
            try:
                self._poly = corner_pixels_from_homography(
                    self.homography, self.width_cm, self.height_cm
                )
            except Exception:
                self._poly = None
        self._aruco_detector = self._build_aruco4_detector()

    def _effective_px_per_cm(self) -> float:
        from ..calibration.geometry import DEFAULT_PX_PER_CM
        return self.px_per_cm if self.px_per_cm > 0 else DEFAULT_PX_PER_CM

    def _build_aruco4_detector(self):
        d = cv.aruco.getPredefinedDictionary(cv.aruco.DICT_4X4_50)
        p = cv.aruco.DetectorParameters()
        p.detectInvertedMarker = bool(self.detect_inverted)
        return cv.aruco.ArucoDetector(d, p)

    def _detect_corners(self, frame_bgr: np.ndarray, gray: Optional[np.ndarray] = None) -> Dict[int, Tuple[float, float]]:
        h, w = frame_bgr.shape[:2]
        if gray is None:
            gray = cv.cvtColor(frame_bgr, cv.COLOR_BGR2GRAY)
        if self.proc_width and w > 0 and self.proc_width < w:
            scale = float(self.proc_width) / float(w)
            new_w = max(1, int(w * scale))
            new_h = max(1, int(h * scale))
            gray = cv.resize(gray, (new_w, new_h), interpolation=cv.INTER_AREA)
        else:
            scale = 1.0

        if self._aruco_detector is None:
            self._aruco_detector = self._build_aruco4_detector()
        detector = self._aruco_detector
        corners, ids, _ = detector.detectMarkers(gray)
        out: Dict[int, Tuple[float, float]] = {}
        if ids is None:
            return out
        for c, idv in zip(corners, ids.flatten().tolist()):
            pts = c.reshape(-1, 2).astype(np.float32)
            if scale < 1.0 and scale > 1e-9:
                pts = pts / float(scale)
            center = pts.mean(axis=0)
            out[int(idv)] = (float(center[0]), float(center[1]))
        return out

    def _order_poly(self, corners_map: Dict[int, Tuple[float, float]]) -> Optional[np.ndarray]:
        # Canonical 4-corner layout: ArUco IDs 0-3
        if all(k in corners_map for k in (0, 1, 2, 3)):
            pts4 = np.array([corners_map[k] for k in (0, 1, 2, 3)], dtype=np.float32)
        # 8-marker perimeter layout: corners are IDs 1, 3, 5, 7
        elif all(k in corners_map for k in (1, 3, 5, 7)):
            pts4 = np.array([corners_map[k] for k in (1, 3, 5, 7)], dtype=np.float32)
        else:
            return None
        idx = np.argsort(pts4[:, 1])  # ascending by y (top first)
        top = pts4[idx[:2]]
        bot = pts4[idx[2:]]
        top = top[np.argsort(top[:, 0])]  # UL, UR
        bot = bot[np.argsort(bot[:, 0])]  # LL, LR
        UL, UR = top[0], top[1]
        LL, LR = bot[0], bot[1]
        return np.array([UL, UR, LR, LL], dtype=np.float32)

    def update(self, frame_bgr: np.ndarray, gray: Optional[np.ndarray] = None) -> None:
        self._corner_frame_count += 1
        # Throttle corner re-detection: only run every N frames
        # Always detect on the first frame (count == 1) or when no polygon exists
        if (self._poly is not None
                and self.corner_detect_interval > 1
                and (self._corner_frame_count - 1) % self.corner_detect_interval != 0):
            return
        cmap = self._detect_corners(frame_bgr, gray=gray)
        poly = self._order_poly(cmap)
        if poly is not None:
            if self._poly is None:
                self._poly = poly
            else:
                # EMA smooth toward new detection to avoid jitter
                self._poly = (
                    self.ema_alpha * poly
                    + (1 - self.ema_alpha) * self._poly
                ).astype(np.float32)

    def get_polygon(self) -> Optional[np.ndarray]:
        return self._poly.copy() if self._poly is not None else None

    def isIn(self, pts: np.ndarray | tuple[float, float]) -> bool:
        """Return True if the given tag points/center lie within the playfield.

        Accepts either:
        - An array of shape (N,2) of tag corners/points; uses their mean as center.
        - A tuple (x, y) representing the center directly.

        If the playfield polygon isn't known yet, returns True (no filtering).
        """
        if self._poly is None:
            return True
        try:
            if isinstance(pts, tuple) or (hasattr(pts, "__len__") and len(pts) == 2 and not hasattr(pts[0], "__len__")):
                u, v = float(pts[0]), float(pts[1])
            else:
                P = np.asarray(pts, dtype=np.float32).reshape(-1, 2)
                c = P.mean(axis=0)
                u, v = float(c[0]), float(c[1])
            inside = cv.pointPolygonTest(self._poly.astype(np.float32), (u, v), False)
            return bool(inside >= 0)
        except Exception:
            return True

    def annotate(self, frame_bgr: np.ndarray) -> None:
        if self._poly is None:
            return
        try:
            poly_i = self._poly.astype(int)
            cv.polylines(frame_bgr, [poly_i], True, (255, 255, 255), 2, cv.LINE_AA)
        except Exception:
            pass

    def deskew_transform(self) -> Optional[Tuple[np.ndarray, Tuple[int, int]]]:
        """Return ``(M, (out_w, out_h))`` for the current playfield polygon.

        When physical dimensions are known (saved-geometry / static-camera
        path), this produces a **metric** top-down rectangle of size
        ``(round(W·px_per_cm), round(H·px_per_cm))`` via the shared
        :func:`calibration.geometry.metric_deskew_matrix` helper. When only a
        live-corner polygon is available (no saved W×H), it falls back to the
        legacy polygon-edge-length pixel rectangle.

        Returns ``None`` when no polygon is available.
        """
        if self._poly is None:
            return None
        if self.width_cm > 0 and self.height_cm > 0:
            from ..calibration.geometry import metric_deskew_matrix
            return metric_deskew_matrix(
                self._poly, self.width_cm, self.height_cm, self._effective_px_per_cm()
            )
        # Fallback: size the rectangle from polygon edge lengths (live-corner
        # path with no saved dimensions).
        UL, UR, LR, LL = self._poly.astype(np.float32)
        w_top = float(np.linalg.norm(UR - UL))
        w_bottom = float(np.linalg.norm(LR - LL))
        h_left = float(np.linalg.norm(LL - UL))
        h_right = float(np.linalg.norm(LR - UR))
        out_w = max(10, int(round(max(w_top, w_bottom))))
        out_h = max(10, int(round(max(h_left, h_right))))
        src = np.array([UL, UR, LR, LL], dtype=np.float32)
        dst = np.array(
            [[0, 0], [out_w, 0], [out_w, out_h], [0, out_h]], dtype=np.float32
        )
        M = cv.getPerspectiveTransform(src, dst)
        return M, (out_w, out_h)

    def deskew(self, frame_bgr: np.ndarray) -> np.ndarray:
        if self._poly is None:
            return frame_bgr
        transform = self.deskew_transform()
        if transform is None:
            return frame_bgr
        M, (out_w, out_h) = transform
        # Mask source to playfield polygon so outside pixels don't bleed in
        mask = np.zeros(frame_bgr.shape[:2], dtype=np.uint8)
        cv.fillConvexPoly(mask, self._poly.astype(np.int32), 255)
        masked = cv.bitwise_and(frame_bgr, frame_bgr, mask=mask)
        return cv.warpPerspective(masked, M, (out_w, out_h))

    def get_deskew_matrix(self) -> np.ndarray | None:
        """Return the 3x3 perspective transform used by :meth:`deskew`, or ``None``."""
        transform = self.deskew_transform()
        return None if transform is None else transform[0]

    # --- tag flow integration ---
    def add_tag(self, tag: AprilTag, homography: Optional[np.ndarray] = None) -> None:
        """Add/Update a tag into the playfield flows, setting in_playfield.

        Computes EMA-smoothed velocity with dead-band suppression and stores
        the result on the flow via :meth:`AprilTagFlow.set_velocity`.

        When *homography* is provided, also computes world-space velocity
        by transforming the pixel velocity through the homography and stores
        it via :meth:`AprilTagFlow.set_world_velocity`.

        If the playfield polygon is unknown, in_playfield defaults to True.
        """
        try:
            tag.in_playfield = self.isIn(tag.center_px)
        except Exception:
            tag.in_playfield = True
        flow = self._flows.get(tag.id)
        if flow is None:
            flow = AprilTagFlow(maxlen=5)
            self._flows[tag.id] = flow
        # Store a snapshot so history isn't mutated by future updates
        flow.add_tag(tag.clone())

        # --- EMA + dead-band velocity computation ---
        tid = tag.id
        cx, cy = tag.center_px
        timestamp = tag.last_ts
        vel_px_val: Tuple[float, float] = (0.0, 0.0)
        speed_px_val: float = 0.0

        if timestamp is not None and tid in self._last_seen:
            px, py, pt = self._last_seen[tid]
            dt = max(1e-3, timestamp - pt)
            dx = (cx - px) / dt
            dy = (cy - py) / dt
            inst_speed = math.hypot(dx, dy)
            prev_ema = self._vel_ema.get(tid)
            smoothed = (
                self.ema_alpha * inst_speed + (1 - self.ema_alpha) * prev_ema
                if prev_ema is not None
                else inst_speed
            )
            self._vel_ema[tid] = smoothed
            if smoothed < self.deadband_threshold:
                vel_px_val = (0.0, 0.0)
                speed_px_val = 0.0
            else:
                vel_px_val = (dx, dy)
                speed_px_val = smoothed

        if timestamp is not None:
            self._last_seen[tid] = (cx, cy, timestamp)

        flow.set_velocity(vel_px_val, speed_px_val)

        # --- world-space velocity via homography ---
        if homography is not None and speed_px_val > 0.0:
            vx, vy = vel_px_val
            p1 = np.array([cx, cy, 1.0], dtype=float)
            p2 = np.array([cx + vx, cy + vy, 1.0], dtype=float)
            w1 = homography @ p1
            w1 = w1 / w1[2]
            w2 = homography @ p2
            w2 = w2 / w2[2]
            wvx = float(w2[0] - w1[0])
            wvy = float(w2[1] - w1[1])
            speed_world = math.hypot(wvx, wvy)
            heading_rad = math.atan2(wvy, wvx)
            flow.set_world_velocity((wvx, wvy), speed_world, heading_rad)

    def get_flows(self) -> Dict[int, AprilTagFlow]:
        return self._flows


# ---------------------------------------------------------------------------
# New user-facing Playfield
# ---------------------------------------------------------------------------


class Playfield:
    """Primary user-facing object for tag detection on a playfield.

    Associates a camera with a physical playfield, manages the detection
    pipeline, and provides access to tags.

    Usage::

        camera = Camera.find("Brio")
        field = Playfield(camera, width_cm=101, height_cm=89)
        field.start()
        tag = field.tag(42)
        if tag:
            tag.update()
            print(f"Tag 42 at ({tag.wx:.1f}, {tag.wy:.1f})")
        field.stop()
    """

    def __init__(
        self,
        camera,
        *,
        width_cm: float = 101.0,
        height_cm: float = 89.0,
        family: str = "36h11",
        calibration: Optional[str] = "auto",
        proc_width: int = 960,
        detect_interval: int = 3,
        data_dir: str = "data",
        px_per_cm: float = 0.0,
    ) -> None:
        from .detector import TagDetector, DetectorConfig
        from .tracker import OpticalFlowTracker
        from .pipeline import DetectionPipeline
        from .detection import RingBuffer

        self._camera = camera
        self._width_cm = width_cm
        self._height_cm = height_cm
        self._calibration = calibration
        self._data_dir = data_dir
        self._px_per_cm = px_per_cm

        # Homography is loaded eagerly only when an explicit path is provided.
        # When calibration=="auto", discovery is deferred to start() so that
        # the camera is already open and device_name/resolution are available.
        self._homography: Optional[np.ndarray] = None
        self._corner_pixels: Optional[np.ndarray] = None
        if calibration not in ("auto", None):
            self._homography = self._load_homography(calibration)
            self._corner_pixels = self._load_corner_pixels(calibration)

        # Internal components. When a homography is known up front, seed the
        # boundary geometry so its polygon is non-None before any live frame.
        self._boundary = PlayfieldBoundary(proc_width=proc_width)
        self._seed_boundary_geometry()
        self._detector = TagDetector(DetectorConfig(family=family, proc_width=proc_width))
        self._tracker = OpticalFlowTracker(detect_interval=detect_interval)
        self._ring_buffer = RingBuffer()
        self._pipeline = DetectionPipeline(
            camera,
            self._detector,
            self._tracker,
            homography=self._homography,
            boundary=self._boundary,
            ring_buffer=self._ring_buffer,
        )
        self._tags: Dict[int, "Tag"] = {}

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def camera(self):
        return self._camera

    @property
    def width_cm(self) -> float:
        return self._width_cm

    @property
    def height_cm(self) -> float:
        return self._height_cm

    @property
    def polygon(self) -> Optional[np.ndarray]:
        return self._boundary.get_polygon()

    @property
    def homography(self) -> Optional[np.ndarray]:
        return self._homography

    @property
    def is_calibrated(self) -> bool:
        return self._homography is not None

    @property
    def is_detecting(self) -> bool:
        return self._pipeline.is_running

    # ------------------------------------------------------------------
    # Tag access (pull interface)
    # ------------------------------------------------------------------

    def tags(self) -> Dict[int, "Tag"]:
        """Return all currently tracked tags, keyed by ID."""
        from .tag import Tag
        latest = self._ring_buffer.get_latest()
        if latest is None:
            return {}
        result: Dict[int, Tag] = {}
        for tr in latest.tags:
            t = self._tags.get(tr.id)
            if t is None:
                t = Tag(tr.id, self._pipeline)
                self._tags[tr.id] = t
            t.update()
            result[tr.id] = t
        return result

    def tag(self, tag_id: int) -> Optional["Tag"]:
        """Get a specific tag by ID, or None if not seen."""
        from .tag import Tag
        t = self._tags.get(tag_id)
        if t is None:
            t = Tag(tag_id, self._pipeline)
            self._tags[tag_id] = t
        t.update()
        return t if t.is_visible else None

    # ------------------------------------------------------------------
    # Tag access (push interface)
    # ------------------------------------------------------------------

    def stream(self):
        """Generator yielding list[Tag] per frame.

        Starts the pipeline if not already running. Yields on each new
        frame until stopped.
        """
        import time
        from .tag import Tag

        if not self.is_detecting:
            self.start()

        last_frame = -1
        while self.is_detecting:
            latest = self._ring_buffer.get_latest()
            if latest is not None and latest.frame_index > last_frame:
                last_frame = latest.frame_index
                tags = []
                for tr in latest.tags:
                    t = self._tags.get(tr.id)
                    if t is None:
                        t = Tag(tr.id, self._pipeline)
                        self._tags[tr.id] = t
                    t.update()
                    tags.append(t)
                yield tags
            else:
                time.sleep(0.001)

    def on_frame(self, callback) -> None:
        """Register a callback invoked on each frame update."""
        self._pipeline.on_frame(callback)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background detection pipeline.

        When ``calibration='auto'`` was specified at construction time, the
        camera is opened here so that device_name and resolution are available
        for homography discovery before the pipeline thread starts.
        """
        if self._calibration == "auto" and self._homography is None:
            self._homography = self._auto_discover_homography_from_camera()
            if self._homography is not None:
                # Propagate the discovered homography into the running pipeline.
                self._pipeline._homography = self._homography
                # Seed boundary geometry so deskew engages from saved corners
                # (or H⁻¹-derived corners) without live ArUco detection.
                self._seed_boundary_geometry()
        self._pipeline.start()

    def stop(self) -> None:
        """Stop the background detection pipeline."""
        self._pipeline.stop()

    def calibrate(self, **kwargs):
        """Run calibration on this playfield's camera."""
        from ..calibration.calibration import calibrate as _calibrate
        return _calibrate(self._camera, width_cm=self._width_cm,
                          height_cm=self._height_cm, **kwargs)

    # ------------------------------------------------------------------
    # Geometry
    # ------------------------------------------------------------------

    def pixel_to_world(self, u: float, v: float) -> Optional[Tuple[float, float]]:
        """Map pixel coordinates to world coordinates (cm)."""
        if self._homography is None:
            return None
        vec = self._homography @ np.array([u, v, 1.0])
        return (float(vec[0] / vec[2]), float(vec[1] / vec[2]))

    def world_to_pixel(self, x: float, y: float) -> Optional[Tuple[float, float]]:
        """Map world coordinates (cm) to pixel coordinates."""
        if self._homography is None:
            return None
        H_inv = np.linalg.inv(self._homography)
        vec = H_inv @ np.array([x, y, 1.0])
        return (float(vec[0] / vec[2]), float(vec[1] / vec[2]))

    def deskew(self, frame: np.ndarray) -> np.ndarray:
        """Perspective-warp frame to a top-down rectangle."""
        return self._boundary.deskew(frame)

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.stop()

    # ------------------------------------------------------------------
    # Homography discovery
    # ------------------------------------------------------------------

    def _auto_discover_homography(
        self,
        device_name: str,
        width: int,
        height: int,
    ) -> Optional[np.ndarray]:
        """Discover and load a homography for *device_name* at *width*x*height*.

        Returns a 3x3 numpy array if a calibration file is found, else None.
        """
        try:
            from ..calibration.homography import discover_homography
            found = discover_homography(device_name, width, height, self._data_dir)
            if found is not None:
                H = self._load_homography(str(found))
                if H is not None:
                    # Also capture saved geometry (corner pixels + dimensions)
                    # for boundary seeding. Dimensions fall back to the
                    # constructor values when absent from the record.
                    self._corner_pixels = self._load_corner_pixels(str(found))
                    self._load_dimensions_into_self(str(found))
                return H
        except Exception:
            pass
        return None

    def _load_dimensions_into_self(self, path: str) -> None:
        """Update ``self._width_cm`` / ``self._height_cm`` from a record's
        ``playfield: {width, height}`` block when present."""
        import json
        from pathlib import Path
        try:
            data = json.loads(Path(path).read_text())
            pf = data.get("playfield", {}) or {}
            w = pf.get("width") or data.get("field_width_cm")
            h = pf.get("height") or data.get("field_height_cm")
            if w:
                self._width_cm = float(w)
            if h:
                self._height_cm = float(h)
        except Exception:
            pass

    def _auto_discover_homography_from_camera(self) -> Optional[np.ndarray]:
        """Open the camera (if not already open), read device_name and resolution,
        then delegate to :meth:`_auto_discover_homography`.
        """
        try:
            camera = self._camera
            # Open the camera so that resolution is queryable.
            if hasattr(camera, "open") and not getattr(camera, "is_open", True):
                camera.open()
            device_name: str
            width: int
            height: int
            if hasattr(camera, "name") and hasattr(camera, "resolution"):
                device_name = camera.name
                width, height = camera.resolution
            else:
                # Fallback for duck-typed cameras that expose .read()
                return None
            return self._auto_discover_homography(device_name, width, height)
        except Exception:
            pass
        return None

    def _load_homography(self, path: str) -> Optional[np.ndarray]:
        import json
        from pathlib import Path
        try:
            data = json.loads(Path(path).read_text())
            if "homography" in data:
                return np.array(data["homography"], dtype=float)
            # Check for cameras dict (unified format)
            cameras = data.get("cameras", {})
            for cam_data in cameras.values():
                if "homography" in cam_data:
                    return np.array(cam_data["homography"], dtype=float)
        except Exception:
            pass
        return None

    def _load_corner_pixels(self, path: str) -> Optional[np.ndarray]:
        """Load saved ``corner_pixels`` (UL/UR/LR/LL) from a calibration file.

        Returns a ``(4, 2)`` float32 array, or ``None`` when absent. When the
        record only carries a homography + dimensions, the boundary derives the
        corners from ``H⁻¹`` instead (see :meth:`_seed_boundary_geometry`).
        """
        import json
        from pathlib import Path
        try:
            data = json.loads(Path(path).read_text())
            cp = data.get("corner_pixels")
            if cp is not None:
                arr = np.asarray(cp, dtype=np.float32).reshape(4, 2)
                return arr
        except Exception:
            pass
        return None

    def _seed_boundary_geometry(self) -> None:
        """Seed the boundary's deskew geometry from saved calibration.

        Propagates the homography, physical dimensions, and (when present) the
        saved corner pixels into the :class:`PlayfieldBoundary` so its polygon
        is non-``None`` before any live frame. When only a homography exists,
        the boundary derives the four corners via ``H⁻¹``. A no-op when no
        homography is available (the live-corner path remains the fallback).
        """
        b = self._boundary
        b.homography = self._homography
        b.width_cm = self._width_cm
        b.height_cm = self._height_cm
        b.px_per_cm = self._px_per_cm
        if self._homography is None:
            return
        if self._corner_pixels is not None:
            b.polygon = self._corner_pixels
            b._poly = np.asarray(self._corner_pixels, dtype=np.float32).reshape(4, 2)
        elif self._width_cm > 0 and self._height_cm > 0:
            from ..calibration.geometry import corner_pixels_from_homography
            try:
                b._poly = corner_pixels_from_homography(
                    self._homography, self._width_cm, self._height_cm
                )
            except Exception:
                pass

