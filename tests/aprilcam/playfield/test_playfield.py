"""Tests for ``aprilcam.playfield.playfield`` (ticket 022-001).

Headless: ``linked_cameras()`` is exercised against a fake in-memory
``camera_configs`` snapshot -- exactly what ``daemon/service.py``
builds at serve time -- never a real daemon or RPC.
"""

from __future__ import annotations

from aprilcam.config.interfaces import CameraConfig, PlayfieldDefinition
from aprilcam.playfield.playfield import Playfield
from aprilcam.playfield.interfaces import Playfield as PlayfieldBase


class _Definition(PlayfieldDefinition):
    """A minimal ``PlayfieldDefinition`` stand-in -- these tests only
    care about ``Playfield``'s own field/``linked_cameras`` behaviour,
    not document parsing (that's ``test_loader.py``'s job)."""


def _definition(name: str = "main-playfield") -> PlayfieldDefinition:
    return _Definition(name=name, width_cm=100.0, height_cm=80.0)


def test_linked_cameras_is_empty_when_no_camera_config_names_this_playfield():
    playfield = Playfield(name="main-playfield", definition=_definition(), camera_configs=())

    assert playfield.linked_cameras() == ()


def test_linked_cameras_reports_a_camera_config_naming_this_playfield():
    config = CameraConfig(name="arducam-ov9782-usb-camera", playfield="main-playfield", settings={})
    playfield = Playfield(
        name="main-playfield", definition=_definition(), camera_configs=(config,)
    )

    assert playfield.linked_cameras() == ("arducam-ov9782-usb-camera",)


def test_linked_cameras_excludes_a_camera_config_naming_a_different_playfield():
    other = CameraConfig(name="hd-usb-camera", playfield="other-field", settings={})
    mine = CameraConfig(name="arducam-ov9782-usb-camera", playfield="main-playfield", settings={})
    playfield = Playfield(
        name="main-playfield", definition=_definition(), camera_configs=(other, mine)
    )

    assert playfield.linked_cameras() == ("arducam-ov9782-usb-camera",)


def test_linked_cameras_excludes_an_unlinked_camera_config():
    unlinked = CameraConfig(name="hd-usb-camera", playfield=None, settings={})
    playfield = Playfield(name="main-playfield", definition=_definition(), camera_configs=(unlinked,))

    assert playfield.linked_cameras() == ()


def test_linked_cameras_reports_every_matching_camera_in_snapshot_order():
    a = CameraConfig(name="camera-a", playfield="main-playfield", settings={})
    b = CameraConfig(name="camera-b", playfield="main-playfield", settings={})
    playfield = Playfield(name="main-playfield", definition=_definition(), camera_configs=(a, b))

    assert playfield.linked_cameras() == ("camera-a", "camera-b")


def test_playfield_is_a_real_playfield_interfaces_playfield():
    playfield = Playfield(name="main-playfield", definition=_definition(), camera_configs=())

    assert isinstance(playfield, PlayfieldBase)
