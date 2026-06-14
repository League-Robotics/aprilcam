"""Unit tests for _handle_set_camera_playfield (Sprint 012, Ticket 006).

Tests are hardware-free: camera registry and playfield_def_registry are
patched with controlled in-memory state via monkeypatch.  No daemon, no
camera hardware, no gRPC needed.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("aprilcam.daemon.grpc_server", reason="requires aprilcam[daemon]")

from aprilcam.server import mcp_server
from aprilcam.server.mcp_server import _handle_set_camera_playfield, CameraRegistry
from aprilcam.core.playfield_def import PlayfieldDefinitionRegistry, PlayfieldDefinition
from aprilcam.camera.camera_config import load_camera_config


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path):
    """Replace module-level registry and _cam_info for each test.

    Yields (registry, playfield_def_registry, cam_info_dict, camera_dir).
    """
    fresh_registry = CameraRegistry()
    fresh_def_registry = PlayfieldDefinitionRegistry()
    fresh_cam_info: dict = {}

    monkeypatch.setattr(mcp_server, "registry", fresh_registry)
    monkeypatch.setattr(mcp_server, "playfield_def_registry", fresh_def_registry)
    monkeypatch.setattr(mcp_server, "_cam_info", fresh_cam_info)

    camera_dir = tmp_path / "arducam-ov9782-usb-camera"
    camera_dir.mkdir(parents=True, exist_ok=True)

    yield fresh_registry, fresh_def_registry, fresh_cam_info, camera_dir


def _register_camera(registry, cam_info, camera_dir, camera_id="arducam-ov9782-usb-camera"):
    """Insert a sentinel camera entry into registry and _cam_info."""
    registry.open(None, handle=camera_id)
    cam_info[camera_id] = {"camera_dir": str(camera_dir)}
    return camera_id


def _register_playfield_def(def_registry, name="main-playfield"):
    """Insert a minimal PlayfieldDefinition directly into the registry."""
    defn = PlayfieldDefinition(
        name=name,
        display_name=name,
        width_cm=101.0,
        height_cm=89.0,
        origin="apriltag-center-a1",
    )
    # Access the internal dict directly — same pattern used in test_playfield_def.py
    def_registry._defs[name] = defn
    return name


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_set_camera_playfield_writes_config(isolated_state):
    """set_camera_playfield writes config.json and returns camera_id, playfield, config_path."""
    reg, def_reg, cam_info, camera_dir = isolated_state
    camera_id = _register_camera(reg, cam_info, camera_dir)
    _register_playfield_def(def_reg, "main-playfield")

    result = _handle_set_camera_playfield(camera_id, "main-playfield")

    assert "error" not in result, f"Unexpected error: {result.get('error')}"
    assert result["camera_id"] == camera_id
    assert result["playfield"] == "main-playfield"
    assert result["config_path"] == str(camera_dir / "config.json")

    # config.json must exist and be loadable with the correct value
    config = load_camera_config(camera_dir)
    assert config == {"playfield": "main-playfield"}


def test_set_camera_playfield_unknown_camera(isolated_state):
    """set_camera_playfield on an unknown camera_id returns an error."""
    _reg, def_reg, _cam_info, _camera_dir = isolated_state
    _register_playfield_def(def_reg, "main-playfield")

    result = _handle_set_camera_playfield("no-such-camera", "main-playfield")

    assert "error" in result
    assert "no-such-camera" in result["error"]


def test_set_camera_playfield_unknown_playfield(isolated_state):
    """set_camera_playfield with an unknown playfield name returns a friendly error listing available names."""
    reg, def_reg, cam_info, camera_dir = isolated_state
    camera_id = _register_camera(reg, cam_info, camera_dir)
    _register_playfield_def(def_reg, "main-playfield")

    result = _handle_set_camera_playfield(camera_id, "nonexistent")

    assert "error" in result
    assert "nonexistent" in result["error"]
    # Friendly listing of available names
    assert "main-playfield" in result["error"]


def test_set_camera_playfield_round_trip_load(isolated_state):
    """After set_camera_playfield, load_camera_config returns {\"playfield\": <name>}."""
    reg, def_reg, cam_info, camera_dir = isolated_state
    camera_id = _register_camera(reg, cam_info, camera_dir)
    _register_playfield_def(def_reg, "arena")

    _handle_set_camera_playfield(camera_id, "arena")

    config = load_camera_config(camera_dir)
    assert config is not None
    assert config["playfield"] == "arena"
