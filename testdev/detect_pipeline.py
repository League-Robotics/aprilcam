#!/usr/bin/env python3
"""Test image processing pipelines for reliable tag detection under variable lighting.

Expected detections on the playfield:
  ArUco 4x4: IDs 0,1,2,3 (corner markers)
  AprilTag 36h11: IDs 3,4,5,6 (edge), 7,8,9,10,11 (interior)
  Colored cubes: 8 total (red, orange, red, orange outer; blue, green, yellow, purple inner)

Usage:
  python testdev/detect_pipeline.py                    # use saved sample frame
  python testdev/detect_pipeline.py --camera 2         # capture live from camera 2
  python testdev/detect_pipeline.py --image path.jpg   # use specific image
"""

from __future__ import annotations
import argparse
import sys
import time
from pathlib import Path

import cv2 as cv
import numpy as np


# ── Expected ground truth ──────────────────────────────────────────────
EXPECTED_ARUCO_IDS = {0, 1, 2, 3}
EXPECTED_APRILTAG_IDS = {3, 4, 5, 6, 7, 8, 9, 10, 11}
EXPECTED_CUBES = 8


# ── Detection helpers ──────────────────────────────────────────────────

def detect_aruco(gray: np.ndarray, **params) -> set[int]:
    """Detect ArUco 4x4 markers, return set of IDs found."""
    d = cv.aruco.getPredefinedDictionary(cv.aruco.DICT_4X4_50)
    p = cv.aruco.DetectorParameters()
    for k, v in params.items():
        if hasattr(p, k):
            setattr(p, k, v)
    det = cv.aruco.ArucoDetector(d, p)
    corners, ids, _ = det.detectMarkers(gray)
    if ids is None:
        return set()
    return set(int(i) for i in ids.flatten())


def detect_apriltags(gray: np.ndarray, **params) -> set[int]:
    """Detect AprilTag 36h11 markers, return set of IDs found."""
    d = cv.aruco.getPredefinedDictionary(cv.aruco.DICT_APRILTAG_36H11)
    p = cv.aruco.DetectorParameters()
    for k, v in params.items():
        if hasattr(p, k):
            setattr(p, k, v)
    det = cv.aruco.ArucoDetector(d, p)
    corners, ids, _ = det.detectMarkers(gray)
    if ids is None:
        return set()
    return set(int(i) for i in ids.flatten())


def score_detection(aruco_ids: set[int], april_ids: set[int]) -> dict:
    """Score detection against expected ground truth."""
    aruco_found = aruco_ids & EXPECTED_ARUCO_IDS
    aruco_extra = aruco_ids - EXPECTED_ARUCO_IDS
    aruco_missing = EXPECTED_ARUCO_IDS - aruco_ids

    april_found = april_ids & EXPECTED_APRILTAG_IDS
    april_extra = april_ids - EXPECTED_APRILTAG_IDS
    april_missing = EXPECTED_APRILTAG_IDS - april_ids

    total_expected = len(EXPECTED_ARUCO_IDS) + len(EXPECTED_APRILTAG_IDS)
    total_found = len(aruco_found) + len(april_found)
    total_extra = len(aruco_extra) + len(april_extra)

    return {
        "aruco_found": sorted(aruco_found),
        "aruco_missing": sorted(aruco_missing),
        "aruco_extra": sorted(aruco_extra),
        "april_found": sorted(april_found),
        "april_missing": sorted(april_missing),
        "april_extra": sorted(april_extra),
        "score": f"{total_found}/{total_expected}",
        "false_positives": total_extra,
    }


# ── Preprocessing pipelines ───────────────────────────────────────────

def pipeline_raw(frame_bgr: np.ndarray) -> np.ndarray:
    """No preprocessing — just grayscale conversion."""
    return cv.cvtColor(frame_bgr, cv.COLOR_BGR2GRAY)


