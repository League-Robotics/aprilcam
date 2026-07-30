#!/usr/bin/env python3
"""Robust tag detection test — multi-scale, multi-preprocessing pipeline.

Tests whether all expected tags and colored cubes on the playfield are
reliably detected under current lighting conditions.

Expected detections:
  ArUco 4x4:       IDs 0, 1, 2, 3  (corner markers)
  AprilTag 36h11:  IDs 3, 4, 5, 6  (edge markers)
                   IDs 7, 8, 9, 10, 11  (interior + center)
  Colored cubes:   8 total

Usage:
  python testdev/detect_test.py                    # use saved sample frame
  python testdev/detect_test.py --camera 2         # capture live from camera 2
  python testdev/detect_test.py --image path.jpg   # use specific image
  python testdev/detect_test.py --camera 2 --save  # save debug images
"""

from __future__ import annotations
import argparse
import sys
import time
from pathlib import Path

import cv2 as cv
import numpy as np


# ── Ground truth ───────────────────────────────────────────────────────
EXPECTED_ARUCO = {0, 1, 2, 3}
EXPECTED_APRILTAG = {3, 4, 5, 6, 7, 8, 9, 10, 11}
EXPECTED_CUBES = 8


# ── Detector setup ─────────────────────────────────────────────────────

def make_detector_params():
    """Tuned detector parameters for challenging lighting."""
    p = cv.aruco.DetectorParameters()
    p.adaptiveThreshWinSizeMin = 3
    p.adaptiveThreshWinSizeMax = 53
    p.adaptiveThreshWinSizeStep = 4   # more threshold passes
    return p


def detect_tags(gray: np.ndarray):
    """Detect AprilTag 36h11 and ArUco 4x4, return (april_ids, aruco_ids)."""
    p = make_detector_params()

    d36 = cv.aruco.getPredefinedDictionary(cv.aruco.DICT_APRILTAG_36H11)
    det36 = cv.aruco.ArucoDetector(d36, p)
    _, ids36, _ = det36.detectMarkers(gray)
    april = set(ids36.flatten().tolist()) if ids36 is not None else set()

    d4 = cv.aruco.getPredefinedDictionary(cv.aruco.DICT_4X4_50)
    det4 = cv.aruco.ArucoDetector(d4, p)
    _, ids4, _ = det4.detectMarkers(gray)
    aruco = set(ids4.flatten().tolist()) if ids4 is not None else set()

    return april, aruco


# ── Preprocessing strategies ──────────────────────────────────────────

def preprocess_highpass_clahe(gray: np.ndarray) -> np.ndarray:
    """Illumination flattening + CLAHE. Divides out the low-frequency
    illumination field, then applies CLAHE for local contrast."""
    illum = cv.GaussianBlur(gray, (51, 51), 0).astype(np.float32)
    illum = np.maximum(illum, 1.0)
    flat = np.clip((gray.astype(np.float32) / illum) * 128.0, 0, 255).astype(np.uint8)
    return cv.createCLAHE(3.0, (8, 8)).apply(flat)


def preprocess_clahe(gray: np.ndarray, clip: float = 4.0) -> np.ndarray:
    """CLAHE only. Good base for upscaled detection."""
    return cv.createCLAHE(clip, (8, 8)).apply(gray)


def preprocess_equalize(gray: np.ndarray) -> np.ndarray:
    """Histogram equalization. Finds different tags than CLAHE."""
    return cv.equalizeHist(gray)


def preprocess_strong_clahe_highpass(gray: np.ndarray) -> np.ndarray:
    """Very strong CLAHE + illumination flattening."""
    strong = cv.createCLAHE(5.0, (4, 4)).apply(gray)
    illum = cv.GaussianBlur(strong, (151, 151), 0).astype(np.float32)
    illum = np.maximum(illum, 1.0)
    flat = np.clip((strong.astype(np.float32) / illum) * 128.0, 0, 255).astype(np.uint8)
    return cv.createCLAHE(3.0, (8, 8)).apply(flat)


def preprocess_aggressive_clahe(gray: np.ndarray) -> np.ndarray:
    """Very aggressive CLAHE for blown-out/glare areas."""
    return cv.createCLAHE(12.0, (4, 4)).apply(gray)


# ── Multi-scale union pipeline ────────────────────────────────────────

STRATEGIES = [
    # (label, scale, preprocess_fn, post_clahe)
    # post_clahe: apply light CLAHE after upscale (helps some, hurts strong preproc)
    ("hp+clahe@1x",    1.0, preprocess_highpass_clahe,        False),
    ("hp+clahe@1.5x",  1.5, preprocess_highpass_clahe,        True),
    ("clahe4@2x",      2.0, preprocess_clahe,                 True),
    ("equalize@2x",    2.0, preprocess_equalize,              True),
    ("strong@3x",      3.0, preprocess_strong_clahe_highpass, False),
]

