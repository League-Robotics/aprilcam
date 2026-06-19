"""Tests for 014-005: MCP server file-proxy RPC wiring.

Covers:
- parse_calibration_from_dict round-trip.
- parse_camera_config validation.
- PlayfieldDefinitionRegistry.clear() and add_from_dict().
- connect_daemon session teardown (mock DaemonControl).
- _on_daemon_connect populates playfield_def_registry via ListPlayfields mock.
- get_version includes active_daemon_host/active_daemon_port.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("aprilcam.daemon.grpc_server", reason="requires aprilcam[daemon]")

# ---------------------------------------------------------------------------
# parse_calibration_from_dict
# ---------------------------------------------------------------------------


def test_parse_calibration_from_dict_round_trip():
    """parse_calibration_from_dict(d) is the inverse of cal.to_dict()."""
    from aprilcam.calibration.calibration import CameraCalibration, parse_calibration_from_dict
    import numpy as np

    H = np.eye(3, dtype=float)
    H[0, 2] = 5.0  # a non-trivial transform

    cal = CameraCalibration(
        device_name="test_camera",
        resolution=(1280, 720),
        homography=H,
    )
    cal_dict = cal.to_dict()

    # Deserialize from the dict (RPC path).
    restored = parse_calibration_from_dict(cal_dict)

    assert restored.device_name == "test_camera"
    assert restored.resolution == (1280, 720)
    assert restored.homography is not None
    np.testing.assert_array_almost_equal(restored.homography, H)


def test_parse_calibration_from_dict_with_no_homography():
    """parse_calibration_from_dict works when homography is absent (None in JSON).

    CameraCalibration.from_dict converts a None homography to array(nan)
    rather than preserving None, so we check the result is non-functional
    (not a valid 3x3) rather than asserting None.
    """
    from aprilcam.calibration.calibration import parse_calibration_from_dict
    import numpy as np

    # Build a dict manually without a valid homography.
    d = {
        "device_name": "cam",
        "resolution": [640, 480],
        "homography": None,
    }

    restored = parse_calibration_from_dict(d)
    assert restored.device_name == "cam"
    # from_dict converts None to array(nan); check no crash and device_name round-tripped.
    # The homography is either None or NaN-scalar — either way it's not a usable 3×3.
    if restored.homography is not None:
        import numpy as np
        assert np.ndim(restored.homography) < 2 or restored.homography.shape != (3, 3)


# ---------------------------------------------------------------------------
# parse_camera_config
# ---------------------------------------------------------------------------


def test_parse_camera_config_returns_dict():
    """parse_camera_config returns the dict unchanged."""
    from aprilcam.camera.camera_config import parse_camera_config

    cfg = {"playfield": "main-playfield", "device_name": "cam_0"}
    result = parse_camera_config(cfg)
    assert result is cfg


def test_parse_camera_config_rejects_non_dict():
    """parse_camera_config raises TypeError for non-dict input."""
    from aprilcam.camera.camera_config import parse_camera_config

    with pytest.raises(TypeError):
        parse_camera_config("not-a-dict")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# PlayfieldDefinitionRegistry.clear() and add_from_dict()
# ---------------------------------------------------------------------------


def test_playfield_def_registry_clear():
    """clear() removes all definitions."""
    from aprilcam.core.playfield_def import PlayfieldDefinitionRegistry

    reg = PlayfieldDefinitionRegistry()
    d = {"playfield": {"width_cm": 100.0, "height_cm": 60.0, "origin": ""}}
    reg.add_from_dict("test-field", d)
    assert "test-field" in reg._defs

    reg.clear()
    assert reg._defs == {}


def test_playfield_def_registry_add_from_dict_minimal():
    """add_from_dict builds a PlayfieldDefinition from a minimal dict."""
    from aprilcam.core.playfield_def import PlayfieldDefinitionRegistry

    reg = PlayfieldDefinitionRegistry()
    d = {
        "playfield": {"width_cm": 134.0, "height_cm": 89.3, "origin": "apriltag-center-a1"},
        "april_tags": [{"id": 1, "x": 0, "y": 0}],
        "aruco_tags": [],
    }
    reg.add_from_dict("main-playfield", d)

    defn = reg.get("main-playfield")
    assert defn.name == "main-playfield"
    assert defn.width_cm == 134.0
    assert defn.height_cm == 89.3
    assert defn.origin == "apriltag-center-a1"
    assert len(defn.april_tags) == 1


def test_playfield_def_registry_add_from_dict_missing_geometry_raises():
    """add_from_dict raises ValueError when width_cm/height_cm are missing."""
    from aprilcam.core.playfield_def import PlayfieldDefinitionRegistry

    reg = PlayfieldDefinitionRegistry()
    with pytest.raises(ValueError):
        reg.add_from_dict("bad-field", {"playfield": {}})


# ---------------------------------------------------------------------------
# _on_daemon_connect populates playfield_def_registry
# ---------------------------------------------------------------------------


def _make_pf_entry(name: str, width_cm: float, height_cm: float) -> MagicMock:
    """Return a mock ListPlayfieldsResponse.PlayfieldEntry."""
    blob = json.dumps({
        "playfield": {"width_cm": width_cm, "height_cm": height_cm, "origin": ""},
    })
    entry = MagicMock()
    entry.name = name
    entry.json_blob = blob
    return entry


def test_on_daemon_connect_populates_registry():
    """_on_daemon_connect clears the registry then populates it from ListPlayfields."""
    from aprilcam.server import mcp_server

    # Mock a DaemonControl client.
    mock_client = MagicMock()
    pf_reply = MagicMock()
    pf_reply.playfields = [
        _make_pf_entry("field-a", 100.0, 60.0),
        _make_pf_entry("field-b", 200.0, 90.0),
    ]
    mock_client.list_playfields.return_value = pf_reply

    # Save the real registry state.
    original_defs = dict(mcp_server.playfield_def_registry._defs)
    try:
        mcp_server._on_daemon_connect(mock_client)

        assert "field-a" in mcp_server.playfield_def_registry._defs
        assert "field-b" in mcp_server.playfield_def_registry._defs
        defn_a = mcp_server.playfield_def_registry.get("field-a")
        assert defn_a.width_cm == 100.0
        defn_b = mcp_server.playfield_def_registry.get("field-b")
        assert defn_b.height_cm == 90.0
    finally:
        # Restore.
        mcp_server.playfield_def_registry._defs.clear()
        mcp_server.playfield_def_registry._defs.update(original_defs)


# ---------------------------------------------------------------------------
# get_version includes daemon target
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_version_no_daemon():
    """get_version returns null host/port when no daemon is connected."""
    from aprilcam.server import mcp_server

    orig_client = mcp_server._daemon_client
    try:
        mcp_server._daemon_client = None
        from aprilcam.server.mcp_server import get_version
        result = await get_version()
        data = json.loads(result[0].text)
        assert "version" in data
        assert data["active_daemon_host"] is None
        assert data["active_daemon_port"] is None
    finally:
        mcp_server._daemon_client = orig_client


@pytest.mark.asyncio
async def test_get_version_with_tcp_daemon():
    """get_version returns host/port when a TCP daemon is connected."""
    from aprilcam.server import mcp_server

    mock_client = MagicMock()
    mock_client._unix_path = None
    mock_client._host = "192.168.1.42"
    mock_client._port = 5280

    orig_client = mcp_server._daemon_client
    try:
        mcp_server._daemon_client = mock_client
        from aprilcam.server.mcp_server import get_version
        result = await get_version()
        data = json.loads(result[0].text)
        assert data["active_daemon_host"] == "192.168.1.42"
        assert data["active_daemon_port"] == 5280
    finally:
        mcp_server._daemon_client = orig_client


@pytest.mark.asyncio
async def test_get_version_with_unix_daemon():
    """get_version returns unix:<path> as host when on a Unix socket."""
    from aprilcam.server import mcp_server

    mock_client = MagicMock()
    mock_client._unix_path = "/run/aprilcam/control.sock"
    mock_client._host = "localhost"
    mock_client._port = 5280

    orig_client = mcp_server._daemon_client
    try:
        mcp_server._daemon_client = mock_client
        from aprilcam.server.mcp_server import get_version
        result = await get_version()
        data = json.loads(result[0].text)
        assert data["active_daemon_host"] == "unix:/run/aprilcam/control.sock"
        assert data["active_daemon_port"] is None
    finally:
        mcp_server._daemon_client = orig_client


# ---------------------------------------------------------------------------
# connect_daemon session teardown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_daemon_tears_down_detection_loops():
    """connect_daemon stops all detection loops and clears detection_registry."""
    from aprilcam.server import mcp_server
    from aprilcam.server.mcp_server import connect_daemon

    # Inject a fake detection loop.
    fake_loop = MagicMock()
    fake_entry = MagicMock()
    fake_entry.loop = fake_loop
    mcp_server.detection_registry["cam_0"] = fake_entry  # type: ignore[assignment]

    # Mock the new daemon connection so it succeeds without real hardware.
    mock_dc = MagicMock()
    mock_dc._unix_path = None
    mock_dc._host = "127.0.0.1"
    mock_dc._port = 5280
    mock_dc.list_cameras.return_value = []
    mock_dc.list_playfields.return_value = MagicMock(playfields=[])
    mock_dc.enumerate_cameras.return_value = []

    with patch("aprilcam.server.mcp_server.DaemonControl") as MockDC:
        MockDC.return_value = mock_dc
        mock_dc.connect.return_value = mock_dc

        # Patch out the registry close_all to avoid touching real cameras.
        with patch.object(mcp_server.registry, "close_all"):
            result = await connect_daemon(host="127.0.0.1", port=5280)

    data = json.loads(result[0].text)
    # Should have torn down the loop.
    fake_loop.stop.assert_called_once()
    # detection_registry should be empty.
    assert mcp_server.detection_registry == {}
    # Result should be success (target key present, no error).
    assert "error" not in data or data.get("error") is None
