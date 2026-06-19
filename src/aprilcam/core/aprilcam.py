from __future__ import annotations

import argparse
import math
import time
from typing import List, Optional, Tuple

import cv2 as cv
import numpy as np

from ..config import AppConfig
from .detection import TagRecord
from ..iohelpers import get_data_dir
from ..camera.camutil import list_cameras as _list_cameras, select_camera_by_pattern, diagnose_camera_failure
from ..errors import CameraError, CameraNotFoundError, CameraInUseError
from .playfield import PlayfieldBoundary as Playfield
from .models import AprilTag as AprilTagModel
from ..ui.display import PlayfieldDisplay
from .detector import TagDetector, DetectorConfig


_OBJECT_COLOR_BGR = {
    "red": (0, 0, 255), "green": (0, 200, 0), "blue": (255, 0, 0),
    "yellow": (0, 255, 255), "orange": (0, 165, 255),
    "purple": (255, 0, 255), "unknown": (200, 200, 200),
}


def _draw_object_boxes(frame: np.ndarray, objects: list) -> None:
    """Draw persistent object detection overlays on a frame."""
    for obj in objects:
        x, y, w, h = obj.bbox
        bgr = _OBJECT_COLOR_BGR.get(obj.color, (200, 200, 200))
        cv.rectangle(frame, (x, y), (x + w, y + h), bgr, 2)
        cv.putText(frame, obj.color, (x, y - 5),
                   cv.FONT_HERSHEY_SIMPLEX, 0.5, bgr, 1, cv.LINE_AA)
        if obj.world_xy:
            coord = f"({obj.world_xy[0]:.1f}, {obj.world_xy[1]:.1f})"
            cv.putText(frame, coord, (x, y + h + 15),
                       cv.FONT_HERSHEY_SIMPLEX, 0.4, bgr, 1, cv.LINE_AA)


