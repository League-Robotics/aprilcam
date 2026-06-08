"""Tests for aprilcam.camera.identity — stable hardware-id resolution.

These tests mock the two identity sources (``cv2-enumerate-cameras`` and
macOS ``system_profiler``) so they run on any platform without cameras.
"""

from __future__ import annotations

import sys

import pytest

from aprilcam.camera import identity
from aprilcam.camera.identity import (
    CameraIdentity,
    _EnumEntry,
    _ProfilerEntry,
    name_resolution_slug,
    resolve_all,
    resolve_identity,
)


def test_avfoundation_unique_id_is_primary():
    enum = {0: _EnumEntry(index=0, name="Brio 501", vid=1133, pid=2374, path="0x1100000046d0946")}
    prof = {"Brio 501": _ProfilerEntry(name="Brio 501", unique_id="0x1100000046d0946", vid=1133, pid=2374)}
    r = resolve_identity(0, enum_entries=enum, profiler_entries=prof)
    assert r.reason == "avfoundation_unique_id"
    assert r.is_fallback is False
    assert r.unique_id == "avf:0x1100000046d0946"
    assert r.vid == 1133 and r.pid == 2374
    assert r.avfoundation_unique_id == "0x1100000046d0946"


def test_usb_serial_used_when_no_avf_unique():
    enum = {0: _EnumEntry(index=0, name="Some Cam", vid=10, pid=20, path="loc-1")}
    prof = {"Some Cam": _ProfilerEntry(name="Some Cam", serial="SN12345")}
    r = resolve_identity(0, enum_entries=enum, profiler_entries=prof)
    assert r.reason == "usb_serial"
    assert r.is_fallback is False
    assert r.unique_id == "serial:SN12345"


def test_vid_pid_location_path():
    enum = {0: _EnumEntry(index=0, name="Generic Cam", vid=3141, pid=25446, path="0xABC123")}
    r = resolve_identity(0, enum_entries=enum, profiler_entries={})
    assert r.reason == "vid_pid_location"
    assert r.is_fallback is False
    assert r.unique_id == "vidpid:0c45:6366@0xabc123"


def test_usb_location_path_fallback_when_only_location():
    enum = {0: _EnumEntry(index=0, name="No IDs Cam", vid=None, pid=None, path="0xDEAD")}
    r = resolve_identity(0, enum_entries=enum, profiler_entries={})
    assert r.reason == "usb_location_path"
    assert r.is_fallback is True
    assert r.unique_id == "loc:0xdead"


def test_name_resolution_slug_last_resort():
    enum = {0: _EnumEntry(index=0, name="Webcam", vid=None, pid=None, path=None)}
    r = resolve_identity(0, width=1920, height=1080, enum_entries=enum, profiler_entries={})
    assert r.reason == "name_resolution_slug"
    assert r.is_fallback is True
    assert r.unique_id == "name:webcam-1920x1080"


def test_no_enum_entry_falls_back_to_name_slug():
    r = resolve_identity(5, name="Mystery Cam", enum_entries={}, profiler_entries={})
    assert r.reason == "name_resolution_slug"
    assert r.unique_id == "name:mystery-cam"


def test_two_identical_name_devices_get_distinct_ids():
    """Two cameras with the same device name must resolve to distinct ids."""
    enum = {
        0: _EnumEntry(index=0, name="Arducam OV9782 USB Camera", vid=3141, pid=25446, path="0xAAAA"),
        1: _EnumEntry(index=1, name="Arducam OV9782 USB Camera", vid=3141, pid=25446, path="0xBBBB"),
    }
    r0 = resolve_identity(0, enum_entries=enum, profiler_entries={})
    r1 = resolve_identity(1, enum_entries=enum, profiler_entries={})
    assert r0.unique_id != r1.unique_id
    # Both came from vid:pid+location since locations differ.
    assert r0.reason == "vid_pid_location"
    assert r1.reason == "vid_pid_location"


def test_resolve_all_resolves_every_index():
    enum = {
        0: _EnumEntry(index=0, name="Cam A", vid=1, pid=2, path="0x01"),
        2: _EnumEntry(index=2, name="Cam B", vid=3, pid=4, path="0x02"),
    }
    out = resolve_all(enum_entries=enum, profiler_entries={})
    assert set(out.keys()) == {0, 2}
    assert all(isinstance(v, CameraIdentity) for v in out.values())
    assert out[0].unique_id != out[2].unique_id


def test_never_raises_on_non_darwin_platform(monkeypatch):
    """On a non-darwin platform with no sources, the resolver still returns."""
    monkeypatch.setattr(sys, "platform", "linux")
    # _macos_camera_profiles short-circuits to {} on non-darwin.
    assert identity._macos_camera_profiles() == {}
    r = resolve_identity(0, name="Linux Cam", enum_entries={}, profiler_entries=None)
    assert r.unique_id  # non-empty
    assert r.reason == "name_resolution_slug"


def test_never_raises_when_system_profiler_missing(monkeypatch):
    """A missing/failing system_profiler degrades to an empty table, no raise."""
    monkeypatch.setattr(sys, "platform", "darwin")

    def boom(*_a, **_k):
        raise FileNotFoundError("system_profiler not found")

    monkeypatch.setattr(identity.subprocess, "run", boom)
    # _run_system_profiler swallows the error → "".
    assert identity._run_system_profiler("SPCameraDataType") == ""
    assert identity._macos_camera_profiles() == {}
    r = resolve_identity(0, name="Cam", enum_entries={}, profiler_entries=None)
    assert r.unique_id == "name:cam"


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
