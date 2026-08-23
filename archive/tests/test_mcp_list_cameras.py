"""Tests for _handle_list_cameras in mcp_server.py — 014-002.

Verifies that list_cameras routes through the daemon EnumerateCameras RPC
instead of probing local hardware directly.  No real daemon or camera hardware
is required; DaemonControl is mocked.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _make_camera_devices(entries: list[tuple[int, str, str]]):
    """Build CameraDevice objects from (index, name, slug) tuples."""
    from aprilcam.client.models import CameraDevice

    return [CameraDevice(index=i, name=n, slug=s) for i, n, s in entries]


def test_handle_list_cameras_uses_rpc(monkeypatch) -> None:
    """_handle_list_cameras calls enumerate_cameras() on the daemon client."""
    from aprilcam.server import mcp_server

    devices = _make_camera_devices([
        (0, "FaceTime HD Camera", "facetime-hd-camera"),
        (1, "OV9782 Global Shutter", "ov9782-global-shutter"),
    ])

    dc = MagicMock()
    dc.enumerate_cameras.return_value = devices

    monkeypatch.setattr(mcp_server, "_ensure_daemon_client", lambda: dc)

    result = mcp_server._handle_list_cameras()

    dc.enumerate_cameras.assert_called_once()
    assert len(result) == 2
    assert result[0]["index"] == 0
    assert result[0]["name"] == "FaceTime HD Camera"
    assert result[0]["slug"] == "facetime-hd-camera"
    assert result[1]["index"] == 1
    assert result[1]["name"] == "OV9782 Global Shutter"


def test_handle_list_cameras_returns_empty_on_exception(monkeypatch) -> None:
    """_handle_list_cameras returns [] when the daemon RPC raises."""
    from aprilcam.server import mcp_server

    def _raise():
        raise RuntimeError("daemon unreachable")

    dc = MagicMock()
    dc.enumerate_cameras.side_effect = RuntimeError("daemon unreachable")

    monkeypatch.setattr(mcp_server, "_ensure_daemon_client", lambda: dc)

    result = mcp_server._handle_list_cameras()

    assert result == []


def test_handle_list_cameras_empty_daemon_result(monkeypatch) -> None:
    """_handle_list_cameras returns [] when the daemon finds no cameras."""
    from aprilcam.server import mcp_server

    dc = MagicMock()
    dc.enumerate_cameras.return_value = []

    monkeypatch.setattr(mcp_server, "_ensure_daemon_client", lambda: dc)

    result = mcp_server._handle_list_cameras()

    assert result == []


def test_handle_list_cameras_dict_has_expected_keys(monkeypatch) -> None:
    """Each dict in the result has index, name, and slug keys."""
    from aprilcam.server import mcp_server

    devices = _make_camera_devices([
        (2, "My Camera", "my-camera"),
    ])

    dc = MagicMock()
    dc.enumerate_cameras.return_value = devices

    monkeypatch.setattr(mcp_server, "_ensure_daemon_client", lambda: dc)

    result = mcp_server._handle_list_cameras()

    assert len(result) == 1
    entry = result[0]
    assert set(entry.keys()) == {"index", "name", "slug"}
    assert entry["index"] == 2
    assert entry["name"] == "My Camera"
    assert entry["slug"] == "my-camera"


def test_handle_list_cameras_no_local_probe(monkeypatch) -> None:
    """_handle_list_cameras must not call camutil.list_cameras directly."""
    from aprilcam.server import mcp_server

    # If list_cameras were called it would raise (no hardware in tests)
    call_count = {"n": 0}

    def _should_not_be_called(*a, **k):
        call_count["n"] += 1
        return []

    monkeypatch.setattr(
        "aprilcam.camera.camutil.list_cameras", _should_not_be_called
    )

    dc = MagicMock()
    dc.enumerate_cameras.return_value = []
    monkeypatch.setattr(mcp_server, "_ensure_daemon_client", lambda: dc)

    mcp_server._handle_list_cameras()

    assert call_count["n"] == 0, "list_cameras was called directly — should route through daemon"
