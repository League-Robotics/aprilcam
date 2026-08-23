from __future__ import annotations

import math
from typing import Iterable, Optional, Tuple, List

import cv2 as cv
import numpy as np

from ..core.playfield import PlayfieldBoundary as Playfield
from ..core.models import AprilTag


class PlayfieldDisplay:
    """Display and overlay manager for the playfield.

    - Can deskew the frame using the Playfield polygon once available.
    - Draws detections, world coords, and velocity vectors.
    """

    def __init__(
        self,
        playfield: Playfield,
        window_name: str = "aprilcam",
        headless: bool = False,
        deskew_overlay: bool = False,
        robot_tag_id: Optional[int] = None,
        gripper_offset_cm: float = 14.0,
        calibration: object = None,
        undistort: bool = False,
    ) -> None:
        # references and flags
        self.playfield = playfield
        self.window = window_name
        self.headless = bool(headless)
        self.deskew_overlay = bool(deskew_overlay)
        self.robot_tag_id = robot_tag_id
        self.gripper_offset_cm = float(gripper_offset_cm)

        # Optional pre-warp undistortion (sprint 011, ticket 007).  When
        # ``undistort`` is enabled AND *calibration* carries
        # camera_matrix + dist_coeffs, the frame is undistorted before the
        # metric deskew warp for a flatter top-down view.  A no-op when
        # disabled or when the calibration lacks intrinsics
        # (``CameraCalibration.undistort`` passes the frame through).
        self.calibration = calibration
        self.undistort_enabled = bool(undistort)

        # perspective (deskew) cache
        self.M_deskew = None
        self.deskew_size = None

        # display mode bookkeeping so overlays map correctly
        self._mode = "full"  # one of: 'full', 'crop', 'deskew'
        self._crop_xy = (0, 0)  # (xmin, ymin)
        self._crop_wh = (0, 0)  # (w, h)

        # window bookkeeping
        self._win_created = False
        self._last_size = (0, 0)  # (w, h)

    def _ensure_window(self) -> None:
        if self.headless:
            return
        if not self._win_created:
            try:
                cv.namedWindow(self.window, cv.WINDOW_NORMAL)
                self._win_created = True
            except Exception:
                pass

    def _update_deskew(self, frame: np.ndarray) -> None:
        poly = self.playfield.get_polygon()
        if not self.deskew_overlay or poly is None or self.M_deskew is not None:
            return
        # Single-source the warp math: the playfield builds the metric
        # top-down transform (W×H × px_per_cm) via calibration.geometry, or
        # falls back to the legacy edge-length pixel rectangle when no saved
        # dimensions exist. Because get_polygon() may be seeded from saved
        # geometry, this engages without live ArUco corners.
        transform = self.playfield.deskew_transform()
        if transform is None:
            return
        self.M_deskew, self.deskew_size = transform

    def _maybe_undistort(self, frame: np.ndarray) -> np.ndarray:
        """Undistort *frame* before the deskew warp when enabled + possible.

        Applies :meth:`CameraCalibration.undistort` only when undistortion is
        enabled (config) AND a calibration with ``camera_matrix`` +
        ``dist_coeffs`` is available.  When disabled, or when the calibration
        lacks intrinsics, the frame is returned unchanged — deskew still works.
        """
        if not self.undistort_enabled or self.calibration is None:
            return frame
        try:
            return self.calibration.undistort(frame)
        except Exception:
            return frame

    def prepare_display(self, frame: np.ndarray) -> np.ndarray:
        # Reset mode by default
        self._mode = "full"
        self._crop_xy = (0, 0)
        self._crop_wh = (frame.shape[1], frame.shape[0])
        poly = self.playfield.get_polygon()
        if poly is None:
            return frame
        try:
            # Deskewed view
            if self.deskew_overlay and self.M_deskew is not None and self.deskew_size is not None:
                w, h = self.deskew_size
                self._mode = "deskew"
                self._crop_xy = (0, 0)
                self._crop_wh = (w, h)
                # Optional pre-warp undistortion for a flatter metric top-down
                # view (no-op when disabled or intrinsics absent).
                frame = self._maybe_undistort(frame)
                return cv.warpPerspective(frame, self.M_deskew, (w, h))
            # Cropped view
            PAD = 8
            x_coords = poly[:, 0]
            y_coords = poly[:, 1]
            xmin = max(0, int(math.floor(float(x_coords.min()) - PAD)))
            ymin = max(0, int(math.floor(float(y_coords.min()) - PAD)))
            xmax = min(frame.shape[1], int(math.ceil(float(x_coords.max()) + PAD)))
            ymax = min(frame.shape[0], int(math.ceil(float(y_coords.max()) + PAD)))
            if xmax > xmin and ymax > ymin:
                self._mode = "crop"
                self._crop_xy = (xmin, ymin)
                self._crop_wh = (xmax - xmin, ymax - ymin)
                return frame[ymin:ymax, xmin:xmax]
        except Exception:
            pass
        return frame

    def _map_points_to_display(self, pts: np.ndarray) -> np.ndarray:
        """Transform points from source-frame coords into display-image coords.

        Accepts an array of shape (N, 2) float32/float64 and returns float32.
        """
        if pts is None or len(pts) == 0:
            return pts
        P = pts.astype(np.float32)
        if self._mode == "deskew" and self.M_deskew is not None:
            # perspectiveTransform expects shape (N,1,2)
            P3 = P.reshape(-1, 1, 2)
            Q = cv.perspectiveTransform(P3, self.M_deskew).reshape(-1, 2)
            return Q
        if self._mode == "crop":
            ox, oy = self._crop_xy
            Q = P.copy()
            Q[:, 0] -= float(ox)
            Q[:, 1] -= float(oy)
            return Q
        return P

    @staticmethod
    def _draw_text_with_outline(img: np.ndarray, text: str, org: Tuple[int, int], color=(255, 255, 255), font_scale=0.7, thickness=1):
        cv.putText(img, text, org, cv.FONT_HERSHEY_SIMPLEX, font_scale, (0, 0, 0), thickness + 2, cv.LINE_AA)
        cv.putText(img, text, org, cv.FONT_HERSHEY_SIMPLEX, font_scale, color, thickness, cv.LINE_AA)

    def draw_overlays(
        self,
        frame: np.ndarray,
        tags: Iterable[AprilTag],
        homography: Optional[np.ndarray] = None,
        origin_x: float = 0.0,
        origin_y: float = 0.0,
    ) -> None:
        # playfield outline (transform into display coords)
        poly = self.playfield.get_polygon()
        if poly is not None:
            try:
                poly_disp = self._map_points_to_display(poly.astype(np.float32))
                cv.polylines(frame, [poly_disp.astype(int)], True, (255, 255, 255), 2, cv.LINE_AA)
            except Exception:
                pass
        # tag boxes, ids, velocity, world coords text
        for tag in tags:
            # map corners and center into display coords
            pts_src = tag.corners_px.astype(np.float32)
            ptsf = self._map_points_to_display(pts_src)
            pts = ptsf.astype(np.int32)
            p0, p1, p2, p3 = pts[0], pts[1], pts[2], pts[3]
            # draw a peaked "roof" indicating the outward top direction
            try:
                # Compute apex in source coords using the tag's top direction
                pts_src4 = tag.corners_px.astype(np.float32)
                top_mid_src = (pts_src4[0] + pts_src4[1]) * 0.5
                nux, nuy = getattr(tag, "top_dir_px", (1.0, 0.0))
                top_len = float(np.linalg.norm(pts_src4[1] - pts_src4[0]))
                roof_len = max(6.0, min(80.0, 0.35 * top_len))
                apex_src = np.array([[top_mid_src[0] + nux * roof_len, top_mid_src[1] + nuy * roof_len]], dtype=np.float32)
                apex = self._map_points_to_display(apex_src).reshape(2).astype(int)
                cv.line(frame, tuple(p0), tuple(apex), (0, 255, 0), 2, cv.LINE_AA)
                cv.line(frame, tuple(p1), tuple(apex), (0, 255, 0), 2, cv.LINE_AA)
            except Exception:
                # fallback: flat green top edge
                cv.line(frame, tuple(p0), tuple(p1), (0, 255, 0), 2, cv.LINE_AA)
            cv.line(frame, tuple(p1), tuple(p2), (0, 0, 255), 2, cv.LINE_AA)
            cv.line(frame, tuple(p2), tuple(p3), (0, 0, 255), 2, cv.LINE_AA)
            cv.line(frame, tuple(p3), tuple(p0), (0, 0, 255), 2, cv.LINE_AA)
            c_src = np.array([tag.center_px], dtype=np.float32)
            c_map = self._map_points_to_display(c_src).reshape(2)
            cx, cy = int(c_map[0]), int(c_map[1])
            # velocity arrow (supports AprilTagFlow with vel_px property)
            vx, vy = (0.0, 0.0)
            try:
                vx, vy = getattr(tag, "vel_px", (0.0, 0.0))
            except Exception:
                vx, vy = (0.0, 0.0)
            norm = math.hypot(vx, vy)
            if norm > 1e-6:
                length_px = int(max(12, min(250, norm * 0.5)))
                ux, uy = (vx / norm, vy / norm)
                # build arrow in source coords, then map both points
                start_src = np.array([[tag.center_px[0], tag.center_px[1]]], dtype=np.float32)
                end_src = np.array([[tag.center_px[0] + ux * length_px, tag.center_px[1] + uy * length_px]], dtype=np.float32)
                start_map = self._map_points_to_display(start_src).reshape(2)
                end_map = self._map_points_to_display(end_src).reshape(2)
                end = (int(end_map[0]), int(end_map[1]))
                cx, cy = int(start_map[0]), int(start_map[1])
                cv.arrowedLine(frame, (cx, cy), end, (0, 255, 255), 2, tipLength=0.12)
                cv.circle(frame, (cx, cy), 4, (0, 255, 255), -1)
            # Gripper position: 14 cm forward from robot tag along orientation
            if self.robot_tag_id is not None and tag.id == self.robot_tag_id and homography is not None:
                try:
                    H_inv = np.linalg.inv(homography)
                    # Map center to world
                    cvec = np.array([tag.center_px[0], tag.center_px[1], 1.0])
                    cw = homography @ cvec
                    cw_xy = np.array([cw[0] / cw[2], cw[1] / cw[2]])
                    # Map top_mid to world to get world orientation
                    top_mid_px = (tag.corners_px[0] + tag.corners_px[1]) * 0.5
                    tvec = np.array([float(top_mid_px[0]), float(top_mid_px[1]), 1.0])
                    tw = homography @ tvec
                    tw_xy = np.array([tw[0] / tw[2], tw[1] / tw[2]])
                    # World orientation direction (center -> top_mid)
                    w_dir = tw_xy - cw_xy
                    w_norm = float(np.linalg.norm(w_dir))
                    if w_norm > 1e-6:
                        w_unit = w_dir / w_norm
                        gripper_world = cw_xy + w_unit * self.gripper_offset_cm
                        gvec = np.array([gripper_world[0], gripper_world[1], 1.0])
                        gp = H_inv @ gvec
                        gripper_px = np.array([[gp[0] / gp[2], gp[1] / gp[2]]], dtype=np.float32)
                        gripper_disp = self._map_points_to_display(gripper_px).reshape(2)
                        gx, gy = int(gripper_disp[0]), int(gripper_disp[1])
                        cv.circle(frame, (gx, gy), 8, (255, 0, 0), -1, cv.LINE_AA)
                except Exception:
                    pass
            # ID label (centered on the tag center)
            id_text = f"{tag.id}"
            (tw, th), base = cv.getTextSize(id_text, cv.FONT_HERSHEY_SIMPLEX, 0.8, 2)
            tx = int(cx - tw * 0.5)
            ty = int(cy + th * 0.5)
            self._draw_text_with_outline(frame, id_text, (tx, ty), color=(0, 0, 255), font_scale=0.8, thickness=2)
            # Yellow cross at the height-adjusted world position
            if tag.world_xy is not None and homography is not None:
                try:
                    H_inv = np.linalg.inv(homography)
                    wx, wy = tag.world_xy
                    # Convert A1-centred back to raw corner-origin for H_inv
                    raw_vec = H_inv @ np.array([wx + origin_x, wy + origin_y, 1.0])
                    rx, ry = raw_vec[0] / raw_vec[2], raw_vec[1] / raw_vec[2]
                    cross_src = np.array([[rx, ry]], dtype=np.float32)
                    cross_disp = self._map_points_to_display(cross_src).reshape(2)
                    gx, gy = int(round(float(cross_disp[0]))), int(round(float(cross_disp[1])))
                    # Arm length = half of 1cm in pixels; map (wx+0.5, wy) to get scale
                    arm_vec = H_inv @ np.array([wx + origin_x + 0.5, wy + origin_y, 1.0])
                    arm_src = np.array([[arm_vec[0] / arm_vec[2], arm_vec[1] / arm_vec[2]]], dtype=np.float32)
                    arm_disp = self._map_points_to_display(arm_src).reshape(2)
                    arm_px = max(4, int(round(float(np.linalg.norm(arm_disp - cross_disp)))))
                    yellow = (0, 255, 255)
                    cv.line(frame, (gx - arm_px, gy), (gx + arm_px, gy), yellow, 2, cv.LINE_AA)
                    cv.line(frame, (gx, gy - arm_px), (gx, gy + arm_px), yellow, 2, cv.LINE_AA)
                except Exception:
                    pass

    def draw_status_panel(
        self,
        frame: np.ndarray,
        tags: Iterable[AprilTag],
        homography: Optional[np.ndarray] = None,
        num_paths: int = 0,
        fps: float = 0.0,
    ) -> None:
        """Draw a status panel on the right side of the frame."""
        fh, fw = frame.shape[:2]
        tag_list = list(tags)
        tag_ids = {t.id for t in tag_list}

        lines: List[Tuple[str, Tuple[int, int, int]]] = []

        # Playfield status
        poly = self.playfield.get_polygon()
        aruco_expected = {0, 1, 2, 3}
        aruco_found = tag_ids & {0, 1, 2, 3}  # ArUco IDs overlap with AprilTag
        # Check which ArUco corners the playfield actually has
        if poly is not None:
            lines.append(("Playfield: OK", (0, 255, 0)))
        else:
            # Report which ArUco corners are missing
            # The playfield detector looks for ArUco 4x4 IDs 0-3
            lines.append(("Playfield: NO", (0, 0, 255)))
            lines.append(("  Need ArUco 0,1,2,3", (0, 0, 255)))

        # Deskew status
        if self._mode == "deskew":
            lines.append(("Deskew: ON", (0, 255, 0)))
        elif self._mode == "crop":
            lines.append(("Deskew: crop", (0, 255, 255)))
        else:
            lines.append(("Deskew: OFF", (128, 128, 128)))

        # Homography status
        if homography is not None:
            lines.append(("Homography: OK", (0, 255, 0)))
        else:
            lines.append(("Homography: NO", (0, 0, 255)))
            lines.append(("  No calibration.json", (0, 0, 255)))

        # Tag count
        april_ids = sorted(tid for tid in tag_ids if tid >= 5)
        lines.append((f"Tags: {len(tag_list)}", (255, 255, 255)))
        if april_ids:
            lines.append((f"  AT: {april_ids}", (200, 200, 200)))

        # Path count
        if num_paths > 0:
            lines.append((f"Paths: {num_paths}", (0, 200, 255)))
        else:
            lines.append(("Paths: none", (128, 128, 128)))

        # FPS
        if fps > 0:
            lines.append((f"FPS: {fps:.1f}", (200, 200, 200)))

        # Gripper
        if self.robot_tag_id is not None:
            if self.robot_tag_id in tag_ids:
                lines.append((f"Robot tag {self.robot_tag_id}: OK", (255, 200, 0)))
            else:
                lines.append((f"Robot tag {self.robot_tag_id}: --", (0, 0, 255)))

        # Draw the panel — top-right corner with a semi-transparent background
        font = cv.FONT_HERSHEY_SIMPLEX
        scale = 0.5
        thickness = 1
        line_height = 20
        margin = 10
        pad = 6

        # Measure max text width for background box
        max_tw = 0
        for text, _ in lines:
            (tw, _), _ = cv.getTextSize(text, font, scale, thickness)
            if tw > max_tw:
                max_tw = tw

        # Draw semi-transparent background
        box_w = max_tw + 2 * pad
        box_h = len(lines) * line_height + 2 * pad
        x0 = fw - margin - box_w
        y0 = margin
        overlay = frame.copy()
        cv.rectangle(overlay, (x0, y0), (x0 + box_w, y0 + box_h), (0, 0, 0), -1)
        cv.addWeighted(overlay, 0.6, frame, 0.4, 0, frame)

        # Draw text lines
        y = y0 + pad + 14  # baseline offset
        for text, color in lines:
            (tw, _), _ = cv.getTextSize(text, font, scale, thickness)
            x = x0 + box_w - pad - tw  # right-align within box
            cv.putText(frame, text, (x, y), font, scale, color, thickness, cv.LINE_AA)
            y += line_height

    def update(self, frame: np.ndarray) -> np.ndarray:
        # ensure playfield cache and deskew once
        self.playfield.update(frame)
        self._update_deskew(frame)
        self._ensure_window()
        return self.prepare_display(frame)

    def show(self, display: np.ndarray) -> None:
        if self.headless:
            return
        # Resize window to match the current display image to avoid whitespace
        try:
            h, w = display.shape[:2]
            if (w, h) != self._last_size and w > 0 and h > 0:
                cv.resizeWindow(self.window, int(w), int(h))
                self._last_size = (int(w), int(h))
        except Exception:
            pass
        cv.imshow(self.window, display)

    def draw_paths(
        self,
        frame: np.ndarray,
        paths: dict,
        playfield: object,
        homography: Optional[np.ndarray],
        origin_x: float = 0.0,
        origin_y: float = 0.0,
    ) -> None:
        """Draw agent-defined paths onto *frame* (in-place).

        Waypoint coordinates are in the A1-centred world frame.  ``origin_x``
        and ``origin_y`` are the field half-dimensions (field_width/2,
        field_height/2); they are added back before applying H_inv so the
        homography receives raw corner-origin coordinates.

        Coordinate pipeline (per waypoint):
          1. A1-centred world cm → raw world cm: add origin_x, origin_y.
          2. Raw world cm → source pixel: apply H_inv = inv(homography).
          3. Source pixel → display pixel: _map_points_to_display.
          4. Pixel radius for size_cm: same pipeline offset by size_cm/2.

        No-op when homography is None (uncalibrated playfield) or paths is empty.

        Args:
            frame: The display image to draw onto (modified in place).
            paths: Dict mapping path_id -> path dict (from Path.to_dict()).
            playfield: The PlayfieldBoundary (unused directly).
            homography: Optional homography matrix for world-to-pixel mapping.
            origin_x: Half the field width in cm (field_width_cm / 2).
            origin_y: Half the field height in cm (field_height_cm / 2).
        """
        if homography is None:
            return
        if not paths:
            return

        H_inv = np.linalg.inv(homography)

        def _world_to_disp(x: float, y: float):
            """Return display-space coords; input is A1-centred world cm."""
            hvec = H_inv @ np.array([x + origin_x, y + origin_y, 1.0])
            sx, sy = hvec[0] / hvec[2], hvec[1] / hvec[2]
            src_pt = np.array([[sx, sy]], dtype=np.float32)
            disp_pt = self._map_points_to_display(src_pt).reshape(2)
            return disp_pt

        def _compute_radius(x: float, y: float, size_cm: float, disp_pt: np.ndarray) -> int:
            """Return pixel half-extent for size_cm at world position (x, y)."""
            hvec2 = H_inv @ np.array([x + size_cm / 2.0 + origin_x, y + origin_y, 1.0])
            sx2, sy2 = hvec2[0] / hvec2[2], hvec2[1] / hvec2[2]
            src_pt2 = np.array([[sx2, sy2]], dtype=np.float32)
            disp_pt2 = self._map_points_to_display(src_pt2).reshape(2)
            return max(1, int(round(float(np.linalg.norm(disp_pt2 - disp_pt)))))

        for path_dict in paths.values():
            waypoints = path_dict.get("waypoints", [])
            if not waypoints:
                continue

            # Pre-compute display points and radii for all waypoints.
            # Entries that fail are stored as None (skipped in both passes).
            computed = []
            for wp in waypoints:
                try:
                    x = float(wp["x"])
                    y = float(wp["y"])
                    size_cm = float(wp["size_cm"])
                    disp_pt = _world_to_disp(x, y)
                    r = _compute_radius(x, y, size_cm, disp_pt)
                    cx = int(round(float(disp_pt[0])))
                    cy = int(round(float(disp_pt[1])))
                    computed.append((cx, cy, r))
                except Exception:
                    computed.append(None)

            # Pass 1: lines (connect waypoint i to waypoint i+1, using
            # waypoint i's line_color; last waypoint has no outgoing line).
            for i, wp in enumerate(waypoints[:-1]):
                pt_a = computed[i]
                pt_b = computed[i + 1]
                if pt_a is None or pt_b is None:
                    continue
                try:
                    lc = wp["line_color"]
                    line_bgr = (int(lc[2]), int(lc[1]), int(lc[0]))
                    cv.line(
                        frame,
                        (pt_a[0], pt_a[1]),
                        (pt_b[0], pt_b[1]),
                        line_bgr,
                        2,
                        cv.LINE_AA,
                    )
                except Exception:
                    pass

            # Pass 2: symbols
            for i, wp in enumerate(waypoints):
                entry = computed[i]
                if entry is None:
                    continue
                symbol = wp.get("symbol", "none")
                if symbol == "none":
                    continue
                try:
                    cx, cy, r = entry
                    sc = wp["symbol_color"]
                    color = (int(sc[2]), int(sc[1]), int(sc[0]))

                    if symbol == "circle":
                        cv.circle(frame, (cx, cy), r, color, thickness=2, lineType=cv.LINE_AA)
                    elif symbol == "filled_circle":
                        cv.circle(frame, (cx, cy), r, color, thickness=cv.FILLED, lineType=cv.LINE_AA)
                    elif symbol == "square":
                        cv.rectangle(frame, (cx - r, cy - r), (cx + r, cy + r), color, thickness=2, lineType=cv.LINE_AA)
                    elif symbol == "filled_square":
                        cv.rectangle(frame, (cx - r, cy - r), (cx + r, cy + r), color, thickness=cv.FILLED, lineType=cv.LINE_AA)
                    elif symbol == "triangle":
                        pts_tri = np.array(
                            [[cx, cy - r], [cx - r, cy + r], [cx + r, cy + r]],
                            dtype=np.int32,
                        )
                        cv.polylines(frame, [pts_tri], True, color, 2, cv.LINE_AA)
                    elif symbol == "filled_triangle":
                        pts_tri = np.array(
                            [[cx, cy - r], [cx - r, cy + r], [cx + r, cy + r]],
                            dtype=np.int32,
                        )
                        cv.fillPoly(frame, [pts_tri], color)
                    elif symbol == "x":
                        cv.line(frame, (cx - r, cy - r), (cx + r, cy + r), color, 2, cv.LINE_AA)
                        cv.line(frame, (cx + r, cy - r), (cx - r, cy + r), color, 2, cv.LINE_AA)
                    # "none" already filtered above
                except Exception:
                    pass

    def draw_live_overlay(
        self,
        frame: np.ndarray,
        overlay_frame,  # aprilcam_pb2.OverlayFrame
        homography: Optional[np.ndarray],
        origin_x: float = 0.0,
        origin_y: float = 0.0,
    ) -> None:
        """Draw live overlay elements onto *frame* in-place.

        Element coordinates are in the A1-centred world frame (cm).
        ``origin_x`` / ``origin_y`` (field_width/2, field_height/2) are added
        back before applying H_inv to convert to raw corner-origin world coords
        that the homography matrix expects.

        No-op when homography is None or the overlay has expired (TTL check).
        """
        import time
        if homography is None:
            return
        if time.time() - overlay_frame.timestamp > overlay_frame.ttl:
            return

        H_inv = np.linalg.inv(homography)

        def _w2d(x: float, y: float):
            hvec = H_inv @ np.array([x + origin_x, y + origin_y, 1.0])
            sx, sy = hvec[0] / hvec[2], hvec[1] / hvec[2]
            src_pt = np.array([[sx, sy]], dtype=np.float32)
            disp_pt = self._map_points_to_display(src_pt).reshape(2)
            return int(round(float(disp_pt[0]))), int(round(float(disp_pt[1])))

        for elem in overlay_frame.elements:
            try:
                p = list(elem.params)
                color_rgb = list(elem.color) if elem.color else [0, 255, 0]
                bgr = (color_rgb[2], color_rgb[1], color_rgb[0])
                t = elem.thickness if elem.thickness != 0 else 2

                if elem.type == "arc":
                    cx, cy, r, start_deg, end_deg = p[0], p[1], p[2], p[3], p[4]
                    cx_d, cy_d = _w2d(cx, cy)
                    rx_d, ry_d = _w2d(cx + r, cy)
                    ryx_d, ryy_d = _w2d(cx, cy + r)
                    rx = max(1, int(round(np.linalg.norm([rx_d - cx_d, ry_d - cy_d]))))
                    ry = max(1, int(round(np.linalg.norm([ryx_d - cx_d, ryy_d - cy_d]))))
                    angle = float(np.degrees(np.arctan2(ry_d - cy_d, rx_d - cx_d)))
                    cv.ellipse(frame, (cx_d, cy_d), (rx, ry), angle,
                               float(start_deg), float(end_deg), bgr, t)

                elif elem.type == "arrow":
                    x1, y1, x2, y2 = p[0], p[1], p[2], p[3]
                    pt1 = _w2d(x1, y1)
                    pt2 = _w2d(x2, y2)
                    cv.arrowedLine(frame, pt1, pt2, bgr, t, tipLength=0.2)

                elif elem.type == "point":
                    x, y, radius_cm = p[0], p[1], p[2]
                    cx_d, cy_d = _w2d(x, y)
                    rx_d, ry_d = _w2d(x + radius_cm, y)
                    r_px = max(1, int(round(np.linalg.norm([rx_d - cx_d, ry_d - cy_d]))))
                    fill = cv.FILLED if t < 0 else t
                    cv.circle(frame, (cx_d, cy_d), r_px, bgr, fill)

                elif elem.type == "polyline":
                    pts_world = [(p[i], p[i + 1]) for i in range(0, len(p) - 1, 2)]
                    disp_pts = np.array([_w2d(x, y) for x, y in pts_world], dtype=np.int32)
                    cv.polylines(frame, [disp_pts], isClosed=False, color=bgr, thickness=t)

                elif elem.type == "text":
                    x, y = p[0], p[1]
                    font_scale = float(p[2]) if len(p) > 2 else 0.6
                    cx_d, cy_d = _w2d(x, y)
                    self._draw_text_with_outline(
                        frame, elem.text, (cx_d, cy_d),
                        color=bgr, font_scale=font_scale, thickness=max(1, t),
                    )

                elif elem.type == "rect":
                    x1, y1, x2, y2 = p[0], p[1], p[2], p[3]
                    cv.rectangle(frame, _w2d(x1, y1), _w2d(x2, y2), bgr, cv.FILLED if t < 0 else t)

                elif elem.type == "polygon":
                    pts_world = [(p[i], p[i + 1]) for i in range(0, len(p) - 1, 2)]
                    disp_pts = np.array([_w2d(x, y) for x, y in pts_world], dtype=np.int32)
                    if t < 0:
                        cv.fillPoly(frame, [disp_pts], bgr)
                    else:
                        cv.polylines(frame, [disp_pts], isClosed=True, color=bgr, thickness=t)

            except Exception:
                pass

    def pause(self, frame: np.ndarray, text: str = " Paused: Press Space to Run") -> None:
        """Overlay a paused message onto the given frame."""
        if frame is None:
            return
        try:
            self._draw_text_with_outline(frame, text, (10, 30), color=(0, 255, 255), font_scale=0.9, thickness=2)
        except Exception:
            pass
