"""Tests for ``aprilcam.camera.identity`` (headless, no hardware).

Covers ticket 020-002's AC1: ``CameraIdentity.slug`` / ``.device_name``
produce the documented readable slugs (e.g. ``arducam-ov9782-usb-camera``)
from the fallback chain, exercised against recorded
``cv2-enumerate-cameras`` / ``system_profiler`` snapshot fixtures — never
real hardware or a real subprocess call.
"""

from __future__ import annotations

import os
import sys
import types

import pytest

from aprilcam.camera import identity
from aprilcam.camera.backends.libcamera import LibcameraDevice
from aprilcam.camera.identity import (
    CameraIdentity,
    _EnumEntry,
    _LibcameraEntry,
    _ProfilerEntry,
    device_name_slug,
    is_pi_internal_node,
    name_resolution_slug,
    resolve_all,
    resolve_identity,
)
from aprilcam.camera.interfaces import CameraIdentity as CameraIdentityProtocol


# ---------------------------------------------------------------------------
# Fallback chain, tier by tier
# ---------------------------------------------------------------------------


def test_avfoundation_unique_id_is_primary():
    enum = {
        0: _EnumEntry(
            index=0, name="Brio 501", vid=1133, pid=2374, path="0x1100000046d0946"
        )
    }
    prof = {
        "Brio 501": _ProfilerEntry(
            name="Brio 501", unique_id="0x1100000046d0946", vid=1133, pid=2374
        )
    }
    r = resolve_identity(0, enum_entries=enum, profiler_entries=prof)
    assert r.source == "avfoundation_unique_id"
    assert r.is_fallback is False
    assert r.hardware_key == "avf:0x1100000046d0946"
    assert r.slug == "brio-501"
    assert r.device_name == "Brio 501"


def test_usb_serial_used_when_no_avf_unique():
    enum = {0: _EnumEntry(index=0, name="Some Cam", vid=10, pid=20, path="loc-1")}
    prof = {"Some Cam": _ProfilerEntry(name="Some Cam", serial="SN12345")}
    r = resolve_identity(0, enum_entries=enum, profiler_entries=prof)
    assert r.source == "usb_serial"
    assert r.is_fallback is False
    assert r.hardware_key == "serial:SN12345"


def test_vid_pid_location_path(monkeypatch):
    """On non-darwin platforms, ``location`` is not guaranteed to be a
    hardware-stable id (a Linux ``path`` is a ``/dev/videoN`` node — see
    ``identity.py``'s "Root-cause fix" section), so this tier is genuinely
    reachable and meaningful there. Explicitly non-darwin so this test
    does not collide with the darwin-only determinism shortcut exercised
    in ``test_darwin_location_used_as_primary_identity_even_without_profiler_match``
    below."""
    monkeypatch.setattr(sys, "platform", "linux")
    enum = {
        0: _EnumEntry(index=0, name="Generic Cam", vid=3141, pid=25446, path="0xABC123")
    }
    r = resolve_identity(0, enum_entries=enum, profiler_entries={})
    assert r.source == "vid_pid_location"
    assert r.is_fallback is False
    assert r.hardware_key == "vidpid:0c45:6366@0xabc123"


def test_usb_location_path_fallback_when_only_location(monkeypatch):
    """Non-darwin, per the note on ``test_vid_pid_location_path`` above."""
    monkeypatch.setattr(sys, "platform", "linux")
    enum = {0: _EnumEntry(index=0, name="No IDs Cam", vid=None, pid=None, path="0xDEAD")}
    r = resolve_identity(0, enum_entries=enum, profiler_entries={})
    assert r.source == "usb_location_path"
    assert r.is_fallback is True
    assert r.hardware_key == "loc:0xdead"


def test_name_resolution_slug_last_resort():
    enum = {0: _EnumEntry(index=0, name="Webcam", vid=None, pid=None, path=None)}
    r = resolve_identity(0, width=1920, height=1080, enum_entries=enum, profiler_entries={})
    assert r.source == "name_resolution_slug"
    assert r.is_fallback is True
    assert r.hardware_key == "name:webcam-1920x1080"
    # The readable slug never carries the resolution -- only the
    # hardware key's last-resort tier does.
    assert r.slug == "webcam"


def test_no_enum_entry_falls_back_to_name_slug():
    r = resolve_identity(5, name="Mystery Cam", enum_entries={}, profiler_entries={})
    assert r.source == "name_resolution_slug"
    assert r.hardware_key == "name:mystery-cam"
    assert r.slug == "mystery-cam"


