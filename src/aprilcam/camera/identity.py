"""Stable hardware-identity resolution for cameras.

This module resolves the best-available *stable* identifier for a camera
device, so that two physically distinct cameras — even of the same model
with identical OS device names — can be told apart and re-recognised across
disconnects and daemon restarts.

Fallback chain (best → worst)
-----------------------------
The resolver walks a documented chain and stops at the first source that
yields a value, recording *which* source was used in ``reason``:

1. ``avfoundation_unique_id`` / ``usb_serial`` — a hardware-unique id
   (AVFoundation ``uniqueID`` on macOS, or a USB serial number). For a USB
   webcam with a real serial, or a UVC device whose AVFoundation uniqueID is
   a UUID, this is fully stable across USB ports.
2. ``vid_pid_location`` — ``VID:PID`` combined with the USB location-id.
   Stable while the camera stays on the same USB port.
3. ``usb_location_path`` — the USB location path (port) alone.
4. ``name_resolution_slug`` — a slug built from the device name (and, when
   provided, the resolution). Last resort; collides for identical models.

Known limitation — USB-port moves
---------------------------------
On platforms/devices that expose **no** serial or stable uniqueID (many
generic UVC webcams), the best available id is derived from the USB
*location* (the port the camera is plugged into). Moving such a camera to a
different USB port changes its location and therefore its ``unique_id`` — it
will present as a *new* camera. This is an accepted, documented limitation
for this project; cameras with a real serial or a UUID uniqueID are immune.

Platform behaviour
------------------
On macOS the resolver consults ``system_profiler SPCameraDataType`` /
``SPUSBDataType`` (the only place in the codebase that shells out to
``system_profiler``) in addition to ``cv2-enumerate-cameras``. On other
platforms it relies on ``cv2-enumerate-cameras`` alone. The resolver never
raises on an unsupported platform or a missing tool: it always returns at
least the name+resolution slug.

This module must not import OpenCV at module top level and returns plain
dataclasses only.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Result schema
# ---------------------------------------------------------------------------


@dataclass
class CameraIdentity:
    """Resolved stable identity for a single camera.

    Attributes
    ----------
    unique_id:
        The best-available stable id. Always populated (never ``None``);
        in the worst case it is the name+resolution slug.
    reason:
        Which source produced ``unique_id`` — one of
        ``"avfoundation_unique_id"``, ``"usb_serial"``,
        ``"vid_pid_location"``, ``"usb_location_path"``,
        ``"name_resolution_slug"``.
    is_fallback:
        ``True`` when ``unique_id`` is *not* a hardware-unique id (i.e. the
        source was a USB location path or the name slug, both of which are
        not robust to USB-port moves / identical models).
    vid, pid:
        USB vendor / product ids when known.
    serial:
        USB serial number when known.
    location:
        USB location path / location-id when known.
    avfoundation_unique_id:
        Raw AVFoundation ``uniqueID`` when known (macOS).
    name:
        OS-reported device name.
    """

    unique_id: str
    reason: str
    is_fallback: bool
    vid: Optional[int] = None
    pid: Optional[int] = None
    serial: Optional[str] = None
    location: Optional[str] = None
    avfoundation_unique_id: Optional[str] = None
    name: Optional[str] = None


# ---------------------------------------------------------------------------
# Slug helper
# ---------------------------------------------------------------------------


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return re.sub(r"-{2,}", "-", s)


def name_resolution_slug(
    name: Optional[str], width: Optional[int] = None, height: Optional[int] = None
) -> str:
    """Last-resort id: a slug of the device name plus optional resolution."""
    base = _slug(name or "camera")
    if not base:
        base = "camera"
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
    """Map OpenCV index → enumeration entry from ``cv2-enumerate-cameras``.

    Returns an empty dict if the library is unavailable or errors. Imports
    are local so this module never pulls OpenCV at import time.
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
    """Parse ``system_profiler SPCameraDataType`` text → {name: entry}.

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
        # A device header is an indented "Name:" line that is not a key:value
        # field we recognise. Device headers have no value after the colon.
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
) -> CameraIdentity:
    """Resolve the best-available stable identity for an OpenCV camera index.

    Parameters
    ----------
    index:
        The OpenCV camera index.
    name:
        OS-reported device name, if already known. When omitted, the name
        from ``cv2-enumerate-cameras`` is used.
    width, height:
        Capture resolution, used only for the last-resort name slug.
    enum_entries, profiler_entries:
        Injected sources, for testing. When ``None`` they are gathered from
        ``cv2-enumerate-cameras`` and ``system_profiler`` respectively.

    Never raises: on any failure it returns a :class:`CameraIdentity` whose
    ``unique_id`` is at worst the name+resolution slug.
    """
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

    def _result(uid: str, reason: str, is_fallback: bool) -> CameraIdentity:
        return CameraIdentity(
            unique_id=uid,
            reason=reason,
            is_fallback=is_fallback,
            vid=vid,
            pid=pid,
            serial=serial,
            location=location,
            avfoundation_unique_id=avf_unique,
            name=dev_name,
        )

    # 1. Hardware-unique id: AVFoundation uniqueID or USB serial.
    if avf_unique:
        return _result(f"avf:{avf_unique}", "avfoundation_unique_id", False)
    if serial:
        return _result(f"serial:{serial}", "usb_serial", False)

    # 2. VID:PID + USB location-id.
    if vid is not None and pid is not None and location:
        uid = f"vidpid:{vid:04x}:{pid:04x}@{_slug(location)}"
        return _result(uid, "vid_pid_location", False)

    # 3. USB location path (port) alone — a fallback (port-move sensitive).
    if location:
        return _result(f"loc:{_slug(location)}", "usb_location_path", True)

    # 4. Last resort: name + resolution slug.
    slug = name_resolution_slug(dev_name, width, height)
    return _result(f"name:{slug}", "name_resolution_slug", True)


def resolve_all(
    cameras: List["object"] | None = None,
    *,
    enum_entries: Optional[Dict[int, _EnumEntry]] = None,
    profiler_entries: Optional[Dict[str, _ProfilerEntry]] = None,
) -> Dict[int, CameraIdentity]:
    """Resolve identities for all currently enumerated cameras.

    Gathers the enumeration and profiler tables *once* and resolves every
    enumerated index, avoiding a per-camera ``system_profiler`` shell-out.
    Returns ``{index: CameraIdentity}``.
    """
    if enum_entries is None:
        enum_entries = _enumerate_cv2()
    if profiler_entries is None:
        profiler_entries = _macos_camera_profiles()
    out: Dict[int, CameraIdentity] = {}
    for idx, entry in enum_entries.items():
        out[idx] = resolve_identity(
            idx,
            name=entry.name,
            enum_entries=enum_entries,
            profiler_entries=profiler_entries,
        )
    return out
