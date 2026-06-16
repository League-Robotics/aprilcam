from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2 as cv
import numpy as np

from ..camera.camutil import camera_slug
from ..config import AppConfig
from ..camera.screencap import ScreenCaptureMSS

# Backward-compat re-exports: types and workflow functions now live in
# calibration.py.  Existing imports from homography still work.
from .calibration import (  # noqa: F401
    CameraCalibration,
    FieldSpec,
    calibration_path,
    calibrate_from_corners,
    calibrate_joint,
    calibrate_single,
    load_calibration,
    load_calibration_for_camera,
    save_calibration,
    save_joint_calibration,
    load_joint_calibration,
)

# Mapping of special 4x4 ArUco IDs to board corners
# These IDs were used earlier when generating markers with labels.
CORNER_ID_MAP = {
    0: "upper_left",
    1: "upper_right",
    2: "lower_left",
    3: "lower_right",
}


def detect_aruco_4x4(gray: np.ndarray) -> List[Tuple[np.ndarray, int]]:
    dictionary = cv.aruco.getPredefinedDictionary(cv.aruco.DICT_4X4_50)
    params = cv.aruco.DetectorParameters()
    det = cv.aruco.ArucoDetector(dictionary, params)
    corners, ids, _ = det.detectMarkers(gray)
    results: List[Tuple[np.ndarray, int]] = []
    if ids is not None and len(ids) > 0:
        for c, i in zip(corners, ids.flatten()):
            pts = np.array(c, dtype=np.float32).reshape(-1, 2)
            results.append((pts, int(i)))
    return results


def choose_corner_point(pts: np.ndarray) -> np.ndarray:
    """Return the tag center for homography correspondence.
    Heights are measured from the center of the tags per spec.
    """
    return pts.mean(axis=0)


def homography_path(slug: str, data_dir: str | Path = "data") -> Path:
    """Return the per-camera homography file path for a given slug."""
    return Path(data_dir) / f"homography-{slug}.json"


def discover_homography(
    device_name: str,
    width: int,
    height: int,
    data_dir: str | Path = "data",
) -> Path | None:
    """Find the best homography file for a specific camera.

    Checks in order:
    1. ``data/calibration.json`` (unified playfield calibration)
    2. ``data/homography-<slug>.json`` (legacy per-camera file)
    3. ``data/homography.json`` (legacy global fallback)

    Returns the path to the file, or ``None`` if nothing found.
    """
    # Prefer unified calibration file
    cal_file = calibration_path(data_dir)
    if cal_file.exists():
        try:
            data = json.loads(cal_file.read_text())
            cameras = data.get("cameras", {})
            for _key, cam_data in cameras.items():
                if cam_data.get("device_name") == device_name:
                    return cal_file
        except Exception:
            pass

    # Legacy per-camera file
    slug = camera_slug(device_name, width, height)
    per_camera = homography_path(slug, data_dir)
    if per_camera.exists():
        return per_camera

    # Legacy global fallback
    fallback = Path(data_dir) / "homography.json"
    if fallback.exists():
        return fallback
    return None


def compute_homography(pixel_pts: np.ndarray, world_pts_cm: np.ndarray) -> np.ndarray:
    # Use Direct Linear Transform via OpenCV. We require 4 points.
    H, mask = cv.findHomography(pixel_pts, world_pts_cm, method=0)
    if H is None:
        raise RuntimeError("Homography computation failed")
    return H