def test_two_identical_name_devices_get_distinct_hardware_keys():
    """Same device name, different USB ports -> distinct hardware keys,
    though both propose the same readable slug (the registry's job, not
    identity's, to disambiguate -- see test_registry.py)."""
    enum = {
        0: _EnumEntry(
            index=0,
            name="Arducam OV9782 USB Camera",
            vid=3141,
            pid=25446,
            path="0xAAAA",
        ),
        1: _EnumEntry(
            index=1,
            name="Arducam OV9782 USB Camera",
            vid=3141,
            pid=25446,
            path="0xBBBB",
        ),
    }
    r0 = resolve_identity(0, enum_entries=enum, profiler_entries={})
    r1 = resolve_identity(1, enum_entries=enum, profiler_entries={})
    assert r0.hardware_key != r1.hardware_key
    assert r0.slug == r1.slug == "arducam-ov9782-usb-camera"


# ---------------------------------------------------------------------------
# Root-cause regression: darwin location determinism (ticket 021-007)
#
# A production registry on this machine was found to have every physical
# camera registered twice -- once under `avf:<id>`, once under
# `vidpid:<vid>:<pid>@<id>` for the *same* id -- because system_profiler's
# device-name match is a flaky gate on which tier resolution used, even
# though cv2-enumerate-cameras' own `path` already *is* that id on macOS
# (cv2_enumerate_cameras/macos_backend.py: `path=d.uniqueID()`). These
# tests pin the fix: the tier used must not depend on whether the
# system_profiler match happened to land this run.
# ---------------------------------------------------------------------------


def test_darwin_location_used_as_primary_identity_even_without_profiler_match(
    monkeypatch,
):
    """No system_profiler entry at all (simulates the observed failure
    mode) still resolves to the avfoundation_unique_id tier on darwin,
    using cv2's own `path` directly."""
    monkeypatch.setattr(sys, "platform", "darwin")
    enum = {
        0: _EnumEntry(
            index=0, name="Brio 501", vid=1133, pid=2374, path="0x1100000046d0946"
        )
    }
    r = resolve_identity(0, enum_entries=enum, profiler_entries={})
    assert r.hardware_key == "avf:0x1100000046d0946"
    assert r.source == "avfoundation_unique_id"
    assert r.is_fallback is False


def test_darwin_identity_deterministic_regardless_of_profiler_match(monkeypatch):
    """The same camera, resolved once with a successful system_profiler
    match and once with none at all, must produce the *same*
    hardware_key -- this is the property whose absence duplicated every
    camera in the registry found on this machine."""
    monkeypatch.setattr(sys, "platform", "darwin")
    enum = {
        0: _EnumEntry(
            index=0, name="Brio 501", vid=1133, pid=2374, path="0x1100000046d0946"
        )
    }
    prof = {
        "Brio 501": _ProfilerEntry(
            name="Brio 501", unique_id="0x1100000046d0946", vid=1133, pid=2374
        )
    }
    with_profiler = resolve_identity(0, enum_entries=enum, profiler_entries=prof)
    without_profiler = resolve_identity(0, enum_entries=enum, profiler_entries={})
    assert with_profiler.hardware_key == without_profiler.hardware_key == "avf:0x1100000046d0946"
    assert with_profiler.source == without_profiler.source == "avfoundation_unique_id"


def test_darwin_location_shortcut_does_not_apply_on_other_platforms(monkeypatch):
    """The same enum data on a non-darwin platform must NOT get the
    location-as-uniqueID shortcut -- there, `path` is not documented (or
    guaranteed) to be a hardware-stable id (see linux_backend.py's
    `/dev/videoN` node path), so it stays a genuine, lower-trust
    fallback tier."""
    monkeypatch.setattr(sys, "platform", "linux")
    enum = {
        0: _EnumEntry(
            index=0, name="Brio 501", vid=1133, pid=2374, path="0x1100000046d0946"
        )
    }
    r = resolve_identity(0, enum_entries=enum, profiler_entries={})
    assert r.source == "vid_pid_location"
    assert r.hardware_key != "avf:0x1100000046d0946"


