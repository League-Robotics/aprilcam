"""Tests for aprilcam.cli.cameras_cli — daemon-based camera listing (014-002).

The cameras CLI now routes enumeration through the daemon (EnumerateCameras RPC)
instead of probing local hardware.  These tests mock the DaemonControl so no
real camera hardware or daemon process is required.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from aprilcam.client.models import CameraDevice
from aprilcam.cli import cameras_cli


def _make_dc(devices: list[CameraDevice]) -> MagicMock:
    """Return a mock DaemonControl whose enumerate_cameras() returns *devices*."""
    dc = MagicMock()
    dc.enumerate_cameras.return_value = devices
    return dc


def test_cameras_cli_lists_devices_from_daemon(monkeypatch, capsys):
    """The listing shows cameras returned by the daemon."""
    devices = [
        CameraDevice(index=0, name="FaceTime HD Camera", slug="facetime-hd-camera"),
        CameraDevice(index=1, name="OV9782 Global Shutter", slug="ov9782-global-shutter"),
    ]
    dc = _make_dc(devices)

    monkeypatch.setattr(
        cameras_cli.AppConfig,
        "load",
        classmethod(lambda cls, *a, **k: type("E", (), {"env": {}})()),
    )

    with patch("aprilcam.cli.cameras_cli.Config") as mock_cfg_cls, \
         patch("aprilcam.cli.cameras_cli.DaemonControl") as mock_dc_cls:
        mock_cfg_cls.load.return_value = MagicMock()
        mock_dc_cls.connect_default.return_value = dc

        rc = cameras_cli.main([])
        out = capsys.readouterr().out

    assert rc == 0
    assert "FaceTime HD Camera" in out
    assert "OV9782 Global Shutter" in out


def test_cameras_cli_empty_list(monkeypatch, capsys):
    """When the daemon returns no devices, the output says none found."""
    dc = _make_dc([])

    monkeypatch.setattr(
        cameras_cli.AppConfig,
        "load",
        classmethod(lambda cls, *a, **k: type("E", (), {"env": {}})()),
    )

    with patch("aprilcam.cli.cameras_cli.Config") as mock_cfg_cls, \
         patch("aprilcam.cli.cameras_cli.DaemonControl") as mock_dc_cls:
        mock_cfg_cls.load.return_value = MagicMock()
        mock_dc_cls.connect_default.return_value = dc

        rc = cameras_cli.main([])
        out = capsys.readouterr().out

    assert rc == 0
    assert "none found" in out.lower()


def test_cameras_cli_details_shows_slug(monkeypatch, capsys):
    """The slug is shown only under --details."""
    devices = [
        CameraDevice(index=0, name="FaceTime HD Camera", slug="facetime-hd-camera"),
    ]
    dc = _make_dc(devices)

    monkeypatch.setattr(
        cameras_cli.AppConfig,
        "load",
        classmethod(lambda cls, *a, **k: type("E", (), {"env": {}})()),
    )

    with patch("aprilcam.cli.cameras_cli.Config") as mock_cfg_cls, \
         patch("aprilcam.cli.cameras_cli.DaemonControl") as mock_dc_cls:
        mock_cfg_cls.load.return_value = MagicMock()
        mock_dc_cls.connect_default.return_value = dc

        rc = cameras_cli.main(["--details"])
        out = capsys.readouterr().out

    assert rc == 0
    assert "facetime-hd-camera" in out


def test_cameras_cli_no_details_no_slug(monkeypatch, capsys):
    """The slug is NOT shown without --details."""
    devices = [
        CameraDevice(index=0, name="FaceTime HD Camera", slug="facetime-hd-camera"),
    ]
    dc = _make_dc(devices)

    monkeypatch.setattr(
        cameras_cli.AppConfig,
        "load",
        classmethod(lambda cls, *a, **k: type("E", (), {"env": {}})()),
    )

    with patch("aprilcam.cli.cameras_cli.Config") as mock_cfg_cls, \
         patch("aprilcam.cli.cameras_cli.DaemonControl") as mock_dc_cls:
        mock_cfg_cls.load.return_value = MagicMock()
        mock_dc_cls.connect_default.return_value = dc

        rc = cameras_cli.main([])
        out = capsys.readouterr().out

    assert rc == 0
    assert "facetime-hd-camera" not in out


def test_cameras_cli_pattern_matches(monkeypatch, capsys):
    """The pattern selector matches on camera name (case-insensitive)."""
    devices = [
        CameraDevice(index=0, name="FaceTime HD Camera", slug="facetime-hd-camera"),
        CameraDevice(index=1, name="OV9782 Global Shutter", slug="ov9782-global-shutter"),
    ]
    dc = _make_dc(devices)

    monkeypatch.setattr(
        cameras_cli.AppConfig,
        "load",
        classmethod(lambda cls, *a, **k: type("E", (), {"env": {}})()),
    )

    with patch("aprilcam.cli.cameras_cli.Config") as mock_cfg_cls, \
         patch("aprilcam.cli.cameras_cli.DaemonControl") as mock_dc_cls:
        mock_cfg_cls.load.return_value = MagicMock()
        mock_dc_cls.connect_default.return_value = dc

        rc = cameras_cli.main(["--pattern", "global shutter"])
        out = capsys.readouterr().out

    assert rc == 0
    assert "global shutter" in out.lower()
    assert "index 1" in out.lower()


def test_cameras_cli_pattern_no_match(monkeypatch, capsys):
    """No match is reported when the pattern doesn't match any camera."""
    devices = [
        CameraDevice(index=0, name="FaceTime HD Camera", slug="facetime-hd-camera"),
    ]
    dc = _make_dc(devices)

    monkeypatch.setattr(
        cameras_cli.AppConfig,
        "load",
        classmethod(lambda cls, *a, **k: type("E", (), {"env": {}})()),
    )

    with patch("aprilcam.cli.cameras_cli.Config") as mock_cfg_cls, \
         patch("aprilcam.cli.cameras_cli.DaemonControl") as mock_dc_cls:
        mock_cfg_cls.load.return_value = MagicMock()
        mock_dc_cls.connect_default.return_value = dc

        rc = cameras_cli.main(["--pattern", "OV9782"])
        out = capsys.readouterr().out

    assert rc == 0
    assert "No camera matched pattern 'OV9782'." in out


def test_cameras_cli_daemon_error(monkeypatch, capsys):
    """When the daemon is unreachable, the CLI returns exit code 1 with an error message."""
    monkeypatch.setattr(
        cameras_cli.AppConfig,
        "load",
        classmethod(lambda cls, *a, **k: type("E", (), {"env": {}})()),
    )

    with patch("aprilcam.cli.cameras_cli.Config") as mock_cfg_cls, \
         patch("aprilcam.cli.cameras_cli.DaemonControl") as mock_dc_cls:
        mock_cfg_cls.load.return_value = MagicMock()
        mock_dc_cls.connect_default.side_effect = RuntimeError("daemon not running")

        rc = cameras_cli.main([])
        out = capsys.readouterr().out

    assert rc == 1
    assert "daemon" in out.lower()
