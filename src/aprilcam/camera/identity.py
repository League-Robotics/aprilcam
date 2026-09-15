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

**Root-cause fix (ticket 021-007): macOS's ``location`` *is* its
avfoundation_unique_id, always, without ``system_profiler``.** A
production registry on this machine was found to have every physical
camera registered *twice* — once under ``avf:<id>`` and once under
``vidpid:<vid>:<pid>@<id>`` for the exact same ``<id>``. Root cause:
``cv2-enumerate-cameras``'s macOS backend sets its enumeration ``path``
to ``AVCaptureDevice.uniqueID()`` directly (see
``cv2_enumerate_cameras/macos_backend.py``: ``path=d.uniqueID()`` — "macOS
does not provide a path, but uniqueID persists with a device over
time"). That is the *same string* ``system_profiler`` separately reports
as "Unique ID" — not an independently-sourced fallback value. Before
this fix, ``resolve_identity`` only trusted that value once
``system_profiler``'s device-name match (``_macos_camera_profiles``)
confirmed it; an occasional match miss (camera busy, a transient
enumeration hiccup, a name that does not match byte-for-byte) fell
through to the ``vid_pid_location`` tier, which embeds the *identical*
id under a different key format — minting a second registry record for
one physical camera depending only on which run's ``system_profiler``
call happened to match. Below, ``location`` is now trusted directly as
the ``avfoundation_unique_id`` tier on macOS whenever ``system_profiler``
did not already backfill one, making resolution deterministic
regardless of that subprocess's success — the tier used no longer
depends on a side-channel matching step at all. This shortcut is
darwin-only: on Linux, ``cv2-enumerate-cameras``' ``path`` is a
``/dev/videoN`` device node (see ``linux_backend.py``), not a hardware
id, and stays exactly as fallback-prone as the module docstring above
already documents (``usb_location_path`` remains genuinely a fallback
there). ``camera/registry.py`` additionally self-heals a registry
already polluted by the pre-fix behaviour — see that module's
``_reconcile_location_duplicates`` — since fixing this resolver alone
does not un-duplicate a registry.json that already has both records.

This module must not import OpenCV at module top level (only inside
``_enumerate_cv2``, lazily) and returns plain dataclasses only.

Linux backend-offset dedup and internal-node filter (ticket 041-001)
----------------------------------------------------------------------
Confirmed by reading the vendored ``cv2_enumerate_cameras`` source
directly (``cv2_enumerate_cameras/__init__.py``,
``linux_backend.py``): calling ``enumerate_cameras()`` with the
default ``apiPreference=CAP_ANY`` (exactly how :func:`_enumerate_cv2`
calls it) iterates every backend in the platform's ``supported_backends``
tuple and yields one ``CameraInfo`` per backend per device, combining
``index = real_index + backend_id``. On macOS ``supported_backends =
(CAP_AVFOUNDATION,)`` — one backend, so the old ``idx = raw -
avf_offset if raw >= avf_offset else raw`` logic was already correct.
On Linux, ``supported_backends = (CAP_GSTREAMER=1800, CAP_V4L2=200)``
— **two** backends over the identical ``/dev/video*`` glob, so every
physical device was yielded twice, at two different combined indices,
and the AVFoundation-only stripping logic mis-handled or ignored both
(a raw ``CAP_GSTREAMER``-tagged index happened to exceed the
AVFoundation offset too, so it was *wrongly* stripped against the
wrong constant, landing on neither the real index nor a stable
duplicate key). :func:`_combined_index_offsets` /
:func:`_strip_backend_offset` generalize the stripping to whichever
offset(s) apply on the current platform, so both backend-tagged
entries for one physical ``/dev/videoN`` now collapse to the same
dict key — fixing the "every row printed twice" bug for every Linux
camera, not just Pi-internal nodes.

Separately, ``cv2_enumerate_cameras``'s own admission test
(``device_can_capture_video``, a ``VIDIOC_QUERYCAP``
``V4L2_CAP_VIDEO_CAPTURE`` check) cannot distinguish a Raspberry Pi
RP1/PiSP-internal V4L2 node (``rp1-cfe-*``, ``pispbe-*``, ``rpivid``,
``bcm2835-codec`` — media-controller nodes a libcamera pipeline owns,
never meant to be opened directly) from a real standalone camera:
both genuinely report that capability bit. v1
(``archive/src/aprilcam/camera/camutil.py::_list_v4l2_usb_cameras``)
already solved this by reading the node's V4L2 driver name via the
``/sys/class/video4linux/videoN/device/driver`` symlink and keeping
only real capture-source drivers. :func:`is_pi_internal_node` inverts
that (excludes known-internal drivers rather than allow-listing
``uvcvideo`` alone), read by :func:`_read_v4l2_driver` (no subprocess,
mirroring ``backends/libcamera.py::list_loopback_cameras``'s ``/sys``
style), with a narrow name-prefix backstop (``rp1-cfe-``,
``pispbe-``) for when the symlink cannot be read at all (verified live
against ``vidar.local``: a v4l2loopback bridge device such as
``aprilcam-imx296-88000`` has no ``device/`` subdirectory at all — it
is a virtual node with no backing hardware — so its driver reads as
unreadable and it falls through to the name check, which does not
match either prefix, so it is kept). The predicate **fails open**:
anything it cannot positively confirm as a known-internal node keeps
the candidate, on the documented asymmetry that a false negative
merely reproduces today's bug while a false positive would silently
break discovery for a real, unrecognized camera. This filter only
ever runs on ``sys.platform.startswith("linux")``; it is a no-op
everywhere else, including macOS, so the AVFoundation path above is
untouched. ``is_pi_internal_node`` is exported (not left
module-private) specifically so ``camera/discovery.py`` (ticket
041-002) can reuse the same classifier, by device name alone, to
purge already-registered junk rows for nodes no longer proposed by
this fixed enumeration.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from aprilcam.camera.interfaces import CameraIdentity

__all__ = [
    "CameraIdentity",
    "device_name_slug",
    "name_resolution_slug",
    "is_pi_internal_node",
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

    **Invariant: the returned slug always contains at least one
    non-digit character.** ``daemon/service.py``'s ``_camera_for``
    (ticket 021-002) parses an all-digits token unambiguously as a
    persistent registry number and anything else as a slug; that
    parsing has no ambiguity only because a slug can never itself be
    all digits. ``_slug``'s regex strips everything but alphanumerics,
    so a device literally named e.g. ``"123"`` would otherwise slip
    through as the bare numeric slug ``"123"`` — indistinguishable from
    the number 123. Guarded here, not merely asserted: an all-digit
    result is prefixed with ``camera-``. This is the readable ``.slug``
    every fallback tier proposes (including the last-resort
    ``name_resolution_slug`` tier below, whose own ``hardware_key`` is
    separately prefixed with ``"name:"`` and is not addressed by this
    invariant — only the readable slug is).
    """
    slug = _slug(name) or "camera"
    if slug.isdigit():
        slug = f"camera-{slug}"
    return slug


def name_resolution_slug(
    name: Optional[str], width: Optional[int] = None, height: Optional[int] = None
) -> str:
    """The last-resort *hardware key* tier: the device-name slug, plus
    resolution when known.

    Distinct from :func:`device_name_slug` — this is for
    ``hardware_key`` disambiguation at the bottom of the fallback chain,
    not for the readable ``slug`` shown to a human (which never carries
    a resolution suffix). Not subject to :func:`device_name_slug`'s
    no-all-digits invariant itself (this value only ever becomes part
    of a ``hardware_key``, behind a ``"name:"`` prefix — never a
    ``CameraRecord.slug``), but it is built from
    :func:`device_name_slug` and so inherits the guard whenever no
    resolution is supplied.
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


def _combined_index_offsets() -> Tuple[int, ...]:
    """Backend-id offset(s) ``enumerate_cameras(CAP_ANY)`` may add to a
    device's real index on the current platform, largest first.

    Mirrors ``cv2_enumerate_cameras``'s own combination
    (``index = i.index + i.backend`` over its ``supported_backends``
    tuple for the running platform — see the module docstring's
    "Linux backend-offset dedup" section for the full trace). Darwin
    has one backend (``CAP_AVFOUNDATION``); Linux has two
    (``CAP_GSTREAMER``, ``CAP_V4L2``). Falls back to the same hardcoded
    constants ``cv2_enumerate_cameras`` itself falls back to when
    ``cv2`` cannot be imported. Returns ``()`` on any other platform
    (Windows is out of this project's target platforms — see
    ``linux_backend``/``macos_backend`` vs. ``windows_backend`` in the
    vendored package) — :func:`_strip_backend_offset` leaves an index
    unchanged when no offset applies.
    """
    try:
        import cv2 as cv  # noqa: PLC0415

        avf = getattr(cv, "CAP_AVFOUNDATION", 1200)
        gst = getattr(cv, "CAP_GSTREAMER", 1800)
        v4l2 = getattr(cv, "CAP_V4L2", 200)
    except Exception:
        avf, gst, v4l2 = 1200, 1800, 200

    if sys.platform == "darwin":
        return (avf,)
    if sys.platform.startswith("linux"):
        return tuple(sorted({gst, v4l2}, reverse=True))
    return ()


def _strip_backend_offset(raw: int, offsets: Tuple[int, ...]) -> int:
    """Recover a real device index from a combined ``index +
    backend_offset`` value.

    ``offsets`` must be sorted largest-first (as
    :func:`_combined_index_offsets` returns them) so a raw index that
    exceeds more than one offset matches the correct (largest
    applicable) one rather than the first offset that happens to fit.
    Two backend-tagged entries for the same physical device (e.g.
    ``205`` under ``CAP_V4L2`` and ``1805`` under ``CAP_GSTREAMER`` for
    real index ``5``) both recover to the same real index, which is
    exactly what collapses them back into one ``_enumerate_cv2()`` dict
    entry. Returns ``raw`` unchanged when no offset applies.
    """
    for offset in offsets:
        if raw >= offset:
            return raw - offset
    return raw


# ---------------------------------------------------------------------------
# Linux-only: exclude Raspberry Pi RP1/PiSP media-pipeline-internal nodes
# ---------------------------------------------------------------------------

_SYS_V4L_ROOT = "/sys/class/video4linux"

# Known V4L2 driver names for nodes that belong to a libcamera-owned
# media pipeline, never a standalone capture camera. Not exhaustive by
# design -- an unrecognized driver keeps the candidate (fail open).
_PI_INTERNAL_DRIVERS = frozenset({"rp1-cfe", "pispbe", "rpivid", "bcm2835-codec"})

# Narrow backstop for when the driver symlink cannot be read at all.
# Deliberately does not cover every internal driver above (e.g.
# "rpivid" has no dash-suffixed name convention on the device it
# names) -- only the two families that consistently name their nodes
# with these prefixes.
_PI_INTERNAL_NAME_PREFIXES = ("rp1-cfe-", "pispbe-")

_VIDEO_NODE_RE = re.compile(r"video(\d+)$")


def _video_node_index(path: Optional[str]) -> Optional[int]:
    """Extract ``N`` from a ``/dev/videoN``-shaped path; ``None`` if
    ``path`` is missing or does not look like a video node."""
    if not path:
        return None
    m = _VIDEO_NODE_RE.search(path)
    return int(m.group(1)) if m else None


def _read_v4l2_driver(video_index: int, *, sys_root: str = _SYS_V4L_ROOT) -> Optional[str]:
    """Read the V4L2 driver bound to ``/dev/video<video_index>``.

    Resolves ``<sys_root>/video<N>/device/driver`` (a symlink to
    ``.../drivers/<driver-name>``, e.g. ``rp1-cfe``) and returns its
    basename. No subprocess -- mirrors
    ``backends/libcamera.py::list_loopback_cameras``'s ``/sys``
    handling. Returns ``None`` (unknown, never "no driver") when the
    symlink is missing or unreadable, which is also the real shape of
    a v4l2loopback device: verified live on ``vidar.local`` that an
    ``aprilcam-*`` loopback node has no ``device/`` subdirectory at
    all (it is virtual, with no backing hardware), so it always reads
    as unknown here and must fail open through :func:`is_pi_internal_node`.

    ``sys_root`` is overridable so tests can point this at a temporary
    directory standing in for ``/sys``, matching
    ``list_loopback_cameras``'s ``sys_glob`` convention.
    """
    link = os.path.join(sys_root, f"video{video_index}", "device", "driver")
    try:
        # Real /sys always exposes ``device/driver`` as a symlink; a
        # v4l2loopback node has no ``device/`` subdirectory at all (it
        # is virtual), so ``islink`` alone -- not ``exists``, which
        # would follow the link and wrongly report "missing" for a
        # symlink whose target a test fixture did not also create --
        # correctly separates "no such node" from "node exists".
        if not os.path.islink(link):
            return None
        target = os.path.realpath(link)
    except OSError:
        return None
    return os.path.basename(target) or None


def is_pi_internal_node(driver: Optional[str], name: Optional[str] = None) -> bool:
    """True if a V4L2 node is a Raspberry Pi RP1/PiSP media-pipeline-
    internal node and should never be proposed as a camera.

    Principled test first: ``driver`` (from :func:`_read_v4l2_driver`)
    against the known-internal driver set. When ``driver`` is ``None``
    (the symlink could not be read), falls back to a narrow name-prefix
    backstop against ``name``. **Fails open** in every other case --
    an unrecognized driver, or a missing/non-matching name when the
    driver is unknown -- so a real USB/UVC camera (``uvcvideo``) or a
    not-yet-seen future driver is never excluded, and callers with only
    a device name (e.g. ``camera/discovery.py``'s registry-purge check,
    which has no live ``/sys`` node to read a driver from) can call
    this with ``driver=None`` and rely on the same backstop.
    """
    if driver is not None:
        return driver in _PI_INTERNAL_DRIVERS
    return bool(name) and name.startswith(_PI_INTERNAL_NAME_PREFIXES)


def _enumerate_cv2() -> Dict[int, _EnumEntry]:
    """Map OpenCV index -> enumeration entry from ``cv2-enumerate-cameras``.

    Empty dict if the library is unavailable or errors. Imports are
    local so this module never pulls OpenCV at import time. On Linux,
    collapses the ``CAP_V4L2``/``CAP_GSTREAMER`` backend-duplicated
    entries for each physical device into one, and excludes Pi
    RP1/PiSP-internal nodes before they ever become an ``_EnumEntry`` --
    see the module docstring's "Linux backend-offset dedup and
    internal-node filter" section.
    """
    out: Dict[int, _EnumEntry] = {}
    try:
        from cv2_enumerate_cameras import enumerate_cameras  # noqa: PLC0415

        offsets = _combined_index_offsets()
        on_linux = sys.platform.startswith("linux")

        for cam in enumerate_cameras():
            raw = getattr(cam, "index", None)
            if raw is None:
                continue
            idx = _strip_backend_offset(raw, offsets)
            name = getattr(cam, "name", None) or None
            path = getattr(cam, "path", None) or None

            if on_linux:
                video_idx = _video_node_index(path)
                driver = (
                    _read_v4l2_driver(video_idx, sys_root=_SYS_V4L_ROOT)
                    if video_idx is not None
                    else None
                )
                if is_pi_internal_node(driver, name):
                    continue

            out[idx] = _EnumEntry(
                index=idx,
                name=name,
                vid=getattr(cam, "vid", None) or None,
                pid=getattr(cam, "pid", None) or None,
                path=path,
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

    # 1b. Root-cause determinism fix (ticket 021-007, module docstring's
    # "Root-cause fix" section): on macOS, cv2-enumerate-cameras' own
    # `path` already *is* the AVFoundation uniqueID -- trust it directly
    # rather than requiring system_profiler's name-keyed match (the
    # `avf_unique`/`serial` checks above) to have succeeded first. This
    # removes resolution's only dependency on that subprocess call's
    # success for the common case (no serial reported at all -- true of
    # every camera on this Mac), so the same physical camera resolves to
    # the same hardware_key every run, whether or not the profiler match
    # landed this time. Checked after, not instead of, `serial` so an
    # explicit serial number (when system_profiler does report one)
    # keeps its documented priority over a location-derived id.
    if sys.platform == "darwin" and location:
        return _result(f"avf:{location}", "avfoundation_unique_id", False)

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
