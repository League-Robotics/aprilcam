"""Sprint 047 ticket 002: presence watch for held cameras."""

from __future__ import annotations

import os
from typing import FrozenSet, List, Tuple

import pytest

from aprilcam.camera.discovery import CameraDiscovery
from aprilcam.camera.identity import CameraIdentity
from aprilcam.camera.interfaces import PresenceSource
from aprilcam.camera.presence import LinuxSysfsPresenceSource
from aprilcam.camera.registry import CameraRegistry


class FakePresence(PresenceSource):
    def __init__(self, keys=()):
        self.keys: FrozenSet[str] = frozenset(keys)

    def present_keys(self) -> FrozenSet[str]:
        return self.keys


def _registered(tmp_path, key="serial:UC762"):
    reg = CameraRegistry(tmp_path / "cameras")
    slug = reg.resolve(
        CameraIdentity(
            slug="uc762", device_name="UC762", hardware_key=key,
            source="usb_serial", is_fallback=False,
        )
    )
    return reg, slug


def _discovery(reg, src, events, reopen_results=None):
    results = list(reopen_results or [])

    def cb(slug, present):
        events.append((slug, present))
        if present and results:
            return results.pop(0)
        return None

    return CameraDiscovery(
        reg, identity_source=lambda: {}, presence_source=src, on_presence=cb
    )


def test_base_is_not_protocol_and_raises():
    with pytest.raises(NotImplementedError):
        PresenceSource().present_keys()


def test_key_gone_drops_and_reappearing_reopens(tmp_path):
    reg, slug = _registered(tmp_path)
    src = FakePresence({"serial:UC762"})
    events: List[Tuple[str, bool]] = []
    d = _discovery(reg, src, events)
    d._presence_prev = src.present_keys()
    d.presence_poll_once()
    assert events == []
    src.keys = frozenset()
    d.presence_poll_once()
    assert events == [(slug, False)]
    src.keys = frozenset({"serial:UC762"})  # same key, (new node)
    d.presence_poll_once()
    assert events == [(slug, False), (slug, True)]


def test_failed_reopen_is_retried_while_key_present(tmp_path):
    reg, slug = _registered(tmp_path)
    src = FakePresence()
    events: List[Tuple[str, bool]] = []
    d = _discovery(reg, src, events, reopen_results=[False, True])
    src.keys = frozenset({"serial:UC762"})
    d.presence_poll_once()  # key appears -> reopen fails
    d.presence_poll_once()  # retry -> ok
    d.presence_poll_once()  # nothing more
    assert events == [(slug, True), (slug, True)]


def test_uncovered_key_is_never_dropped(tmp_path):
    reg, _ = _registered(tmp_path, key="libcamera:x")
    src = FakePresence({"serial:other"})
    events: List[Tuple[str, bool]] = []
    d = _discovery(reg, src, events)
    d._presence_prev = src.present_keys()
    src.keys = frozenset()
    d.presence_poll_once()
    assert events == []


def test_sysfs_source_reads_serial_and_skips_non_usb(tmp_path):
    root = tmp_path / "v4l"
    usb = tmp_path / "usb" / "3-2"
    (usb / "3-2:1.0").mkdir(parents=True)
    (usb / "idVendor").write_text("0c45")
    (usb / "serial").write_text("UC762\n")
    (root / "video0").mkdir(parents=True)
    os.symlink(usb / "3-2:1.0", root / "video0" / "device")
    (root / "video1").mkdir()  # loopback-like: no device
    src = LinuxSysfsPresenceSource(str(root))
    assert src.present_keys() == frozenset({"serial:UC762"})
    assert LinuxSysfsPresenceSource(str(tmp_path / "missing")).present_keys() == frozenset()


def test_service_callback_drops_and_reopens_held_camera(tmp_path):
    from aprilcam.camera.camera import Camera
    from aprilcam.camera.interfaces import CameraLifecycle
    from aprilcam.daemon.service import DaemonService
    from tests.aprilcam.camera.test_camera import FakeBackend

    reg, slug = _registered(tmp_path)
    backend = FakeBackend()
    cam = Camera(slug, backend)
    cam.open()
    cameras = {slug: cam}
    svc = DaemonService.__new__(DaemonService)
    import threading

    svc._lock = threading.RLock()
    svc._cameras = cameras

    assert svc.on_hardware_presence("unheld", False) is None  # non-held: no-op
    assert cam.lifecycle is CameraLifecycle.PRESENT
    svc.on_hardware_presence(slug, False)
    assert cam.lifecycle is CameraLifecycle.ABSENT
    assert cam.state.present is False
    assert svc.on_hardware_presence(slug, True) is True
    assert cam.lifecycle is CameraLifecycle.PRESENT


# -- ticket 047-008: macOS AVFoundation presence ---------------------------


def test_macos_presence_keys_are_avf_unique_ids():
    from aprilcam.camera.presence import MacosAvfPresenceSource

    paths = ["0x1", "0x2"]
    src = MacosAvfPresenceSource(lambda: list(paths))
    assert src.present_keys() == frozenset({"avf:0x1", "avf:0x2"})
    paths.pop()
    assert src.present_keys() == frozenset({"avf:0x1"})


def test_macos_presence_enumeration_error_propagates_and_watcher_keeps_snapshot(tmp_path):
    from aprilcam.camera.presence import MacosAvfPresenceSource

    reg, slug = _registered(tmp_path, key="avf:0x1")
    state = {"fail": False}

    def enum():
        if state["fail"]:
            raise RuntimeError("AVFoundation hiccup")
        return ["0x1"]

    events: List[Tuple[str, bool]] = []
    d = _discovery(reg, MacosAvfPresenceSource(enum), events)
    d.presence_poll_once()
    events.clear()
    state["fail"] = True
    d.presence_poll_once()
    assert events == []  # a failed listing is not an unplug


def test_macos_presence_unplug_and_replug_notify_held_camera(tmp_path):
    from aprilcam.camera.presence import MacosAvfPresenceSource

    reg, slug = _registered(tmp_path, key="avf:0x1")
    paths = ["0x1"]
    events: List[Tuple[str, bool]] = []
    d = _discovery(reg, MacosAvfPresenceSource(lambda: list(paths)), events)
    d._presence_prev = frozenset({"avf:0x1"})
    paths.clear()
    d.presence_poll_once()
    paths.append("0x1")
    d.presence_poll_once()
    assert events == [(slug, False), (slug, True)]
