"""Color classifiers for detecting colored objects in camera frames."""

from __future__ import annotations

import cv2 as cv
import numpy as np

from .objects import ObjectRecord


class LABColorClassifier:
    """Detect colored objects using Mahalanobis distance in CIELAB color space.

    Calibrate by providing frames and known box pixel positions; the classifier
    samples those ROIs to build per-color Gaussian models (mean + covariance).
    Detection creates a per-color mask from per-pixel Mahalanobis distance,
    then finds contours and filters by area.

    Handles difficult colors (near-white yellow, dark black, low-saturation
    magenta) that confuse HSV thresholding.
    """

    def __init__(
        self,
        min_area: int = 400,
        max_area: int = 5000,
        mahal_threshold: float = 4.5,
    ):
        self.min_area = min_area
        self.max_area = max_area
        self.mahal_threshold = mahal_threshold
        # color -> (mean (3,), cov_inv (3,3))
        self.color_models: dict[str, tuple[np.ndarray, np.ndarray]] = {}

    def calibrate(
        self,
        frames_bgr: list[np.ndarray],
        color_positions: dict[str, tuple[int, int]],
        roi_radius: int = 12,
        background_positions: list[tuple[int, int]] | None = None,
        background_roi_radius: int = 20,
    ) -> None:
        """Learn per-color LAB Gaussian models from known positions across frames.

        If background_positions is provided, a background model is built from
        those points and participates in winner-takes-all classification —
        pixels closest to the background are suppressed from results.

        Args:
            frames_bgr: List of BGR frames from a burst capture.
            color_positions: Map of color name -> (cx, cy) pixel center in frame.
            roi_radius: Half-side of the square ROI sampled around each center.
            background_positions: List of (cx, cy) points on the table surface
                (between color boxes) used to build a background exclusion model.
            background_roi_radius: Half-side of ROI sampled around each background point.
        """
        self.color_models = {}
        for color, (cx, cy) in color_positions.items():
            samples: list[np.ndarray] = []
            for frame in frames_bgr:
                lab = cv.cvtColor(frame, cv.COLOR_BGR2LAB)
                y1 = max(0, cy - roi_radius)
                y2 = min(frame.shape[0], cy + roi_radius)
                x1 = max(0, cx - roi_radius)
                x2 = min(frame.shape[1], cx + roi_radius)
                roi = lab[y1:y2, x1:x2]
                if roi.size > 0:
                    samples.append(roi.reshape(-1, 3).astype(np.float64))
            if not samples:
                continue
            pts = np.vstack(samples)
            mean = pts.mean(axis=0)
            cov = np.cov(pts.T) + np.eye(3) * 0.5  # regularize
            self.color_models[color] = (mean, np.linalg.inv(cov))

        if background_positions:
            r = background_roi_radius
            bg_samples: list[np.ndarray] = []
            for bx, by in background_positions:
                for frame in frames_bgr:
                    lab = cv.cvtColor(frame, cv.COLOR_BGR2LAB)
                    y1 = max(0, by - r)
                    y2 = min(frame.shape[0], by + r)
                    x1 = max(0, bx - r)
                    x2 = min(frame.shape[1], bx + r)
                    roi = lab[y1:y2, x1:x2]
                    if roi.size > 0:
                        bg_samples.append(roi.reshape(-1, 3).astype(np.float64))
            if bg_samples:
                pts = np.vstack(bg_samples)
                mean = pts.mean(axis=0)
                cov = np.cov(pts.T) + np.eye(3) * 0.5
                self.color_models["__background__"] = (mean, np.linalg.inv(cov))

    def classify(
        self,
        frame_bgr: np.ndarray,
        homography: np.ndarray | None = None,
    ) -> list[ObjectRecord]:
        """Detect colored objects using winner-takes-all Mahalanobis distance.

        Computes Mahalanobis distance from every pixel to every color model,
        assigns each pixel to its nearest color (winner-takes-all), then
        thresholds on that minimum distance.  This prevents any region from
        being claimed by more than one color.

        Args:
            frame_bgr: BGR uint8 image.
            homography: Optional 3x3 matrix mapping pixel → world coords.

        Returns:
            List of ObjectRecord for each detected region.
        """
        if not self.color_models:
            return []

        lab = cv.cvtColor(frame_bgr, cv.COLOR_BGR2LAB).astype(np.float64)
        H, W = lab.shape[:2]
        pts = lab.reshape(-1, 3)  # (N, 3)

        colors = list(self.color_models.keys())
        n = len(colors)
        all_mah = np.empty((n, H * W), dtype=np.float64)

        for i, color_name in enumerate(colors):
            mean, cov_inv = self.color_models[color_name]
            diff = pts - mean
            tmp = diff @ cov_inv
            all_mah[i] = np.sqrt(np.maximum((tmp * diff).sum(axis=1), 0.0))

        best_idx = all_mah.argmin(axis=0)           # (N,) — index of nearest color
        min_mah = all_mah[best_idx, np.arange(H * W)]  # (N,) — nearest distance

        kernel = np.ones((3, 3), np.uint8)
        results: list[ObjectRecord] = []

        for i, color_name in enumerate(colors):
            if color_name == "__background__":
                continue
            mask = (
                (best_idx == i) & (min_mah < self.mahal_threshold)
            ).reshape(H, W).astype(np.uint8) * 255
            mask = cv.morphologyEx(mask, cv.MORPH_OPEN, kernel)

            contours, _ = cv.findContours(
                mask, cv.RETR_EXTERNAL, cv.CHAIN_APPROX_SIMPLE
            )
            for cnt in contours:
                area = cv.contourArea(cnt)
                if area < self.min_area or area > self.max_area:
                    continue
                M = cv.moments(cnt)
                if M["m00"] < 1e-6:
                    continue
                cx_f = M["m10"] / M["m00"]
                cy_f = M["m01"] / M["m00"]
                x, y, bw, bh = cv.boundingRect(cnt)

                world_xy = None
                if homography is not None:
                    vec = homography @ np.array([cx_f, cy_f, 1.0])
                    if abs(vec[2]) > 1e-9:
                        world_xy = (float(vec[0] / vec[2]), float(vec[1] / vec[2]))

                results.append(
                    ObjectRecord(
                        center_px=(float(cx_f), float(cy_f)),
                        bbox=(x, y, bw, bh),
                        area_px=float(area),
                        world_xy=world_xy,
                        color=color_name,
                        object_type="cube",
                        confidence=1.0,
                    )
                )
        return results

    def classify_at_point(
        self, frame_bgr: np.ndarray, x: float, y: float, radius: int = 15
    ) -> str:
        """Classify the dominant color at a point using minimum Mahalanobis distance."""
        if not self.color_models:
            return "unknown"
        h_img, w_img = frame_bgr.shape[:2]
        r = max(4, min(radius, 15))
        x1, y1 = max(0, int(x - r)), max(0, int(y - r))
        x2, y2 = min(w_img, int(x + r)), min(h_img, int(y + r))
        if x2 <= x1 or y2 <= y1:
            return "unknown"
        roi_lab = cv.cvtColor(frame_bgr[y1:y2, x1:x2], cv.COLOR_BGR2LAB)
        mean_lab = roi_lab.reshape(-1, 3).astype(np.float64).mean(axis=0)
        best_color, best_dist = "unknown", float("inf")
        for color_name, (mean, cov_inv) in self.color_models.items():
            diff = mean_lab - mean
            d = float(np.sqrt(max(0.0, diff @ cov_inv @ diff)))
            if d < best_dist:
                best_dist, best_color = d, color_name
        return best_color if best_dist < self.mahal_threshold * 3 else "unknown"


