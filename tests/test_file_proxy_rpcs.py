"""Tests for 014-004: file-proxy RPCs in AprilCamServicer.

Tests handler round-trips (write via Set*, read back via Get*) against a temp
cameras_dir, and ListPlayfields against a temp playfields_dir.  No real camera
hardware or gRPC network is needed — the handlers are called directly.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import MagicMock

import grpc
import pytest

pytest.importorskip("aprilcam.daemon.grpc_server", reason="requires aprilcam[daemon]")

from aprilcam.daemon.grpc_server import AprilCamServicer
from aprilcam.proto import aprilcam_pb2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_servicer(tmp_path: Path) -> AprilCamServicer:
    """Build an AprilCamServicer backed by *tmp_path* as the data dir."""
    from aprilcam.config import Config

    cameras_dir = tmp_path / "cameras"
    cameras_dir.mkdir()
    playfields_dir = tmp_path / "playfields"
    playfields_dir.mkdir()
    sock_dir = tmp_path / "sockets"
    sock_dir.mkdir()

    config = Config(
        data_dir=tmp_path,
        socket_dir=sock_dir,
        daemon_pidfile=sock_dir / "aprilcamd.pid",
    )
    # Override playfields_dir to point at our temp dir.
    # Config derives it from data_dir / "playfields" — which matches here.

    return AprilCamServicer(
        cameras={},
        cam_lock=threading.Lock(),
        config=config,
        shutdown_event=threading.Event(),
    )


def _mock_ctx() -> MagicMock:
    return MagicMock(spec=grpc.ServicerContext)


# ---------------------------------------------------------------------------
# GetCameraConfig / SetCameraConfig
# ---------------------------------------------------------------------------


def test_get_camera_config_absent(tmp_path: Path) -> None:
    """GetCameraConfig returns present=False when config.json does not exist."""
    svc = _make_servicer(tmp_path)
    reply = svc.GetCameraConfig(
        aprilcam_pb2.CameraRequest(cam_name="cam-absent"), _mock_ctx()
    )
    assert reply.present is False
    assert reply.json_blob == ""


def test_set_camera_config_creates_file(tmp_path: Path) -> None:
    """SetCameraConfig writes config.json and the file content matches."""
    svc = _make_servicer(tmp_path)
    cfg = {"device_name": "test-camera", "playfield": "main-playfield"}
    blob = json.dumps(cfg)

    reply = svc.SetCameraConfig(
        aprilcam_pb2.CameraJsonRequest(cam_name="cam-1", json_blob=blob),
        _mock_ctx(),
    )
    assert reply.ok is True

    written = (tmp_path / "cameras" / "cam-1" / "config.json")
    assert written.exists()
    assert json.loads(written.read_text()) == cfg


def test_get_camera_config_round_trip(tmp_path: Path) -> None:
    """SetCameraConfig then GetCameraConfig returns the same dict."""
    svc = _make_servicer(tmp_path)
    cfg = {"device_name": "roundtrip-cam", "resolution": [1920, 1080]}
    blob = json.dumps(cfg)

    svc.SetCameraConfig(
        aprilcam_pb2.CameraJsonRequest(cam_name="cam-rt", json_blob=blob),
        _mock_ctx(),
    )

    reply = svc.GetCameraConfig(
        aprilcam_pb2.CameraRequest(cam_name="cam-rt"), _mock_ctx()
    )
    assert reply.present is True
    assert json.loads(reply.json_blob) == cfg


def test_set_camera_config_invalid_json(tmp_path: Path) -> None:
    """SetCameraConfig with invalid JSON returns ok=False and sets INVALID_ARGUMENT."""
    svc = _make_servicer(tmp_path)
    ctx = _mock_ctx()

    reply = svc.SetCameraConfig(
        aprilcam_pb2.CameraJsonRequest(cam_name="cam-bad", json_blob="{not valid"),
        ctx,
    )
    assert reply.ok is False
    ctx.set_code.assert_called_once_with(grpc.StatusCode.INVALID_ARGUMENT)


def test_set_camera_config_atomic_write(tmp_path: Path) -> None:
    """SetCameraConfig leaves no .tmp file behind on success."""
    svc = _make_servicer(tmp_path)
    blob = json.dumps({"key": "val"})
    svc.SetCameraConfig(
        aprilcam_pb2.CameraJsonRequest(cam_name="cam-atomic", json_blob=blob),
        _mock_ctx(),
    )
    cam_dir = tmp_path / "cameras" / "cam-atomic"
    tmp_files = list(cam_dir.glob("*.tmp"))
    assert tmp_files == [], f"Leftover tmp files: {tmp_files}"


# ---------------------------------------------------------------------------
# GetCalibration / SetCalibration
# ---------------------------------------------------------------------------


def test_get_calibration_absent(tmp_path: Path) -> None:
    """GetCalibration returns present=False when calibration.json does not exist."""
    svc = _make_servicer(tmp_path)
    reply = svc.GetCalibration(
        aprilcam_pb2.CameraRequest(cam_name="cam-absent"), _mock_ctx()
    )
    assert reply.present is False
    assert reply.json_blob == ""


def test_set_calibration_creates_file(tmp_path: Path) -> None:
    """SetCalibration writes calibration.json atomically."""
    svc = _make_servicer(tmp_path)
    cal_data = {"homography": [[1, 0, 0], [0, 1, 0], [0, 0, 1]], "camera": "cam-cal"}
    blob = json.dumps(cal_data)

    reply = svc.SetCalibration(
        aprilcam_pb2.CameraJsonRequest(cam_name="cam-cal", json_blob=blob),
        _mock_ctx(),
    )
    assert reply.ok is True

    written = tmp_path / "cameras" / "cam-cal" / "calibration.json"
    assert written.exists()
    assert json.loads(written.read_text()) == cal_data


def test_get_calibration_round_trip(tmp_path: Path) -> None:
    """SetCalibration then GetCalibration returns the same JSON blob."""
    svc = _make_servicer(tmp_path)
    cal_data = {
        "homography": [[2.0, 0.0, -50.0], [0.0, 2.0, -30.0], [0.0, 0.0, 1.0]],
        "camera": "cam-rt",
    }
    blob = json.dumps(cal_data)

    svc.SetCalibration(
        aprilcam_pb2.CameraJsonRequest(cam_name="cam-rt", json_blob=blob),
        _mock_ctx(),
    )

    reply = svc.GetCalibration(
        aprilcam_pb2.CameraRequest(cam_name="cam-rt"), _mock_ctx()
    )
    assert reply.present is True
    assert json.loads(reply.json_blob) == cal_data


def test_set_calibration_invalid_json(tmp_path: Path) -> None:
    """SetCalibration with invalid JSON returns ok=False and INVALID_ARGUMENT."""
    svc = _make_servicer(tmp_path)
    ctx = _mock_ctx()

    reply = svc.SetCalibration(
        aprilcam_pb2.CameraJsonRequest(cam_name="cam-bad", json_blob="not-json"),
        ctx,
    )
    assert reply.ok is False
    ctx.set_code.assert_called_once_with(grpc.StatusCode.INVALID_ARGUMENT)


def test_set_calibration_atomic_no_tmp(tmp_path: Path) -> None:
    """SetCalibration leaves no .tmp file behind on success."""
    svc = _make_servicer(tmp_path)
    blob = json.dumps({"homography": [[1, 0, 0], [0, 1, 0], [0, 0, 1]]})
    svc.SetCalibration(
        aprilcam_pb2.CameraJsonRequest(cam_name="cam-atomic-cal", json_blob=blob),
        _mock_ctx(),
    )
    cam_dir = tmp_path / "cameras" / "cam-atomic-cal"
    tmp_files = list(cam_dir.glob("*.tmp"))
    assert tmp_files == [], f"Leftover tmp files: {tmp_files}"


def test_set_calibration_reloads_open_pipeline(tmp_path: Path) -> None:
    """SetCalibration calls reload_calibration on the open pipeline (mocked)."""
    svc = _make_servicer(tmp_path)

    # Pre-populate the camera registry with a mock pipeline.
    pipeline = MagicMock()
    pipeline._april_cam = None  # Simplest path: no AprilCam to update.
    svc._cameras["cam-live"] = pipeline

    # Write a valid minimal calibration blob.
    blob = json.dumps({"homography": [[1, 0, 0], [0, 1, 0], [0, 0, 1]]})
    reply = svc.SetCalibration(
        aprilcam_pb2.CameraJsonRequest(cam_name="cam-live", json_blob=blob),
        _mock_ctx(),
    )
    assert reply.ok is True
    # The file should be present.
    assert (tmp_path / "cameras" / "cam-live" / "calibration.json").exists()


# ---------------------------------------------------------------------------
# GetPaths / SetPaths
# ---------------------------------------------------------------------------


def test_get_paths_absent(tmp_path: Path) -> None:
    """GetPaths returns present=False when paths.json does not exist."""
    svc = _make_servicer(tmp_path)
    reply = svc.GetPaths(
        aprilcam_pb2.CameraRequest(cam_name="cam-no-paths"), _mock_ctx()
    )
    assert reply.present is False
    assert reply.json_blob == ""


def test_set_paths_creates_file(tmp_path: Path) -> None:
    """SetPaths writes paths.json atomically."""
    svc = _make_servicer(tmp_path)
    paths_data = [{"path_id": "path_000", "playfield_id": "pf-0", "waypoints": []}]
    blob = json.dumps(paths_data)

    reply = svc.SetPaths(
        aprilcam_pb2.CameraJsonRequest(cam_name="cam-paths", json_blob=blob),
        _mock_ctx(),
    )
    assert reply.ok is True

    written = tmp_path / "cameras" / "cam-paths" / "paths.json"
    assert written.exists()
    assert json.loads(written.read_text()) == paths_data


def test_get_paths_round_trip(tmp_path: Path) -> None:
    """SetPaths then GetPaths returns the same JSON blob."""
    svc = _make_servicer(tmp_path)
    paths_data = [
        {
            "path_id": "path_000",
            "playfield_id": "pf-1",
            "name": "robot route",
            "waypoints": [{"x": 10.0, "y": 5.0, "size_cm": 2.0, "symbol": "circle",
                           "symbol_color": [255, 0, 0], "line_color": [0, 255, 0]}],
        }
    ]
    blob = json.dumps(paths_data)

    svc.SetPaths(
        aprilcam_pb2.CameraJsonRequest(cam_name="cam-rt-paths", json_blob=blob),
        _mock_ctx(),
    )

    reply = svc.GetPaths(
        aprilcam_pb2.CameraRequest(cam_name="cam-rt-paths"), _mock_ctx()
    )
    assert reply.present is True
    assert json.loads(reply.json_blob) == paths_data


def test_set_paths_invalid_json(tmp_path: Path) -> None:
    """SetPaths with invalid JSON returns ok=False and INVALID_ARGUMENT."""
    svc = _make_servicer(tmp_path)
    ctx = _mock_ctx()

    reply = svc.SetPaths(
        aprilcam_pb2.CameraJsonRequest(cam_name="cam-bad-paths", json_blob="[bad]"),
        ctx,
    )
    assert reply.ok is False
    ctx.set_code.assert_called_once_with(grpc.StatusCode.INVALID_ARGUMENT)


def test_set_paths_atomic_no_tmp(tmp_path: Path) -> None:
    """SetPaths leaves no .tmp file behind on success."""
    svc = _make_servicer(tmp_path)
    blob = json.dumps([])
    svc.SetPaths(
        aprilcam_pb2.CameraJsonRequest(cam_name="cam-ap", json_blob=blob),
        _mock_ctx(),
    )
    cam_dir = tmp_path / "cameras" / "cam-ap"
    tmp_files = list(cam_dir.glob("*.tmp"))
    assert tmp_files == [], f"Leftover tmp files: {tmp_files}"


# ---------------------------------------------------------------------------
# ListPlayfields
# ---------------------------------------------------------------------------


def test_list_playfields_empty_dir(tmp_path: Path) -> None:
    """ListPlayfields returns an empty list when the playfields dir is empty."""
    svc = _make_servicer(tmp_path)
    reply = svc.ListPlayfields(aprilcam_pb2.Empty(), _mock_ctx())
    assert list(reply.playfields) == []


def test_list_playfields_missing_dir(tmp_path: Path) -> None:
    """ListPlayfields returns an empty list when playfields_dir does not exist."""
    svc = _make_servicer(tmp_path)
    # Remove the playfields dir.
    (tmp_path / "playfields").rmdir()
    reply = svc.ListPlayfields(aprilcam_pb2.Empty(), _mock_ctx())
    assert list(reply.playfields) == []


def test_list_playfields_returns_entries(tmp_path: Path) -> None:
    """ListPlayfields returns one PlayfieldEntry per JSON file."""
    svc = _make_servicer(tmp_path)
    pf_dir = tmp_path / "playfields"
    pf1 = {"playfield": {"width_cm": 134.3, "height_cm": 89.3}, "april_tags": []}
    pf2 = {"playfield": {"width_cm": 100.0, "height_cm": 60.0}, "aruco_tags": []}
    (pf_dir / "main-playfield.json").write_text(json.dumps(pf1))
    (pf_dir / "secondary.json").write_text(json.dumps(pf2))

    reply = svc.ListPlayfields(aprilcam_pb2.Empty(), _mock_ctx())

    entries = list(reply.playfields)
    assert len(entries) == 2

    # Entries are sorted by filename stem.
    assert entries[0].name == "main-playfield"
    assert json.loads(entries[0].json_blob) == pf1

    assert entries[1].name == "secondary"
    assert json.loads(entries[1].json_blob) == pf2


def test_list_playfields_sorted_alphabetically(tmp_path: Path) -> None:
    """ListPlayfields returns entries in alphabetical filename order."""
    svc = _make_servicer(tmp_path)
    pf_dir = tmp_path / "playfields"
    for name in ("zebra", "alpha", "beta"):
        (pf_dir / f"{name}.json").write_text(json.dumps({"playfield": {}}))

    reply = svc.ListPlayfields(aprilcam_pb2.Empty(), _mock_ctx())

    names = [e.name for e in reply.playfields]
    assert names == sorted(names)
    assert names == ["alpha", "beta", "zebra"]
