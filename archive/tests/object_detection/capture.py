"""Capture a burst of frames from camera 4 for object detection tuning.

Usage:
    cd tests/object_detection
    python capture.py
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent.parent / "src"))

import cv2 as cv

from aprilcam.client.control import DaemonControl
from aprilcam.config import Config

HERE = pathlib.Path(__file__).parent
CAMERA_INDEX = 4
N_FRAMES = 12


def main():
    config = Config.load()
    print("Connecting to daemon...")
    client = DaemonControl.connect_default(config)

    print(f"Opening camera {CAMERA_INDEX}...")
    cam_name = client.open_camera(CAMERA_INDEX)
    print(f"  → {cam_name}")

    print(f"Capturing {N_FRAMES} frames as fast as possible...")
    for i in range(N_FRAMES):
        frame = client.capture_frame(cam_name)
        if frame is None:
            print(f"  frame {i:02d}: ERROR (got None)")
            continue
        path = HERE / f"frame_{i:02d}.jpg"
        cv.imwrite(str(path), frame)
        h, w = frame.shape[:2]
        print(f"  frame_{i:02d}.jpg  {w}x{h}")

    print("Done.")


if __name__ == "__main__":
    main()