class AprilCam:
    def __init__(
        self,
        index: int,
        backend: Optional[int],
        speed_alpha: float,
        family: str,
        proc_width: int,
        cap_width: Optional[int] = None,
        cap_height: Optional[int] = None,
        quad_decimate: float = 1.0,
        quad_sigma: float = 0.0,
        corner_refine: str = "subpix",
        detect_inverted: bool = True,
        detect_interval: int = 3,
        use_clahe: bool = False,
        use_sharpen: bool = False,
        use_highpass: bool = True,
        highpass_ksize: int = 51,
        april_min_wb_diff: float = 3.0,
        april_min_cluster_pixels: int = 5,
        april_max_line_fit_mse: float = 20.0,
        print_tags: bool = False,
        cap: Optional[cv.VideoCapture] = None,
        homography: Optional[np.ndarray] = None,
        headless: bool = False,
        deskew_overlay: bool = False,
        detect_aruco_4x4: bool = False,
        playfield_poly_init: Optional[np.ndarray] = None,
        robot_tag_id: Optional[int] = None,
        gripper_offset_cm: float = 14.0,
    ) -> None:
        """Initialize the AprilCam controller.

        Args:
            index: Camera index (ignored when an explicit cap is provided).
            backend: Preferred OpenCV capture backend constant or None for auto.
            speed_alpha: EMA smoothing factor for printed speeds in [0,1].
            family: AprilTag family name (or 'all').
            proc_width: Processing width for detection downscale (0 disables).
            cap_width: Optional capture width hint for the camera.
            cap_height: Optional capture height hint for the camera.
            quad_decimate: AprilTag decimation (>=1, larger is faster/rougher).
            quad_sigma: AprilTag Gaussian blur sigma in pixels.
            corner_refine: Corner refinement mode: none/contour/subpix.
            detect_inverted: Whether to also detect inverted (white-on-black) tags.
            detect_interval: Detect every N frames; track between.
            use_clahe: Apply CLAHE preprocessing before detection.
            use_sharpen: Apply light sharpening before detection.
            april_min_wb_diff: Min white/black intensity diff for AprilTag.
            april_min_cluster_pixels: Min cluster pixel size for AprilTag.
            april_max_line_fit_mse: Max line fit MSE for AprilTag.
            print_tags: Print per-tag info each frame when detections exist.
            cap: Optional pre-opened cv.VideoCapture or image source.
            homography: 3x3 projective transform to world coords (cm).
            headless: If True, never opens a window.
            deskew_overlay: If True, warp the playfield to a rectangle for display.
            playfield_poly_init: Optional initial 4x2 polygon for playfield.
        """
        self.index = index
        self.backend = backend
        self.speed_alpha = float(speed_alpha)
        self.family = family
        self.proc_width = int(proc_width)
        self.cap_width = cap_width
        self.cap_height = cap_height
        self.quad_decimate = float(quad_decimate)
        self.quad_sigma = float(quad_sigma)
        self.corner_refine = corner_refine
        self.detect_inverted = bool(detect_inverted)
        self.detect_interval = int(detect_interval)
        self.use_clahe = bool(use_clahe)
        self.use_sharpen = bool(use_sharpen)
        self.use_highpass = bool(use_highpass)
        self.highpass_ksize = int(highpass_ksize)
        self.april_min_wb_diff = float(april_min_wb_diff)
        self.april_min_cluster_pixels = int(april_min_cluster_pixels)
        self.april_max_line_fit_mse = float(april_max_line_fit_mse)
        self.detect_aruco_4x4 = bool(detect_aruco_4x4)
        self.print_tags = bool(print_tags)
        self.cap = cap
        self.homography = homography
        self.headless = bool(headless)
        self.deskew_overlay = bool(deskew_overlay)
        try:
            from ..camera.camutil import get_device_name
            self.camera_name: str = get_device_name(index)
        except Exception:
            self.camera_name = f"camera-{index}"
        self.play_poly: Optional[np.ndarray] = None
        if playfield_poly_init is not None and isinstance(playfield_poly_init, np.ndarray) and playfield_poly_init.shape == (4, 2):
            self.play_poly = playfield_poly_init.astype(np.float32)

        # Stateless detection engine (builds detector objects once)
        self._tag_detector = TagDetector(DetectorConfig(
            family=self.family,
            proc_width=self.proc_width,
            quad_decimate=self.quad_decimate,
            quad_sigma=self.quad_sigma,
            corner_refine=self.corner_refine,
            detect_inverted=self.detect_inverted,
            detect_aruco_4x4=self.detect_aruco_4x4,
            use_highpass=self.use_highpass,
            highpass_ksize=self.highpass_ksize,
            use_clahe=self.use_clahe,
            use_sharpen=self.use_sharpen,
            april_min_wb_diff=self.april_min_wb_diff,
            april_min_cluster_pixels=self.april_min_cluster_pixels,
            april_max_line_fit_mse=self.april_max_line_fit_mse,
        ))
        self.playfield = Playfield(
            proc_width=self.proc_width or 960,
            detect_inverted=False,
            ema_alpha=self.speed_alpha,
        )
        self.window = "aprilcam"
        self.display = PlayfieldDisplay(
            self.playfield,
            window_name=self.window,
            headless=self.headless,
            deskew_overlay=self.deskew_overlay,
            robot_tag_id=robot_tag_id,
            gripper_offset_cm=gripper_offset_cm,
        )

        # Tracking state (initialized by reset_state)
        self._prev_gray: Optional[np.ndarray] = None
        self._tracks: dict[int, np.ndarray] = {}
        self._track_families: dict[int, str] = {}
        self._tag_models: dict[int, AprilTagModel] = {}
        self._frame_idx: int = 0

        # EMA state for TUI display smoothing
        self._ema: dict[int, dict[str, float]] = {}
        self._ema_alpha: float = 0.05  # smoothing factor (lower = smoother)
        self._tui_live: Optional[Any] = None  # Rich Live instance

    def reset_state(self) -> None:
        """Reset all tracking state to initial values."""
        self._prev_gray = None
        self._tracks = {}
        self._track_families = {}
        self._tag_models = {}
        self._frame_idx = 0
        self._ema = {}
        self._tui_live = None

    def _ema_smooth(self, tag_id: int, key: str, value: float) -> float:
        """Apply exponential moving average to a value for a given tag/key."""
        if tag_id not in self._ema:
            self._ema[tag_id] = {}
        state = self._ema[tag_id]
        if key not in state:
            state[key] = value
        else:
            alpha = self._ema_alpha
            state[key] = alpha * value + (1.0 - alpha) * state[key]
        return state[key]

    def _build_tui_layout(self, tag_records: list, has_world: bool, pipe_mode: str = "color", fps: float = 0.0):
        """Build a Rich Layout with tag table and status panel."""
        from rich.table import Table
        from rich.columns import Columns

        current_ids: set[int] = set()
        rows: dict[int, dict] = {}

        for tr in tag_records:
            tag_id = tr.id
            current_ids.add(tag_id)
            cx = self._ema_smooth(tag_id, "cx", float(tr.center_px[0]))
            cy = self._ema_smooth(tag_id, "cy", float(tr.center_px[1]))
            ori = self._ema_smooth(tag_id, "ori", math.degrees(tr.orientation_yaw))
            spd_raw = float(tr.speed_px) if tr.speed_px is not None else 0.0
            spd = self._ema_smooth(tag_id, "spd", spd_raw)
            vx, vy = tr.vel_px if tr.vel_px is not None else (0.0, 0.0)
            vang_raw = math.degrees(math.atan2(vy, vx)) if (vx != 0.0 or vy != 0.0) else 0.0
            vang = self._ema_smooth(tag_id, "vang", vang_raw)

            row = {"id": tag_id, "cx": cx, "cy": cy, "ori": ori, "spd": spd, "vang": vang, "visible": True}

            if has_world:
                H = self.homography
                u, v = float(tr.center_px[0]), float(tr.center_px[1])
                vec = np.array([u, v, 1.0], dtype=float)
                Xw = H @ vec
                if abs(Xw[2]) > 1e-6:
                    row["wx"] = self._ema_smooth(tag_id, "wx", Xw[0] / Xw[2])
                    row["wy"] = self._ema_smooth(tag_id, "wy", Xw[1] / Xw[2])

            rows[tag_id] = row

        for old_id, ema_data in self._ema.items():
            if old_id not in current_ids:
                row = {
                    "id": old_id,
                    "cx": ema_data.get("cx", 0), "cy": ema_data.get("cy", 0),
                    "ori": ema_data.get("ori", 0), "spd": 0.0, "vang": 0.0,
                    "visible": False,
                }
                if "wx" in ema_data:
                    row["wx"] = ema_data["wx"]
                if "wy" in ema_data:
                    row["wy"] = ema_data["wy"]
                rows[old_id] = row

        sorted_rows = sorted(rows.values(), key=lambda r: r["id"])

        # --- Tag table ---
        table = Table(title="Tags", border_style="blue", title_style="bold cyan")
        table.add_column("ID", justify="right", style="cyan", width=4)
        table.add_column("CX", justify="right", width=7)
        table.add_column("CY", justify="right", width=7)
        table.add_column("ORI", justify="right", width=8)
        table.add_column("SPD", justify="right", width=8)
        if has_world:
            table.add_column("WX", justify="right", width=8)
            table.add_column("WY", justify="right", width=8)

        for r in sorted_rows:
            style = "dim" if not r["visible"] else ""
            cols = [
                str(r["id"]),
                f"{r['cx']:.1f}",
                f"{r['cy']:.1f}",
                f"{r['ori']:+.1f}°",
                f"{r['spd']:.1f}",
            ]
            if has_world:
                cols.append(f"{r.get('wx', 0):.1f}" if "wx" in r else "—")
                cols.append(f"{r.get('wy', 0):.1f}" if "wy" in r else "—")
            table.add_row(*cols, style=style)

        # --- Status table (same structure as tag table) ---
        st = Table(title="Status", border_style="blue", title_style="bold cyan",
                   show_header=False, min_width=40)
        st.add_column("Key", style="white", width=14)
        st.add_column("Value", width=24)

        st.add_row("Camera", self.camera_name)
        st.add_row("Frame", str(self._frame_idx))
        st.add_row("FPS", f"{fps:.1f}")

        view_labels = {
            "color": "0: Color",
            "gray": "1: Grayscale",
            "flat": "2: Flattened",
            "clahe": "3: CLAHE",
            "flat+clahe": "4: Flat+CLAHE",
            "threshold": "5: Threshold",
        }
        st.add_row("View", f"[cyan]{view_labels.get(pipe_mode, pipe_mode)}[/cyan]")

        poly = self.playfield.get_polygon()
        if poly is not None:
            st.add_row("Playfield", "[green]OK[/green]")
        else:
            st.add_row("Playfield", "[red]NO — need ArUco 0-3[/red]")

        if self.deskew_overlay:
            if self.display.M_deskew is not None:
                st.add_row("Deskew", "[green]ON[/green]")
            else:
                st.add_row("Deskew", "[yellow]waiting[/yellow]")

        if has_world:
            st.add_row("Homography", "[green]OK[/green]")
        else:
            st.add_row("Homography", "[red]NO — no calibration[/red]")

        visible = sorted(r["id"] for r in sorted_rows if r["visible"])
        missing = sorted(r["id"] for r in sorted_rows if not r["visible"])
        st.add_row("Visible", f"[green]{visible}[/green]")
        if missing:
            st.add_row("Missing", f"[red]{missing}[/red]")

        return Columns([table, st])

    def _print_tui(self, tag_records: list, has_world: bool, pipe_mode: str = "color", fps: float = 0.0) -> None:
        """Update the Rich Live TUI dashboard."""
        from rich.live import Live
        from rich.console import Console

        layout = self._build_tui_layout(tag_records, has_world, pipe_mode=pipe_mode, fps=fps)

        if self._tui_live is None:
            console = Console()
            self._tui_live = Live(
                layout,
                console=console,
                refresh_per_second=15,
                screen=True,
            )
            self._tui_live.start()
        else:
            self._tui_live.update(layout)

    def _stop_tui(self) -> None:
        """Stop the Rich Live display if running."""
        if self._tui_live is not None:
            try:
                self._tui_live.stop()
            except Exception:
                pass
            self._tui_live = None

    @staticmethod
    def _get_dict_by_family(name: str):
        """Map family string to OpenCV ArUco predefined dictionary."""
        m = {
            "16h5": cv.aruco.DICT_APRILTAG_16h5,
            "25h9": cv.aruco.DICT_APRILTAG_25h9,
            "36h10": cv.aruco.DICT_APRILTAG_36h10,
            "36h11": cv.aruco.DICT_APRILTAG_36h11,
            "aruco_4x4": cv.aruco.DICT_4X4_50,
        }
        return m.get(name, cv.aruco.DICT_APRILTAG_36h11)

    def _build_detectors(self):
        """Create per-family ArUco detectors configured with AprilTag params."""
        fams = [self.family] if self.family != "all" else ["16h5", "25h9", "36h10", "36h11"]
        if self.detect_aruco_4x4:
            fams.append("aruco_4x4")
        detectors = []
        for f in fams:
            d = cv.aruco.getPredefinedDictionary(self._get_dict_by_family(f))
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
            }.get(self.corner_refine, cv.aruco.CORNER_REFINE_SUBPIX)
            p.aprilTagQuadDecimate = float(max(1.0, self.quad_decimate))
            p.aprilTagQuadSigma = float(max(0.0, self.quad_sigma))
            if hasattr(p, "aprilTagMinWhiteBlackDiff"):
                try:
                    p.aprilTagMinWhiteBlackDiff = int(max(0, int(round(self.april_min_wb_diff))))
                except Exception:
                    p.aprilTagMinWhiteBlackDiff = 3
            if hasattr(p, "aprilTagMinClusterPixels"):
                try:
                    p.aprilTagMinClusterPixels = int(max(1, int(self.april_min_cluster_pixels)))
                except Exception:
                    p.aprilTagMinClusterPixels = 5
            if hasattr(p, "aprilTagMaxLineFitMse"):
                try:
                    p.aprilTagMaxLineFitMse = float(max(1.0, float(self.april_max_line_fit_mse)))
                except Exception:
                    p.aprilTagMaxLineFitMse = 20.0
            p.detectInvertedMarker = bool(self.detect_inverted)
            detectors.append((d, p, f))
        return detectors

    @staticmethod
    def _maybe_preprocess(
        gray: np.ndarray,
        use_clahe: bool,
        use_sharpen: bool,
        use_highpass: bool = True,
        highpass_ksize: int = 51,
    ) -> np.ndarray:
        """Optionally apply preprocessing to a grayscale image.

        Illumination flattening is on by default — estimates the low-
        frequency illumination field via a large Gaussian blur and
        divides it out.  Because illumination is multiplicative
        (pixel = reflectance x illumination), division recovers the
        reflectance: a white tag square under dim light and one under
        bright glare both come out at the same value.
        """
        out = gray
        if use_highpass:
            k = highpass_ksize
            if k % 2 == 0:
                k += 1
            # Estimate the low-frequency illumination field
            illum = cv.GaussianBlur(out, (k, k), 0).astype(np.float32)
            # Clamp to avoid division by zero in very dark regions
            illum = np.maximum(illum, 1.0)
            # Divide out the illumination and rescale to 0-255
            flat = (out.astype(np.float32) / illum) * 128.0
            out = np.clip(flat, 0, 255).astype(np.uint8)
        if use_clahe:
            clahe = cv.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
            out = clahe.apply(out)
        if use_sharpen:
            k = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]], dtype=np.float32)
            out = cv.filter2D(out, -1, k)
        return out

    def detect_apriltags(self, frame_bgr: np.ndarray, scale: float = 1.0, gray: Optional[np.ndarray] = None) -> List[Tuple[np.ndarray, np.ndarray, int, str]]:
        """Detect AprilTags in a BGR frame.

        Delegates to :class:`~aprilcam.core.detector.TagDetector`.
        Downscaling for detection speed is handled by ``proc_width`` inside
        ``TagDetector``; the *scale* argument is accepted for back-compat
        but should be 1.0 — callers should not compute scale externally
        when ``proc_width`` is set.

        Args:
            frame_bgr: Input color frame in BGR order.
            scale: Ignored (kept for API compatibility). Downscaling is
                controlled by ``DetectorConfig.proc_width``.
            gray: Optional pre-computed grayscale image.

        Returns:
            A list of ``(pts[4x2], raw_pts[4x2], id, family)`` tuples for
            each detected tag.
        """
        raw = self._tag_detector.detect(frame_bgr, gray=gray)
        return [(det.corners, det.corners.copy(), det.id, det.family) for det in raw]

    @staticmethod
    def lk_track(prev_gray: np.ndarray, gray: np.ndarray, pts: np.ndarray) -> Optional[np.ndarray]:
        """Track points using pyramidal Lucas–Kanade optical flow.

        Returns the new points (Nx2) in the current frame or None on failure.
        """
        p0 = pts.reshape(-1, 1, 2).astype(np.float32)
        p1, st, err = cv.calcOpticalFlowPyrLK(prev_gray, gray, p0, None, winSize=(21, 21), maxLevel=3,
                                              criteria=(cv.TERM_CRITERIA_EPS | cv.TERM_CRITERIA_COUNT, 30, 0.01))
        if p1 is None or st is None or int(st.sum()) < 4:
            return None
        return p1.reshape(-1, 2)


    # ---------- core loop ----------
    def _init_capture(self) -> Optional[cv.VideoCapture]:
        """Open the capture device or use the provided cap; apply size hints.

        DAEMON-ONLY path: called only from AprilCam.run(), which is the
        daemon's full interactive loop.  The MCP server never calls run();
        it drives detection via DetectionLoop + process_frame() instead,
        so this VideoCapture(device_index) open is never reached from the
        MCP/CLI-client path.
        """
        if self.cap is None:
            self.cap = cv.VideoCapture(int(self.index), 0 if self.backend is None else int(self.backend))
        if not self.cap or not self.cap.isOpened():
            diag = diagnose_camera_failure(int(self.index))
            if not diag.get("exists", True):
                raise CameraNotFoundError(
                    f"Camera at index {self.index} does not exist."
                )
            blocking = diag.get("blocking_processes", [])
            if blocking:
                proc = blocking[0]
                raise CameraInUseError(
                    f"Camera {self.index} is in use by process "
                    f"'{proc['name']}' (PID {proc['pid']}). "
                    f"Kill it with: kill {proc['pid']}",
                    pid=proc["pid"],
                    process_name=proc["name"],
                )
            raise CameraError(f"Failed to open camera {self.index}")
        if self.cap_width:
            self.cap.set(cv.CAP_PROP_FRAME_WIDTH, int(self.cap_width))
        if self.cap_height:
            self.cap.set(cv.CAP_PROP_FRAME_HEIGHT, int(self.cap_height))
        return self.cap

    def _update_playfield(
        self,
        frame: np.ndarray,
        gray: Optional[np.ndarray] = None,
        detections: Optional[List[Tuple[np.ndarray, np.ndarray, int, str]]] = None,
    ) -> None:
        """Update cached playfield polygon via Playfield.

        Deskew is handled by PlayfieldDisplay; this only updates geometry.

        In static-camera mode the boundary also needs this frame's AprilTag
        centers (so AprilTag 1 can serve as a fill-in / movement sentinel), so
        the current *detections* are summarised to ``{id: (cx, cy)}`` and passed
        through.  In live-corner mode the boundary ignores them.
        """
        try:
            apriltags: Optional[dict] = None
            if detections:
                apriltags = {}
                for pts, _raw, tid, _fam in detections:
                    if tid > 0:  # AprilTags have positive ids
                        c = np.asarray(pts, dtype=np.float32).reshape(-1, 2).mean(axis=0)
                        apriltags[int(tid)] = (float(c[0]), float(c[1]))
            self.playfield.update(frame, gray=gray, apriltags=apriltags)
            poly = self.playfield.get_polygon()
            if poly is not None:
                self.play_poly = poly.astype(np.float32)
        except Exception:
            pass



    def process_frame(self, frame_bgr: np.ndarray, timestamp: float) -> List[TagRecord]:
        """Process a single BGR frame: detect/track tags and return TagRecords.

        This is the stateful detection/tracking core extracted from ``run()``.
        It updates ``self._prev_gray``, ``self._tracks``, ``self._tag_models``,
        and ``self._frame_idx``.

        The method does **not** open windows, call ``waitKey``, print, or read
        from a camera.

        Args:
            frame_bgr: A BGR image (numpy array) to process.
            timestamp: Monotonic timestamp for this frame.

        Returns:
            A list of :class:`TagRecord` for every tag detected/tracked in
            this frame.
        """
        # 2) Convert to gray and perform detection or faster LK tracking
        gray = cv.cvtColor(frame_bgr, cv.COLOR_BGR2GRAY)
        detections: List[Tuple[np.ndarray, np.ndarray, int, str]] = []
        if (self.detect_interval <= 1
                or self._frame_idx % max(1, self.detect_interval) == 0
                or self._prev_gray is None
                or not self._tracks):
            # TagDetector handles proc_width downscaling internally.
            detections = self.detect_apriltags(frame_bgr, gray=gray)
            self._tracks = {tid: pts for (pts, _raw, tid, _fam) in detections}
            self._track_families = {tid: fam for (_pts, _raw, tid, fam) in detections}
        else:
            # Track existing tag corners forward with LK; fall back to detection on loss
            new_tracks: dict[int, np.ndarray] = {}
            for tid, pts in self._tracks.items():
                new_pts = AprilCam.lk_track(self._prev_gray, gray, pts)
                if new_pts is not None:
                    new_tracks[tid] = new_pts
                    fam = self._track_families.get(tid, "36h11")
                    detections.append((new_pts, new_pts, tid, fam))
            self._tracks = new_tracks
            if len(detections) == 0:
                detections = self.detect_apriltags(frame_bgr, gray=gray)
                self._tracks = {tid: pts for (pts, _raw, tid, _fam) in detections}
                self._track_families = {tid: fam for (_pts, _raw, tid, fam) in detections}

        # 3) Update Playfield cache (polygon) for cropping/deskew.  Pass the
        # pre-filter detections so static-camera mode sees AprilTag sentinels
        # (e.g. AprilTag 1) even if they would later fall outside the polygon.
        self._update_playfield(frame_bgr, gray=gray, detections=detections)

        # 4) Keep only detections inside the current playfield polygon
        if detections:
            in_dets: List[Tuple[np.ndarray, np.ndarray, int, str]] = []
            for pts, raw, tid, fam in detections:
                if self.playfield.isIn(pts):
                    in_dets.append((pts, raw, tid, fam))
            detections = in_dets
            self._tracks = {tid: pts for (pts, _raw, tid, _fam) in detections}

        # 5) Update/maintain tag models and playfield flows
        for pts, _raw, tid, fam in detections:
            if tid in self._tag_models:
                self._tag_models[tid].update(pts, timestamp=timestamp, homography=self.homography)
            else:
                self._tag_models[tid] = AprilTagModel.from_corners(
                    tid, pts, homography=self.homography,
                    timestamp=timestamp, frame=self._frame_idx,
                    family=fam,
                )
            self._tag_models[tid].frame = self._frame_idx
            self.playfield.add_tag(self._tag_models[tid], homography=self.homography)

        # Prune models not seen for >1 second
        seen_ids = {tid for _pts, _r, tid, _fam in detections}
        stale_cutoff = 1.0  # seconds
        for tid in list(self._tag_models.keys()):
            if (tid not in seen_ids
                    and self._tag_models[tid].last_ts is not None
                    and (timestamp - float(self._tag_models[tid].last_ts)) > stale_cutoff):
                del self._tag_models[tid]

        # Build TagRecord objects for CURRENT detections (age=0)
        tag_records: List[TagRecord] = []
        flows = self.playfield.get_flows()
        for pts, _raw, tid, _fam in detections:
            model = self._tag_models.get(tid)
            if model is None:
                continue

            flow = flows.get(tid)
            vel_px_val: Optional[Tuple[float, float]] = flow.vel_px if flow else None
            speed_px_val: Optional[float] = flow.speed_px if flow else None

            tr = TagRecord.from_apriltag(
                model,
                vel_px=vel_px_val,
                speed_px=speed_px_val,
                vel_world=flow.vel_world if flow else None,
                speed_world=flow.speed_world if flow else None,
                heading_rad=flow.heading_rad if flow else None,
                timestamp=timestamp,
                frame_index=self._frame_idx,
                age=0.0,
            )
            tag_records.append(tr)

        # Add STALE tags (not seen this frame but seen within stale_cutoff)
        for tid, model in self._tag_models.items():
            if tid not in seen_ids and model.last_ts is not None:
                age = timestamp - float(model.last_ts)
                flow = flows.get(tid)
                vel_px_val = flow.vel_px if flow else None
                speed_px_val = flow.speed_px if flow else None
                tr = TagRecord.from_apriltag(
                    model,
                    vel_px=vel_px_val,
                    speed_px=speed_px_val,
                    vel_world=flow.vel_world if flow else None,
                    speed_world=flow.speed_world if flow else None,
                    heading_rad=flow.heading_rad if flow else None,
                    timestamp=timestamp,
                    frame_index=self._frame_idx,
                    age=age,
                )
                tag_records.append(tr)

        # Bookkeeping
        self._frame_idx += 1
        self._prev_gray = gray
        return tag_records

    def run(self, color_camera: Optional[int] = None) -> None:
        """Main capture/detect/track loop with display and overlays.

        Args:
            color_camera: Optional camera index for a color camera.
                When provided and 'd' is pressed, the color camera is
                used to classify object colors via HSV thresholding.

        Key bindings:
            q / Esc — quit
            Space   — pause / resume
            d       — run one-shot object detection, draw results (persists)
            c       — clear object detection overlays
        """
        cap = self._init_capture()
        if cap is None:
            return
        # Window is managed by PlayfieldDisplay

        self.reset_state()
        paused = False
        last_display: Optional[np.ndarray] = None
        # Persistent object overlays (list of ObjectRecord or None)
        _detected_objects: list | None = None
        # Persistent square detector that caches tag positions
        from aprilcam.vision.objects import SquareDetector as _SD
        _sq_detector = _SD()

        # Open color camera at startup if specified (avoids USB contention on each 'd')
        # DAEMON-ONLY path: this is inside run(), the daemon's interactive loop.
        # The MCP server never calls run(), so this VideoCapture open is not
        # reachable from the MCP/CLI-client path.
        _color_cap = None
        _color_cal = None
        if color_camera is not None:
            _color_cap = cv.VideoCapture(color_camera)
            if not _color_cap.isOpened():
                print(f"Warning: color camera {color_camera} failed to open")
                _color_cap = None
            else:
                # Load calibration
                try:
                    from aprilcam.calibration.calibration import load_calibration
                    all_cals = load_calibration()
                    for _name, _cal in all_cals.items():
                        if _cal.dist_coeffs is not None or _cal.resolution[0] > 1280:
                            _color_cal = _cal
                            break
                except Exception:
                    pass

        # Pipeline view modes — number keys switch the displayed image
        _PIPE_MODES = {
            ord("0"): "color",
            ord("1"): "gray",
            ord("2"): "flat",
            ord("3"): "clahe",
            ord("4"): "flat+clahe",
            ord("5"): "threshold",
        }
        _pipe_mode = "color"

        if not self.headless:
            print("Keys: [q]uit [space]pause [d]etect [c]lear  Views: [0]color [1]gray [2]hp [3]clahe [4]hp+clahe [5]thresh")

        _fps = 0.0
        _last_fps_time = time.monotonic()
        _fps_frame_count = 0

        try:
            while True:
                if not paused:
                    # 1) Read next frame
                    ok, frame = cap.read()
                    if not ok:
                        print("Camera read failed.")
                        break

                    now = time.monotonic()
                    tag_records = self.process_frame(frame, now)

                    # FPS calculation (smoothed over 1-second windows)
                    _fps_frame_count += 1
                    fps_elapsed = now - _last_fps_time
                    if fps_elapsed >= 1.0:
                        _fps = _fps_frame_count / fps_elapsed
                        _fps_frame_count = 0
                        _last_fps_time = now

                    # Feed tag positions to persistent detector for exclusion cache
                    if tag_records:
                        _sq_detector.update_known_tags(tag_records)

                    # 6) Optional TUI display (fixed-position, EMA-smoothed)
                    if self.print_tags and tag_records:
                        self._print_tui(tag_records, has_world=self.homography is not None, pipe_mode=_pipe_mode, fps=_fps)

                    # 7) Build pipeline debug images
                    gray_img = cv.cvtColor(frame, cv.COLOR_BGR2GRAY)
                    illum = cv.GaussianBlur(gray_img, (51, 51), 0).astype(np.float32)
                    illum = np.maximum(illum, 1.0)
                    flat_img = np.clip((gray_img.astype(np.float32) / illum) * 128.0, 0, 255).astype(np.uint8)
                    clahe_img = cv.createCLAHE(3.0, (8, 8)).apply(gray_img)
                    flat_clahe_img = cv.createCLAHE(3.0, (8, 8)).apply(flat_img)
                    _, thresh_img = cv.threshold(flat_img, 150, 255, cv.THRESH_BINARY)

                    if _pipe_mode == "color":
                        view_frame = frame
                    elif _pipe_mode == "gray":
                        view_frame = cv.cvtColor(gray_img, cv.COLOR_GRAY2BGR)
                    elif _pipe_mode == "flat":
                        view_frame = cv.cvtColor(flat_img, cv.COLOR_GRAY2BGR)
                    elif _pipe_mode == "clahe":
                        view_frame = cv.cvtColor(clahe_img, cv.COLOR_GRAY2BGR)
                    elif _pipe_mode == "flat+clahe":
                        view_frame = cv.cvtColor(flat_clahe_img, cv.COLOR_GRAY2BGR)
                    elif _pipe_mode == "threshold":
                        view_frame = cv.cvtColor(thresh_img, cv.COLOR_GRAY2BGR)
                    else:
                        view_frame = frame

                    # 8) Prepare display — playfield detection on raw, display on view
                    self.display.playfield.update(frame)
                    self.display._update_deskew(frame)
                    self.display._ensure_window()
                    display = self.display.prepare_display(view_frame)

                    flows = self.playfield.get_flows()
                    tags_for_overlay = list(flows.values())
                    self.display.draw_overlays(display if display is not None else view_frame, tags_for_overlay, homography=self.homography)

                    # Draw persistent object overlays if any
                    if _detected_objects:
                        _draw_object_boxes(display if display is not None else frame, _detected_objects)

                    last_display = display.copy()
                else:
                    # Paused branch: reuse last display buffer and show a pause overlay
                    if last_display is None:
                        ok, frame = cap.read()
                        if not ok:
                            print("Camera read failed.")
                            break
                        last_display = frame.copy()
                    display = last_display.copy()
                    if not self.headless:
                            self.display.pause(display)

                # 9) Present frame (if not headless) and process input
                if not self.headless:
                    self.display.show(display)
                    key = cv.waitKey(1) & 0xFF
                    if key in (27, ord('q')):
                        break
                    if key == ord(' '):
                        paused = not paused
                        continue
                    if key == ord('d'):
                        # One-shot object detection + color classification
                        # Uses joint calibration for world-coord fusion.
                        try:
                            from aprilcam.vision.objects import ObjectFuser
                            from dataclasses import replace as _replace

                            raw_gray = cv.cvtColor(frame, cv.COLOR_BGR2GRAY)
                            tag_corners = [
                                np.array(t.corners_px, dtype=np.float32)
                                for t in tag_records
                            ] if tag_records else []
                            pf_poly = self.playfield.get_polygon()

                            _detected_objects = _sq_detector.detect(
                                raw_gray,
                                homography=self.homography,
                                tag_corners=tag_corners,
                                playfield_polygon=pf_poly,
                            )
                            print(f"[d] {len(_detected_objects)} squares on B&W frame")

                            # Color classify BEFORE deskew remap — center_px
                            # is still in original frame coords at this point.
                            try:
                                from aprilcam.vision.color_classifier import ColorClassifier
                                classifier = ColorClassifier()
                                colored = []

                                if _color_cap is not None and _color_cal is not None:
                                    ret_c, cf = _color_cap.read()
                                    if ret_c and cf is not None:
                                        cf = _color_cal.undistort(cf)
                                        H_inv = np.linalg.inv(_color_cal.homography)
                                        for obj in _detected_objects:
                                            if obj.world_xy:
                                                vec = H_inv @ np.array([obj.world_xy[0], obj.world_xy[1], 1.0])
                                                cpx, cpy = vec[0] / vec[2], vec[1] / vec[2]
                                                c = classifier.classify_at_point(cf, cpx, cpy)
                                                colored.append(_replace(obj, color=c))
                                            else:
                                                colored.append(obj)
                                else:
                                    for obj in _detected_objects:
                                        cx, cy = obj.center_px
                                        c = classifier.classify_at_point(frame, cx, cy)
                                        colored.append(_replace(obj, color=c))
                                _detected_objects = colored
                            except Exception:
                                pass

                            # Map to display coords if deskewed
                            deskew_M = self.playfield.get_deskew_matrix()
                            if display is not None and deskew_M is not None:
                                mapped = []
                                for obj in _detected_objects:
                                    cx, cy = obj.center_px
                                    pt = deskew_M @ np.array([cx, cy, 1.0])
                                    if abs(pt[2]) > 1e-9:
                                        ncx, ncy = pt[0] / pt[2], pt[1] / pt[2]
                                        x, y, w, h = obj.bbox
                                        mapped.append(_replace(
                                            obj,
                                            center_px=(float(ncx), float(ncy)),
                                            bbox=(int(ncx - w/2), int(ncy - h/2), w, h),
                                        ))
                                _detected_objects = mapped

                            n = len(_detected_objects)
                            print(f"[d] {n} object{'s' if n != 1 else ''} — press [c] to clear")
                        except Exception as e:
                            import traceback
                            print(f"[d] detection failed: {e}")
                            traceback.print_exc()
                    if key == ord('c'):
                        _detected_objects = None
                    if key in _PIPE_MODES:
                        _pipe_mode = _PIPE_MODES[key]
                else:
                    # Headless: small sleep to avoid tight loop
                    time.sleep(0.001)
        finally:
            # Cleanup resources
            self._stop_tui()
            try:
                if _color_cap is not None:
                    _color_cap.release()
            except Exception:
                pass
            try:
                if self.cap is not None:
                    self.cap.release()
            except Exception:
                pass
            try:
                if not self.headless:
                    cv.destroyAllWindows()
            except Exception:
                pass


def save_last_camera(idx: int):
    try:
        p = get_data_dir() / "last_camera"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(int(idx)))
    except Exception:
        pass


def load_last_camera() -> Optional[int]:
    try:
        p = get_data_dir() / "last_camera"
        if p.exists():
            return int(p.read_text().strip())
    except Exception:
        return None
    return None


 


# CLI main moved to aprilcam.cli.aprilcam_cli


# Module-level build_detectors() and detect_apriltags() were removed.
# Use TagDetector / DetectorConfig from aprilcam.core.detector instead.
