"""Lucas-Kanade optical flow tracking between detection frames.

This module provides :class:`OpticalFlowTracker`, which maintains tag positions
at the full camera frame rate by running expensive full detection only every
``detect_interval`` frames and using LK pyramidal optical flow on the
intervening frames.
"""
from __future__ import annotations

from typing import Optional

import cv2 as cv
import numpy as np

from .detector import Detection


class OpticalFlowTracker:
    """Lucas-Kanade optical flow tracking between detection frames.

    Tracks tag corners between full detection runs to maintain tag positions
    at the full frame rate while only running expensive detection every N frames.

    Example::

        tracker = OpticalFlowTracker(detect_interval=3)
        for frame_bgr in camera_stream():
            gray = cv.cvtColor(frame_bgr, cv.COLOR_BGR2GRAY)
            if tracker.should_detect():
                detections = tag_detector.detect(frame_bgr, gray=gray)
            else:
                detections = None
            tags = tracker.update(gray, detections)
    """

    def __init__(self, detect_interval: int = 3) -> None:
        """Create a tracker.

        Args:
            detect_interval: Number of frames between full detection runs.
                ``1`` disables tracking (detect every frame).
                ``3`` means detect on frame 0, track frames 1–2, detect frame 3, …
        """
        self._detect_interval = detect_interval
        self._prev_gray: Optional[np.ndarray] = None
        self._tracks: dict[int, np.ndarray] = {}   # tag_id -> (4, 2) float32 corners
        self._families: dict[int, str] = {}         # tag_id -> family name
        self._frame_idx: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(
        self,
        gray: np.ndarray,
        detections: list[Detection] | None = None,
    ) -> list[Detection]:
        """Process a frame and return current tag detections.

        Args:
            gray: Current grayscale frame (single-channel uint8).
            detections: Fresh detections from :class:`~aprilcam.core.TagDetector`
                when this is a detection frame.  Pass ``None`` on tracking-only
                frames.

        Returns:
            List of :class:`~aprilcam.core.Detection` objects — either the
            supplied fresh detections or detections derived from LK tracking.
            Returns an empty list when tracking state is unavailable.
        """
        if detections is not None:
            # Fresh detections — reset tracking state.
            self._tracks = {d.id: d.corners.copy() for d in detections}
            self._families = {d.id: d.family for d in detections}
            self._prev_gray = gray.copy()
            self._frame_idx += 1
            return detections

        # No fresh detections — attempt LK optical flow tracking.
        if self._prev_gray is None or not self._tracks:
            self._prev_gray = gray.copy()
            self._frame_idx += 1
            return []

        tracked = self._lk_track(self._prev_gray, gray, self._tracks)
        if tracked is None:
            # Tracking failed — clear state so next call forces detection.
            self._tracks.clear()
            self._prev_gray = gray.copy()
            self._frame_idx += 1
            return []

        self._tracks = tracked
        self._prev_gray = gray.copy()
        self._frame_idx += 1

        # Convert tracked corners back to Detection objects.
        result: list[Detection] = []
        for tid, corners in tracked.items():
            center = corners.mean(axis=0)
            result.append(Detection(
                id=tid,
                center=(float(center[0]), float(center[1])),
                corners=corners.astype(np.float32),
                family=self._families.get(tid, "unknown"),
            ))
        return result

    def should_detect(self) -> bool:
        """Return ``True`` if the current frame should run full tag detection.

        Full detection is forced when:
        - ``detect_interval`` is 1 (detection on every frame), or
        - No tracks exist yet (first frame or after tracking failure), or
        - The current frame index is a multiple of ``detect_interval``.
        """
        if self._detect_interval <= 1:
            return True
        if not self._tracks or self._prev_gray is None:
            return True
        return (self._frame_idx % self._detect_interval) == 0

    def reset(self) -> None:
        """Clear all tracking state.

        After ``reset()``, the next call to :meth:`should_detect` returns
        ``True`` and :meth:`update` requires fresh detections to re-establish
        tracking.
        """
        self._prev_gray = None
        self._tracks.clear()
        self._families.clear()
        self._frame_idx = 0

    @property
    def frame_index(self) -> int:
        """Number of frames processed since construction or last :meth:`reset`."""
        return self._frame_idx

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _lk_track(
        prev_gray: np.ndarray,
        curr_gray: np.ndarray,
        tracks: dict[int, np.ndarray],
    ) -> dict[int, np.ndarray] | None:
        """Track corners from *prev_gray* to *curr_gray* via LK optical flow.

        All tag corners are batched into a single ``calcOpticalFlowPyrLK``
        call for efficiency.  Tags whose corners are not all successfully
        tracked are dropped from the result.

        Args:
            prev_gray: Previous frame (single-channel uint8).
            curr_gray: Current frame (single-channel uint8), same size.
            tracks: Mapping of tag ID to ``(4, 2)`` float32 corner array.

        Returns:
            Updated tracks dict (may be a subset of *tracks* when some tags
            lose track), or ``None`` if the flow call fails or all tags are
            lost.
        """
        # Flatten all corners into one (N, 1, 2) array for batch LK.
        tag_ids: list[int] = []
        all_pts: list[np.ndarray] = []
        for tid, corners in tracks.items():
            for pt in corners:
                tag_ids.append(tid)
                all_pts.append(pt)

        if not all_pts:
            return None

        pts0 = np.array(all_pts, dtype=np.float32).reshape(-1, 1, 2)

        pts1, status, _err = cv.calcOpticalFlowPyrLK(
            prev_gray,
            curr_gray,
            pts0,
            None,
            winSize=(21, 21),
            maxLevel=3,
            criteria=(
                cv.TERM_CRITERIA_EPS | cv.TERM_CRITERIA_COUNT,
                30,
                0.01,
            ),
        )

        if pts1 is None or status is None:
            return None

        # Rebuild per-tag corner arrays from the flat tracked-point list.
        result: dict[int, np.ndarray] = {}
        idx = 0
        for tid, corners in tracks.items():
            n = len(corners)
            tag_pts: list[np.ndarray] = []
            tag_ok = True
            for i in range(n):
                if status[idx + i][0]:
                    tag_pts.append(pts1[idx + i][0])
                else:
                    tag_ok = False
                    break
            if tag_ok and len(tag_pts) == n:
                result[tid] = np.array(tag_pts, dtype=np.float32)
            idx += n

        return result if result else None
