#!/usr/bin/env python3
"""Pygame-based playfield simulator with moving AprilTags.

This script displays a window with 4x4 ArUco corner fiducials and four
AprilTag 36h11 markers that move in different patterns: two edge-runners
(clockwise around the perimeter), one circular mover, and one center
spinner.

Requirements:
    pip install pygame>=2.5
    The ``aprilcam`` package must be importable (install it or add its
    ``src/`` directory to PYTHONPATH).
"""
from __future__ import annotations

import argparse
import math
from typing import Tuple, List

import cv2 as cv
import numpy as np

# --- Simulator helpers ---
MARGIN = 20  # margin from window edge to the outer edge of the fiducial's quiet zone


def _draw_marker_img(dictionary: cv.aruco.Dictionary, marker_id: int, side: int):
    if hasattr(cv.aruco, "generateImageMarker"):
        img = cv.aruco.generateImageMarker(dictionary, int(marker_id), int(side))
    else:
        img = cv.aruco.drawMarker(dictionary, int(marker_id), int(side))
    # Return RGB for pygame
    return cv.cvtColor(img, cv.COLOR_GRAY2RGB)


def _np_to_surface_rgb(arr_hwc_rgb):
    import pygame

    # pygame.surfarray expects (W,H,3); convert from (H,W,3)
    return pygame.surfarray.make_surface(np.transpose(arr_hwc_rgb, (1, 0, 2)))


def _make_padded_marker_surface(dictionary: cv.aruco.Dictionary, marker_id: int, side: int, quiet_ratio: float = 2.0/7.0):
    import pygame
    side = int(side)
    if float(quiet_ratio) <= 0.0:
        pad = 0
    else:
        pad = max(1, int(round(float(side) * float(quiet_ratio))))
    marker = _draw_marker_img(dictionary, int(marker_id), int(side))  # RGB
    h, w = marker.shape[:2]
    canvas = np.full((h + 2 * pad, w + 2 * pad, 3), 255, dtype=np.uint8)
    canvas[pad:pad + h, pad:pad + w] = marker
    return _np_to_surface_rgb(canvas), (w + 2 * pad, h + 2 * pad)


