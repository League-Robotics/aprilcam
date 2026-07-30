"""Diagnostic script — simulates the updated _handle_get_objects logic.

Usage:  uv run python test_detect.py
"""
import sys
import cv2 as cv
import numpy as np

sys.path.insert(0, "src")

from aprilcam.client.control import DaemonControl
from aprilcam.config import Config
from aprilcam.vision.color_classifier import ColorClassifier

CAMERA_INDEX = 4
# Playfield corners from create_playfield (captured earlier this session)
PF_CORNERS = np.array([[68, 46], [1044, 53], [1047, 699], [58, 705]], dtype=np.float32)
BORDER_MARGIN = 60


def make_shrunk_poly(corners, margin):
    center = corners.mean(axis=0)
    dirs = corners - center
    lens = np.linalg.norm(dirs, axis=1, keepdims=True)
    lens = np.maximum(lens, 1e-6)
    return (corners - dirs / lens * margin).reshape(-1, 1, 2).astype(np.float32)


def detect_objects(frame, shrunk_poly, homography=None):
    classifier = ColorClassifier(min_area=600, max_area=30000)
    raw = classifier.classify(frame, homography=homography)
    results = []
    for obj in raw:
        cx, cy = obj.center_px
        if cv.pointPolygonTest(shrunk_poly, (float(cx), float(cy)), False) < 0:
            continue
        x, y, bw, bh = obj.bbox
        aspect = max(bw, bh) / max(min(bw, bh), 1)
        if aspect > 2.0 or min(bw, bh) < 15:
            continue
        results.append(obj)
    return raw, results


def main():
    config = Config.load()
    print("Connecting to daemon...")
    client = DaemonControl.connect_default(config)

    print(f"Opening camera {CAMERA_INDEX}...")
    cam_name = client.open_camera(CAMERA_INDEX)
    print(f"  → {cam_name}")

    shrunk_poly = make_shrunk_poly(PF_CORNERS, BORDER_MARGIN)

    for i in range(3):
        print(f"\n=== Frame {i+1} ===")
        frame = client.capture_frame(cam_name)
        if frame is None:
            print("  ERROR: got None frame")
            continue
        h, w = frame.shape[:2]
        print(f"  shape={w}x{h}")

        raw, filtered = detect_objects(frame, shrunk_poly)
        print(f"  Raw: {len(raw)}  Filtered: {len(filtered)}")
        for obj in filtered:
            cx, cy = obj.center_px
            x, y, bw, bh = obj.bbox
            wxy = f" world={obj.world_xy[0]:.1f},{obj.world_xy[1]:.1f}cm" if obj.world_xy else ""
            print(f"    {obj.color:10s}  px=({cx:.0f},{cy:.0f})  {bw}x{bh}  area={obj.area_px:.0f}{wxy}")

        # Annotated image
        vis = frame.copy()
        for obj in filtered:
            cx, cy = int(obj.center_px[0]), int(obj.center_px[1])
            x, y, bw, bh = obj.bbox
            cv.rectangle(vis, (x, y), (x+bw, y+bh), (0, 255, 0), 2)
            cv.putText(vis, obj.color, (x, y-4), cv.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
        out = f"/tmp/detect_frame{i+1}.jpg"
        cv.imwrite(out, vis)
        print(f"  → {out}")

    import subprocess
    subprocess.run(["open", "/tmp/detect_frame3.jpg"])


if __name__ == "__main__":
    main()
