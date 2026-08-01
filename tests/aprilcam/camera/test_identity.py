"""Tests for ``aprilcam.camera.identity`` (headless, no hardware).

Covers ticket 020-002's AC1: ``CameraIdentity.slug`` / ``.device_name``
produce the documented readable slugs (e.g. ``arducam-ov9782-usb-camera``)
from the fallback chain, exercised against recorded
``cv2-enumerate-cameras`` / ``system_profiler`` snapshot fixtures — never
real hardware or a real subprocess call.
"""

from __future__ import annotations

import sys

import pytest

from aprilcam.camera import identity
from aprilcam.camera.identity import (
    CameraIdentity,
    _EnumEntry,
    _LibcameraEntry,
    _ProfilerEntry,
    device_name_slug,
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


def test_vid_pid_location_path():
    enum = {
        0: _EnumEntry(index=0, name="Generic Cam", vid=3141, pid=25446, path="0xABC123")
    }
    r = resolve_identity(0, enum_entries=enum, profiler_entries={})
    assert r.source == "vid_pid_location"
    assert r.is_fallback is False
    assert r.hardware_key == "vidpid:0c45:6366@0xabc123"


def test_usb_location_path_fallback_when_only_location():
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


def test_resolve_all_resolves_every_index():
    enum = {
        0: _EnumEntry(index=0, name="Cam A", vid=1, pid=2, path="0x01"),
        2: _EnumEntry(index=2, name="Cam B", vid=3, pid=4, path="0x02"),
    }
    out = resolve_all(enum_entries=enum, profiler_entries={})
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


def test_resolve_all_prefers_libcamera_when_present():
    lc = {0: _LibcameraEntry(camera_id="/base/cam0", name="Cam0")}
    out = resolve_all(libcamera_entries=lc)
    assert set(out.keys()) == {0}
    assert out[0].source == "libcamera_id"
