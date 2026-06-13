"""Tests for selecting cameras by stable enumeration number (011-003).

The number printed by ``aprilcam cameras`` (a camera's enumeration number) is
the number a user types into ``aprilcam view`` / ``aprilcam calibrate``. These
tests verify that an integer CLI argument is interpreted as that enumeration
number and resolved — via the registry + live identity table — to the live OS
index before the daemon is asked to open the camera. No real hardware or daemon
is required: ``DaemonControl``, ``resolve_all``, and the registry are mocked.
"""

from __future__ import annotations

import pytest

from aprilcam.camera.identity import CameraIdentity
from aprilcam.camera.registry import CameraRegistry
from aprilcam.cli import calibrate_cli, view_cli


def _identity(uid, name="Cam"):
    return CameraIdentity(
        unique_id=uid, reason="avfoundation_unique_id", is_fallback=False, name=name
    )


def _seed(cameras_dir, uid="avf:0xA", name="Brio 501"):
    """Register a single camera so it receives enumeration #1."""
    reg = CameraRegistry(cameras_dir)
    reg.resolve(_identity(uid, name=name))
    return reg


class _FakeDC:
    """Minimal DaemonControl stand-in recording the index it was asked to open."""

    def __init__(self):
        self.opened_index = None
        self.closed = False

    def open_camera(self, index):
        self.opened_index = index
        # Short-circuit the rest of the command: raising here is enough to
        # assert the index resolution happened correctly.
        raise RuntimeError("stop-after-open")

    def close(self):
        self.closed = True


# -- view --------------------------------------------------------------------


def test_view_resolves_enum_number_to_live_index(tmp_path, monkeypatch, capsys):
    cameras_dir = tmp_path / "cameras"
    cameras_dir.mkdir(parents=True)
    _seed(cameras_dir)  # enum #1 → uid avf:0xA

    cfg = type("Cfg", (), {"cameras_dir": cameras_dir})()
    monkeypatch.setattr(view_cli, "_noop", None, raising=False)

    fake_dc = _FakeDC()
    monkeypatch.setattr(
        "aprilcam.config.Config.load", classmethod(lambda cls, *a, **k: cfg)
    )
    monkeypatch.setattr(
        "aprilcam.client.control.DaemonControl.connect_default",
        classmethod(lambda cls, *a, **k: fake_dc),
    )
    # The camera with enum #1 is currently connected at OS index 7.
    monkeypatch.setattr(
        "aprilcam.camera.identity.resolve_all",
        lambda *a, **k: {7: _identity("avf:0xA", name="Brio 501")},
    )

    rc = view_cli.main(["1"])

    assert rc == 1  # short-circuited via RuntimeError in open_camera
    # The enumeration number #1 resolved to the live OS index 7.
    assert fake_dc.opened_index == 7
    assert fake_dc.closed is True


def test_view_unknown_enum_number_errors(tmp_path, monkeypatch, capsys):
    cameras_dir = tmp_path / "cameras"
    cameras_dir.mkdir(parents=True)
    _seed(cameras_dir)  # only enum #1 exists

    cfg = type("Cfg", (), {"cameras_dir": cameras_dir})()
    fake_dc = _FakeDC()
    monkeypatch.setattr(
        "aprilcam.config.Config.load", classmethod(lambda cls, *a, **k: cfg)
    )
    monkeypatch.setattr(
        "aprilcam.client.control.DaemonControl.connect_default",
        classmethod(lambda cls, *a, **k: fake_dc),
    )
    monkeypatch.setattr(
        "aprilcam.camera.identity.resolve_all",
        lambda *a, **k: {7: _identity("avf:0xA", name="Brio 501")},
    )

    rc = view_cli.main(["9"])  # no camera #9
    err = capsys.readouterr().err

    assert rc == 1
    assert "no camera #9" in err
    assert fake_dc.opened_index is None  # never tried to open
    assert fake_dc.closed is True


# -- view status: enumeration number, not OS index (011-003) -----------------


class _FakeRegistry:
    """Registry stand-in exposing only ``records()`` for ``_display_enum``."""

    def __init__(self, records):
        self._records = list(records)

    def records(self):
        return list(self._records)


