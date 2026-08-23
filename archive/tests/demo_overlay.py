"""Demo script exercising paths and live overlay features.

Run with a live camera and view open:
    aprilcam view <camera_index> &
    uv run python tests/demo_overlay.py

The script walks through several scenes:
  1. Static path  — writes a triangular waypoint path to paths.json
  2. Static overlays — arc, arrow, point, polyline held for several seconds
  3. Animation — a "robot" circle orbits the playfield with a heading arrow
  4. Cleanup — removes all paths and clears the overlay

World coordinates are in cm.  The demo assumes a calibrated playfield
centred around (cx=60, cy=45) with roughly 120×90 cm extent; adjust
FIELD_CX, FIELD_CY, and FIELD_R at the top of the file if needed.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Tunable constants — adjust to match your playfield
# ---------------------------------------------------------------------------
FIELD_CX = 60.0   # world X of playfield centre (cm)
FIELD_CY = 45.0   # world Y of playfield centre (cm)
FIELD_R  = 35.0   # orbit radius for the animation (cm)

# Camera whose paths.json we'll write.  Must have a paths_file entry in
# its info.json (i.e. it was opened by the daemon at least once).
CAM_NAME = "arducam-ov9782-usb-camera"

PATHS_FILE = Path(
    "/Volumes/Proj/proj/RobotProjects/AprilTags/data/aprilcam/cameras"
) / CAM_NAME / "paths.json"

ANIMATION_HZ = 8        # overlay publish rate during animation
ANIMATION_SECS = 15     # how long to animate
STATIC_HOLD_SECS = 5    # how long to hold each static scene
OVERLAY_TTL = 0.5       # seconds before view auto-drops a stale overlay

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_paths(paths: list[dict]) -> None:
    """Atomically write paths to paths.json (view_cli picks it up within ~33ms)."""
    tmp = PATHS_FILE.with_suffix(".tmp")
    PATHS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(paths))
    os.replace(tmp, PATHS_FILE)
    print(f"  paths.json written ({len(paths)} path(s))")


def _clear_paths() -> None:
    _write_paths([])
    print("  paths cleared")


def _sep(title: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print('─' * 60)


# ---------------------------------------------------------------------------
# Path scenes
# ---------------------------------------------------------------------------

def _make_triangle_path() -> list[dict]:
    """A green equilateral triangle centred at the playfield."""
    r = 30.0
    pts = [
        (FIELD_CX + r * math.cos(math.radians(a)),
         FIELD_CY + r * math.sin(math.radians(a)))
        for a in (270, 30, 150)
    ]
    waypoints = []
    for i, (x, y) in enumerate(pts):
        is_last = i == len(pts) - 1
        waypoints.append({
            "x": x, "y": y, "size_cm": 4.0,
            "symbol": "filled_circle",
            "symbol_color": [0, 220, 60],
            "line_color": [0, 220, 60] if not is_last else [0, 0, 0],
        })
    # close the loop by repeating the first point
    x0, y0 = pts[0]
    waypoints.append({
        "x": x0, "y": y0, "size_cm": 0.1,
        "symbol": "none",
        "symbol_color": [0, 0, 0],
        "line_color": [0, 0, 0],
    })
    return [{
        "path_id": "path_000",
        "playfield_id": CAM_NAME,
        "waypoints": waypoints,
    }]


def _make_cross_path() -> list[dict]:
    """A magenta cross (+ shape) centred at the playfield."""
    arm = 25.0
    pts = [
        (FIELD_CX, FIELD_CY - arm),
        (FIELD_CX, FIELD_CY + arm),
        (FIELD_CX, FIELD_CY),        # back to centre
        (FIELD_CX - arm, FIELD_CY),
        (FIELD_CX + arm, FIELD_CY),
    ]
    waypoints = []
    for i, (x, y) in enumerate(pts):
        waypoints.append({
            "x": x, "y": y, "size_cm": 3.0,
            "symbol": "x" if i == 2 else "filled_circle",
            "symbol_color": [220, 0, 220],
            "line_color": [220, 0, 220],
        })
    return [{
        "path_id": "path_000",
        "playfield_id": CAM_NAME,
        "waypoints": waypoints,
    }]


# ---------------------------------------------------------------------------
# Overlay scenes
# ---------------------------------------------------------------------------

def _scene_static_overlays(dc) -> None:
    """Show arc + arrow + point + polyline simultaneously."""
    elements = [
        # large cyan arc — "look-ahead" semicircle
        {
            "type": "arc",
            "params": [FIELD_CX, FIELD_CY, 20.0, -120.0, 120.0],
            "color": [0, 220, 220],
            "thickness": 3,
        },
        # orange arrow pointing right — heading
        {
            "type": "arrow",
            "params": [FIELD_CX, FIELD_CY, FIELD_CX + 18.0, FIELD_CY],
            "color": [255, 140, 0],
            "thickness": 3,
        },
        # red filled circle — target point
        {
            "type": "point",
            "params": [FIELD_CX + 18.0, FIELD_CY, 3.0],
            "color": [220, 50, 50],
            "thickness": -1,
        },
        # yellow polyline — planned route
        {
            "type": "polyline",
            "params": [
                FIELD_CX - 30, FIELD_CY + 20,
                FIELD_CX - 10, FIELD_CY + 5,
                FIELD_CX + 10, FIELD_CY + 5,
                FIELD_CX + 30, FIELD_CY + 20,
            ],
            "color": [255, 230, 0],
            "thickness": 2,
        },
    ]

    deadline = time.time() + STATIC_HOLD_SECS
    print(f"  holding static overlays for {STATIC_HOLD_SECS}s …")
    while time.time() < deadline:
        dc.publish_overlay(CAM_NAME, elements, ttl=OVERLAY_TTL)
        time.sleep(1.0 / ANIMATION_HZ)


def _scene_animate_robot(dc) -> None:
    """Orbit a 'robot' circle + heading arrow around the playfield centre."""
    print(f"  animating for {ANIMATION_SECS}s at {ANIMATION_HZ} Hz …")
    start = time.time()
    period = 6.0  # seconds per full revolution
    dt = 1.0 / ANIMATION_HZ

    while (elapsed := time.time() - start) < ANIMATION_SECS:
        angle = 2 * math.pi * (elapsed / period)  # current position angle
        head  = angle + math.pi / 2                # heading = tangent

        # Robot position
        rx = FIELD_CX + FIELD_R * math.cos(angle)
        ry = FIELD_CY + FIELD_R * math.sin(angle)

        # Lookahead point (30° ahead on the circle)
        la_angle = angle + math.radians(30)
        lax = FIELD_CX + FIELD_R * math.cos(la_angle)
        lay = FIELD_CY + FIELD_R * math.sin(la_angle)

        # Heading arrow tip
        arrow_len = 12.0
        ax2 = rx + arrow_len * math.cos(head)
        ay2 = ry + arrow_len * math.sin(head)

        # Lookahead radius for pure-pursuit arc display
        lookahead_r = math.hypot(lax - rx, lay - ry)

        elements = [
            # robot body — blue filled circle
            {
                "type": "point",
                "params": [rx, ry, 5.0],
                "color": [60, 120, 255],
                "thickness": -1,
            },
            # heading arrow — white
            {
                "type": "arrow",
                "params": [rx, ry, ax2, ay2],
                "color": [240, 240, 240],
                "thickness": 3,
            },
            # lookahead arc — green, 180° forward fan
            {
                "type": "arc",
                "params": [rx, ry, lookahead_r,
                           math.degrees(head) - 90,
                           math.degrees(head) + 90],
                "color": [0, 220, 60],
                "thickness": 2,
            },
            # lookahead target — red point
            {
                "type": "point",
                "params": [lax, lay, 3.0],
                "color": [255, 50, 50],
                "thickness": -1,
            },
            # orbit path hint — faint polyline (approximated as 24-point polygon)
            {
                "type": "polyline",
                "params": [
                    coord
                    for i in range(25)
                    for coord in (
                        FIELD_CX + FIELD_R * math.cos(2 * math.pi * i / 24),
                        FIELD_CY + FIELD_R * math.sin(2 * math.pi * i / 24),
                    )
                ],
                "color": [100, 100, 200],
                "thickness": 1,
            },
        ]

        dc.publish_overlay(CAM_NAME, elements, ttl=OVERLAY_TTL)
        time.sleep(dt)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    from aprilcam.config import Config
    from aprilcam.client.control import DaemonControl

    print("Connecting to daemon …")
    config = Config.load()
    dc = DaemonControl.connect_default(config)
    print("  connected.")

    cameras = dc.list_cameras()
    print(f"  open cameras: {cameras}")

    if CAM_NAME not in cameras:
        print(f"\nCamera '{CAM_NAME}' is not open.  Open it first with:")
        print(f"  aprilcam view <index>   # or open_camera via MCP")
        sys.exit(1)

    try:
        # ── Scene 1: static path — triangle ──────────────────────────────
        _sep("Scene 1: triangular path (green, file-based)")
        _write_paths(_make_triangle_path())
        print(f"  holding for {STATIC_HOLD_SECS}s …")
        time.sleep(STATIC_HOLD_SECS)

        # ── Scene 2: swap path to cross ───────────────────────────────────
        _sep("Scene 2: swap to cross path (magenta)")
        _write_paths(_make_cross_path())
        print(f"  holding for {STATIC_HOLD_SECS}s …")
        time.sleep(STATIC_HOLD_SECS)

        # ── Scene 3: static overlays on top of cross path ─────────────────
        _sep("Scene 3: static overlays (arc + arrow + point + polyline)")
        _scene_static_overlays(dc)

        # ── Scene 4: animation — robot orbiting ───────────────────────────
        _sep("Scene 4: animated robot orbit")
        _clear_paths()          # remove persistent path so animation is clear
        _scene_animate_robot(dc)

        # ── Cleanup ───────────────────────────────────────────────────────
        _sep("Cleanup")
        _clear_paths()
        dc.publish_overlay(CAM_NAME, [], ttl=0)   # clear overlay immediately
        print("  overlay cleared.  Done.")

    finally:
        dc.close()


if __name__ == "__main__":
    main()
