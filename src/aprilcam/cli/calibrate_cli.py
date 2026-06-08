"""CLI for running playfield calibration.

Usage:
    aprilcam calibrate                     # re-calibrate all cameras in calibration.json
    aprilcam calibrate 1 3                 # calibrate camera #1 and #3 (the numbers shown by `aprilcam cameras`)
    aprilcam calibrate "Global Shutter"    # calibrate by name pattern
    aprilcam calibrate --width 101 --height 89   # override field dimensions
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import List, Optional

import time

import cv2 as cv
import grpc
import numpy as np

from ..camera.camutil import list_cameras, select_camera_by_pattern
from ..config import Config
from ..client.control import DaemonControl


def _warmup_capture(dc: DaemonControl, cam_name: str, count: int = 10, timeout: float = 5.0) -> None:
    """Capture ``count`` frames, retrying while the daemon has no frame yet."""
    deadline = time.monotonic() + timeout
    captured = 0
    while captured < count:
        try:
            dc.capture_frame(cam_name)
            captured += 1
        except grpc.RpcError as e:
            if (
                e.code() == grpc.StatusCode.UNAVAILABLE
                and "no frame captured yet" in (e.details() or "")
                and time.monotonic() < deadline
            ):
                time.sleep(0.05)
                continue
            raise


class _DaemonCapture:
    """Thin VideoCapture adapter that routes frame reads through the daemon.

    Exposes just enough of the cv.VideoCapture interface that
    :func:`~aprilcam.calibration.homography.detect_all_tags` and
    :func:`~aprilcam.calibration.calibration.calibrate_single` can use it
    without modification.
    """

    def __init__(self, dc: DaemonControl, cam_name: str) -> None:
        self._dc = dc
        self._cam_name = cam_name
        self._width: Optional[int] = None
        self._height: Optional[int] = None

    def _fetch_frame(self) -> Optional[np.ndarray]:
        """Fetch one JPEG frame from the daemon and decode it to BGR."""
        frame = self._dc.capture_frame(self._cam_name)
        if frame is not None and self._width is None:
            self._height, self._width = frame.shape[:2]
        return frame

    def read(self):
        """Mimic cv.VideoCapture.read() → (ret, frame)."""
        frame = self._fetch_frame()
        if frame is None:
            return False, None
        return True, frame

    def get(self, prop_id: int) -> float:
        """Mimic cv.VideoCapture.get() for width/height props."""
        if prop_id == cv.CAP_PROP_FRAME_WIDTH:
            if self._width is None:
                self._fetch_frame()
            return float(self._width or 0)
        if prop_id == cv.CAP_PROP_FRAME_HEIGHT:
            if self._height is None:
                self._fetch_frame()
            return float(self._height or 0)
        return 0.0

    def isOpened(self) -> bool:
        return True

    def release(self) -> None:
        pass


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="aprilcam calibrate",
        description="Run playfield calibration for one or more cameras.",
    )
    parser.add_argument(
        "cameras",
        nargs="*",
        help="Camera numbers (the # shown by `aprilcam cameras`) or name "
        "patterns to calibrate. If omitted, re-calibrates all cameras in "
        "calibration.json.",
    )
    parser.add_argument(
        "--width",
        type=float,
        default=None,
        help="Playfield width in cm between ArUco corners (default: from calibration.json or 101.0)",
    )
    parser.add_argument(
        "--height",
        type=float,
        default=None,
        help="Playfield height in cm between ArUco corners (default: from calibration.json or 89.0)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output path for calibration.json (default: from daemon config)",
    )
    parser.add_argument(
        "--frames",
        type=int,
        default=30,
        help="Number of frames to accumulate for tag detection (default: 30)",
    )
    parser.add_argument(
        "--joint",
        action="store_true",
        help="With exactly 2 cameras: calibrate the first as primary (ArUco corners), "
             "then calibrate the second using the first camera's homography as reference.",
    )
    args = parser.parse_args(argv)

    # Start (or connect to) the daemon
    config = Config.load()
    dc = DaemonControl.connect_default(config)

    # cameras_dir is <data_dir>/cameras — each camera has its own subdir
    if args.output:
        cameras_dir = Path(args.output)
    else:
        cameras_dir = config.data_dir / "cameras"

    # Load field dimension defaults from any existing calibration.json in a subdir
    field_width = args.width
    field_height = args.height
    if (field_width is None or field_height is None) and cameras_dir.is_dir():
        for cal_file in cameras_dir.glob("*/calibration.json"):
            try:
                d = json.loads(cal_file.read_text())
                field_width = field_width or d.get("field_width_cm")
                field_height = field_height or d.get("field_height_cm")
                if field_width and field_height:
                    break
            except Exception:
                pass
    field_width = field_width or 101.0
    field_height = field_height or 89.0

    # Resolve which cameras to calibrate
    camera_indices: list[tuple[int, str]] = []  # (index, label)
    available = list_cameras()

    if args.cameras:
        from ..camera.identity import resolve_all
        from ..camera.registry import (
            CameraRegistry,
            CameraSelectError,
            resolve_enum_to_index,
        )

        registry = CameraRegistry(cameras_dir)
        live_identities = resolve_all()

        for spec in args.cameras:
            if spec.lstrip("-").isdigit():
                # A numeric spec is the stable enumeration number shown by
                # `aprilcam cameras` — resolve it to the live OS index. (The
                # old volatile-OS-index warning no longer applies: the
                # enumeration number is stable across plug/unplug.)
                try:
                    idx = resolve_enum_to_index(
                        int(spec), registry, live_identities
                    )
                except CameraSelectError as exc:
                    print(f"  {exc}, skipping.")
                    continue
                label = next(
                    (c.device_name or c.name for c in available if c.index == idx),
                    f"Camera {idx}",
                )
                camera_indices.append((idx, label))
                continue
            idx = select_camera_by_pattern(spec, available)
            if idx is not None:
                label = next((c.device_name or c.name for c in available if c.index == idx), f"Camera {idx}")
                camera_indices.append((idx, label))
            else:
                print(f"  No camera matching '{spec}', skipping.")
    else:
        # Re-calibrate all cameras that already have a calibration.json subdir
        cal_subdirs = list(cameras_dir.glob("*/calibration.json")) if cameras_dir.is_dir() else []
        if not cal_subdirs:
            print(f"No cameras specified and {cameras_dir} has no existing calibration files.")
            print("Specify cameras to calibrate: aprilcam calibrate 'Global Shutter' 'Arducam'")
            return 1
        for cal_file in sorted(cal_subdirs):
            try:
                device_name = json.loads(cal_file.read_text()).get("device_name", "")
            except Exception:
                continue
            if not device_name:
                continue
            idx = select_camera_by_pattern(device_name, available)
            if idx is not None:
                camera_indices.append((idx, device_name))
            else:
                print(f"  Camera '{device_name}' in {cal_file.parent.name}/ not found, skipping.")

    if not camera_indices:
        print("No cameras to calibrate.")
        return 1

    print(f"Playfield: {field_width} x {field_height} cm")
    print(f"Output: {cameras_dir}")
    print(f"Cameras to calibrate: {len(camera_indices)}")
    for idx, label in camera_indices:
        print(f"  [{idx}] {label}")
    print()

    from ..calibration.calibration import (
        calibrate_single,
        calibrate_secondary,
        save_calibration_to_camera_dir,
    )

    if args.joint:
        if len(camera_indices) != 2:
            print("--joint requires exactly 2 cameras.")
            return 1

        pri_idx, pri_label = camera_indices[0]
        sec_idx, sec_label = camera_indices[1]

        print(f"Joint calibration: [{pri_idx}] {pri_label} → primary, [{sec_idx}] {sec_label} → secondary")
        print()

        try:
            # Open both cameras
            pri_name, _ = dc.open_camera(pri_idx)
            sec_name, _ = dc.open_camera(sec_idx)

            # Warm up
            _warmup_capture(dc, pri_name)
            _warmup_capture(dc, sec_name)

            pri_cap = _DaemonCapture(dc, pri_name)
            sec_cap = _DaemonCapture(dc, sec_name)

            # Calibrate primary with ArUco corner assignment.
            # Distortion correction is skipped for the primary: cv.calibrateCamera
            # requires multiple views to reliably estimate distortion, and a single
            # planar capture produces degenerate coefficients that hurt accuracy.
            print(f"Calibrating primary [{pri_idx}] {pri_label} ...")
            pri_cal = calibrate_single(
                pri_cap,
                field_width_cm=field_width,
                field_height_cm=field_height,
                num_frames=args.frames,
                camera_index=pri_idx,
                correct_distortion=False,
            )
            pri_dir = cameras_dir / pri_name
            pri_file = save_calibration_to_camera_dir(pri_cal, pri_dir, field_width, field_height)
            print(f"  Saved: {pri_file}")
            print(f"  {pri_cal.device_name} {pri_cal.resolution}, {pri_cal.tags_used} tags, RMS {pri_cal.rms_error:.6f}")
            if pri_cal.dist_coeffs is not None:
                print(f"  Barrel distortion correction: yes")
            dc.reload_calibration(pri_name)
            print()

            # Calibrate secondary using primary homography
            print(f"Calibrating secondary [{sec_idx}] {sec_label} using primary homography ...")
            sec_cal = calibrate_secondary(
                secondary_cap=sec_cap,
                primary_cal=pri_cal,
                primary_cap=pri_cap,
                num_frames=args.frames,
                secondary_index=sec_idx,
            )
            sec_dir = cameras_dir / sec_name
            sec_file = save_calibration_to_camera_dir(sec_cal, sec_dir, field_width, field_height)
            print(f"  Saved: {sec_file}")
            print(f"  {sec_cal.device_name} {sec_cal.resolution}, {sec_cal.tags_used} tags, RMS {sec_cal.rms_error:.6f}")
            if sec_cal.dist_coeffs is not None:
                print(f"  Barrel distortion correction: yes")
            dc.reload_calibration(sec_name)
            print()

        except Exception as e:
            import traceback
            print(f"  ERROR: {e}")
            traceback.print_exc()
            return 1

    else:
        # Independent single-camera calibration for each camera
        for idx, label in camera_indices:
            print(f"Calibrating [{idx}] {label} ...")
            try:
                cam_name, _ = dc.open_camera(idx)

                _warmup_capture(dc, cam_name)

                cap = _DaemonCapture(dc, cam_name)

                cal = calibrate_single(
                    cap,
                    field_width_cm=field_width,
                    field_height_cm=field_height,
                    num_frames=args.frames,
                    camera_index=idx,
                )

                camera_dir = cameras_dir / cam_name
                cal_file = save_calibration_to_camera_dir(cal, camera_dir, field_width, field_height)

                print(f"Calibration saved to {cal_file}")
                print(f"  Camera: {cal.device_name} {cal.resolution}, {cal.tags_used} tags, RMS {cal.rms_error:.6f}")
                if cal.dist_coeffs is not None:
                    print(f"  Barrel distortion correction: yes")

                dc.reload_calibration(cam_name)

                print()
            except Exception as e:
                import traceback
                print(f"  ERROR: {e}")
                traceback.print_exc()
                print()

    print("Done.")
    return 0