def detect_all_tags(
    cap: cv.VideoCapture,
    num_frames: int = 30,
) -> Dict[int, np.ndarray]:
    """Detect AprilTags and ArUco 4x4 markers, return averaged centers.

    Accumulates detections over *num_frames* and averages pixel positions
    for stability.  ArUco 4x4 IDs are stored as negative numbers
    (-1, -2, -3, -4 for IDs 0-3) to avoid collision with AprilTag IDs.

    Returns:
        Dict mapping tag_id → (cx, cy) averaged pixel center.
    """
    d36 = cv.aruco.getPredefinedDictionary(cv.aruco.DICT_APRILTAG_36H11)
    p36 = cv.aruco.DetectorParameters()
    det36 = cv.aruco.ArucoDetector(d36, p36)

    d4 = cv.aruco.getPredefinedDictionary(cv.aruco.DICT_4X4_50)
    p4 = cv.aruco.DetectorParameters()
    det4 = cv.aruco.ArucoDetector(d4, p4)

    accum: Dict[int, List[np.ndarray]] = {}

    for _ in range(num_frames):
        ret, frame = cap.read()
        if not ret:
            break
        gray = cv.cvtColor(frame, cv.COLOR_BGR2GRAY)

        corners36, ids36, _ = det36.detectMarkers(gray)
        if ids36 is not None:
            for c, tid in zip(corners36, ids36.flatten()):
                center = c.reshape(-1, 2).mean(axis=0)
                accum.setdefault(int(tid), []).append(center)

        corners4, ids4, _ = det4.detectMarkers(gray)
        if ids4 is not None:
            for c, tid in zip(corners4, ids4.flatten()):
                center = c.reshape(-1, 2).mean(axis=0)
                # Negative IDs for ArUco 4x4 to avoid collision
                accum.setdefault(-(int(tid) + 1), []).append(center)

    result: Dict[int, np.ndarray] = {}
    for tid, pts_list in accum.items():
        result[tid] = np.array(pts_list).mean(axis=0)
    return result


# ArUco 4x4 corner world positions (cm).
# Stored as negative IDs: -1=ArUco0=UL, -2=ArUco1=UR, -3=ArUco2=LL, -4=ArUco3=LR.
ARUCO_CORNER_WORLD: Dict[int, Tuple[float, float]] = {
    -1: (0.0, 0.0),       # ArUco 0 = upper-left
    -2: (134.3, 0.0),     # ArUco 1 = upper-right
    -3: (0.0, 89.3),      # ArUco 2 = lower-left
    -4: (134.3, 89.3),    # ArUco 3 = lower-right
}