# ---------------------------------------------------------------------------
# Legacy HSV classifier — kept for backward compatibility
# ---------------------------------------------------------------------------

DEFAULT_COLOR_RANGES: dict[str, list[tuple[tuple[int, ...], tuple[int, ...]]]] = {
    "black":   [((0,   0,   0),  (180, 255,  75))],
    "red":     [((0,   60,  60), (10,  255, 255)), ((165, 60, 60), (180, 255, 255))],
    "orange":  [((14,  60,  60), (26,  255, 255))],
    "yellow":  [((0,   0,  215), (180,  35, 255))],
    "green":   [((35,  60,  60), (93,  255, 255))],
    "blue":    [((93,  200,  60), (102, 255, 255))],
    "purple":  [((102, 150,  60), (130, 255, 255))],
    "magenta": [((135,  60,  60), (165, 255, 255))],
}


class ColorClassifier:
    """Detect and classify colored objects using HSV thresholding."""

    def __init__(
        self,
        color_ranges: dict | None = None,
        min_area: int = 200,
        max_area: int = 5000,
    ):
        self.color_ranges = (
            color_ranges if color_ranges is not None else dict(DEFAULT_COLOR_RANGES)
        )
        self.min_area = min_area
        self.max_area = max_area

    def classify(
        self,
        frame_bgr: np.ndarray,
        homography: np.ndarray | None = None,
    ) -> list[ObjectRecord]:
        hsv = cv.cvtColor(frame_bgr, cv.COLOR_BGR2HSV)
        kernel = np.ones((3, 3), np.uint8)
        results: list[ObjectRecord] = []

        for color_name, ranges in self.color_ranges.items():
            mask = np.zeros(hsv.shape[:2], dtype=np.uint8)
            for lo, hi in ranges:
                mask |= cv.inRange(
                    hsv,
                    np.array(lo, dtype=np.uint8),
                    np.array(hi, dtype=np.uint8),
                )
            mask = cv.morphologyEx(mask, cv.MORPH_OPEN, kernel)
            contours, _ = cv.findContours(
                mask, cv.RETR_EXTERNAL, cv.CHAIN_APPROX_SIMPLE
            )

            for cnt in contours:
                area = cv.contourArea(cnt)
                if area < self.min_area or area > self.max_area:
                    continue
                M = cv.moments(cnt)
                if M["m00"] < 1e-6:
                    continue
                cx = M["m10"] / M["m00"]
                cy = M["m01"] / M["m00"]
                x, y, w, h = cv.boundingRect(cnt)

                world_xy = None
                if homography is not None:
                    vec = homography @ np.array([cx, cy, 1.0])
                    if abs(vec[2]) > 1e-9:
                        world_xy = (
                            float(vec[0] / vec[2]),
                            float(vec[1] / vec[2]),
                        )

                results.append(
                    ObjectRecord(
                        center_px=(float(cx), float(cy)),
                        bbox=(x, y, w, h),
                        area_px=float(area),
                        world_xy=world_xy,
                        color=color_name,
                        object_type="cube",
                        confidence=1.0,
                    )
                )
        return results

    def classify_at_point(
        self, frame_bgr: np.ndarray, x: float, y: float, radius: int = 20
    ) -> str:
        h_img, w_img = frame_bgr.shape[:2]
        r = min(radius, 10)
        x1 = max(0, int(x - r))
        y1 = max(0, int(y - r))
        x2 = min(w_img, int(x + r))
        y2 = min(h_img, int(y + r))
        if x2 <= x1 or y2 <= y1:
            return "unknown"

        roi = frame_bgr[y1:y2, x1:x2]
        hsv_roi = cv.cvtColor(roi, cv.COLOR_BGR2HSV)

        bright_sat_mask = (hsv_roi[:, :, 1] > 40) & (hsv_roi[:, :, 2] > 80)
        n_valid = int(np.count_nonzero(bright_sat_mask))
        if n_valid < 5:
            bright_sat_mask = hsv_roi[:, :, 1] > 25
            n_valid = int(np.count_nonzero(bright_sat_mask))
            if n_valid < 5:
                return "unknown"

        best_color = "unknown"
        best_pct = 0.10

        for color_name, ranges in self.color_ranges.items():
            mask = np.zeros(hsv_roi.shape[:2], dtype=np.uint8)
            for lo, hi in ranges:
                mask |= cv.inRange(
                    hsv_roi,
                    np.array(lo, dtype=np.uint8),
                    np.array(hi, dtype=np.uint8),
                )
            match_count = int(np.count_nonzero(mask & bright_sat_mask.astype(np.uint8)))
            pct = float(match_count) / n_valid
            if pct > best_pct:
                best_pct = pct
                best_color = color_name
        return best_color
