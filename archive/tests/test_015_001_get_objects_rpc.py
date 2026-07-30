"""Tests for the GetObjects gRPC handler — ticket 015-001.

These tests use a fake camera pipeline with a synthetic BGR frame so no
real camera hardware is required.  They mirror the pattern from
test_grpc_servicer.py.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

pytest.importorskip("aprilcam.daemon.grpc_server", reason="requires aprilcam[daemon]")
pytest.importorskip("cv2", reason="requires OpenCV")

import cv2 as cv
import grpc

from aprilcam.daemon.grpc_server import AprilCamServicer
from aprilcam.proto import aprilcam_pb2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_servicer(cameras=None, *, tmp_path: Path) -> AprilCamServicer:
    """Build an AprilCamServicer with a pre-populated camera registry."""
    import tempfile

    from aprilcam.config import Config

    base = Path(tempfile.mkdtemp(prefix="go_test_", dir="/tmp"))
    sock_dir = base / "s"
    data_dir = base / "d"
    sock_dir.mkdir()
    data_dir.mkdir()

    config = Config(
        data_dir=data_dir,
        socket_dir=sock_dir,
        daemon_pidfile=sock_dir / "aprilcamd.pid",
    )

    registry: dict = cameras if cameras is not None else {}
    lock = threading.Lock()
    shutdown = threading.Event()
    return AprilCamServicer(
        cameras=registry,
        cam_lock=lock,
        config=config,
        shutdown_event=shutdown,
    )


def _mock_context() -> MagicMock:
    """Return a MagicMock that satisfies the grpc.ServicerContext interface."""
    return MagicMock(spec=grpc.ServicerContext)


def _make_frame_with_colored_rect(
    height: int = 480,
    width: int = 640,
    color_bgr: tuple = (0, 0, 200),   # red in BGR
    rect_top_left: tuple = (200, 150),
    rect_bottom_right: tuple = (260, 210),
) -> np.ndarray:
    """Return a synthetic BGR frame with a colored rectangle in it.

    The rectangle is large enough (60×60 px, area=3600) to pass the
    min_area=600 filter used by the GetObjects handler.
    """
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    cv.rectangle(frame, rect_top_left, rect_bottom_right, color_bgr, thickness=-1)
    return frame


class _FakePipeline:
    """Minimal stand-in for CameraPipeline used by GetObjects tests."""

    def __init__(
        self,
        frame: Optional[np.ndarray] = None,
        calibration=None,
        april_cam=None,
    ):
        self._latest_raw_frame = frame
        self._raw_lock = threading.Lock()
        self._calibration = calibration
        self._april_cam = april_cam


# ---------------------------------------------------------------------------
# Test 1: handler detects a colored object and returns non-empty results
# ---------------------------------------------------------------------------


def test_get_objects_returns_detected_object(tmp_path: Path) -> None:
    """GetObjects finds a red rectangle in a synthetic frame (uncalibrated path)."""
    frame = _make_frame_with_colored_rect()
    pipeline = _FakePipeline(frame=frame)
    servicer = _make_servicer(cameras={"cam-0": pipeline}, tmp_path=tmp_path)

    request = aprilcam_pb2.CameraRequest(cam_name="cam-0")
    response = servicer.GetObjects(request, _mock_context())

    assert response.cam_name == "cam-0"
    assert len(response.objects) >= 1, (
        "Expected at least one detected object in the synthetic frame"
    )
    obj = response.objects[0]
    # Color should be 'red' (BGR (0,0,200) maps to red in HSV)
    assert obj.color == "red"
    # Pixel centre should be inside the rectangle region
    assert 200 <= obj.cx_px <= 260
    assert 150 <= obj.cy_px <= 210
    # Uncalibrated: world coords are 0
    assert obj.wx == pytest.approx(0.0)
    assert obj.wy == pytest.approx(0.0)
    # BBox fields populated
    assert obj.w_bbox > 0
    assert obj.h_bbox > 0
    assert obj.area_px > 0.0


# ---------------------------------------------------------------------------
# Test 2: unknown cam_name returns NOT_FOUND
# ---------------------------------------------------------------------------


def test_get_objects_unknown_camera_returns_not_found(tmp_path: Path) -> None:
    """GetObjects sets NOT_FOUND status when the camera is not open."""
    servicer = _make_servicer(tmp_path=tmp_path)
    ctx = _mock_context()

    request = aprilcam_pb2.CameraRequest(cam_name="ghost-cam")
    servicer.GetObjects(request, ctx)

    ctx.set_code.assert_called_once_with(grpc.StatusCode.NOT_FOUND)


# ---------------------------------------------------------------------------
# Test 3: last_frame=None returns empty objects list (no error)
# ---------------------------------------------------------------------------


def test_get_objects_no_frame_returns_empty(tmp_path: Path) -> None:
    """GetObjects returns an empty objects list when no frame is available yet."""
    pipeline = _FakePipeline(frame=None)
    servicer = _make_servicer(cameras={"cam-0": pipeline}, tmp_path=tmp_path)

    request = aprilcam_pb2.CameraRequest(cam_name="cam-0")
    response = servicer.GetObjects(request, _mock_context())

    assert response.cam_name == "cam-0"
    assert len(response.objects) == 0


# ---------------------------------------------------------------------------
# Test 4: polygon filter excludes objects outside the playfield
# ---------------------------------------------------------------------------


def test_get_objects_uses_a1_origin_not_field_centre(tmp_path: Path) -> None:
    """Object world_xy is re-centred on the A1 origin (``pipeline._a1_origin()``),
    matching get_tags — NOT on the field centre (fw/2, fh/2).

    Regression for the double-shift bug: the stored homography is already
    A1-centred, so subtracting fw/2,fh/2 pushed a centre object to (-fw/2,-fh/2).
    """
    from aprilcam.calibration.calibration import CameraCalibration

    # Identity homography → raw world == pixel coords. Rect centre ≈ (230, 180).
    frame = _make_frame_with_colored_rect(
        color_bgr=(0, 0, 200),
        rect_top_left=(200, 150),
        rect_bottom_right=(260, 210),
    )
    cal = CameraCalibration(device_name="c", resolution=(640, 480), homography=np.eye(3))
    cal.playfield_width_cm = 400.0    # fw/2 = 200  (the WRONG, pre-fix origin)
    cal.playfield_height_cm = 300.0   # fh/2 = 150
    cal.static_markers = {"apriltag:1": {"pixel": [30.0, 80.0], "world": [30.0, 80.0]}}

    class _CalPipeline(_FakePipeline):
        def _a1_origin(self):  # mirrors CameraPipeline._a1_origin
            m = self._calibration.static_markers["apriltag:1"]
            return (float(m["world"][0]), float(m["world"][1]))

    # april_cam=None → no polygon filter, so the object is not filtered out.
    pipeline = _CalPipeline(frame=frame, calibration=cal)
    servicer = _make_servicer(cameras={"cam-0": pipeline}, tmp_path=tmp_path)

    response = servicer.GetObjects(
        aprilcam_pb2.CameraRequest(cam_name="cam-0"), _mock_context()
    )
    assert len(response.objects) >= 1
    obj = response.objects[0]
    # Re-centred on the A1 origin (30, 80), NOT the field centre (200, 150).
    assert obj.wx == pytest.approx(obj.cx_px - 30.0, abs=1.5)
    assert obj.wy == pytest.approx(obj.cy_px - 80.0, abs=1.5)
    # Guard against a regression to the field-centre origin.
    assert obj.wx != pytest.approx(obj.cx_px - 200.0, abs=1.5)


def test_get_objects_polygon_filter_excludes_outside(tmp_path: Path) -> None:
    """Objects outside the (inset) playfield polygon are filtered out."""
    # Rectangle is painted in the top-left corner (40, 20) → (100, 80).
    # The fake playfield polygon covers only the central region (200,150)-(440,330).
    # After 60 px inset the polygon is even smaller.  The rectangle should be excluded.
    frame = _make_frame_with_colored_rect(
        color_bgr=(0, 0, 200),
        rect_top_left=(40, 20),
        rect_bottom_right=(100, 80),
    )

    # Fake playfield polygon: central 240×180 area
    poly = np.array(
        [[200, 150], [440, 150], [440, 330], [200, 330]], dtype=np.float32
    )

    fake_april_cam = MagicMock()
    fake_april_cam.playfield.get_polygon.return_value = poly

    pipeline = _FakePipeline(frame=frame, april_cam=fake_april_cam)
    servicer = _make_servicer(cameras={"cam-0": pipeline}, tmp_path=tmp_path)

    request = aprilcam_pb2.CameraRequest(cam_name="cam-0")
    response = servicer.GetObjects(request, _mock_context())

    assert response.cam_name == "cam-0"
    assert len(response.objects) == 0, (
        "Object outside the playfield polygon should be filtered out"
    )