def compute_fiducial_centers(width: int, height: int, fid_side: int, quiet_ratio: float = 2.0/7.0) -> dict[int, Tuple[int, int]]:
    four = cv.aruco.getPredefinedDictionary(cv.aruco.DICT_4X4_50)
    _surf, (pad_w, pad_h) = _make_padded_marker_surface(four, 0, int(fid_side), quiet_ratio)
    positions = {
        0: (MARGIN + pad_w // 2, MARGIN + pad_h // 2),
        1: (width - MARGIN - pad_w // 2, MARGIN + pad_h // 2),
        3: (width - MARGIN - pad_w // 2, height - MARGIN - pad_h // 2),
        2: (MARGIN + pad_w // 2, height - MARGIN - pad_h // 2),
    }
    return positions


def get_playfield_rect(width: int, height: int, fid_side: int, quiet_ratio: float = 2.0/7.0) -> Tuple[float, float, float, float]:
    pos = compute_fiducial_centers(width, height, fid_side, quiet_ratio)
    ul = pos[0]
    ur = pos[1]
    ll = pos[2]
    l = float(ul[0])
    r = float(ur[0])
    t = float(ul[1])
    b = float(ll[1])
    return l, t, r, b


def place_fiducials(screen, width: int, height: int, fid_side: int, quiet_ratio: float = 2.0/7.0) -> None:
    import pygame
    four = cv.aruco.getPredefinedDictionary(cv.aruco.DICT_4X4_50)
    positions = compute_fiducial_centers(width, height, fid_side, quiet_ratio)
    for cid, center in positions.items():
        surf, _ = _make_padded_marker_surface(four, cid, int(fid_side), quiet_ratio)
        rect = surf.get_rect(center=center)
        screen.blit(surf, rect)


class SimPlayfield:
    def __init__(
        self,
        width: int = 1200,
        height: int = 800,
        tag_size: int = 48,
        fiducial_size: int = 48,
        tag_ids: List[int] | None = None,
        fps: float = 60.0,
        quiet_ratio: float = 2.0/7.0,
    ) -> None:
        self.width = int(width)
        self.height = int(height)
        self.tag_size = int(tag_size)
        self.fiducial_size = int(fiducial_size)
        self.tag_ids = list(tag_ids or [1, 2, 3, 4])
        self.fps = float(fps)
        self.quiet_ratio = float(quiet_ratio)

    def run(self) -> int:
        try:
            import pygame
        except Exception:
            print("This program requires pygame. Install with: pip install pygame")
            return 2

        pygame.init()
        screen = pygame.display.set_mode((self.width, self.height))
        pygame.display.set_caption("AprilCam Playfield")
        clock = pygame.time.Clock()

        apriltag_dict = cv.aruco.getPredefinedDictionary(cv.aruco.DICT_APRILTAG_36h11)

        tag_ids = self.tag_ids or [1, 2, 3, 4]
        if len(tag_ids) < 4:
            last = tag_ids[-1]
            tag_ids = tag_ids + [last] * (4 - len(tag_ids))

        tag_surfs = []
        tag_dims = []
        for tid in tag_ids[:4]:
            surf, (pw, ph) = _make_padded_marker_surface(apriltag_dict, tid, int(self.tag_size), quiet_ratio=self.quiet_ratio)
            tag_surfs.append(surf)
            tag_dims.append((pw, ph))

        W, H = self.width, self.height
        pf_l, pf_t, pf_r, pf_b = get_playfield_rect(W, H, int(self.fiducial_size), quiet_ratio=self.quiet_ratio)

        def _half_extents(surf, angle_deg: float) -> Tuple[float, float]:
            import pygame
            rot = pygame.transform.rotozoom(surf, -angle_deg, 1.0)
            return rot.get_width() / 2.0, rot.get_height() / 2.0

        hx_edge, hy_edge = _half_extents(tag_surfs[0], 0.0)
        edge_path = [
            (pf_l + hx_edge, pf_t + hy_edge),
            (pf_r - hx_edge, pf_t + hy_edge),
            (pf_r - hx_edge, pf_b - hy_edge),
            (pf_l + hx_edge, pf_b - hy_edge),
        ]

        def _seg_len(a, b):
            return math.hypot(b[0] - a[0], b[1] - a[1])

        edge_lengths = [
            _seg_len(edge_path[i], edge_path[(i + 1) % 4]) for i in range(4)
        ]
        edge_perim = sum(edge_lengths)

        def sample_edge_path(s: float) -> Tuple[float, float]:
            s = s % edge_perim
            acc = 0.0
            for i in range(4):
                a = edge_path[i]
                b = edge_path[(i + 1) % 4]
                L = edge_lengths[i]
                if s <= acc + L:
                    t = (s - acc) / (L if L > 1e-6 else 1.0)
                    return (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))
                acc += L
            return edge_path[0]

        edge_speed = 220.0
        edge_s1 = 0.0
        edge_s2 = edge_perim / 2.0

        cx_c = (pf_l + pf_r) * 0.5
        cy_c = (pf_t + pf_b) * 0.5
        pw_c, ph_c = tag_dims[2]
        half_diag_c = 0.5 * math.hypot(pw_c, ph_c)
        max_r_x = (pf_r - pf_l) * 0.5 - half_diag_c
        max_r_y = (pf_b - pf_t) * 0.5 - half_diag_c
        circ_center = (cx_c, cy_c)
        circ_r = max(10.0, min(max_r_x, max_r_y))
        circ_ang = 0.0
        circ_w = math.radians(40.0)

        spin_ang = 0.0
        spin_w = math.radians(90.0)

        running = True
        prev_t = pygame.time.get_ticks() / 1000.0
        speed = 1.0
        while running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                elif event.type == pygame.KEYDOWN:
                    if event.key in (pygame.K_ESCAPE, pygame.K_q):
                        running = False
                    elif event.key == pygame.K_UP:
                        speed = min(8.0, speed * 1.10)
                        print(f"Speed: {speed:.2f}x")
                    elif event.key == pygame.K_DOWN:
                        speed = max(0.05, speed / 1.10)
                        print(f"Speed: {speed:.2f}x")

            now = pygame.time.get_ticks() / 1000.0
            dt = max(1e-3, min(0.1, now - prev_t))
            prev_t = now

            edge_s1 = (edge_s1 + edge_speed * dt * speed) % edge_perim
            edge_s2 = (edge_s2 + edge_speed * dt * speed) % edge_perim
            e1x, e1y = sample_edge_path(edge_s1)
            e2x, e2y = sample_edge_path(edge_s2)

            circ_ang = (circ_ang + circ_w * dt * speed) % (2 * math.pi)
            cx = circ_center[0] + circ_r * math.cos(circ_ang)
            cy = circ_center[1] + circ_r * math.sin(circ_ang)

            spin_ang = (spin_ang + spin_w * dt * speed) % (2 * math.pi)

            screen.fill((24, 24, 24))
            place_fiducials(screen, W, H, int(self.fiducial_size), quiet_ratio=self.quiet_ratio)

            draw_list = [
                ((e1x, e1y), 0.0, 0),
                ((e2x, e2y), 0.0, 1),
                ((cx, cy), 0.0, 2),
                ((cx_c, cy_c), math.degrees(spin_ang), 3),
            ]
            for (pos, angle_deg, idx) in draw_list:
                rotated = pygame.transform.rotozoom(tag_surfs[idx], -angle_deg, 1.0)
                rect = rotated.get_rect(center=(int(pos[0]), int(pos[1])))
                screen.blit(rotated, rect)

            pygame.display.flip()
            clock.tick(self.fps)

        pygame.quit()
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="playfield-sim",
        description="Pygame window with 4x4 corner fiducials and four AprilTags: 2 edge-runners (clockwise), 1 circular mover, 1 center spinner",
    )
    parser.add_argument("--width", type=int, default=1200, help="Window width in pixels (default 1200)")
    parser.add_argument("--height", type=int, default=800, help="Window height in pixels (default 800)")
    parser.add_argument("--tag-size", type=int, default=48, help="Moving AprilTag side length in pixels (default 48)")
    parser.add_argument("--fiducial-size", type=int, default=48, help="Corner fiducial side length in pixels (default 48)")
    parser.add_argument(
        "--tag-ids",
        type=str,
        default="1,2,3,4",
        help="Comma-separated AprilTag IDs to render for moving tags (first 4 used: two edge-runners, circular mover, spinner)",
    )
    parser.add_argument("--fps", type=float, default=60.0, help="Target frame rate (default 60)")
    parser.add_argument(
        "--quiet-ratio",
        type=float,
        default=2.0/7.0,
        help=(
            "White quiet zone thickness as a fraction of the tag side added on each edge. "
            "Example: 0.285714 (~2/7) for two tag modules. Use 0 to disable."
        ),
    )
    args = parser.parse_args(argv)

    ids = [int(p.strip()) for p in str(args.tag_ids).split(',') if p.strip()]
    sim = SimPlayfield(
        width=int(args.width),
        height=int(args.height),
        tag_size=int(args.tag_size),
        fiducial_size=int(args.fiducial_size),
        tag_ids=ids or [1, 2, 3, 4],
        fps=float(args.fps),
        quiet_ratio=float(args.quiet_ratio),
    )
    return sim.run()


if __name__ == "__main__":
    raise SystemExit(main())
