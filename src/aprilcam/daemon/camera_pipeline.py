"""
aprilcam.daemon.camera_pipeline — per-camera capture, detection, and fan-out.

CameraPipeline owns one camera index.  It:
  - opens a cv.VideoCapture on start()
  - loads calibration for the camera (if available)
  - runs a background capture thread that:
      * reads frames from the camera
      * calls AprilCam.process_frame()
      * JPEG-encodes the result
      * calls ImageStreamProducer.publish() if a producer is set
      * builds a TagFrame protobuf and calls TagStreamProducer.publish_if_changed()
        if a tag producer is set
  - writes info.json atomically to <data_dir>/<cam_name>/info.json

Stream producers (ImageStreamProducer / TagStreamProducer) are injected via
set_producers() after the pipeline is created.  If no producers are set the
pipeline runs silently (useful for capture_frame() RPC-only use).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from shutil import which
from typing import Dict, List, Optional

import cv2 as cv
import numpy as np

from ..calibration.calibration import (
    load_calibration_from_camera_dir,
    load_field_dimensions_from_camera_dir,
)
from ..config import Config
from ..core.aprilcam import AprilCam
from ..core.detection import FrameRecord, RingBuffer, TagRecord
from ..proto import aprilcam_pb2

log = logging.getLogger(__name__)

_JPEG_QUALITY = 85


def _apply_camera_settings(
    settings: Dict,
    device_name: str,
    config: Config,
) -> None:
    """Apply hardware control settings to a camera using the configured program.

    Currently supports ``"program": "uvc-util"``.  Searches for the binary
    at ``<env_dir>/bin/uvc-util`` first, then falls back to ``PATH``.
    Each control in ``settings["controls"]`` is applied as ``-s key=value``.
    """
    program = settings.get("program")
    controls: Dict[str, str] = settings.get("controls", {})
    if not controls:
        return

    if program != "uvc-util":
        log.warning("Unknown camera settings program %r; skipping", program)
        return

    # Locate uvc-util binary
    uvc: Optional[Path] = None
    if config.env_dir:
        candidate = config.env_dir / "bin" / "uvc-util"
        if candidate.exists() and os.access(candidate, os.X_OK):
            uvc = candidate
    if uvc is None:
        found = which("uvc-util")
        if found:
            uvc = Path(found)
    if uvc is None:
        log.warning("uvc-util not found; skipping camera settings for %s", device_name)
        return

    log.info("Applying uvc-util settings to %s", device_name)
    for ctrl, value in controls.items():
        cmd = [str(uvc), "-N", device_name, "-s", f"{ctrl}={value}"]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if result.returncode != 0:
                log.warning(
                    "uvc-util %s=%s returned %d: %s",
                    ctrl, value, result.returncode, result.stderr.strip(),
                )
        except Exception as exc:
            log.warning("uvc-util error applying %s=%s: %s", ctrl, value, exc)


_RING_BUFFER_SIZE = 300
_FPS_WINDOW = 30  # number of recent frame timestamps to keep for rolling FPS


class CameraPipeline:
    """Capture, detect, encode, and publish for a single camera.

    Lifecycle::

        pipeline = CameraPipeline("cam0", 0, config)
        pipeline.set_producers(image_producer, tag_producer)
        pipeline.start()
        ...
        pipeline.stop()

    If no producers are set, the pipeline runs without publishing frames
    (useful for capture_frame() RPC-only use).
    """

    def __init__(
        self,
        cam_name: str,
        index: int,
        config: Config,
        detection_fps: int = 10,
    ) -> None:
        """Set up state only.  Does NOT open the camera.

        Args:
            cam_name:       Human-readable camera name (used as directory key).
            index:          OpenCV camera index.
            config:         Daemon configuration (paths, etc.).
            detection_fps:  Target detection loop rate in Hz (default 10).
        """
        self.cam_name = cam_name
        self.index = index
        self.config = config
        self._detection_fps = max(1, detection_fps)

        # Camera and detection state (populated on start())
        self._cap: Optional[cv.VideoCapture] = None
        self._april_cam: Optional[AprilCam] = None
        self._calibration = None  # CameraCalibration | None
        self._tag_heights: dict[int, float] = {}  # loaded from data_dir/tags.json
        self.device_name: str = cam_name  # resolved to OS name in start()

        # Ring buffer for tag history
        self._ring: RingBuffer = RingBuffer(maxlen=_RING_BUFFER_SIZE)

        # Latest raw frame (JPEG bytes) for capture_frame() RPC
        self._latest_raw_jpeg: Optional[bytes] = None
        # Latest raw frame (ndarray) so capture_frame() can encode on demand
        # when the loop is not already producing JPEGs for an image subscriber.
        self._latest_raw_frame = None
        self._latest_frame_id: int = -1
        self._latest_jpeg_frame_id: int = -1
        self._raw_lock = threading.Lock()

        # Idle-gating: monotonic time of the last pull RPC (capture_frame /
        # get_current_tags).  When there are no stream subscribers and no recent
        # pull, the capture loop drops to a slow idle rate and skips JPEG
        # encoding so the daemon does not burn CPU for nobody.
        self._last_pull_mono: float = 0.0
        self._idle_grace_s: float = 3.0
        self._idle_interval_s: float = 1.0

        # Stream producers (optional — injected after construction)
        self._image_producer = None   # ImageStreamProducer | None
        self._tag_producer = None     # TagStreamProducer | None
        self._producers_lock = threading.Lock()

        # Frame counter
        self._frame_id: int = 0

        # Rolling FPS (deque of monotonic timestamps)
        self._ts_deque: deque[float] = deque(maxlen=_FPS_WINDOW)

        # Thread control
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Producer injection
    # ------------------------------------------------------------------

    def set_producers(self, image_producer, tag_producer) -> None:
        """Attach stream producers.

        Args:
            image_producer: ``ImageStreamProducer`` or ``None``.
            tag_producer:   ``TagStreamProducer`` or ``None``.
        """
        with self._producers_lock:
            self._image_producer = image_producer
            self._tag_producer = tag_producer

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Open the camera, load calibration, write info.json, start thread."""
        if self._thread is not None and self._thread.is_alive():
            log.warning("CameraPipeline(%s): already running", self.cam_name)
            return

        # Resolve device name before opening — calibration is keyed by OS name.
        from ..camera.camutil import get_device_name
        device_name = get_device_name(self.index)
        if not device_name:
            device_name = self.cam_name
        self.device_name = device_name

        # Load calibration from <cameras_dir>/<cam_name>/calibration.json
        camera_dir = self.config.cameras_dir / self.cam_name
        self._calibration = load_calibration_from_camera_dir(camera_dir)
        tags_file = self.config.data_dir / "tags.json"
        try:
            import json as _json
            raw = _json.loads(tags_file.read_text())
            self._tag_heights = {int(k): float(v) for k, v in raw.get("tag_heights", {}).items()}
        except Exception:
            self._tag_heights = {}

        # Open camera
        cap = cv.VideoCapture(self.index)
        if not cap.isOpened():
            raise RuntimeError(
                f"CameraPipeline: failed to open camera index {self.index}"
            )
        self._cap = cap

        # Drain a few frames so AVFoundation finishes initialising its capture
        # session before we apply UVC controls — otherwise the OS resets them.
        for _ in range(5):
            cap.read()

        # Apply hardware settings now that the capture session is stable.
        # Prefer settings from config.json (the authoritative source after the
        # config/calibration split).  Fall back to calibration.settings for
        # legacy un-migrated cameras that still carry settings there.
        from ..camera.camera_config import load_camera_config as _load_cfg
        _cam_cfg = _load_cfg(camera_dir)
        _settings = (
            (_cam_cfg.get("settings") if _cam_cfg else None)
            or (self._calibration.settings if self._calibration is not None else None)
        )
        if _settings:
            _apply_camera_settings(_settings, device_name, self.config)

        # Build AprilCam instance (headless, no display)
        homography: Optional[np.ndarray] = None
        if self._calibration is not None:
            homography = self._calibration.homography

        # Build pipeline kwargs from calibration's pipeline section (if present).
        # All keys map directly to AprilCam constructor parameters.
        pipeline_cfg: dict = {}
        if self._calibration is not None and self._calibration.pipeline:
            pipeline_cfg = dict(self._calibration.pipeline)

        self._april_cam = AprilCam(
            index=self.index,
            backend=None,
            speed_alpha=0.1,
            family=pipeline_cfg.pop("family", "36h11"),
            proc_width=pipeline_cfg.pop("proc_width", 0),
            cap=self._cap,
            homography=homography,
            headless=True,
            **pipeline_cfg,
        )

        # Seed static-camera geometry (corner pixels + static markers) into the
        # AprilCam playfield boundary so static-mode fill-in / movement
        # invalidation activate when the saved calibration carries them.
        self._seed_static_geometry()

        # Determine frame size
        frame_w = int(cap.get(cv.CAP_PROP_FRAME_WIDTH))
        frame_h = int(cap.get(cv.CAP_PROP_FRAME_HEIGHT))

        # Track the stale-calibration flag last written to info.json so the
        # capture loop only rewrites the file when it flips.
        self._last_stale_written: bool = False

        # Write info.json
        self._write_info_json(frame_w, frame_h, homography, device_name)
        self._info_frame_size = (frame_w, frame_h)
        self._info_device_name = device_name
        self._info_homography = homography

        # Start capture thread
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._capture_loop,
            name=f"aprilcam-{self.cam_name}",
            daemon=True,
        )
        self._thread.start()
        log.info("CameraPipeline(%s): started (index=%d)", self.cam_name, self.index)

    def stop(self) -> None:
        """Signal the capture thread to stop, then release the camera."""
        if self._thread is not None:
            self._stop_event.set()
            self._thread.join(timeout=5.0)
            if self._thread.is_alive():
                log.warning(
                    "CameraPipeline(%s): thread did not stop cleanly", self.cam_name
                )
            self._thread = None

        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None

        log.info("CameraPipeline(%s): stopped", self.cam_name)

    # ------------------------------------------------------------------
    # Public query
    # ------------------------------------------------------------------

    def capture_frame(self) -> Optional[bytes]:
        """Return the most recent raw camera frame as JPEG bytes.

        Encodes on demand from the latest raw frame when the capture loop is
        not already producing JPEGs for an image-stream subscriber, so a pull
        always gets a current frame.  Records the pull so the loop stays at
        full rate.  Returns ``None`` if no frame has been captured yet.
        """
        self._last_pull_mono = time.monotonic()
        with self._raw_lock:
            if (
                self._latest_raw_jpeg is not None
                and self._latest_jpeg_frame_id == self._latest_frame_id
            ):
                return self._latest_raw_jpeg
            frame = self._latest_raw_frame
            fid = self._latest_frame_id
        if frame is None:
            return None
        ok, buf = cv.imencode(".jpg", frame, [cv.IMWRITE_JPEG_QUALITY, _JPEG_QUALITY])
        if not ok:
            return None
        jpeg = buf.tobytes()
        with self._raw_lock:
            self._latest_raw_jpeg = jpeg
            self._latest_jpeg_frame_id = fid
        return jpeg

    def get_current_tags(self) -> "aprilcam_pb2.TagFrameResponse":
        """Return a ``TagFrameResponse`` built from the latest ring-buffer entry.

        Returns an empty ``TagFrameResponse`` when no frames have been
        captured yet.
        """
        self._last_pull_mono = time.monotonic()
        latest = self._ring.get_latest()
        if latest is None:
            return aprilcam_pb2.TagFrameResponse()

        tag_records = latest.tags
        homography = (
            self._april_cam.homography if self._april_cam is not None else None
        )

        # Homography: flatten 3×3 → 9 floats, row-major
        homo_flat: list = []
        if homography is not None:
            homo_flat = homography.flatten().tolist()

        # Playfield corners: flatten 4×2 → 8 floats
        corners_flat: list = []
        if self._april_cam is not None:
            poly = self._april_cam.playfield.get_polygon()
            if poly is not None:
                for pt in poly:
                    corners_flat.extend([float(pt[0]), float(pt[1])])

        # Build TagMsg list
        tag_msgs = []
        for tr in tag_records:
            cx, cy = tr.center_px
            wx, wy = tr.world_xy if tr.world_xy is not None else (0.0, 0.0)
            vx_px, vy_px = tr.vel_px if tr.vel_px is not None else (0.0, 0.0)
            vx_w, vy_w = tr.vel_world if tr.vel_world is not None else (0.0, 0.0)
            corners_flat_tag: list = []
            for corner in tr.corners_px:
                corners_flat_tag.extend([float(corner[0]), float(corner[1])])
            tag_msgs.append(
                aprilcam_pb2.TagMsg(
                    id=tr.id,
                    cx_px=float(cx),
                    cy_px=float(cy),
                    corners_px=corners_flat_tag,
                    yaw=float(tr.orientation_yaw),
                    wx=float(wx),
                    wy=float(wy),
                    in_playfield=bool(tr.in_playfield),
                    vx_px=float(vx_px),
                    vy_px=float(vy_px),
                    speed_px=float(tr.speed_px) if tr.speed_px is not None else 0.0,
                    vx_world=float(vx_w),
                    vy_world=float(vy_w),
                    speed_world=float(tr.speed_world) if tr.speed_world is not None else 0.0,
                    heading_rad=float(tr.heading_rad) if tr.heading_rad is not None else 0.0,
                    age=float(tr.age),
                )
            )

        cal_fw = self._calibration.playfield_width_cm if self._calibration else 0.0
        cal_fh = self._calibration.playfield_height_cm if self._calibration else 0.0
        origin_x, origin_y = self._a1_origin()

        return aprilcam_pb2.TagFrameResponse(
            frame_id=latest.frame_index,
            tags=tag_msgs,
            homography=homo_flat,
            playfield_corners=corners_flat,
            field_width_cm=float(cal_fw),
            field_height_cm=float(cal_fh),
            origin_x=float(origin_x),
            origin_y=float(origin_y),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _seed_static_geometry(self) -> None:
        """Seed the AprilCam playfield boundary for static-camera mode.

        Static-camera deskew is **auto-on**: whenever the saved calibration
        carries a homography, this seeds the boundary's ``corner_pixels`` /
        ``static_markers`` / ``static_marker_ids`` and physical dimensions so the
        deskew polygon is non-``None`` before any live frame and static-marker
        fill-in / movement-invalidation (ticket 006) activate without live ArUco
        corner detection.  The metric output resolution (``px_per_cm``) and the
        movement-invalidation threshold are read from config.

        The ``APRILCAM_STATIC_DESKEW`` config flag overrides this: when it is
        disabled, this is a no-op and the legacy live-corner path is preserved
        even when a calibration exists.  Also a no-op when there is no
        calibration / no homography.
        """
        if self._april_cam is None or self._calibration is None:
            return
        cal = self._calibration
        boundary = self._april_cam.playfield

        # Wire optional pre-warp undistortion into the display deskew path.
        # No-op unless APRILCAM_UNDISTORT is on and the calibration carries
        # camera_matrix + dist_coeffs.
        display = getattr(self._april_cam, "display", None)
        if display is not None:
            display.calibration = cal
            display.undistort_enabled = bool(self.config.undistort)

        # Config-gated knobs (sprint 011, ticket 007).
        boundary.px_per_cm = self.config.deskew_px_per_cm
        boundary.movement_threshold_px = self.config.movement_threshold_px
        # Static-camera mode override: False forces the live-corner path even
        # when static markers exist; True/None keep the auto-on behaviour.
        boundary.static_mode = True if self.config.static_deskew else False

        # Operator opted out of static-camera deskew: leave the boundary in
        # live-corner mode (no seeding) so it detects ArUco corners per frame.
        if not self.config.static_deskew:
            return

        boundary.homography = cal.homography
        boundary.width_cm = cal.playfield_width_cm
        boundary.height_cm = cal.playfield_height_cm
        if cal.static_marker_ids is not None:
            boundary.static_marker_ids = list(cal.static_marker_ids)
        if cal.static_markers:
            seeded = {
                str(key): (float(rec["pixel"][0]), float(rec["pixel"][1]))
                for key, rec in cal.static_markers.items()
                if rec.get("pixel") and len(rec["pixel"]) >= 2
            }
            if seeded:
                boundary.static_markers = seeded
                boundary._held_static = dict(seeded)
        if cal.corner_pixels is not None:
            try:
                boundary._poly = np.asarray(
                    cal.corner_pixels, dtype=np.float32
                ).reshape(4, 2)
            except Exception:
                pass
        elif (
            boundary._poly is None
            and cal.homography is not None
            and cal.playfield_width_cm > 0
            and cal.playfield_height_cm > 0
        ):
            # No persisted corner pixels: derive the deskew polygon from H⁻¹ so
            # a fixed camera deskews with zero recalibration (the floor path).
            try:
                from ..calibration.geometry import corner_pixels_from_homography
                boundary._poly = corner_pixels_from_homography(
                    cal.homography,
                    cal.playfield_width_cm,
                    cal.playfield_height_cm,
                )
            except Exception:
                pass

    def _maybe_update_stale_flag(self) -> None:
        """Rewrite info.json if the boundary's stale-calibration flag flipped.

        The movement-invalidation flag lives on the playfield boundary; the
        daemon surfaces it to clients through the per-camera ``info.json``.  Only
        rewrites when the flag changes to avoid per-frame disk churn.
        """
        if self._april_cam is None:
            return
        boundary = self._april_cam.playfield
        stale = bool(getattr(boundary, "calibration_stale", False))
        if stale == self._last_stale_written:
            return
        self._last_stale_written = stale
        fw, fh = self._info_frame_size
        self._write_info_json(fw, fh, self._info_homography, self._info_device_name)

    def _write_info_json(
        self,
        frame_w: int,
        frame_h: int,
        homography: Optional[np.ndarray],
        device_name: str = "",
    ) -> None:
        """Write <cameras_dir>/<cam_name>/info.json atomically."""
        cam_dir = self.config.cameras_dir / self.cam_name
        cam_dir.mkdir(parents=True, exist_ok=True)

        # Surface the movement-invalidation flag from the playfield boundary so
        # clients can tell when the static deskew transform has gone stale.
        stale = False
        if self._april_cam is not None:
            stale = bool(getattr(self._april_cam.playfield, "calibration_stale", False))

        info = {
            "paths_file": str(cam_dir / "paths.json"),
            "calibration_stale": stale,
        }
        dest = cam_dir / "info.json"
        tmp = cam_dir / "info.json.tmp"
        tmp.write_text(json.dumps(info, indent=2))
        tmp.rename(dest)

    def _rolling_fps(self) -> float:
        """Compute FPS as frames / elapsed over the rolling window."""
        now = time.monotonic()
        self._ts_deque.append(now)
        if len(self._ts_deque) < 2:
            return 0.0
        elapsed = self._ts_deque[-1] - self._ts_deque[0]
        if elapsed <= 0.0:
            return 0.0
        return (len(self._ts_deque) - 1) / elapsed

    def _a1_origin(self) -> tuple[float, float]:
        """Return the (origin_x, origin_y) used to express ``world_xy`` in the
        A1-centred frame.

        This MUST match the origin applied to ``world_xy`` in the capture loop
        so that consumers (e.g. the live view) can invert the transform and
        project A1-centred world coordinates back to pixels.  Prefers the
        calibration-time world position of AprilTag 1 (the physical origin);
        falls back to the geometric field centre (half the playfield
        dimensions) when that marker was not recorded.
        """
        cal = self._calibration
        if cal is None:
            return (0.0, 0.0)
        origin_x = cal.playfield_width_cm / 2.0
        origin_y = cal.playfield_height_cm / 2.0
        if cal.static_markers:
            _m = cal.static_markers.get("apriltag:1")
            if _m and _m.get("world") and len(_m["world"]) >= 2:
                origin_x = float(_m["world"][0])
                origin_y = float(_m["world"][1])
        return (origin_x, origin_y)

    def _build_tag_frame(
        self,
        tag_records: List[TagRecord],
        homography,  # np.ndarray | None
        fps: float,
        ts_mono_ns: int,
        ts_wall_ms: int,
    ) -> "aprilcam_pb2.TagFrame":
        """Build a protobuf TagFrame from the current detection results."""
        assert self._april_cam is not None

        # Homography: flatten 3x3 → 9 floats, row-major
        homo_flat: list = []
        if homography is not None:
            homo_flat = homography.flatten().tolist()

        # Playfield corners: flatten 4×2 → 8 floats
        poly = self._april_cam.playfield.get_polygon()
        corners_flat: list = []
        if poly is not None:
            for pt in poly:
                corners_flat.extend([float(pt[0]), float(pt[1])])

        # Build TagMsg list
        tag_msgs = []
        for tr in tag_records:
            cx, cy = tr.center_px
            wx, wy = tr.world_xy if tr.world_xy is not None else (0.0, 0.0)
            vx_px, vy_px = tr.vel_px if tr.vel_px is not None else (0.0, 0.0)
            vx_w, vy_w = tr.vel_world if tr.vel_world is not None else (0.0, 0.0)
            corners_flat_tag: list = []
            for corner in tr.corners_px:
                corners_flat_tag.extend([float(corner[0]), float(corner[1])])
            tag_msg = aprilcam_pb2.TagMsg(
                id=tr.id,
                cx_px=float(cx),
                cy_px=float(cy),
                corners_px=corners_flat_tag,
                yaw=float(tr.orientation_yaw),
                wx=float(wx),
                wy=float(wy),
                in_playfield=bool(tr.in_playfield),
                vx_px=float(vx_px),
                vy_px=float(vy_px),
                speed_px=float(tr.speed_px) if tr.speed_px is not None else 0.0,
                vx_world=float(vx_w),
                vy_world=float(vy_w),
                speed_world=float(tr.speed_world) if tr.speed_world is not None else 0.0,
                heading_rad=float(tr.heading_rad) if tr.heading_rad is not None else 0.0,
                age=float(tr.age),
            )
            tag_msgs.append(tag_msg)

        field_w = self._calibration.playfield_width_cm if self._calibration else 0.0
        field_h = self._calibration.playfield_height_cm if self._calibration else 0.0
        origin_x, origin_y = self._a1_origin()

        return aprilcam_pb2.TagFrame(
            frame_id=self._frame_id,
            ts_mono_ns=ts_mono_ns,
            ts_wall_ms=ts_wall_ms,
            tags=tag_msgs,
            homography=homo_flat,
            playfield_corners=corners_flat,
            fps=float(fps),
            field_width_cm=float(field_w),
            field_height_cm=float(field_h),
            origin_x=float(origin_x),
            origin_y=float(origin_y),
        )

    def _capture_loop(self) -> None:
        """Background thread: read → detect → encode → publish."""
        assert self._cap is not None
        assert self._april_cam is not None

        homography = self._april_cam.homography

        # Set a read timeout so cap.read() doesn't block forever if the
        # camera is unplugged (POSIX: CAP_PROP_READ_TIMEOUT_MSEC, best-effort).
        self._cap.set(cv.CAP_PROP_BUFFERSIZE, 1)

        consecutive_failures = 0
        while not self._stop_event.is_set():
            frame_start = time.monotonic()
            ret, frame = self._cap.read()
            if not ret or frame is None:
                consecutive_failures += 1
                if consecutive_failures >= 5:
                    log.warning(
                        "CameraPipeline(%s): camera read failed repeatedly, stopping",
                        self.cam_name,
                    )
                    break
                time.sleep(0.05)
                continue
            consecutive_failures = 0

            now_mono = time.monotonic()
            ts_mono_ns = time.monotonic_ns()
            ts_wall_ms = int(time.time() * 1000)

            # Resolve current consumers up front — drives idle-gating.
            with self._producers_lock:
                image_prod = self._image_producer
                tag_prod = self._tag_producer
            img_subs = image_prod.has_subscribers() if image_prod is not None else False
            tag_subs = tag_prod.has_subscribers() if tag_prod is not None else False

            # Always keep the latest raw frame so capture_frame() can encode on
            # demand.  Only JPEG-encode here when an image-stream subscriber is
            # live; capture_frame() encodes lazily otherwise.
            with self._raw_lock:
                self._latest_raw_frame = frame
                self._latest_frame_id = self._frame_id
            frame_jpeg = b""
            if img_subs:
                ok_raw, raw_buf = cv.imencode(
                    ".jpg", frame, [cv.IMWRITE_JPEG_QUALITY, _JPEG_QUALITY]
                )
                if ok_raw:
                    frame_jpeg = raw_buf.tobytes()
                    with self._raw_lock:
                        self._latest_raw_jpeg = frame_jpeg
                        self._latest_jpeg_frame_id = self._frame_id

            # Run detection / tracking
            try:
                tag_records: List[TagRecord] = self._april_cam.process_frame(
                    frame, now_mono
                )
            except Exception:
                log.exception(
                    "CameraPipeline(%s): process_frame error", self.cam_name
                )
                tag_records = []

            # Surface movement-invalidation: if the boundary flagged the static
            # calibration stale this frame, reflect it in info.json.
            self._maybe_update_stale_flag()

            # Re-centre world_xy on AprilTag 1 and apply parallax correction.
            # The def-driven homography is already A1-centred with +y pointing
            # NORTH (north markers map to +y; see calibrate_from_playfield_def /
            # geometry._world_corners), so this is a pure origin shift — NOT a
            # y-flip.  Subtracting origin in BOTH axes keeps north = +y, matching
            # the operator map.  (A stale `origin_y - y` here inverted north/south.)
            if self._calibration and (
                self._calibration.playfield_width_cm > 0
                or self._calibration.camera_position is not None
            ):
                import dataclasses as _dc
                origin_x, origin_y = self._a1_origin()
                corrected = []
                for tr in tag_records:
                    if tr.world_xy is None:
                        corrected.append(tr)
                        continue
                    wx = tr.world_xy[0] - origin_x
                    wy = tr.world_xy[1] - origin_y
                    tag_h = self._tag_heights.get(tr.id, 0.0)
                    if self._calibration.camera_position and tag_h > 0.0:
                        wx, wy = self._calibration.correct_world_for_height(wx, wy, tag_h)
                    tr = _dc.replace(tr, world_xy=(wx, wy))
                    corrected.append(tr)
                tag_records = corrected

            # Store in ring buffer
            frame_record = FrameRecord(
                timestamp=now_mono,
                frame_index=self._frame_id,
                tags=tag_records,
            )
            self._ring.append(frame_record)

            # Compute rolling FPS
            fps = self._rolling_fps()

            # Frame size
            frame_h, frame_w = frame.shape[:2]

            # image_prod / tag_prod / frame_jpeg were resolved at the top of the
            # loop (frame_jpeg is empty unless an image subscriber is live).
            if image_prod is not None and frame_jpeg:
                try:
                    image_prod.publish(
                        self._frame_id,
                        ts_mono_ns,
                        ts_wall_ms,
                        frame_jpeg,
                        frame_w,
                        frame_h,
                    )
                except Exception:
                    log.exception(
                        "CameraPipeline(%s): image_producer.publish error",
                        self.cam_name,
                    )

            # Build and publish TagFrame protobuf (if tag producer is set)
            if tag_prod is not None:
                try:
                    tag_frame_proto = self._build_tag_frame(
                        tag_records, homography, fps, ts_mono_ns, ts_wall_ms
                    )
                    tag_prod.publish_if_changed(tag_frame_proto)
                except Exception:
                    log.exception(
                        "CameraPipeline(%s): tag_producer.publish_if_changed error",
                        self.cam_name,
                    )

            self._frame_id += 1

            # Throttle to the configured rate when something is consuming;
            # idle down to a slow rate when there are no subscribers and no
            # recent pull, so the daemon stops burning CPU for nobody.
            now_end = time.monotonic()
            active = (
                img_subs
                or tag_subs
                or (now_end - self._last_pull_mono) < self._idle_grace_s
            )
            target_interval = (
                1.0 / self._detection_fps if active else self._idle_interval_s
            )
            sleep_time = target_interval - (now_end - frame_start)
            if sleep_time > 0:
                time.sleep(sleep_time)
