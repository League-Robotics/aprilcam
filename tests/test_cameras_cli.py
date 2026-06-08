"""Tests for aprilcam.cli.cameras_cli — registry-merged camera listing (011-003).

These tests mock the live device list and use a temporary registry so no real
camera hardware is required.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aprilcam.camera.camutil import CameraInfo
from aprilcam.camera.registry import CameraRecord, CameraRegistry
from aprilcam.cli import cameras_cli


def _seed_registry(cameras_dir: Path) -> None:
    """Seed a registry with one connected + one disconnected camera record."""
    reg = CameraRegistry(cameras_dir, adopt=False)
    reg.upsert(
        CameraRecord(
            unique_id="avf:CONNECTED",
            enum=1,
            dir="connected-cam",
            name="Connected Cam",
        ),
        save=False,
    )
    reg.upsert(
        CameraRecord(
            unique_id="avf:OFFLINE",
            enum=2,
            dir="offline-cam",
            name="Offline Cam",
        ),
        save=True,
    )


def test_cameras_cli_lists_connected_and_disconnected(tmp_path, monkeypatch, capsys):
    """The listing shows both connected and disconnected cameras.

    The connected camera (unique_id present in the live list) shows its OS
    index; the disconnected one is grayed-out/offline. Both keep their enum
    numbers.
    """
    cameras_dir = tmp_path / "cameras"
    cameras_dir.mkdir(parents=True)
    _seed_registry(cameras_dir)

    # Live list: only the connected camera is present, at OS index 3.
    live = [
        CameraInfo(
            index=3,
            name="Connected Cam (AVFOUNDATION)",
            device_name="Connected Cam",
            unique_id="avf:CONNECTED",
        )
    ]
    monkeypatch.setattr(cameras_cli, "list_cameras", lambda *a, **k: live)

    # Point Config at our temp data dir so the CLI registry resolves there.
    cfg = type("Cfg", (), {"cameras_dir": cameras_dir})()
    monkeypatch.setattr(cameras_cli.Config, "load", classmethod(lambda cls, *a, **k: cfg))
    monkeypatch.setattr(
        cameras_cli.AppConfig, "load", classmethod(lambda cls, *a, **k: type("E", (), {"env": {}})())
    )
    # Force ANSI styling so the dim style is emitted for assertion.
    monkeypatch.setenv("FORCE_COLOR", "1")

    rc = cameras_cli.main(["--quiet"])
    out = capsys.readouterr().out

    assert rc == 0
    # Both cameras appear, with their enum numbers.
    assert "#1" in out
    assert "#2" in out
    # Connected camera shows its OS index.
    assert "[3]" in out
    assert "Connected Cam" in out
    # Disconnected camera is marked offline.
    assert "offline" in out.lower()
    assert "Offline Cam" in out


def test_cameras_cli_registers_new_connected_camera(tmp_path, monkeypatch, capsys):
    """A connected camera not yet in the registry gets a fresh enum number."""
    cameras_dir = tmp_path / "cameras"
    cameras_dir.mkdir(parents=True)

    live = [
        CameraInfo(
            index=0,
            name="Brand New Cam",
            device_name="Brand New Cam",
            unique_id="avf:NEW",
        )
    ]
    monkeypatch.setattr(cameras_cli, "list_cameras", lambda *a, **k: live)

    cfg = type("Cfg", (), {"cameras_dir": cameras_dir})()
    monkeypatch.setattr(cameras_cli.Config, "load", classmethod(lambda cls, *a, **k: cfg))
    monkeypatch.setattr(
        cameras_cli.AppConfig, "load", classmethod(lambda cls, *a, **k: type("E", (), {"env": {}})())
    )

    rc = cameras_cli.main(["--quiet"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[0]" in out
    assert "Brand New Cam" in out

    # The registry now persists the new camera with enum #1.
    reg = CameraRegistry(cameras_dir, adopt=False)
    rec = reg.get("avf:NEW")
    assert rec is not None
    assert rec.enum == 1


def test_cameras_cli_pattern_uses_connected_only(tmp_path, monkeypatch, capsys):
    """The pattern selector operates on connected cameras only."""
    cameras_dir = tmp_path / "cameras"
    cameras_dir.mkdir(parents=True)
    _seed_registry(cameras_dir)

    live = [
        CameraInfo(
            index=3,
            name="Connected Cam",
            device_name="Connected Cam",
            unique_id="avf:CONNECTED",
        )
    ]
    monkeypatch.setattr(cameras_cli, "list_cameras", lambda *a, **k: live)
    cfg = type("Cfg", (), {"cameras_dir": cameras_dir})()
    monkeypatch.setattr(cameras_cli.Config, "load", classmethod(lambda cls, *a, **k: cfg))
    monkeypatch.setattr(
        cameras_cli.AppConfig, "load", classmethod(lambda cls, *a, **k: type("E", (), {"env": {}})())
    )

    # An offline camera name must NOT match the pattern selector.
    rc = cameras_cli.main(["--quiet", "--pattern", "Offline"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "No camera matched pattern 'Offline'." in out

    # A connected camera name matches and suggests its index.
    rc = cameras_cli.main(["--quiet", "--pattern", "Connected"])
    out = capsys.readouterr().out
    assert "Suggested index by pattern 'Connected': 3" in out