def test_resolve_all_resolves_every_index():
    """A cv2-only host (no libcamera loopbacks) resolves exactly as it
    did before ticket 042-001's merge fix -- the union's other
    degenerate case."""
    enum = {
        0: _EnumEntry(index=0, name="Cam A", vid=1, pid=2, path="0x01"),
        2: _EnumEntry(index=2, name="Cam B", vid=3, pid=4, path="0x02"),
    }
    out = resolve_all(enum_entries=enum, profiler_entries={}, libcamera_entries={})
    assert set(out.keys()) == {0, 2}
    assert all(isinstance(v, CameraIdentity) for v in out.values())
    assert out[0].hardware_key != out[2].hardware_key


# ---------------------------------------------------------------------------
# Never raises
# ---------------------------------------------------------------------------


def test_never_raises_on_non_darwin_platform(monkeypatch):
    """On a non-darwin platform with no sources, the resolver still returns."""
    monkeypatch.setattr(sys, "platform", "linux")
    # _macos_camera_profiles short-circuits to {} on non-darwin.
    assert identity._macos_camera_profiles() == {}
    r = resolve_identity(0, name="Linux Cam", enum_entries={}, profiler_entries=None)
    assert r.hardware_key  # non-empty
    assert r.source == "name_resolution_slug"


def test_never_raises_when_system_profiler_missing(monkeypatch):
    """A missing/failing system_profiler degrades to an empty table, no raise."""
    monkeypatch.setattr(sys, "platform", "darwin")

    def boom(*_a, **_k):
        raise FileNotFoundError("system_profiler not found")

    monkeypatch.setattr(identity.subprocess, "run", boom)
    # _run_system_profiler swallows the error -> "".
    assert identity._run_system_profiler("SPCameraDataType") == ""
    assert identity._macos_camera_profiles() == {}
    r = resolve_identity(0, name="Cam", enum_entries={}, profiler_entries=None)
    assert r.hardware_key == "name:cam"


def test_parse_sp_camera_real_world_shape():
    text = """Camera:

    Global Shutter Camera:

      Model ID: UVC Camera VendorID_13028 ProductID_5553
      Unique ID: 0x211411132e415b1

    Brio 501:

      Model ID: UVC Camera VendorID_1133 ProductID_2374
      Unique ID: 0x1100000046d0946
"""
    entries = identity._parse_sp_camera(text)
    assert set(entries.keys()) == {"Global Shutter Camera", "Brio 501"}
    gs = entries["Global Shutter Camera"]
    assert gs.unique_id == "0x211411132e415b1"
    assert gs.vid == 13028 and gs.pid == 5553


def test_name_resolution_slug_helpers():
    assert name_resolution_slug("Brio 501", 1920, 1080) == "brio-501-1920x1080"
    assert name_resolution_slug("Brio 501") == "brio-501"
    assert name_resolution_slug(None) == "camera"
    assert name_resolution_slug("") == "camera"


def test_device_name_slug_never_carries_resolution():
    assert device_name_slug("Brio 501") == "brio-501"
    assert device_name_slug(None) == "camera"
    assert device_name_slug("") == "camera"


# ---------------------------------------------------------------------------
# device_name_slug's no-all-digits invariant (ticket 021-002) -- daemon-side
# number/slug addressing depends on a slug never being confusable with a
# persistent registry number.
# ---------------------------------------------------------------------------


def test_device_name_slug_guards_a_bare_numeric_device_name():
    assert device_name_slug("123") == "camera-123"
    assert not device_name_slug("123").isdigit()


def test_device_name_slug_guards_a_numeric_looking_device_name_with_symbols():
    # "4-2" slugifies to "4-2" -- already non-digit (contains '-'), so
    # the guard should not touch it (no double-prefixing).
    assert device_name_slug("4-2") == "4-2"


def test_name_resolution_slug_last_resort_tier_never_all_digits():
    """The last-resort hardware-key tier is built from
    ``device_name_slug`` and so inherits its guard whenever no
    resolution is supplied."""
    assert name_resolution_slug("123") == "camera-123"
    assert not name_resolution_slug("123").isdigit()


def test_resolve_identity_last_resort_tier_with_numeric_device_name():
    """End-to-end through the actual fallback chain: a camera whose OS-
    reported name is a bare number must not resolve to an all-digits
    ``.slug`` -- that would collide with number-addressing in
    ``daemon/service.py``'s ``_camera_for``."""
    r = resolve_identity(0, name="123", enum_entries={}, profiler_entries={})
    assert r.source == "name_resolution_slug"
    assert r.slug == "camera-123"
    assert not r.slug.isdigit()


# ---------------------------------------------------------------------------
# This Mac's actual cameras (UC-14.S-1) -- recorded enumeration snapshot
# ---------------------------------------------------------------------------


