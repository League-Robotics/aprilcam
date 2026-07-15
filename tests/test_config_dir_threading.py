"""Tests for ticket 019-003 — threading config_dir through daemon-local
callers and repointing config-touching RPCs.

Ticket 019-002 made ``load_calibration_from_camera_dir``,
``calibrate_from_playfield_def``, and ``Playfield._load_static_marker_ids``
accept an optional config-root parameter without wiring real values through.
This ticket wires ``Config.effective_config_dir`` (or the local equivalent)
into every daemon-local call site. These tests confirm each site actually
passes a non-``None``, correctly-resolved ``config_dir`` — as opposed to
ticket 002's tests, which only covered the loader functions' own behavior
given an explicit ``config_dir``.

Covers:
- ``daemon/camera_pipeline.py`` (``CameraPipeline.start()``)
- ``daemon/grpc_server.py`` (``ReloadCalibration``, post-``SetCalibration``
  reload)
- ``stream.py`` (``_load_homography_matrix``)
- ``cli/view_cli.py`` (calibration load for undistort wiring)
- ``core/playfield.py`` (``Playfield.__init__`` / ``_auto_discover_homography``
  now accept and thread an explicit ``config_dir`` constructor param)
- ``core/playfield_query.py`` / ``cli/calibrate_cli.py`` (playfield
  definitions read from ``config_playfields_dir`` / ``effective_config_dir``;
  ``calibrate_cli.py`` also pre-loads config.json from the config root and
  passes it via ``camera_config=`` since ``calibrate_from_playfield_def``'s
  own Step 1 read intentionally stays pinned to ``camera_dir``/state)

All tests run without camera hardware or a live gRPC network.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

pytest.importorskip("cv2", reason="requires aprilcam[imaging]")
pytest.importorskip("aprilcam.daemon.grpc_server", reason="requires aprilcam[daemon]")

pytestmark = pytest.mark.needs_cv2

_IDENTITY_H = np.eye(3, dtype=float).tolist()


class _Sentinel(Exception):
    """Raised by a patched loader to abort a call chain right after the
    call under test, before any code that needs real camera hardware runs."""


# ---------------------------------------------------------------------------
# daemon/camera_pipeline.py — CameraPipeline.start()
# ---------------------------------------------------------------------------


def test_camera_pipeline_start_threads_effective_config_dir(tmp_path, monkeypatch):
    """CameraPipeline.start() passes config_dir=self.config.effective_config_dir
    to load_calibration_from_camera_dir."""
    from aprilcam.config import Config
    from aprilcam.daemon import camera_pipeline as cp_mod

    config_dir = tmp_path / "config"
    cfg = Config(data_dir=tmp_path, config_dir=config_dir, socket_dir=tmp_path / "sock")
    pipe = cp_mod.CameraPipeline("cam0", 0, cfg)

    captured = {}

    def _fake_loader(camera_dir, config_dir=None, **kwargs):
        captured["camera_dir"] = Path(camera_dir)
        captured["config_dir"] = config_dir
        raise _Sentinel()

    monkeypatch.setattr(cp_mod, "load_calibration_from_camera_dir", _fake_loader)
    monkeypatch.setattr(
        cp_mod, "get_device_name", lambda index: "cam0", raising=False
    )

    with pytest.raises(_Sentinel):
        pipe.start()

    assert captured["config_dir"] is not None
    assert Path(captured["config_dir"]) == cfg.effective_config_dir / "cameras"
    assert captured["camera_dir"] == cfg.cameras_dir / "cam0"


def test_camera_pipeline_start_config_dir_falls_back_legacy(tmp_path, monkeypatch):
    """When config_dir is unset and data_dir already holds legacy co-located
    config files, effective_config_dir resolves to data_dir — CameraPipeline
    threads that resolved value through unchanged."""
    from aprilcam.config import Config
    from aprilcam.daemon import camera_pipeline as cp_mod

    # Legacy single-tree layout: config.json co-located under data_dir.
    cam_dir = tmp_path / "cameras" / "cam0"
    cam_dir.mkdir(parents=True)
    (cam_dir / "config.json").write_text(json.dumps({"device_name": "cam0"}))

    # Point config_dir at a distinct EMPTY tmp dir so the test is hermetic
    # (never reads the user's real ~/.config/aprilcam). With no config files
    # there and legacy config co-located under data_dir, effective_config_dir
    # must fall back to data_dir.
    cfg = Config(
        data_dir=tmp_path,
        config_dir=tmp_path / "config",
        socket_dir=tmp_path / "sock",
    )
    assert cfg.effective_config_dir == tmp_path  # sanity: legacy fallback engaged

    pipe = cp_mod.CameraPipeline("cam0", 0, cfg)

    captured = {}

    def _fake_loader(camera_dir, config_dir=None, **kwargs):
        captured["config_dir"] = config_dir
        raise _Sentinel()

    monkeypatch.setattr(cp_mod, "load_calibration_from_camera_dir", _fake_loader)
    monkeypatch.setattr(
        cp_mod, "get_device_name", lambda index: "cam0", raising=False
    )

    with pytest.raises(_Sentinel):
        pipe.start()

    assert Path(captured["config_dir"]) == tmp_path / "cameras"


# ---------------------------------------------------------------------------
# daemon/grpc_server.py — ReloadCalibration / post-SetCalibration reload
# ---------------------------------------------------------------------------


def _make_servicer(tmp_path: Path, config_dir: Path):
    from aprilcam.config import Config
    from aprilcam.daemon.grpc_server import AprilCamServicer

    (tmp_path / "cameras").mkdir()
    sock_dir = tmp_path / "sockets"
    sock_dir.mkdir()
    config = Config(
        data_dir=tmp_path,
        config_dir=config_dir,
        socket_dir=sock_dir,
        daemon_pidfile=sock_dir / "aprilcamd.pid",
    )
    return AprilCamServicer(
        cameras={},
        cam_lock=threading.Lock(),
        config=config,
        shutdown_event=threading.Event(),
    )


def _mock_ctx():
    import grpc

    return MagicMock(spec=grpc.ServicerContext)


def test_reload_calibration_threads_effective_config_dir(tmp_path, monkeypatch):
    """ReloadCalibration passes config_dir=self._config.effective_config_dir
    to load_calibration_from_camera_dir."""
    from aprilcam.proto import aprilcam_pb2
    import aprilcam.calibration.calibration as cal_mod

    config_dir = tmp_path / "config"
    svc = _make_servicer(tmp_path, config_dir)

    pipeline = MagicMock()
    pipeline._april_cam = None
    svc._cameras["cam-reload"] = pipeline

    captured = {}

    def _fake_loader(camera_dir, config_dir=None, **kwargs):
        captured["config_dir"] = config_dir
        return None

    monkeypatch.setattr(cal_mod, "load_calibration_from_camera_dir", _fake_loader)

    svc.ReloadCalibration(
        aprilcam_pb2.CameraRequest(cam_name="cam-reload"), _mock_ctx()
    )

    assert captured["config_dir"] is not None
    assert Path(captured["config_dir"]) == svc._config.effective_config_dir / "cameras"


def test_set_calibration_reload_threads_effective_config_dir(tmp_path, monkeypatch):
    """SetCalibration's post-write pipeline reload passes
    config_dir=self._config.effective_config_dir to
    load_calibration_from_camera_dir."""
    from aprilcam.proto import aprilcam_pb2
    import aprilcam.calibration.calibration as cal_mod

    config_dir = tmp_path / "config"
    svc = _make_servicer(tmp_path, config_dir)

    pipeline = MagicMock()
    pipeline._april_cam = None
    svc._cameras["cam-set-cal"] = pipeline

    captured = {}

    def _fake_loader(camera_dir, config_dir=None, **kwargs):
        captured["config_dir"] = config_dir
        return None

    monkeypatch.setattr(cal_mod, "load_calibration_from_camera_dir", _fake_loader)

    blob = json.dumps({"homography": [[1, 0, 0], [0, 1, 0], [0, 0, 1]]})
    reply = svc.SetCalibration(
        aprilcam_pb2.CameraJsonRequest(cam_name="cam-set-cal", json_blob=blob),
        _mock_ctx(),
    )

    assert reply.ok is True
    assert captured["config_dir"] is not None
    assert Path(captured["config_dir"]) == svc._config.effective_config_dir / "cameras"


# ---------------------------------------------------------------------------
# stream.py — _load_homography_matrix
# ---------------------------------------------------------------------------


def test_stream_load_homography_matrix_threads_effective_config_dir(
    tmp_path, monkeypatch
):
    """stream.py's homography='auto' path threads
    cfg.effective_config_dir into load_calibration_from_camera_dir.

    The call site is wrapped in a broad ``try/except Exception: pass``, so
    the fake loader records its kwargs and returns None (letting the
    surrounding code continue and eventually return None) rather than
    raising — a raised exception would be silently swallowed by that
    except clause and prove nothing.
    """
    from aprilcam.config import Config
    import aprilcam.stream as stream_mod

    config_dir = tmp_path / "config"
    cfg = Config(data_dir=tmp_path, config_dir=config_dir, socket_dir=tmp_path / "sock")

    monkeypatch.setattr(stream_mod, "get_device_name", lambda idx: "test-device")
    monkeypatch.setattr(
        "aprilcam.calibration.calibration.load_calibration_for_camera",
        lambda device_name, data_path: None,
    )
    monkeypatch.setattr("aprilcam.config.Config.load", classmethod(lambda cls: cfg))

    captured = {}

    def _fake_loader(camera_dir, config_dir=None, **kwargs):
        captured["config_dir"] = config_dir
        return None

    monkeypatch.setattr(
        "aprilcam.calibration.calibration.load_calibration_from_camera_dir",
        _fake_loader,
    )
    # discover_homography (reached after the patched block, since the fake
    # loader returns None) should also report nothing found.
    monkeypatch.setattr(stream_mod, "discover_homography", lambda *a, **k: None)

    fake_cap = MagicMock()
    fake_cap.get.return_value = 640

    result = stream_mod._load_homography_matrix("auto", fake_cap, 0, tmp_path)

    assert result is None
    assert captured["config_dir"] is not None
    assert Path(captured["config_dir"]) == cfg.effective_config_dir / "cameras"


# ---------------------------------------------------------------------------
# cli/view_cli.py
# ---------------------------------------------------------------------------


def test_view_cli_source_reads_calibration_with_effective_config_dir():
    """view_cli.py's calibration-load call site (~557) is wired to call
    load_calibration_from_camera_dir with
    config_dir=config.effective_config_dir / "cameras" (the per-camera
    config root load_calibration_from_camera_dir expects — see its
    docstring: config_dir is the root holding per-camera config
    subdirectories, i.e. the config-side equivalent of cameras_dir).

    Static-source inspection of the call site (rather than driving the full
    Tk/socket-consumer main() loop, which needs a live daemon) — confirms
    the kwarg is present in the source so a future edit can't silently drop
    it.
    """
    import inspect

    from aprilcam.cli import view_cli as view_cli_mod

    src = inspect.getsource(view_cli_mod)
    assert 'config_dir=config.effective_config_dir / "cameras"' in src


# ---------------------------------------------------------------------------
# core/playfield.py — Playfield config_dir constructor param
# ---------------------------------------------------------------------------


def test_playfield_threads_config_dir_into_static_marker_ids(tmp_path, monkeypatch):
    """Playfield(..., calibration=<path>, config_dir=<root>) threads
    config_dir into _load_static_marker_ids at construction time."""
    from aprilcam.core.playfield import Playfield

    config_dir = tmp_path / "config"
    cal_dir = tmp_path / "cameras" / "cam0"
    cal_dir.mkdir(parents=True)
    cal_path = cal_dir / "calibration.json"
    cal_path.write_text(json.dumps({"homography": _IDENTITY_H}))

    captured = {}
    orig = Playfield._load_static_marker_ids

    def _spy(self, path, config_dir=None):
        captured["config_dir"] = config_dir
        return orig(self, path, config_dir)

    monkeypatch.setattr(Playfield, "_load_static_marker_ids", _spy)

    Playfield(
        camera=MagicMock(),
        calibration=str(cal_path),
        config_dir=config_dir,
    )

    assert captured["config_dir"] == config_dir


def test_playfield_config_dir_defaults_to_none_sibling_behavior(tmp_path, monkeypatch):
    """Omitting config_dir preserves the pre-019 sibling-lookup default
    (backward compat for callers that don't yet know about config roots)."""
    from aprilcam.core.playfield import Playfield

    cal_dir = tmp_path / "cameras" / "cam0"
    cal_dir.mkdir(parents=True)
    cal_path = cal_dir / "calibration.json"
    cal_path.write_text(json.dumps({"homography": _IDENTITY_H}))

    captured = {}
    orig = Playfield._load_static_marker_ids

    def _spy(self, path, config_dir=None):
        captured["config_dir"] = config_dir
        return orig(self, path, config_dir)

    monkeypatch.setattr(Playfield, "_load_static_marker_ids", _spy)

    Playfield(camera=MagicMock(), calibration=str(cal_path))

    assert captured["config_dir"] is None


# ---------------------------------------------------------------------------
# core/playfield_query.py — load_playfield_map reads config_playfields_dir
# ---------------------------------------------------------------------------


def test_load_playfield_map_reads_config_playfields_dir(tmp_path):
    from aprilcam.config import Config
    from aprilcam.core.playfield_query import load_playfield_map

    config_dir = tmp_path / "config"
    cfg = Config(data_dir=tmp_path, config_dir=config_dir, socket_dir=tmp_path / "sock")

    pf_dir = config_dir / "playfields"
    pf_dir.mkdir(parents=True)
    (pf_dir / "main.json").write_text(
        json.dumps(
            {
                "playfield": {"width_cm": 100.0, "height_cm": 50.0, "origin": None},
                "april_tags": [],
                "aruco_tags": [],
                "rectangles": [],
                "dots": [],
            }
        )
    )

    result = load_playfield_map(cfg)
    assert result["playfield"]["width_cm"] == 100.0


def test_load_playfield_map_legacy_fallback_to_data_dir(tmp_path):
    """When config_dir is empty and data_dir holds legacy co-located
    playfield defs, load_playfield_map still finds them via the
    effective_config_dir fallback."""
    from aprilcam.config import Config
    from aprilcam.core.playfield_query import load_playfield_map

    config_dir = tmp_path / "config"  # never populated
    cfg = Config(data_dir=tmp_path, config_dir=config_dir, socket_dir=tmp_path / "sock")

    pf_dir = tmp_path / "playfields"
    pf_dir.mkdir(parents=True)
    (pf_dir / "legacy-main.json").write_text(
        json.dumps(
            {
                "playfield": {"width_cm": 77.0, "height_cm": 33.0, "origin": None},
                "april_tags": [],
                "aruco_tags": [],
                "rectangles": [],
                "dots": [],
            }
        )
    )

    result = load_playfield_map(cfg)
    assert result["playfield"]["width_cm"] == 77.0


# ---------------------------------------------------------------------------
# cli/calibrate_cli.py — pre-loads config.json from the config root and
# passes it as camera_config so calibrate_from_playfield_def's Step 1 read
# (intentionally pinned to camera_dir/state — see the function's docstring)
# sees the real config-root content on a split install.
# ---------------------------------------------------------------------------


def test_calibrate_cli_source_preloads_camera_config_from_config_root():
    """calibrate_cli.py's calibrate_from_playfield_def call site loads
    config.json from config.config_cameras_dir and passes it via
    camera_config= (rather than relying on calibrate_from_playfield_def's
    Step 1 read, which stays pinned to camera_dir/state for backward
    compat) — a static source check so a future edit can't silently drop
    the config-root read.
    """
    import inspect

    from aprilcam.cli import calibrate_cli as calibrate_cli_mod

    src = inspect.getsource(calibrate_cli_mod)
    assert "config.config_cameras_dir" in src
    assert "camera_config=_cam_config" in src


# ---------------------------------------------------------------------------
# End-to-end: fully split layout (config root and state root are two
# different tmp dirs) — config.json under config_dir, calibration.json under
# data_dir, no legacy fallback needed. Covers the ticket's "camera opens
# correctly end-to-end" acceptance criterion without real camera hardware:
# GetCameraConfig returns config-root content, GetCalibration returns
# state-root content, and CameraPipeline's own calibration-derived world-math
# (_a1_origin / _seed_static_geometry inputs) resolves correctly once the
# calibration loaded via the threaded config_dir carries a homography.
# ---------------------------------------------------------------------------


def test_split_layout_end_to_end_config_and_calibration_resolve_independently(
    tmp_path,
):
    from aprilcam.config import Config
    from aprilcam.calibration.calibration import (
        CameraCalibration,
        save_calibration_to_camera_dir,
    )
    from aprilcam.camera.camera_config import save_camera_config
    from aprilcam.daemon.camera_pipeline import CameraPipeline
    from aprilcam.daemon.grpc_server import AprilCamServicer
    from aprilcam.proto import aprilcam_pb2

    state_root = tmp_path / "state"
    config_root = tmp_path / "config"
    (state_root / "cameras").mkdir(parents=True)
    (state_root / "sockets").mkdir(parents=True)

    cam_name = "split-cam"

    # config.json under the config root only.
    save_camera_config(
        config_root / "cameras" / cam_name,
        {"device_name": "Split Camera", "playfield": "main-playfield"},
    )

    # calibration.json under the state root only, with a real homography and
    # AprilTag-1 static marker so _a1_origin resolves a non-trivial origin.
    cal = CameraCalibration(
        device_name=cam_name,
        resolution=(1280, 800),
        homography=np.array(
            [[1.0, 0.0, 10.0], [0.0, 1.0, 20.0], [0.0, 0.0, 1.0]]
        ),
        playfield_width_cm=134.3,
        playfield_height_cm=89.3,
        static_markers={"apriltag:1": {"pixel": [100.0, 200.0], "world": [5.0, 7.0]}},
    )
    save_calibration_to_camera_dir(
        cal, state_root / "cameras" / cam_name, 134.3, 89.3
    )

    cfg = Config(
        data_dir=state_root,
        config_dir=config_root,
        socket_dir=state_root / "sockets",
        daemon_pidfile=state_root / "sockets" / "aprilcamd.pid",
    )
    assert cfg.effective_config_dir == config_root  # split layout, no fallback

    # 1. GetCameraConfig returns the config-root content.
    svc = AprilCamServicer(
        cameras={}, cam_lock=threading.Lock(), config=cfg, shutdown_event=threading.Event()
    )
    cfg_reply = svc.GetCameraConfig(
        aprilcam_pb2.CameraRequest(cam_name=cam_name), _mock_ctx()
    )
    assert cfg_reply.present is True
    assert json.loads(cfg_reply.json_blob)["device_name"] == "Split Camera"

    # 2. GetCalibration returns the state-root content.
    cal_reply = svc.GetCalibration(
        aprilcam_pb2.CameraRequest(cam_name=cam_name), _mock_ctx()
    )
    assert cal_reply.present is True
    cal_blob = json.loads(cal_reply.json_blob)
    assert cal_blob["homography"][0][2] == 10.0

    # 3. CameraPipeline (the daemon-local loader caller) loads calibration
    #    from the state root with the config.json overlay from the config
    #    root, and world-math built from that calibration resolves the
    #    surveyed A1 origin (5.0, 7.0), not the geometric field-centre
    #    fallback — proving the loaded calibration actually carries the
    #    state-root's static_markers.
    pipe = CameraPipeline(cam_name, 0, cfg)
    from aprilcam.calibration.calibration import load_calibration_from_camera_dir

    pipe._calibration = load_calibration_from_camera_dir(
        cfg.cameras_dir / cam_name, config_dir=cfg.effective_config_dir / "cameras"
    )
    assert pipe._calibration is not None
    assert pipe._calibration.device_name == "Split Camera"  # from config.json overlay
    origin_x, origin_y = pipe._a1_origin()
    assert (origin_x, origin_y) == (5.0, 7.0)
