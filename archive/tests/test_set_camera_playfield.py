"""Unit tests for _handle_set_camera_playfield (Sprint 012, Ticket 006).

Tests are hardware-free: camera registry and playfield_def_registry are
patched with controlled in-memory state via monkeypatch.  No daemon, no
camera hardware, no gRPC needed.

Since 014-005, _handle_set_camera_playfield uses GetCameraConfig /
SetCameraConfig RPCs instead of local file writes.  Tests patch
_ensure_daemon_client with a fake client that captures RPC calls.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest.importorskip("aprilcam.daemon.grpc_server", reason="requires aprilcam[daemon]")

from aprilcam.server import mcp_server
from aprilcam.server.mcp_server import _handle_set_camera_playfield, CameraRegistry
from aprilcam.core.playfield_def import PlayfieldDefinitionRegistry, PlayfieldDefinition


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path):
    """Replace module-level registry, _cam_info, and daemon client for each test.

    Yields (registry, playfield_def_registry, cam_info_dict, fake_client).
    """
    fresh_registry = CameraRegistry()
    fresh_def_registry = PlayfieldDefinitionRegistry()
    fresh_cam_info: dict = {}

    monkeypatch.setattr(mcp_server, "registry", fresh_registry)
    monkeypatch.setattr(mcp_server, "playfield_def_registry", fresh_def_registry)
    monkeypatch.setattr(mcp_server, "_cam_info", fresh_cam_info)

    # Build a fake daemon client that stores what SetCameraConfig was called with.
    fake_client = MagicMock()
    cfg_store: dict[str, dict] = {}

    def _fake_get_camera_config(cam_name: str) -> MagicMock:
        reply = MagicMock()
        existing = cfg_store.get(cam_name)
        reply.present = existing is not None
        reply.json_blob = json.dumps(existing) if existing is not None else ""
        return reply

    def _fake_set_camera_config(cam_name: str, json_blob: str) -> MagicMock:
        cfg_store[cam_name] = json.loads(json_blob)
        reply = MagicMock()
        reply.ok = True
        return reply

    fake_client.get_camera_config.side_effect = _fake_get_camera_config
    fake_client.set_camera_config.side_effect = _fake_set_camera_config

    monkeypatch.setattr(mcp_server, "_ensure_daemon_client", lambda: fake_client)

    yield fresh_registry, fresh_def_registry, fresh_cam_info, fake_client, cfg_store


def _register_camera(registry, cam_info, camera_id="arducam-ov9782-usb-camera"):
    """Insert a sentinel camera entry into registry and _cam_info."""
    registry.open(None, handle=camera_id)
    cam_info[camera_id] = {"cam_name": camera_id}
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
    """set_camera_playfield calls SetCameraConfig RPC and returns no filesystem path."""
    reg, def_reg, cam_info, fake_client, cfg_store = isolated_state
    camera_id = _register_camera(reg, cam_info)
    _register_playfield_def(def_reg, "main-playfield")

    result = _handle_set_camera_playfield(camera_id, "main-playfield")

    assert "error" not in result, f"Unexpected error: {result.get('error')}"
    assert result["camera_id"] == camera_id
    assert result["playfield"] == "main-playfield"
    assert result["linked"] is True
    # The response must not leak filesystem paths to the client.
    assert "config_path" not in result
    assert not any(isinstance(v, str) and "/" in v for v in result.values())

    # Verify the RPC was called with the correct data.
    fake_client.set_camera_config.assert_called_once()
    stored = cfg_store.get(camera_id)
    assert stored is not None
    assert stored["playfield"] == "main-playfield"


def test_set_camera_playfield_unknown_camera(isolated_state):
    """set_camera_playfield on an unknown camera_id returns an error."""
    _reg, def_reg, _cam_info, _fake_client, _cfg_store = isolated_state
    _register_playfield_def(def_reg, "main-playfield")

    result = _handle_set_camera_playfield("no-such-camera", "main-playfield")

    assert "error" in result
    assert "no-such-camera" in result["error"]


def test_set_camera_playfield_unknown_playfield(isolated_state):
    """set_camera_playfield with an unknown playfield name returns a friendly error listing available names."""
    reg, def_reg, cam_info, fake_client, _cfg_store = isolated_state
    camera_id = _register_camera(reg, cam_info)
    _register_playfield_def(def_reg, "main-playfield")

    result = _handle_set_camera_playfield(camera_id, "nonexistent")

    assert "error" in result
    assert "nonexistent" in result["error"]
    # Friendly listing of available names
    assert "main-playfield" in result["error"]


def test_set_camera_playfield_round_trip_load(isolated_state):
    """After set_camera_playfield, the stored config has the linked playfield."""
    reg, def_reg, cam_info, fake_client, cfg_store = isolated_state
    camera_id = _register_camera(reg, cam_info)
    _register_playfield_def(def_reg, "arena")

    _handle_set_camera_playfield(camera_id, "arena")

    # Verify via the captured RPC store (not local disk).
    config = cfg_store.get(camera_id)
    assert config is not None
    assert config["playfield"] == "arena"