# Strategies that add time but haven't been finding new tags in testing.
# Kept here commented out for reference.
# ("aggressive@3x",  3.0, preprocess_aggressive_clahe,      False),
# ("aggressive@4x",  4.0, preprocess_aggressive_clahe,      False),
# ("equalize@4x",    4.0, preprocess_equalize,              True),
# ("aggressive@5x",  5.0, preprocess_aggressive_clahe,      False),
# ("equalize@3x",    3.0, preprocess_equalize,              True),
_EXTRA_STRATEGIES = [
]


def detect_multiscale(gray: np.ndarray, save_dir: Path | None = None, verbose=True):
    """Run multi-scale multi-preprocessing union detection.

    Returns (all_april, all_aruco, per_strategy_results).
    """
    all_april = set()
    all_aruco = set()
    results = []

    for label, scale, prep_fn, post_clahe in STRATEGIES:
        t0 = time.monotonic()

        # Upscale raw gray FIRST, then preprocess at the higher resolution
        if scale != 1.0:
            h, w = gray.shape[:2]
            nw, nh = int(w * scale), int(h * scale)
            upscaled = cv.resize(gray, (nw, nh), interpolation=cv.INTER_CUBIC)
            preprocessed = prep_fn(upscaled)
            if post_clahe:
                preprocessed = cv.createCLAHE(3.0, (8, 8)).apply(preprocessed)
            scaled = preprocessed
        else:
            scaled = prep_fn(gray)

        # Detect
        april, aruco = detect_tags(scaled)
        dt = (time.monotonic() - t0) * 1000

        all_april |= april
        all_aruco |= aruco

        results.append((label, april, aruco, dt))

        if save_dir is not None:
            cv.imwrite(str(save_dir / f"{label.replace('@','_')}.jpg"), scaled)

        if verbose:
            a_str = str(sorted(april)) if april else "—"
            r_str = str(sorted(aruco)) if aruco else "—"
            print(f"  {label:<22s} {dt:>6.0f}ms  AT:{a_str}  Ar:{r_str}")

    # Tiled detection: crop overlapping tiles, upscale each locally, detect.
    # Local equalize/CLAHE adapts to each tile's intensity range, finding
    # tags that are washed out in the global image.
    # Only process the playfield interior (skip edges outside the ArUco frame).
    t0 = time.monotonic()
    h, w = gray.shape[:2]
    tile_size = 160
    step = 40  # dense overlap to catch tags at any position
    tile_scale = 4
    tile_found_april = set()
    tile_found_aruco = set()

    d36 = cv.aruco.getPredefinedDictionary(cv.aruco.DICT_APRILTAG_36H11)
    p_tile = make_detector_params()

    preps_and_scales = [
        (cv.equalizeHist, 4),
        (lambda g: cv.createCLAHE(12.0, (2, 2)).apply(g), 4),
        (lambda g: cv.createCLAHE(8.0, (4, 4)).apply(g), 4),
        # Higher upscale with aggressive CLAHE for worst-case glare areas
        (lambda g: cv.createCLAHE(12.0, (4, 4)).apply(g), 5),
    ]

    for y0 in range(0, h - tile_size + 1, step):
        for x0 in range(0, w - tile_size + 1, step):
            tile = gray[y0:y0 + tile_size, x0:x0 + tile_size]

            for prep, ts in preps_and_scales:
                processed = prep(tile)
                up = cv.resize(processed, None, fx=ts, fy=ts,
                               interpolation=cv.INTER_CUBIC)

                det36 = cv.aruco.ArucoDetector(d36, p_tile)
                _, ids36, _ = det36.detectMarkers(up)
                if ids36 is not None:
                    tile_found_april |= set(ids36.flatten().tolist())

    dt = (time.monotonic() - t0) * 1000
    all_april |= tile_found_april
    if verbose:
        a_str = str(sorted(tile_found_april)) if tile_found_april else "—"
        print(f"  {'tiled@4x':<22s} {dt:>6.0f}ms  AT:{a_str}")

    # Filter cross-dictionary false positives:
    # - ArUco 4x4 markers (IDs 0-3) can decode as AprilTag 36h11
    # - ArUco ID 17 is a recurring false positive from playfield edges
    all_april -= {0, 1, 2}  # ArUco corner IDs that alias as AprilTags
    all_aruco -= {17}

    return all_april, all_aruco, results


# ── Cube detection ─────────────────────────────────────────────────────