def run_once(cap: cv.VideoCapture) -> Optional[Dict[str, Tuple[float, float]]]:
    ok, frame = cap.read()
    if not ok:
        return None
    gray = cv.cvtColor(frame, cv.COLOR_BGR2GRAY)
    dets = detect_aruco_4x4(gray)
    corner_centers: Dict[str, Tuple[float, float]] = {}
    for pts, tid in dets:
        if tid in CORNER_ID_MAP:
            key = CORNER_ID_MAP[tid]
            c = choose_corner_point(pts)
            corner_centers[key] = (float(c[0]), float(c[1]))
    return corner_centers


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="homocal",
        description=(
            "Select input (camera or screen), compute pixel->field homography from four 4x4 ArUco corner tags, "
            "and write JSON with source metadata plus a PNG snapshot."
        ),
    )
    # Field spec
    parser.add_argument("--width", type=float, default=40.0, help="Field width (left->right). Default 40 (inches)")
    parser.add_argument("--height", type=float, default=35.0, help="Field height (upper->lower). Default 35 (inches)")
    parser.add_argument("--units", choices=["inch", "cm"], default="inch", help="Units for width/height input (default inch)")
    # Input selection (camera or screen)
    parser.add_argument("--screen", action="store_true", help="Use screen capture (primary display by default)")
    parser.add_argument("--screen-monitor", type=int, default=1, help="mss monitor index (1=primary, 0=all/virtual)")
    parser.add_argument("--screen-fps", type=float, default=30.0, help="Target FPS for screen capture")
    parser.add_argument("--screen-region", type=str, help="Optional region x,y,w,h within the chosen monitor")
    parser.add_argument("--camera", type=int, help="Camera index (if not using --screen)")
    parser.add_argument("--backend", type=str, choices=["auto", "avfoundation", "v4l2", "msmf", "dshow"], default="auto")
    parser.add_argument("--max-cams", type=int, default=10, help="Max camera indices to probe when resolving a camera")
    parser.add_argument("--cap-width", type=int, help="Optional camera capture width")
    parser.add_argument("--cap-height", type=int, help="Optional camera capture height")
    # Detection behavior baked into file (so aprilcam needn't carry flags)
    parser.add_argument("--detect-inverted", action="store_true", default=True, help="Enable detecting inverted tags (default on)")
    parser.add_argument("--no-detect-inverted", dest="detect_inverted", action="store_false")
    # Homography file
    parser.add_argument("--homography", type=str, default="homography.json", help="Output homography JSON filename (in data dir unless absolute)")
    parser.add_argument("--frames", type=int, default=30, help="Max frames to search for all four tags before failing")
    parser.add_argument(
        "--sleep",
        type=float,
        default=None,
        help=(
            "Delay in seconds before starting capture; prints a countdown. "
            "Defaults to 3s when --screen is used, otherwise 0."
        ),
    )
    args = parser.parse_args(argv)

    # Config and data dir
    cfg = AppConfig.load()

    # Resolve input
    cap = None
    source_meta: Dict[str, object]
    if args.screen:
        region = None
        if args.screen_region:
            try:
                parts = [int(p.strip()) for p in str(args.screen_region).split(",")]
                if len(parts) != 4:
                    raise ValueError
                region = (parts[0], parts[1], parts[2], parts[3])
            except Exception:
                print("Invalid --screen-region. Use x,y,w,h")
                return 2
        try:
            cap = ScreenCaptureMSS(monitor=int(args.screen_monitor), fps=float(args.screen_fps), region=region)
        except Exception as e:
            print(f"Failed to initialize screen capture: {e}")
            return 2
        source_meta = {
            "type": "screen",
            "monitor": int(args.screen_monitor),
            "fps": float(args.screen_fps),
            "region": list(region) if region else None,
        }
    else:
        # Camera path using AppConfig helpers
        cap = cfg.get_camera(arg=args.camera, backend=args.backend, max_cams=int(args.max_cams), quiet=True)
        if not cap or not cap.isOpened():
            print("Failed to open camera")
            return 2
        if args.cap_width:
            cap.set(cv.CAP_PROP_FRAME_WIDTH, int(args.cap_width))
        if args.cap_height:
            cap.set(cv.CAP_PROP_FRAME_HEIGHT, int(args.cap_height))
        source_meta = {
            "type": "camera",
            "index": int(args.camera) if args.camera is not None else None,
            "backend": str(args.backend),
            "cap_width": int(args.cap_width) if args.cap_width else None,
            "cap_height": int(args.cap_height) if args.cap_height else None,
        }

    try:
        # Optional start delay with countdown
        sleep_secs: float = 3.0 if (args.sleep is None and args.screen) else (float(args.sleep) if args.sleep is not None else 0.0)
        if sleep_secs > 0:
            total = int(math.floor(sleep_secs))
            frac = max(0.0, float(sleep_secs) - float(total))
            print(f"Starting calibration in {sleep_secs:.1f}s...")
            for i in range(total, 0, -1):
                print(f"{i}...")
                time.sleep(1.0)
            if frac > 0:
                time.sleep(frac)

        # Resolve output paths and prepare snapshot path
        out_path = Path(args.homography)
        if not out_path.is_absolute():
            out_path = cfg.data_dir / out_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        snap_path = out_path.with_suffix('.png')
        # Capture an initial snapshot (raw). We'll overwrite with annotations later once detections exist.
        snap = None
        ok_snap = False
        try:
            ok_snap, snap = cap.read()
            if ok_snap and snap is not None:
                cv.imwrite(str(snap_path), snap)
                print(f"Saved snapshot to {snap_path}")
        except Exception:
            pass

        # Accumulate corner detections across frames
        found: Dict[str, Tuple[float, float]] = {}
        for _ in range(max(1, int(args.frames))):
            obs = run_once(cap) or {}
            found.update(obs)
            missing = [k for k in ("upper_left", "upper_right", "lower_left", "lower_right") if k not in found]
            if not missing:
                break
        if len(found) < 4:
            print("Did not observe all four corner tags. Move camera or ensure IDs 0..3 are visible.")
            return 3

        # Build correspondences. Pixel: detected centers. World: field coordinates in cm.
        field = FieldSpec(width_in=float(args.width), height_in=float(args.height), units=str(args.units))
        H, pixel_pts, world_pts_cm = calibrate_from_corners(found, field)

        # Save JSON with homography and source metadata
        out = {
            "units": "cm",
            "width_cm": field.width_cm,
            "height_cm": field.height_cm,
            "pixel_points": pixel_pts.tolist(),
            "world_points_cm": world_pts_cm.tolist(),
            "homography": H.tolist(),
            "note": "Maps [u,v,1]^T pixels to [X,Y,W]^T; use X/W,Y/W in centimeters.",
            "source": source_meta,
            "detect_inverted": bool(args.detect_inverted),
        }
        out_path.write_text(json.dumps(out, indent=2))
        print(f"Wrote homography to {out_path}")

        # Also save a per-camera named file when source is a camera
        if source_meta.get("type") == "camera":
            try:
                from ..camera.camutil import macos_avfoundation_device_names
                cam_idx = source_meta.get("index")
                cap_w = int(cap.get(cv.CAP_PROP_FRAME_WIDTH))
                cap_h = int(cap.get(cv.CAP_PROP_FRAME_HEIGHT))
                av_names = macos_avfoundation_device_names()
                dev_name = av_names.get(cam_idx, f"camera-{cam_idx}") if cam_idx is not None else None
                if dev_name and cap_w and cap_h:
                    slug = camera_slug(dev_name, cap_w, cap_h)
                    per_cam_path = homography_path(slug, cfg.data_dir)
                    per_cam_path.write_text(json.dumps(out, indent=2))
                    print(f"Wrote per-camera homography to {per_cam_path}")
            except Exception as e:
                print(f"Warning: could not save per-camera homography: {e}")

        # Draw annotations on snapshot: fiducial centers, fiducial bounding boxes, and playfield boundary
        draw_img = None
        try:
            if ok_snap and snap is not None:
                draw_img = snap.copy()
            else:
                ok2, frm = cap.read()
                if ok2 and frm is not None:
                    draw_img = frm.copy()
        except Exception:
            draw_img = None

        if draw_img is not None:
            try:
                # Attempt detection on the draw frame to get boxes
                gray2 = cv.cvtColor(draw_img, cv.COLOR_BGR2GRAY)
                dets2 = detect_aruco_4x4(gray2)
                for pts, tid in dets2:
                    if tid in CORNER_ID_MAP:
                        pts_i = pts.astype(np.int32).reshape(-1, 1, 2)
                        cv.polylines(draw_img, [pts_i], isClosed=True, color=(255, 0, 0), thickness=2)
                        c = pts.mean(axis=0)
                        cv.circle(draw_img, (int(c[0]), int(c[1])), 6, (0, 255, 0), -1)

                # Always draw centerpoints from the consolidated 'found' dict
                for name in ("upper_left", "upper_right", "lower_right", "lower_left"):
                    if name in found:
                        x, y = found[name]
                        cv.circle(draw_img, (int(x), int(y)), 5, (0, 200, 0), -1)

                # Draw playfield boundary polygon (UL -> UR -> LR -> LL)
                ul = found["upper_left"]
                ur = found["upper_right"]
                lr = found["lower_right"]
                ll = found["lower_left"]
                poly = np.array([ul, ur, lr, ll], dtype=np.int32).reshape(-1, 1, 2)
                cv.polylines(draw_img, [poly], isClosed=True, color=(0, 255, 255), thickness=2)

                cv.imwrite(str(snap_path), draw_img)
                print(f"Updated annotated snapshot at {snap_path}")
            except Exception as e:
                # If annotation fails, keep the raw snapshot
                print(f"Warning: failed to annotate snapshot: {e}")

        # Headless: no preview windows
        return 0
    finally:
        try:
            cap.release()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
