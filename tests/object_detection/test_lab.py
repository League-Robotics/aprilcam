"""Test LABColorClassifier: calibrate from live camera, detect 8 boxes.

Usage (from project root):
    python tests/object_detection/test_lab.py
"""
import sys
import pathlib
import pytest

pytest.importorskip("cv2", reason="requires aprilcam[imaging]")

import cv2 as cv
import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent.parent / "src"))

from aprilcam.client.control import DaemonControl
from aprilcam.config import Config
from aprilcam.vision.color_classifier import LABColorClassifier

CAMERA_INDEX = 4
N_CALIB_FRAMES = 20
N_TEST_FRAMES = 3
INSET = 120  # polygon filter inset px — excludes corner ArUco markers + robot

# Known box centers in raw camera frame (px)
BOX_POSITIONS = {
    "purple":  (305, 200),
    "black":   (558, 200),  # measured L=37 at this position
    "orange":  (815, 200),
    "yellow":  (808, 385),
    "green":   (810, 550),
    "magenta": (558, 550),
    "blue":    (295, 550),
    "red":     (295, 375),
}

# Table surface sample points — between the color boxes, away from any tag.
# Used to build a background exclusion model so wood grain is suppressed.
BACKGROUND_POSITIONS = [
    (430, 200),   # between purple and black (top row)
    (686, 200),   # between black and orange (top row)
    (550, 375),   # center of playfield
    (430, 550),   # between blue and magenta (bottom row)
    (686, 550),   # between magenta and green (bottom row)
    (305, 460),   # between blue and red (left column)
    (810, 290),   # between orange and yellow (right column)
    (810, 468),   # between yellow and green (right column)
    (200, 200),   # far left top corner area
    (200, 550),   # far left bottom corner area
    (900, 200),   # far right top corner area
    (900, 550),   # far right bottom corner area
    (550, 130),   # top middle
    (550, 620),   # bottom middle
]

COLOR_BGR = {
    "black":   (80,  80,  80),
    "red":     (0,   0,  220),
    "orange":  (0,  128, 255),
    "yellow":  (0,  220, 220),
    "green":   (0,  200,   0),
    "blue":    (220, 100,  0),
    "purple":  (180,  0,  180),
    "magenta": (200,  0,  200),
}


def make_inset_poly(frame):
    h, w = frame.shape[:2]
    return np.array(
        [[INSET, INSET], [w - INSET, INSET],
         [w - INSET, h - INSET], [INSET, h - INSET]],
        dtype=np.float32,
    ).reshape(-1, 1, 2)


def filter_objs(objs, frame):
    poly = make_inset_poly(frame)
    out = []
    for obj in objs:
        cx, cy = obj.center_px
        if cv.pointPolygonTest(poly, (float(cx), float(cy)), False) < 0:
            continue
        x, y, bw, bh = obj.bbox
        aspect = max(bw, bh) / max(min(bw, bh), 1)
        if aspect > 1.6 or min(bw, bh) < 12 or max(bw, bh) > 70:
            continue
        out.append(obj)
    return out


def main():
    config = Config.load()
    client = DaemonControl.connect_default(config)

    print(f"Opening camera {CAMERA_INDEX}...")
    cam_name = client.open_camera(CAMERA_INDEX)
    print(f"  → {cam_name}")

    # --- Calibration ---
    print(f"\nCapturing {N_CALIB_FRAMES} calibration frames...")
    calib_frames = []
    for _ in range(N_CALIB_FRAMES):
        f = client.capture_frame(cam_name)
        if f is not None:
            calib_frames.append(f)
    print(f"  Got {len(calib_frames)} frames")

    clf = LABColorClassifier(min_area=350, max_area=4000, mahal_threshold=4.0)
    clf.calibrate(
        calib_frames, BOX_POSITIONS, roi_radius=8,
        background_positions=BACKGROUND_POSITIONS, background_roi_radius=20,
    )

    # Print learned LAB centers
    print("\nLearned LAB color centers:")
    print(f"  {'Color':12s}  {'L':>6s} {'a':>6s} {'b':>6s}")
    print("  " + "-" * 36)
    for color, (mean, _) in clf.color_models.items():
        tag = " (background)" if color == "__background__" else ""
        print(f"  {color:12s}  {mean[0]:6.1f} {mean[1]:6.1f} {mean[2]:6.1f}{tag}")

    # Diagnostic: Mahalanobis distance at each known position
    diag_frame = calib_frames[0]
    lab_d = cv.cvtColor(diag_frame, cv.COLOR_BGR2LAB).astype(np.float64)
    print("\nMahalanobis distances at known box positions (winner shown with *):")
    for tgt_color, (cx, cy) in BOX_POSITIONS.items():
        roi = lab_d[cy-4:cy+4, cx-4:cx+4].reshape(-1, 3).mean(axis=0)
        dists = {}
        for color, (mean, cov_inv) in clf.color_models.items():
            diff = roi - mean
            d = float(np.sqrt(max(0.0, diff @ cov_inv @ diff)))
            dists[color] = d
        winner = min(dists, key=dists.get)
        dist_str = "  ".join(
            f"{'*' if c==winner else ' '}{c[:4]}={dists[c]:.1f}" for c in dists
        )
        print(f"  {tgt_color:10s}  {dist_str}")

    # --- Detection ---
    print(f"\nDetecting on {N_TEST_FRAMES} test frames...")
    for i in range(N_TEST_FRAMES):
        frame = client.capture_frame(cam_name)
        if frame is None:
            print(f"  Frame {i+1}: None")
            continue

        raw = clf.classify(frame)
        objs = filter_objs(raw, frame)

        print(f"\n=== Frame {i+1} === raw={len(raw)} filtered={len(objs)}")
        for obj in objs:
            cx, cy = obj.center_px
            x, y, bw, bh = obj.bbox
            print(f"  {obj.color:10s}  px=({cx:.0f},{cy:.0f})  {bw}x{bh}  area={obj.area_px:.0f}")

        # Annotate
        vis = frame.copy()
        # Draw calibration sample points
        for color, (px, py) in BOX_POSITIONS.items():
            cv.circle(vis, (px, py), 6, (0, 255, 0), 1)
        # Draw detections
        for obj in objs:
            x, y, bw, bh = obj.bbox
            bgr = COLOR_BGR.get(obj.color, (200, 200, 200))
            cv.rectangle(vis, (x, y), (x + bw, y + bh), bgr, 2)
            cv.putText(vis, obj.color, (x, max(y - 4, 12)),
                       cv.FONT_HERSHEY_SIMPLEX, 0.45, bgr, 1, cv.LINE_AA)
        out = f"/tmp/lab_frame{i+1}.jpg"
        cv.imwrite(out, vis)
        print(f"  → {out}")

    import subprocess
    subprocess.run(["open", "/tmp/lab_frame3.jpg"])


if __name__ == "__main__":
    main()
