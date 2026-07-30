"""Background detection pipeline orchestrating detect-track-velocity-buffer.

The :class:`DetectionPipeline` reads frames from a camera, runs tag
detection (every N frames) with optical-flow tracking in between,
computes per-tag velocity, and writes results to a thread-safe ring
buffer.
"""

from __future__ import annotations

import math
import threading
import time
from typing import Callable, Optional

import cv2 as cv
import numpy as np

from .detection import TagRecord, FrameRecord, RingBuffer
from .detector import TagDetector, Detection
from .models import AprilTag as AprilTagModel, world_yaw
from .motion import VelocityEstimator
from .tracker import OpticalFlowTracker


class DetectionPipeline:
    """Background thread running the full detection pipeline.

    Lifecycle::

        pipeline = DetectionPipeline(camera, detector, tracker)
        pipeline.start()
        ...
        latest = pipeline.ring_buffer.get_latest()
        ...
        pipeline.stop()

    The pipeline is also usable as a context manager.
    """

    def __init__(
        self,
        camera,
        detector: TagDetector,
        tracker: OpticalFlowTracker,
        *,
        homography: np.ndarray | None = None,
        boundary=None,
        ring_buffer: RingBuffer | None = None,
        ema_alpha: float = 0.3,
        deadband: float = 50.0,
    ) -> None:
        self._camera = camera
        self._detector = detector
        self._tracker = tracker
        self._homography = homography
        self._boundary = boundary  # PlayfieldBoundary for polygon filtering
        self._ring_buffer = ring_buffer if ring_buffer is not None else RingBuffer()
        self._ema_alpha = ema_alpha
        self._deadband = deadband

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._frame_count = 0
        self._error: Exception | None = None
        self._last_frame: np.ndarray | None = None
        self._on_frame: Callable[[FrameRecord], None] | None = None

        # Per-tag state
        self._velocities: dict[int, VelocityEstimator] = {}
        self._tag_models: dict[int, AprilTagModel] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def ring_buffer(self) -> RingBuffer:
        return self._ring_buffer

    @property
    def is_running(self) -> bool:
        return (self._thread is not None and self._thread.is_alive()
                and not self._stop_event.is_set())

    @property
    def frame_count(self) -> int:
        return self._frame_count

    @property
    def error(self) -> Exception | None:
        return self._error

    @property
    def last_frame(self) -> np.ndarray | None:
        return self._last_frame

    def on_frame(self, callback: Callable[[FrameRecord], None] | None) -> None:
        """Register (or clear) a callback invoked after each frame."""
        self._on_frame = callback

    def start(self) -> None:
        """Start the background detection thread. Idempotent."""
        if self.is_running:
            return
        self._stop_event.clear()
        self._frame_count = 0
        self._error = None
        self._velocities.clear()
        self._tag_models.clear()
        self._tracker.reset()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the background thread. Idempotent."""
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.stop()

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    def _run(self) -> None:
        max_failures = 10
        consecutive_failures = 0

        while not self._stop_event.is_set():
            try:
                ret, frame = self._camera.read()
                if not ret:
                    consecutive_failures += 1
                    if consecutive_failures >= max_failures:
                        break
                    continue

                ts = time.monotonic()
                self._last_frame = frame
                gray = cv.cvtColor(frame, cv.COLOR_BGR2GRAY)

                # Detect or track
                if self._tracker.should_detect():
                    detections = self._detector.detect(frame, gray=gray)
                    detections = self._tracker.update(gray, detections)
                else:
                    detections = self._tracker.update(gray)
                    if not detections:
                        # Tracking failed — force detection
                        detections = self._detector.detect(frame, gray=gray)
                        detections = self._tracker.update(gray, detections)

                # Update boundary if available
                if self._boundary is not None:
                    self._boundary.update(frame, gray=gray)

                # Filter by boundary polygon
                if self._boundary is not None:
                    filtered = []
                    for d in detections:
                        try:
                            if self._boundary.isIn(d.center):
                                filtered.append(d)
                        except Exception:
                            filtered.append(d)
                    detections = filtered

                # Update tag models and velocity
                seen_ids = set()
                for d in detections:
                    seen_ids.add(d.id)
                    model = self._tag_models.get(d.id)
                    if model is not None:
                        model.update(d.corners, timestamp=ts, homography=self._homography)
                    else:
                        model = AprilTagModel.from_corners(
                            d.id, d.corners,
                            homography=self._homography,
                            timestamp=ts,
                            frame=self._frame_count,
                            family=d.family,
                        )
                        self._tag_models[d.id] = model

                    # Velocity
                    ve = self._velocities.get(d.id)
                    if ve is None:
                        ve = VelocityEstimator(ema_alpha=self._ema_alpha, deadband=self._deadband)
                        self._velocities[d.id] = ve
                    vel_px, speed_px = ve.update(model.center_px, ts)

                    # World velocity
                    vel_world = None
                    speed_world = None
                    heading_rad = None
                    if self._homography is not None and speed_px > 0:
                        cx, cy = model.center_px
                        vx, vy = vel_px
                        p1 = np.array([cx, cy, 1.0], dtype=float)
                        p2 = np.array([cx + vx, cy + vy, 1.0], dtype=float)
                        w1 = self._homography @ p1
                        w1 = w1 / w1[2]
                        w2 = self._homography @ p2
                        w2 = w2 / w2[2]
                        wvx = float(w2[0] - w1[0])
                        wvy = float(w2[1] - w1[1])
                        # (wvx, wvy) come from an A1-centred, +y-north
                        # homography — already y-up, so report directly (no Y
                        # flip) to match world_xy & orientation_yaw; 0°=+X, CCW.
                        vel_world = (wvx, wvy)
                        speed_world = math.hypot(wvx, wvy)
                        heading_rad = math.atan2(wvy, wvx)

                    model.frame = self._frame_count
                    if self._boundary is not None:
                        try:
                            model.in_playfield = self._boundary.isIn(model.center_px)
                        except Exception:
                            model.in_playfield = True
                    else:
                        model.in_playfield = True

                # Build TagRecords
                tag_records: list[TagRecord] = []
                stale_cutoff = 1.0

                for d in detections:
                    model = self._tag_models[d.id]
                    ve = self._velocities.get(d.id)
                    tag_records.append(TagRecord.from_apriltag(
                        model,
                        vel_px=ve.velocity if ve else None,
                        speed_px=ve.speed if ve else None,
                        vel_world=vel_world,
                        speed_world=speed_world,
                        heading_rad=heading_rad,
                        timestamp=ts,
                        frame_index=self._frame_count,
                        age=0.0,
                    ))

                # Add stale tags
                for tid, model in self._tag_models.items():
                    if tid not in seen_ids and model.last_ts is not None:
                        age = ts - float(model.last_ts)
                        if age <= stale_cutoff:
                            ve = self._velocities.get(tid)
                            tag_records.append(TagRecord.from_apriltag(
                                model,
                                vel_px=ve.velocity if ve else None,
                                speed_px=ve.speed if ve else None,
                                vel_world=None,
                                speed_world=None,
                                heading_rad=None,
                                timestamp=ts,
                                frame_index=self._frame_count,
                                age=age,
                            ))

                # Prune old models
                for tid in list(self._tag_models.keys()):
                    if (tid not in seen_ids
                            and self._tag_models[tid].last_ts is not None
                            and (ts - float(self._tag_models[tid].last_ts)) > stale_cutoff):
                        del self._tag_models[tid]
                        self._velocities.pop(tid, None)

                frame_record = FrameRecord(
                    timestamp=ts,
                    frame_index=self._frame_count,
                    tags=tag_records,
                )
                self._ring_buffer.append(frame_record)
                self._frame_count += 1
                consecutive_failures = 0

                if self._on_frame is not None:
                    self._on_frame(frame_record)

            except Exception as exc:
                self._error = exc
                consecutive_failures += 1
                if consecutive_failures >= max_failures:
                    break