def pipeline_clahe(frame_bgr: np.ndarray, clip_limit=3.0, tile_size=8) -> np.ndarray:
    """CLAHE adaptive histogram equalization."""
    gray = cv.cvtColor(frame_bgr, cv.COLOR_BGR2GRAY)
    clahe = cv.createCLAHE(clipLimit=clip_limit, tileGridSize=(tile_size, tile_size))
    return clahe.apply(gray)


def pipeline_clahe_strong(frame_bgr: np.ndarray) -> np.ndarray:
    """CLAHE with aggressive settings for low-contrast scenes."""
    return pipeline_clahe(frame_bgr, clip_limit=6.0, tile_size=4)


def pipeline_adaptive_thresh(frame_bgr: np.ndarray) -> np.ndarray:
    """Adaptive thresholding to handle uneven illumination."""
    gray = cv.cvtColor(frame_bgr, cv.COLOR_BGR2GRAY)
    return cv.adaptiveThreshold(
        gray, 255, cv.ADAPTIVE_THRESH_GAUSSIAN_C, cv.THRESH_BINARY, 51, 10
    )


def pipeline_highpass(frame_bgr: np.ndarray, ksize=51) -> np.ndarray:
    """High-pass filter: subtract blurred version to remove low-freq illumination."""
    gray = cv.cvtColor(frame_bgr, cv.COLOR_BGR2GRAY)
    blur = cv.GaussianBlur(gray, (ksize, ksize), 0)
    # Subtract and re-center around 128
    hp = cv.subtract(gray, blur)
    hp = cv.add(hp, 128)
    return hp


def pipeline_highpass_clahe(frame_bgr: np.ndarray) -> np.ndarray:
    """High-pass filter followed by CLAHE."""
    hp = pipeline_highpass(frame_bgr)
    clahe = cv.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    return clahe.apply(hp)


def pipeline_normalize(frame_bgr: np.ndarray) -> np.ndarray:
    """Min-max normalization to full 0-255 range."""
    gray = cv.cvtColor(frame_bgr, cv.COLOR_BGR2GRAY)
    return cv.normalize(gray, None, 0, 255, cv.NORM_MINMAX)


def pipeline_bilateral(frame_bgr: np.ndarray) -> np.ndarray:
    """Bilateral filter (edge-preserving denoise) then CLAHE."""
    gray = cv.cvtColor(frame_bgr, cv.COLOR_BGR2GRAY)
    filtered = cv.bilateralFilter(gray, 9, 75, 75)
    clahe = cv.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    return clahe.apply(filtered)


def pipeline_unsharp_mask(frame_bgr: np.ndarray) -> np.ndarray:
    """Unsharp mask: sharpen edges to help tag corner detection."""
    gray = cv.cvtColor(frame_bgr, cv.COLOR_BGR2GRAY)
    blur = cv.GaussianBlur(gray, (0, 0), 3.0)
    return cv.addWeighted(gray, 1.5, blur, -0.5, 0)


def pipeline_green_channel(frame_bgr: np.ndarray) -> np.ndarray:
    """Use green channel only (often best SNR on color cameras)."""
    green = frame_bgr[:, :, 1]
    clahe = cv.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    return clahe.apply(green)


def pipeline_lab_l_channel(frame_bgr: np.ndarray) -> np.ndarray:
    """Extract L channel from LAB color space (perceptual lightness)."""
    lab = cv.cvtColor(frame_bgr, cv.COLOR_BGR2LAB)
    l_channel = lab[:, :, 0]
    clahe = cv.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    return clahe.apply(l_channel)


def pipeline_morphological(frame_bgr: np.ndarray) -> np.ndarray:
    """CLAHE + morphological close to fill small gaps in tag edges."""
    gray = pipeline_clahe(frame_bgr)
    kernel = cv.getStructuringElement(cv.MORPH_RECT, (3, 3))
    return cv.morphologyEx(gray, cv.MORPH_CLOSE, kernel)