def test_this_macs_arducam_resolves_to_documented_slug():
    enum = {
        0: _EnumEntry(
            index=0,
            name="Arducam OV9782 USB Camera",
            vid=0x0C45,
            pid=0x6366,
            path="0x21141400c456366",
        ),
    }
    prof = {
        "Arducam OV9782 USB Camera": _ProfilerEntry(
            name="Arducam OV9782 USB Camera",
            unique_id="0x21141400c456366",
            vid=0x0C45,
            pid=0x6366,
        )
    }
    r = resolve_identity(0, enum_entries=enum, profiler_entries=prof)
    assert r.slug == "arducam-ov9782-usb-camera"
    assert r.device_name == "Arducam OV9782 USB Camera"


def test_this_macs_hd_camera_resolves_to_documented_slug():
    enum = {0: _EnumEntry(index=0, name="HD USB Camera", vid=None, pid=None, path=None)}
    r = resolve_identity(0, enum_entries=enum, profiler_entries={})
    assert r.slug == "hd-usb-camera"


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_satisfies_camera_identity_protocol():
    r = resolve_identity(0, name="Cam", enum_entries={}, profiler_entries={})
    assert isinstance(r, CameraIdentityProtocol)


# ---------------------------------------------------------------------------
# libcamera tier -- inert today, but the injection point works (ticket 003
# wires the real probe in without changing this module's signatures)
# ---------------------------------------------------------------------------


def test_probe_libcamera_default_is_empty():
    assert identity._probe_libcamera() == {}


def test_libcamera_entry_wins_when_present():
    lc = {0: _LibcameraEntry(camera_id="/base/soc/i2c0mux/i2c@1/imx219@10", name="IMX219")}
    r = resolve_identity(0, libcamera_entries=lc, enum_entries={}, profiler_entries={})
    assert r.source == "libcamera_id"
    assert r.is_fallback is False
    assert r.hardware_key == "libcamera:/base/soc/i2c0mux/i2c@1/imx219@10"
    assert r.slug == "imx219"


def test_resolve_all_loopback_only_host_unchanged():
    """A loopback-only host (no cv2 entries) resolves exactly as it did
    before ticket 042-001's merge fix -- the degenerate case of the
    union over one empty source. ``enum_entries``/``profiler_entries``
    are passed explicitly (rather than left to default) so this test
    does not depend on this machine's real cv2 enumeration."""
    lc = {0: _LibcameraEntry(camera_id="/base/cam0", name="Cam0")}
    out = resolve_all(libcamera_entries=lc, enum_entries={}, profiler_entries={})
    assert set(out.keys()) == {0}
    assert out[0].source == "libcamera_id"


# ---------------------------------------------------------------------------
# Ticket 041-001: Linux backend-offset dedup + Pi-internal-node filter
# ---------------------------------------------------------------------------


def _fake_cam(index, name, vid=None, pid=None, path=None):
    """A stand-in for ``cv2_enumerate_cameras.camera_info.CameraInfo`` --
    ``_enumerate_cv2`` only ever reads these five attributes via
    ``getattr``, so a plain namespace is enough."""
    return types.SimpleNamespace(index=index, name=name, vid=vid, pid=pid, path=path)


def _write_driver_symlink(sys_root, video_index: int, driver: str) -> None:
    """Fake the real ``/sys/class/video4linux/videoN/device/driver``
    shape: a symlink whose basename is the driver name. The link
    target need not exist on disk -- ``os.path.realpath`` resolves the
    symlink text itself, exactly as it does for the real (also often
    dangling-until-resolved) ``/sys`` symlink."""
    device_dir = sys_root / f"video{video_index}" / "device"
    device_dir.mkdir(parents=True, exist_ok=True)
    os.symlink(f"../../../bus/platform/drivers/{driver}", device_dir / "driver")


def _patch_enumerate_cameras(monkeypatch, cams):
    """Patch the vendored ``cv2_enumerate_cameras.enumerate_cameras``
    itself (not a local alias) -- ``_enumerate_cv2``'s ``from
    cv2_enumerate_cameras import enumerate_cameras`` is a *local*
    import executed at call time, so patching the module attribute
    beforehand is picked up exactly like patching any other module
    attribute a function reads through ``import`` at call time."""
    import cv2_enumerate_cameras

    monkeypatch.setattr(cv2_enumerate_cameras, "enumerate_cameras", lambda: cams)