def _record(dir_name, enum):
    return type("Rec", (), {"dir": dir_name, "enum": enum})()


def test_display_enum_numeric_selection_returns_that_enum():
    # When the user selected by number, that number is shown directly — the
    # registry is never consulted.
    reg = _FakeRegistry([_record("arducam-ov9782", 3)])
    assert view_cli._display_enum(reg, "arducam-ov9782", 3) == 3


def test_display_enum_name_selection_looks_up_by_dir():
    # Name selection: enum_no is None, so look up the record whose dir matches
    # the daemon-returned cam_name and return its enum.
    reg = _FakeRegistry(
        [_record("brio-501", 1), _record("arducam-ov9782", 3)]
    )
    assert view_cli._display_enum(reg, "arducam-ov9782", None) == 3


def test_display_enum_unknown_dir_returns_none():
    reg = _FakeRegistry([_record("brio-501", 1)])
    assert view_cli._display_enum(reg, "no-such-dir", None) is None


def test_display_enum_missing_cam_name_returns_none():
    reg = _FakeRegistry([_record("brio-501", 1)])
    assert view_cli._display_enum(reg, None, None) is None


# -- calibrate ---------------------------------------------------------------


def test_calibrate_numeric_spec_is_enumeration_number(tmp_path, monkeypatch, capsys):
    cameras_dir = tmp_path / "cameras"
    cameras_dir.mkdir(parents=True)
    _seed(cameras_dir)  # enum #1 → uid avf:0xA

    cfg = type(
        "Cfg",
        (),
        {"data_dir": tmp_path, "cameras_dir": cameras_dir},
    )()
    fake_dc = _FakeDC()
    monkeypatch.setattr(
        "aprilcam.config.Config.load", classmethod(lambda cls, *a, **k: cfg)
    )
    monkeypatch.setattr(
        "aprilcam.client.control.DaemonControl.connect_default",
        classmethod(lambda cls, *a, **k: fake_dc),
    )
    # Live device list (camutil) — the camera is at OS index 4.
    from aprilcam.camera.camutil import CameraInfo

    monkeypatch.setattr(
        calibrate_cli,
        "list_cameras",
        lambda *a, **k: [CameraInfo(index=4, name="Brio 501", device_name="Brio 501")],
    )
    # Enumeration #1 is connected at OS index 4.
    monkeypatch.setattr(
        "aprilcam.camera.identity.resolve_all",
        lambda *a, **k: {4: _identity("avf:0xA", name="Brio 501")},
    )

    rc = calibrate_cli.main(["1"])
    out = capsys.readouterr().out

    # Calibration itself fails fast (fake daemon open raises), but the camera
    # list resolved the enumeration number #1 → OS index 4 and labelled it.
    assert "[4] Brio 501" in out
    # The old volatile-OS-index warning is gone.
    assert "indices change" not in out


def test_calibrate_unknown_enumeration_number_skips(tmp_path, monkeypatch, capsys):
    cameras_dir = tmp_path / "cameras"
    cameras_dir.mkdir(parents=True)
    _seed(cameras_dir)  # only enum #1 exists

    cfg = type(
        "Cfg",
        (),
        {"data_dir": tmp_path, "cameras_dir": cameras_dir},
    )()
    fake_dc = _FakeDC()
    monkeypatch.setattr(
        "aprilcam.config.Config.load", classmethod(lambda cls, *a, **k: cfg)
    )
    monkeypatch.setattr(
        "aprilcam.client.control.DaemonControl.connect_default",
        classmethod(lambda cls, *a, **k: fake_dc),
    )
    from aprilcam.camera.camutil import CameraInfo

    monkeypatch.setattr(
        calibrate_cli,
        "list_cameras",
        lambda *a, **k: [CameraInfo(index=4, name="Brio 501", device_name="Brio 501")],
    )
    monkeypatch.setattr(
        "aprilcam.camera.identity.resolve_all",
        lambda *a, **k: {4: _identity("avf:0xA", name="Brio 501")},
    )

    rc = calibrate_cli.main(["9"])  # no camera #9
    out = capsys.readouterr().out

    assert rc == 1  # nothing to calibrate
    assert "no camera #9" in out