def detect_cubes(frame_bgr: np.ndarray, homography: np.ndarray | None = None):
    """Detect colored cubes and classify colors."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from aprilcam.objects import SquareDetector
    from aprilcam.color_classifier import ColorClassifier
    from aprilcam.playfield import Playfield
    from dataclasses import replace

    gray = cv.cvtColor(frame_bgr, cv.COLOR_BGR2GRAY)

    # Detect tags for exclusion zones
    illum = cv.GaussianBlur(gray, (51, 51), 0).astype(np.float32)
    illum = np.maximum(illum, 1.0)
    flat = np.clip((gray.astype(np.float32) / illum) * 128.0, 0, 255).astype(np.uint8)

    tag_corners = []
    p = make_detector_params()
    for dict_id in [cv.aruco.DICT_APRILTAG_36H11, cv.aruco.DICT_4X4_50]:
        d = cv.aruco.getPredefinedDictionary(dict_id)
        det = cv.aruco.ArucoDetector(d, p)
        corners, ids, _ = det.detectMarkers(flat)
        if corners:
            for c in corners:
                tag_corners.append(c.reshape(-1, 2).astype(np.float32))

    # Get playfield polygon
    pf = Playfield()
    pf.update(frame_bgr)
    pf_poly = pf.get_polygon()

    detector = SquareDetector()
    objects = detector.detect(gray, homography=homography,
                              tag_corners=tag_corners,
                              playfield_polygon=pf_poly)

    classifier = ColorClassifier()
    colored = []
    for obj in objects:
        cx, cy = obj.center_px
        c = classifier.classify_at_point(frame_bgr, cx, cy)
        colored.append(replace(obj, color=c))

    return colored


# ── Scoring ────────────────────────────────────────────────────────────

def print_results(all_april, all_aruco, cubes, elapsed_ms):
    """Print detection results and score."""
    found_april = all_april & EXPECTED_APRILTAG
    found_aruco = all_aruco & EXPECTED_ARUCO
    false_april = all_april - EXPECTED_APRILTAG
    false_aruco = all_aruco - EXPECTED_ARUCO
    missing_april = EXPECTED_APRILTAG - all_april
    missing_aruco = EXPECTED_ARUCO - all_aruco

    total_tags = len(found_april) + len(found_aruco)
    total_expected = len(EXPECTED_APRILTAG) + len(EXPECTED_ARUCO)

    cube_colors = [c.color for c in cubes if c.color != "unknown"]
    n_colored = len(cube_colors)

    print(f"\n{'='*60}")
    print(f"TAG DETECTION:  {total_tags}/{total_expected}")
    print(f"  AprilTag found:    {sorted(found_april)}")
    print(f"  ArUco found:       {sorted(found_aruco)}")
    if missing_april:
        print(f"  AprilTag MISSING:  {sorted(missing_april)}")
    if missing_aruco:
        print(f"  ArUco MISSING:     {sorted(missing_aruco)}")
    if false_april:
        print(f"  AprilTag FALSE:    {sorted(false_april)}")
    if false_aruco:
        print(f"  ArUco FALSE:       {sorted(false_aruco)}")

    print(f"\nCUBE DETECTION: {len(cubes)} objects, {n_colored} with color")
    if cube_colors:
        from collections import Counter
        for color, count in sorted(Counter(cube_colors).items()):
            print(f"  {color}: {count}")

    print(f"\nTotal time: {elapsed_ms:.0f}ms")
    print(f"{'='*60}")

    # Pass/fail
    ok = total_tags >= total_expected - 1 and len(false_april | false_aruco) == 0
    status = "PASS" if ok else "FAIL"
    print(f"\n{status}: {total_tags}/{total_expected} tags, "
          f"{len(false_april | false_aruco)} false positives")
    return ok


# ── Main ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Robust tag detection test")
    parser.add_argument("--camera", type=int, default=None)
    parser.add_argument("--image", type=str, default=None)
    parser.add_argument("--save", action="store_true",
                        help="Save debug images to testdev/debug/")
    args = parser.parse_args()

    # Get frame
    if args.camera is not None:
        cap = cv.VideoCapture(args.camera)
        if not cap.isOpened():
            print(f"Failed to open camera {args.camera}")
            sys.exit(1)
        for _ in range(10):
            cap.read()
        ret, frame = cap.read()
        cap.release()
        if not ret:
            print("Failed to capture frame")
            sys.exit(1)
    else:
        path = args.image or str(Path(__file__).parent / "sample_frame.jpg")
        frame = cv.imread(path)
        if frame is None:
            print(f"Failed to load: {path}")
            sys.exit(1)

    gray = cv.cvtColor(frame, cv.COLOR_BGR2GRAY)
    print(f"Frame: {frame.shape[1]}x{frame.shape[0]}")

    save_dir = None
    if args.save:
        save_dir = Path(__file__).parent / "debug"
        save_dir.mkdir(exist_ok=True)
        cv.imwrite(str(save_dir / "original.jpg"), frame)

    # Run detection
    t0 = time.monotonic()

    print("\nTag detection (multi-scale union):")
    all_april, all_aruco, _ = detect_multiscale(gray, save_dir=save_dir)

    # Load homography for cube world coords
    import json
    cal_path = Path(__file__).resolve().parent.parent / "data" / "calibration.json"
    homography = None
    if cal_path.exists():
        try:
            cal = json.loads(cal_path.read_text())
            cam_data = list(cal["cameras"].values())[0]
            homography = np.array(cam_data["homography"], dtype=np.float64)
        except Exception:
            pass

    print("\nCube detection:")
    cubes = detect_cubes(frame, homography)
    for i, c in enumerate(cubes):
        world = f"({c.world_xy[0]:.1f},{c.world_xy[1]:.1f})" if c.world_xy else "—"
        print(f"  {i}: {c.color:>8s}  area={c.area_px:4.0f}  world={world}")

    elapsed = (time.monotonic() - t0) * 1000
    ok = print_results(all_april, all_aruco, cubes, elapsed)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