class TestLinuxBackendOffsetDedup:
    def test_v4l2_and_gstreamer_entries_for_one_device_collapse_to_one(
        self, monkeypatch
    ):
        """Root-cause regression: on Linux, ``enumerate_cameras()``
        (called with the default ``CAP_ANY``) yields one ``CameraInfo``
        per backend in ``linux_backend.supported_backends =
        (CAP_GSTREAMER=1800, CAP_V4L2=200)`` for the same physical
        ``/dev/video5`` -- combined indices 205 and 1805. Both must
        recover to real index 5 and collapse to a single dict entry."""
        monkeypatch.setattr(sys, "platform", "linux")
        cams = [
            _fake_cam(205, "USB Camera", vid=0x0C45, pid=0x6366, path="/dev/video5"),
            _fake_cam(1805, "USB Camera", vid=0x0C45, pid=0x6366, path="/dev/video5"),
        ]
        _patch_enumerate_cameras(monkeypatch, cams)

        out = identity._enumerate_cv2()

        assert set(out.keys()) == {5}
        assert out[5].name == "USB Camera"
        assert out[5].path == "/dev/video5"

    def test_macos_single_backend_offset_path_unchanged(self, monkeypatch):
        """macOS has exactly one backend (``CAP_AVFOUNDATION``), so the
        pre-existing offset-stripping behaviour must be unaffected by
        the generalization to multiple offsets, and the Linux-only
        driver filter must never run here."""
        monkeypatch.setattr(sys, "platform", "darwin")
        cams = [_fake_cam(1200, "Brio 501", vid=1133, pid=2374, path="0xabc")]
        _patch_enumerate_cameras(monkeypatch, cams)

        def _boom(*_a, **_k):
            raise AssertionError("driver /sys read must not happen on darwin")

        monkeypatch.setattr(identity, "_read_v4l2_driver", _boom)

        out = identity._enumerate_cv2()

        assert set(out.keys()) == {0}
        assert out[0].name == "Brio 501"

    def test_no_offset_applies_index_passes_through_unchanged(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        cams = [_fake_cam(3, "USB Camera", path="/dev/video3")]
        _patch_enumerate_cameras(monkeypatch, cams)

        out = identity._enumerate_cv2()

        assert set(out.keys()) == {3}


class TestPiInternalNodeFilter:
    """Pure predicate tests -- no ``/sys`` I/O, per the classifier's
    documented fail-open contract."""

    @pytest.mark.parametrize(
        "driver", ["rp1-cfe", "pispbe", "rpivid", "bcm2835-codec"]
    )
    def test_known_internal_driver_excluded(self, driver):
        assert is_pi_internal_node(driver, "irrelevant-name") is True

    def test_uvcvideo_driver_never_excluded(self):
        assert is_pi_internal_node("uvcvideo", "USB Camera") is False

    def test_unknown_driver_kept_fail_open(self):
        assert is_pi_internal_node("some-future-driver", "rp1-cfe-fe_image0") is False

    def test_unreadable_driver_backstop_matches_rp1_cfe_prefix(self):
        assert is_pi_internal_node(None, "rp1-cfe-csi2-ch0") is True

    def test_unreadable_driver_backstop_matches_pispbe_prefix(self):
        assert is_pi_internal_node(None, "pispbe-input") is True

    def test_unreadable_driver_and_unmatched_name_kept_fail_open(self):
        assert is_pi_internal_node(None, "aprilcam-imx296-88000") is False

    def test_unreadable_driver_and_no_name_kept_fail_open(self):
        assert is_pi_internal_node(None, None) is False

    def test_unreadable_driver_and_empty_name_kept_fail_open(self):
        assert is_pi_internal_node(None, "") is False


class TestReadV4l2Driver:
    """``/sys`` reading -- fake trees only, per ``list_loopback_cameras``'s
    established ``tmp_path`` convention (``test_backends.py``)."""

    def test_reads_driver_basename_from_symlink(self, tmp_path):
        _write_driver_symlink(tmp_path, 0, "rp1-cfe")
        assert identity._read_v4l2_driver(0, sys_root=str(tmp_path)) == "rp1-cfe"

    def test_missing_device_directory_reads_as_unknown(self, tmp_path):
        """The real shape of a v4l2loopback node, verified live on
        vidar.local: no ``device/`` subdirectory at all (it is a
        virtual node with no backing hardware)."""
        (tmp_path / "video70").mkdir(parents=True)
        assert identity._read_v4l2_driver(70, sys_root=str(tmp_path)) is None

    def test_missing_video_node_entirely_reads_as_unknown(self, tmp_path):
        assert identity._read_v4l2_driver(99, sys_root=str(tmp_path)) is None


class TestEnumerateCv2LinuxFiltering:
    """End-to-end through ``_enumerate_cv2()`` with a faked ``/sys``
    tree and faked ``cv2_enumerate_cameras`` results -- no real
    hardware, no real vendored-library call."""

    def _run(self, monkeypatch, tmp_path, cams):
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(identity, "_SYS_V4L_ROOT", str(tmp_path))
        _patch_enumerate_cameras(monkeypatch, cams)
        return identity._enumerate_cv2()

    def test_rp1_cfe_and_pispbe_and_rpivid_nodes_excluded(self, monkeypatch, tmp_path):
        _write_driver_symlink(tmp_path, 0, "rp1-cfe")
        _write_driver_symlink(tmp_path, 20, "pispbe")
        _write_driver_symlink(tmp_path, 19, "rpivid")
        cams = [
            _fake_cam(0, "rp1-cfe-csi2_ch0", path="/dev/video0"),
            _fake_cam(20, "pispbe-input", path="/dev/video20"),
            _fake_cam(19, "rpivid", path="/dev/video19"),
        ]

        out = self._run(monkeypatch, tmp_path, cams)

        assert out == {}

    def test_real_uvc_camera_kept(self, monkeypatch, tmp_path):
        _write_driver_symlink(tmp_path, 5, "uvcvideo")
        cams = [_fake_cam(5, "Logitech Webcam", path="/dev/video5")]

        out = self._run(monkeypatch, tmp_path, cams)

        assert set(out.keys()) == {5}
        assert out[5].name == "Logitech Webcam"

    def test_unrecognized_driver_kept_fail_open(self, monkeypatch, tmp_path):
        _write_driver_symlink(tmp_path, 7, "some-future-sensor-driver")
        cams = [_fake_cam(7, "Future Camera", path="/dev/video7")]

        out = self._run(monkeypatch, tmp_path, cams)

        assert set(out.keys()) == {7}

    def test_unreadable_driver_symlink_falls_back_to_name_prefix(
        self, monkeypatch, tmp_path
    ):
        """No ``/sys`` fixture written at all for this node -- the
        driver symlink is unreadable, so the name-prefix backstop
        alone must exclude it."""
        cams = [_fake_cam(1, "rp1-cfe-csi2-ch0", path="/dev/video1")]

        out = self._run(monkeypatch, tmp_path, cams)

        assert out == {}

    def test_loopback_nodes_kept(self, monkeypatch, tmp_path):
        """Real vidar.local evidence: ``aprilcam-*`` v4l2loopback nodes
        have no ``device/`` subdirectory at all, so the driver reads as
        unknown and the name does not match the backstop prefixes --
        the loopback path (sprint 040) must survive this filter."""
        cams = [
            _fake_cam(70, "aprilcam-imx296-88000", path="/dev/video70"),
            _fake_cam(71, "aprilcam-imx296-80000", path="/dev/video71"),
        ]

        out = self._run(monkeypatch, tmp_path, cams)

        assert set(out.keys()) == {70, 71}

    def test_dedup_and_filter_together(self, monkeypatch, tmp_path):
        """The full vidar.local shape in one pass: a real camera
        enumerated under both Linux backends (dedup) alongside an
        internal node that must never survive (filter)."""
        _write_driver_symlink(tmp_path, 5, "uvcvideo")
        _write_driver_symlink(tmp_path, 0, "rp1-cfe")
        cams = [
            _fake_cam(205, "Real USB Camera", path="/dev/video5"),
            _fake_cam(1805, "Real USB Camera", path="/dev/video5"),
            _fake_cam(1800, "rp1-cfe-csi2_ch0", path="/dev/video0"),
        ]

        out = self._run(monkeypatch, tmp_path, cams)

        assert set(out.keys()) == {5}
        assert out[5].name == "Real USB Camera"


# ---------------------------------------------------------------------------
# Ticket 042-001: merge libcamera and cv2 enumeration on one index domain
# ---------------------------------------------------------------------------


def _libcamera_device(
    *, position: int = 0, device_index: int = 70, camera_id: str = "aprilcam-imx296-88000"
) -> LibcameraDevice:
    return LibcameraDevice(
        position=position,
        slug="imx296-88000",
        model="imx296",
        device_index=device_index,
        camera_id=camera_id,
    )


class TestProbeLibcameraKeying:
    """``_probe_libcamera()`` keys its dict by ``device_index`` (the real
    ``/dev/videoN`` number), not ``LibcameraDevice.position`` (its
    0-based enumeration order) -- the two can differ, exactly as they
    do live on vidar.local (``aprilcam-imx296-88000`` is enumeration
    position 0 but device index 70)."""

    def test_keys_by_device_index_not_position(self, monkeypatch):
        device = _libcamera_device(position=0, device_index=70)
        monkeypatch.setattr(
            "aprilcam.camera.backends.libcamera.list_loopback_cameras",
            lambda: [device],
        )
        out = identity._probe_libcamera()
        assert set(out.keys()) == {70}
        assert out[70].camera_id == "aprilcam-imx296-88000"

    def test_two_loopbacks_key_by_their_own_device_index(self, monkeypatch):
        """The two real vidar.local loopbacks: imx296-88000 at position
        0/device_index 70, imx296-80000 at position 1/device_index 71."""
        devices = [
            _libcamera_device(position=0, device_index=70, camera_id="aprilcam-imx296-88000"),
            _libcamera_device(position=1, device_index=71, camera_id="aprilcam-imx296-80000"),
        ]
        monkeypatch.setattr(
            "aprilcam.camera.backends.libcamera.list_loopback_cameras",
            lambda: devices,
        )
        out = identity._probe_libcamera()
        assert set(out.keys()) == {70, 71}
        assert out[70].camera_id == "aprilcam-imx296-88000"
        assert out[71].camera_id == "aprilcam-imx296-80000"


class TestResolveAllMerge:
    """``resolve_all()`` merges both enumeration sources by taking the
    union of their keys, instead of returning only the libcamera-
    resolved identities whenever any loopback is present."""

    def test_mixed_host_resolves_both_sources_each_once(self):
        """The vidar.local shape: a USB camera at cv2 index 0 alongside
        a working CSI loopback at device index 70 -- both must be
        resolved, each exactly once."""
        enum = {
            0: _EnumEntry(
                index=0, name="Streaming Camera", vid=0x3141, pid=0x6363, path="/dev/video0"
            ),
        }
        lc = {70: _LibcameraEntry(camera_id="aprilcam-imx296-88000", name="imx296")}

        out = resolve_all(enum_entries=enum, profiler_entries={}, libcamera_entries=lc)

        assert set(out.keys()) == {0, 70}
        assert out[70].hardware_key == "libcamera:aprilcam-imx296-88000"
        assert out[70].source == "libcamera_id"
        assert out[0].source != "libcamera_id"
        assert out[0].device_name == "Streaming Camera"

    def test_loopback_present_in_both_sources_resolves_once_under_libcamera_identity(
        self,
    ):
        """A loopback device also enumerated by cv2 under the *same*
        ``/dev/videoN`` (exactly what vidar.local shows -- cv2 sees the
        v4l2loopback node too) must resolve exactly once, as its
        libcamera identity -- never a second ``loc:``/``vidpid:``
        record for the same physical device."""
        enum = {
            70: _EnumEntry(index=70, name="aprilcam-imx296-88000", path="/dev/video70"),
        }
        lc = {70: _LibcameraEntry(camera_id="aprilcam-imx296-88000", name="imx296")}

        out = resolve_all(enum_entries=enum, profiler_entries={}, libcamera_entries=lc)

        assert set(out.keys()) == {70}
        assert out[70].hardware_key == "libcamera:aprilcam-imx296-88000"
        assert out[70].source == "libcamera_id"

    def test_macos_cv2_only_host_unchanged(self, monkeypatch):
        """macOS has no libcamera loopbacks at all -- the union
        degenerates to the cv2 source alone, unchanged from before this
        fix."""
        monkeypatch.setattr(sys, "platform", "darwin")
        enum = {
            0: _EnumEntry(index=0, name="Brio 501", vid=1133, pid=2374, path="0xabc"),
        }
        out = resolve_all(enum_entries=enum, profiler_entries={}, libcamera_entries={})
        assert set(out.keys()) == {0}
        assert out[0].source != "libcamera_id"

    def test_position_device_index_collision_no_longer_misidentifies(self, monkeypatch):
        """Regression for the latent collision ticket 042-001 fixes: a
        loopback's enumeration *position* (0) numerically coincides
        with an unrelated USB camera's real cv2 index (0), while the
        loopback's actual device is ``/dev/video70``. Before the
        re-keying fix, ``_probe_libcamera()`` keyed its dict by
        position, so ``resolve_identity(0, ...)`` -- exactly what
        ``OpenCVBackend``/``WorkerBackend`` call as their post-open
        anti-substitution check -- would have wrongly returned the
        loopback's identity for the USB camera's index. After keying by
        ``device_index``, indices 0 and 70 no longer share a key."""
        monkeypatch.setattr(sys, "platform", "linux")
        device = _libcamera_device(position=0, device_index=70)
        monkeypatch.setattr(
            "aprilcam.camera.backends.libcamera.list_loopback_cameras",
            lambda: [device],
        )
        libcamera_entries = identity._probe_libcamera()
        assert set(libcamera_entries.keys()) == {70}  # not {0}

        enum = {
            0: _EnumEntry(
                index=0, name="Streaming Camera", vid=0x3141, pid=0x6363, path="/dev/video0"
            ),
        }

        # The exact call OpenCVBackend.open()/WorkerBackend make post-open.
        observed = resolve_identity(
            0, enum_entries=enum, profiler_entries={}, libcamera_entries=libcamera_entries
        )
        assert observed.source != "libcamera_id"
        assert observed.hardware_key != "libcamera:aprilcam-imx296-88000"
        assert observed.device_name == "Streaming Camera"

        out = resolve_all(
            enum_entries=enum, profiler_entries={}, libcamera_entries=libcamera_entries
        )
        assert set(out.keys()) == {0, 70}
        assert out[0].source != "libcamera_id"
        assert out[70].source == "libcamera_id"


def _write_usb_device(sys_root, video_index: int, port: str, **attrs) -> None:
    """Fake the real Linux shape: ``videoN/device`` is a symlink to the
    UVC *interface* directory, whose parent is the USB device holding
    ``idVendor``/``product``/``serial`` attribute files."""
    usb_dev = sys_root / "devices" / "usb3" / port
    iface = usb_dev / f"{port}:1.0"
    iface.mkdir(parents=True, exist_ok=True)
    for name, value in {"idVendor": "0c45", **attrs}.items():
        (usb_dev / name).write_text(value + "\n")
    node = sys_root / f"video{video_index}"
    node.mkdir(parents=True, exist_ok=True)
    os.symlink(iface, node / "device")


class TestLinuxUsbIdentity:
    """buzzkill (Ubuntu 26.04): uvcvideo card names read
    ``"<product>: <entity>"`` truncated to 31 chars, and ``/dev/videoN``
    is not a stable location -- identity comes from the USB device."""

    def _run(self, monkeypatch, tmp_path, cams):
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(identity, "_SYS_V4L_ROOT", str(tmp_path))
        monkeypatch.setattr(identity, "_probe_libcamera", lambda: {})
        _patch_enumerate_cameras(monkeypatch, cams)
        return identity.resolve_all(profiler_entries={})

    def test_product_string_names_the_camera_and_serial_keys_it(self, monkeypatch, tmp_path):
        _write_usb_device(tmp_path, 0, "3-2.1.4",
                          product="Arducam OV9782 USB Camera", serial="UC852")
        cams = [_fake_cam(0, "Arducam OV9782 USB Camera: Ardu", path="/dev/video0")]

        ident = self._run(monkeypatch, tmp_path, cams)[0]

        assert ident.slug == "arducam-ov9782-usb-camera"
        assert ident.device_name == "Arducam OV9782 USB Camera"
        assert ident.hardware_key == "serial:UC852"
        assert ident.source == "usb_serial"

    def test_no_serial_keys_on_usb_port_not_dev_node(self, monkeypatch, tmp_path):
        _write_usb_device(tmp_path, 2, "3-2.1.2", product="HD USB CAMERA")
        cams = [_fake_cam(2, "HD USB CAMERA: HD USB CAMERA", path="/dev/video2",
                          vid=0x32E4, pid=0x0317)]

        ident = self._run(monkeypatch, tmp_path, cams)[2]

        assert ident.slug == "hd-usb-camera"
        assert ident.hardware_key == "vidpid:32e4:0317@3-2-1-2"
        assert ident.location == "3-2.1.2"

    def test_non_usb_node_keeps_card_name(self, monkeypatch, tmp_path):
        cams = [_fake_cam(70, "aprilcam-imx296-88000", path="/dev/video70")]

        ident = self._run(monkeypatch, tmp_path, cams)[70]

        assert ident.slug == "aprilcam-imx296-88000"
        assert ident.location == "/dev/video70"
