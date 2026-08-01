"""Stable per-camera identity resolution — the fallback chain (UC-14, UC-25).

Converted from ``archive/src/aprilcam/camera/identity.py`` (v1, 407
lines). Resolves the ``CameraIdentity`` frozen dataclass declared in
``camera/interfaces.py`` (this module imports it rather than declaring
its own — see that module's docstring): ``.slug`` (a readable,
human-facing name) and ``.device_name`` (the raw OS-reported descriptor,
explicitly **not** the identity).

Two different questions, two different fields
-----------------------------------------------
This module answers two questions that must never be conflated:

1. **"What should a human call this camera?"** — always the device
   name, slugified (``arducam-ov9782-usb-camera``), because that is what
   UC-14.S-1 and UC-25 expect to see, regardless of which hardware
   source resolved it. This is :attr:`CameraIdentity.slug`.
2. **"Is this the same physical camera I saw before?"** — the fallback
   chain below, from most to least trustworthy. This is
   :attr:`CameraIdentity.hardware_key`, the field
   :mod:`aprilcam.camera.registry` reads as its
   primary key so a replugged camera resolves to the same registry entry
   (UC-25.S-3), while two identical-model cameras on different ports
   still get *different* hardware keys (their ``location`` differs) even
   though they propose the *same* readable slug — the collision
   ``CameraRegistry.resolve`` disambiguates rather than merges (UC-25's
   open-question caveat).

Fallback chain (best -> worst), recorded in ``source``
--------------------------------------------------------
1. ``libcamera_id`` — the libcamera camera-id path (Raspberry Pi).
   Stable across reboots and ports. No v2 backend reads ``/sys`` or
   shells out to the ``cam`` tool yet — that is ticket 003's
   ``camera/backends`` work per the sprint architecture's module table —
   so this tier is inert (``_probe_libcamera`` always returns ``{}``)
   until then, but the injection point (``libcamera_entries``) is real
   and exercised by this module's tests, so wiring the real probe later
   is a one-function change, not a signature change.
2. ``avfoundation_unique_id`` / ``usb_serial`` — a hardware-unique id
   (AVFoundation ``uniqueID`` on macOS, or a USB serial number).
3. ``vid_pid_location`` — ``VID:PID`` plus the USB location-id. Stable
   while the camera stays on the same USB port; differs across ports,
   which is exactly what makes two identical models on different ports
   distinguishable even though their *names* collide.
4. ``usb_location_path`` — the USB location path (port) alone.
5. ``name_resolution_slug`` — device-name(-plus-resolution). Last
   resort; collides for identical models with no serial and no
   location.

Every tier but the first two is a **fallback** — ``is_fallback`` marks
tiers 3-5, where the hardware key is not genuinely unique to this
physical device (a USB-port move or re-enumeration can produce a new
key). This is an accepted, documented limitation carried over from v1:
cameras with a real serial or a stable uniqueID are immune.

Platform behaviour
-------------------
On macOS the resolver also consults ``system_profiler SPCameraDataType``
(the only place in the codebase that shells out to it) alongside
``cv2-enumerate-cameras``. On other platforms it relies on
``cv2-enumerate-cameras`` alone. Every source function degrades to an
empty result on any failure — this module never raises for an
unsupported platform or a missing tool; at worst ``resolve_identity``
returns the name+resolution slug.

This module must not import OpenCV at module top level (only inside
``_enumerate_cv2``, lazily) and returns plain dataclasses only.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Dict, Optional

from aprilcam.camera.interfaces import CameraIdentity

__all__ = [
    "CameraIdentity",
    "device_name_slug",
    "name_resolution_slug",
    "resolve_identity",
    "resolve_all",
]


# ---------------------------------------------------------------------------
# Slug helpers
# ---------------------------------------------------------------------------


def _slug(text: Optional[str]) -> str:
    """Lower-case, hyphen-joined slug of ``text``.

    Collapses runs of non-alphanumerics to one ``-`` and strips leading/
    trailing ``-``. Never raises; empty input yields ``""``.
    """
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return re.sub(r"-{2,}", "-", s)


def device_name_slug(name: Optional[str]) -> str:
    """The readable slug shown to humans — ``CameraIdentity.slug``.

    Built from the device name alone, **never** the resolution — see the
    module docstring's "two different questions" note. Falls back to
    ``"camera"`` for an empty/missing name so ``.slug`` is never empty.
    """
    return _slug(name) or "camera"


def name_resolution_slug(
    name: Optional[str], width: Optional[int] = None, height: Optional[int] = None
) -> str:
    """The last-resort *hardware key* tier: the device-name slug, plus
    resolution when known.

    Distinct from :func:`device_name_slug` — this is for
    ``hardware_key`` disambiguation at the bottom of the fallback chain,
    not for the readable ``slug`` shown to a human (which never carries
    a resolution suffix).
    """
    base = device_name_slug(name)
    if width and height:
        return f"{base}-{int(width)}x{int(height)}"
    return base


# ---------------------------------------------------------------------------
# Source: cv2-enumerate-cameras
# ---------------------------------------------------------------------------


@dataclass
class _EnumEntry:
    index: int
    name: Optional[str] = None
    vid: Optional[int] = None
    pid: Optional[int] = None
    path: Optional[str] = None


def _enumerate_cv2() -> Dict[int, _EnumEntry]:
    """Map OpenCV index -> enumeration entry from ``cv2-enumerate-cameras``.

    Empty dict if the library is unavailable or errors. Imports are
    local so this module never pulls OpenCV at import time.
    """
    out: Dict[int, _EnumEntry] = {}
    try:
        from cv2_enumerate_cameras import enumerate_cameras  # noqa: PLC0415

        try:
            import cv2 as cv  # noqa: PLC0415

            avf_offset = getattr(cv, "CAP_AVFOUNDATION", 1200)
        except Exception:
            avf_offset = 1200

        for cam in enumerate_cameras():
            raw = getattr(cam, "index", None)
            if raw is None:
                continue
            idx = raw - avf_offset if raw >= avf_offset else raw
            out[idx] = _EnumEntry(
                index=idx,
                name=getattr(cam, "name", None) or None,
                vid=getattr(cam, "vid", None) or None,
                pid=getattr(cam, "pid", None) or None,
                path=getattr(cam, "path", None) or None,
            )
    except ImportError:
        pass
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# Source: macOS system_profiler
# ---------------------------------------------------------------------------


@dataclass
class _ProfilerEntry:
    name: str
    unique_id: Optional[str] = None
    vid: Optional[int] = None
    pid: Optional[int] = None
    serial: Optional[str] = None


def _run_system_profiler(datatype: str) -> str:
    """Run ``system_profiler <datatype>`` and return stdout (``""`` on error)."""
    try:
        proc = subprocess.run(
            ["system_profiler", datatype],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
            check=False,
        )
        return proc.stdout or ""
    except Exception:
        return ""


_VENDOR_RE = re.compile(r"VendorID[_\s]+(\d+)", re.IGNORECASE)
_PRODUCT_RE = re.compile(r"ProductID[_\s]+(\d+)", re.IGNORECASE)


def _parse_sp_camera(text: str) -> Dict[str, _ProfilerEntry]:
    """Parse ``system_profiler SPCameraDataType`` text -> {name: entry}.

    The output looks like::

        Camera:

            Global Shutter Camera:

              Model ID: UVC Camera VendorID_13028 ProductID_5553
              Unique ID: 0x211411132e415b1
    """
    entries: Dict[str, _ProfilerEntry] = {}
    current: Optional[_ProfilerEntry] = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        # A device header is an indented "Name:" line that is not a
        # key:value field we recognise. Device headers have no value
        # after the colon.
        if stripped.endswith(":") and not stripped.lower().startswith(
            ("model id", "unique id", "serial")
        ):
            name = stripped[:-1].strip()
            if name.lower() == "camera":
                current = None
                continue
            current = _ProfilerEntry(name=name)
            entries[name] = current
            continue
        if current is None:
            continue
        low = stripped.lower()
        if low.startswith("unique id:"):
            current.unique_id = stripped.split(":", 1)[1].strip() or None
        elif low.startswith("model id:"):
            m = _VENDOR_RE.search(stripped)
            p = _PRODUCT_RE.search(stripped)
            if m:
                current.vid = int(m.group(1))
            if p:
                current.pid = int(p.group(1))
        elif low.startswith("serial number:") or low.startswith("serial:"):
            current.serial = stripped.split(":", 1)[1].strip() or None
    return entries


def _macos_camera_profiles() -> Dict[str, _ProfilerEntry]:
    """Best-effort macOS camera identity table keyed by device name."""
    if sys.platform != "darwin":
        return {}
    return _parse_sp_camera(_run_system_profiler("SPCameraDataType"))


# ---------------------------------------------------------------------------
# Source: libcamera (inert until ticket 003's backend exists)
# ---------------------------------------------------------------------------


@dataclass
class _LibcameraEntry:
    """One libcamera-enumerated camera.

    ``camera_id`` is the stable libcamera path (e.g.
    ``/base/soc/i2c0mux/i2c@1/imx219@10``); ``name`` is its human-facing
    model name.
    """

    camera_id: str
    name: Optional[str] = None


def _probe_libcamera() -> Dict[int, _LibcameraEntry]:
    """Enumerate libcamera devices, keyed by position/index.

    Ticket 020-003 wired this to
    ``camera.backends.libcamera.list_loopback_cameras()`` — the
    ``/sys``-based, subprocess-free enumeration of labelled
    v4l2loopback devices a Pi's out-of-process ``libcamerasrc`` bridge
    creates (see that module's docstring for what was and was not
    ported this ticket: enumeration only, no ``cam``-tool "gst mode",
    no capture backend yet). This is a safe, no-behaviour-change swap
    on every machine that has no ``/sys/class/video4linux`` tree —
    this Mac included, where ``list_loopback_cameras()`` degrades to
    ``[]`` via a plain ``glob`` miss, same as the empty-dict stub this
    replaced. Never raises: any unexpected failure degrades to empty,
    matching this module's "every source function degrades on failure"
    rule.
    """
    try:
        from aprilcam.camera.backends.libcamera import list_loopback_cameras  # noqa: PLC0415
    except Exception:
        return {}
    try:
        devices = list_loopback_cameras()
    except Exception:
        return {}
    return {
        d.position: _LibcameraEntry(camera_id=d.camera_id, name=d.model)
        for d in devices
    }


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


def resolve_identity(
    index: int,
    name: Optional[str] = None,
    width: Optional[int] = None,
    height: Optional[int] = None,
    *,
    enum_entries: Optional[Dict[int, _EnumEntry]] = None,
    profiler_entries: Optional[Dict[str, _ProfilerEntry]] = None,
    libcamera_entries: Optional[Dict[int, _LibcameraEntry]] = None,
) -> CameraIdentity:
    """Resolve the best-available identity for one enumerated camera index.

    Parameters
    ----------
    index:
        The OpenCV camera index (or libcamera position).
    name:
        OS-reported device name, if already known. When omitted, the
        name from ``cv2-enumerate-cameras`` is used.
    width, height:
        Capture resolution, used only for the last-resort hardware-key
        tier — never for the readable ``slug``.
    enum_entries, profiler_entries, libcamera_entries:
        Injected sources, for testing against recorded snapshots. When
        ``None`` they are gathered from ``cv2-enumerate-cameras``,
        ``system_profiler``, and (for now, always empty) libcamera.

    Never raises: on any failure this returns a :class:`CameraIdentity`
    whose ``hardware_key`` is at worst the name+resolution slug and
    whose ``slug`` is at worst ``"camera"``.
    """
    if libcamera_entries is None:
        libcamera_entries = _probe_libcamera()
    lc = libcamera_entries.get(index)
    if lc is not None:
        # A distinct local name from the ``dev_name`` bound below: that
        # one is genuinely Optional[str] (unknown until enum/profiler
        # lookups run), this one is always str (``lc.camera_id`` is a
        # non-optional fallback) -- reusing one name across both would
        # make mypy unify them to a single (wrong) declared type.
        lc_dev_name = lc.name or lc.camera_id
        return CameraIdentity(
            slug=device_name_slug(lc_dev_name),
            device_name=lc_dev_name,
            hardware_key=f"libcamera:{lc.camera_id}",
            source="libcamera_id",
            is_fallback=False,
            location=lc.camera_id,
        )

    if enum_entries is None:
        enum_entries = _enumerate_cv2()
    if profiler_entries is None:
        profiler_entries = _macos_camera_profiles()

    entry = enum_entries.get(index)
    dev_name = name or (entry.name if entry else None)

    vid = entry.vid if entry else None
    pid = entry.pid if entry else None
    location = entry.path if entry else None
    serial: Optional[str] = None
    avf_unique: Optional[str] = None

    # Backfill from system_profiler by matching on device name.
    prof = profiler_entries.get(dev_name) if dev_name else None
    if prof is not None:
        avf_unique = prof.unique_id or avf_unique
        serial = prof.serial or serial
        vid = vid if vid is not None else prof.vid
        pid = pid if pid is not None else prof.pid

    def _result(hardware_key: str, source: str, is_fallback: bool) -> CameraIdentity:
        return CameraIdentity(
            slug=device_name_slug(dev_name),
            device_name=dev_name or "camera",
            hardware_key=hardware_key,
            source=source,
            is_fallback=is_fallback,
            vid=vid,
            pid=pid,
            serial=serial,
            location=location,
            avfoundation_unique_id=avf_unique,
        )

    # 1. Hardware-unique id: AVFoundation uniqueID or USB serial.
    if avf_unique:
        return _result(f"avf:{avf_unique}", "avfoundation_unique_id", False)
    if serial:
        return _result(f"serial:{serial}", "usb_serial", False)

    # 2. VID:PID + USB location-id.
    if vid is not None and pid is not None and location:
        hardware_key = f"vidpid:{vid:04x}:{pid:04x}@{_slug(location)}"
        return _result(hardware_key, "vid_pid_location", False)

    # 3. USB location path (port) alone — a fallback (port-move sensitive).
    if location:
        return _result(f"loc:{_slug(location)}", "usb_location_path", True)

    # 4. Last resort: name + resolution slug.
    return _result(
        f"name:{name_resolution_slug(dev_name, width, height)}",
        "name_resolution_slug",
        True,
    )


def resolve_all(
    *,
    enum_entries: Optional[Dict[int, _EnumEntry]] = None,
    profiler_entries: Optional[Dict[str, _ProfilerEntry]] = None,
    libcamera_entries: Optional[Dict[int, _LibcameraEntry]] = None,
) -> Dict[int, CameraIdentity]:
    """Resolve identities for every currently enumerated camera.

    Gathers each source table *once* and resolves every known index,
    avoiding a per-camera ``system_profiler`` shell-out. Returns
    ``{index: CameraIdentity}``.
    """
    if libcamera_entries is None:
        libcamera_entries = _probe_libcamera()
    if libcamera_entries:
        return {
            idx: resolve_identity(idx, libcamera_entries=libcamera_entries)
            for idx in libcamera_entries
        }

    if enum_entries is None:
        enum_entries = _enumerate_cv2()
    if profiler_entries is None:
        profiler_entries = _macos_camera_profiles()
    return {
        idx: resolve_identity(
            idx,
            name=entry.name,
            enum_entries=enum_entries,
            profiler_entries=profiler_entries,
            libcamera_entries={},
        )
        for idx, entry in enum_entries.items()
    }