def pipeline_downscale_detect(frame_bgr: np.ndarray) -> np.ndarray:
    """Downscale to reduce noise, then upscale back.
    Sometimes helps by smoothing out noise while preserving tag structure."""
    gray = cv.cvtColor(frame_bgr, cv.COLOR_BGR2GRAY)
    h, w = gray.shape
    small = cv.resize(gray, (w // 2, h // 2), interpolation=cv.INTER_AREA)
    back = cv.resize(small, (w, h), interpolation=cv.INTER_LINEAR)
    clahe = cv.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    return clahe.apply(back)


def pipeline_combined_best(frame_bgr: np.ndarray) -> np.ndarray:
    """Combined pipeline: highpass → CLAHE → unsharp mask."""
    # Step 1: remove low-frequency illumination gradient
    gray = cv.cvtColor(frame_bgr, cv.COLOR_BGR2GRAY)
    blur = cv.GaussianBlur(gray, (51, 51), 0)
    hp = cv.subtract(gray, blur)
    hp = cv.add(hp, 128)
    # Step 2: CLAHE for local contrast
    clahe = cv.createCLAHE(clipLimit=4.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(hp)
    # Step 3: light sharpen
    blur2 = cv.GaussianBlur(enhanced, (0, 0), 2.0)
    sharpened = cv.addWeighted(enhanced, 1.3, blur2, -0.3, 0)
    return sharpened


# ── Detector parameter sets ───────────────────────────────────────────

DETECTOR_PARAMS = {
    "default": {},
    "relaxed": {
        "adaptiveThreshWinSizeMin": 3,
        "adaptiveThreshWinSizeMax": 53,
        "adaptiveThreshWinSizeStep": 4,
        "adaptiveThreshConstant": 7,
        "minMarkerPerimeterRate": 0.01,
        "maxMarkerPerimeterRate": 4.0,
        "polygonalApproxAccuracyRate": 0.05,
        "minCornerDistanceRate": 0.01,
        "minDistanceToBorder": 1,
        "minMarkerDistanceRate": 0.01,
    },
    "very_relaxed": {
        "adaptiveThreshWinSizeMin": 3,
        "adaptiveThreshWinSizeMax": 73,
        "adaptiveThreshWinSizeStep": 4,
        "adaptiveThreshConstant": 5,
        "minMarkerPerimeterRate": 0.005,
        "maxMarkerPerimeterRate": 4.0,
        "polygonalApproxAccuracyRate": 0.08,
        "minCornerDistanceRate": 0.005,
        "minDistanceToBorder": 1,
        "minMarkerDistanceRate": 0.005,
        "cornerRefinementMethod": cv.aruco.CORNER_REFINE_SUBPIX,
    },
}


# ── All pipelines ─────────────────────────────────────────────────────

PIPELINES = {
    "raw": pipeline_raw,
    "clahe": pipeline_clahe,
    "clahe_strong": pipeline_clahe_strong,
    "adaptive_thresh": pipeline_adaptive_thresh,
    "highpass": pipeline_highpass,
    "highpass+clahe": pipeline_highpass_clahe,
    "normalize": pipeline_normalize,
    "bilateral+clahe": pipeline_bilateral,
    "unsharp_mask": pipeline_unsharp_mask,
    "green_channel": pipeline_green_channel,
    "lab_l_channel": pipeline_lab_l_channel,
    "morphological": pipeline_morphological,
    "downscale": pipeline_downscale_detect,
    "combined_best": pipeline_combined_best,
}


# ── Main ──────────────────────────────────────────────────────────────

def get_frame(args) -> np.ndarray:
    """Get a frame from camera or file."""
    if args.camera is not None:
        cap = cv.VideoCapture(args.camera)
        if not cap.isOpened():
            print(f"Failed to open camera {args.camera}")
            sys.exit(1)
        # Warm up
        for _ in range(10):
            cap.read()
        ret, frame = cap.read()
        cap.release()
        if not ret:
            print("Failed to capture frame")
            sys.exit(1)
        return frame
    else:
        path = args.image or str(Path(__file__).parent / "sample_frame.jpg")
        frame = cv.imread(path)
        if frame is None:
            print(f"Failed to load image: {path}")
            sys.exit(1)
        return frame


def run_all(frame: np.ndarray, save_dir: Path | None = None):
    """Run all pipeline × detector_param combinations and report results."""
    print(f"Frame size: {frame.shape[1]}x{frame.shape[0]}")
    print(f"Expected: ArUco {sorted(EXPECTED_ARUCO_IDS)}, "
          f"AprilTag {sorted(EXPECTED_APRILTAG_IDS)}")
    print(f"Total expected: {len(EXPECTED_ARUCO_IDS) + len(EXPECTED_APRILTAG_IDS)} tags\n")

    results = []

    for pipe_name, pipe_fn in PIPELINES.items():
        for param_name, params in DETECTOR_PARAMS.items():
            t0 = time.monotonic()
            try:
                processed = pipe_fn(frame)
            except Exception as e:
                print(f"  {pipe_name} + {param_name}: PIPELINE ERROR: {e}")
                continue

            aruco_ids = detect_aruco(processed, **params)
            april_ids = detect_apriltags(processed, **params)
            dt = (time.monotonic() - t0) * 1000

            score = score_detection(aruco_ids, april_ids)
            total_found = len(score["aruco_found"]) + len(score["april_found"])
            total_expected = len(EXPECTED_ARUCO_IDS) + len(EXPECTED_APRILTAG_IDS)

            results.append({
                "pipeline": pipe_name,
                "params": param_name,
                "score": score,
                "total_found": total_found,
                "total_expected": total_expected,
                "false_positives": score["false_positives"],
                "time_ms": dt,
            })

            # Save processed image if requested
            if save_dir is not None:
                fname = f"{pipe_name}_{param_name}.jpg"
                cv.imwrite(str(save_dir / fname), processed)

    # Sort by score descending, then by false positives ascending
    results.sort(key=lambda r: (-r["total_found"], r["false_positives"], r["time_ms"]))

    # Print results table
    print(f"{'Pipeline':<20s} {'Params':<14s} {'Score':<8s} {'FP':<4s} "
          f"{'Time':<8s} {'Missing'}")
    print("─" * 90)
    for r in results:
        s = r["score"]
        missing = s["aruco_missing"] + s["april_missing"]
        missing_str = str(missing) if missing else "—"
        fp_str = str(r["false_positives"]) if r["false_positives"] else "—"
        print(f"{r['pipeline']:<20s} {r['params']:<14s} "
              f"{r['score']['score']:<8s} {fp_str:<4s} "
              f"{r['time_ms']:>5.0f}ms  {missing_str}")

    # Summary
    print()
    best = results[0] if results else None
    if best:
        print(f"Best: {best['pipeline']} + {best['params']} → "
              f"{best['score']['score']} ({best['false_positives']} FP)")
        if best["score"]["aruco_missing"]:
            print(f"  Missing ArUco: {best['score']['aruco_missing']}")
        if best["score"]["april_missing"]:
            print(f"  Missing AprilTag: {best['score']['april_missing']}")


def main():
    parser = argparse.ArgumentParser(description="Test tag detection pipelines")
    parser.add_argument("--camera", type=int, default=None,
                        help="Camera index to capture from")
    parser.add_argument("--image", type=str, default=None,
                        help="Image file to process")
    parser.add_argument("--save", type=str, default=None,
                        help="Directory to save processed images")
    args = parser.parse_args()

    frame = get_frame(args)

    save_dir = None
    if args.save:
        save_dir = Path(args.save)
        save_dir.mkdir(parents=True, exist_ok=True)

    run_all(frame, save_dir=save_dir)


if __name__ == "__main__":
    main()
