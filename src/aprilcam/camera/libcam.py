"""libcamera (Raspberry Pi CSI) camera backend.

On a Raspberry Pi the CSI cameras (e.g. the IMX296 modules on a Pi 5) are not
reachable through OpenCV ``VideoCapture(index)`` / V4L2 — the kernel exposes
dozens of ISP pipeline ``/dev/video*`` nodes, none plainly capturable. The
cameras are driven by **libcamera**.

Running GStreamer+libcamera *inside* the gRPC daemon is unstable (it segfaults
via gRPC's fork handlers and hangs against the event loop). So the supported
setup is a **v4l2loopback bridge**: an out-of-process ``libcamerasrc ->
v4l2sink`` feeds a labelled v4l2loopback device per camera, and the daemon reads
that device through its plain, battle-tested V4L2 path. This module is the seam
that lets the rest of AprilCam keep its index-based camera model.

Two capture modes (``APRILCAM_LIBCAM_CAPTURE``):

* ``loopback`` (default) — enumerate the labelled v4l2loopback devices via
  ``/sys`` (no subprocess; robust inside the daemon) and capture them via plain
  V4L2. The bridge owns libcamera; the daemon never touches it.
* ``gst`` — enumerate via the ``cam`` tool and capture via a ``libcamerasrc``
  GStreamer pipeline directly. Fine for a standalone script; unstable in the
  daemon. Kept for completeness.

Backend selection: ``APRILCAM_CAMERA_BACKEND`` = ``auto`` | ``libcamera`` |
``v4l2`` (auto enables the libcamera backend when it finds cameras). Loopback
device label prefix: ``APRILCAM_LIBCAM_LOOPBACK_LABEL`` (default ``aprilcam-``).
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class LibcamCamera:
    """One libcamera-backed camera as seen by AprilCam."""

    position: int          # 0-based enumeration order (stable)
    camera_id: str         # stable unique id (libcamera id path or loopback label)
    model: str             # sensor model, e.g. "imx296"
    slug: str              # filesystem-safe per-camera key (the dir name)
    friendly_name: str     # human-readable name
    device_index: Optional[int] = None  # v4l2 device index (loopback mode)


def _id_token(camera_id: str) -> str:
    """A short, stable token distinguishing cameras of the same model."""
    m = re.search(r"i2c@([0-9a-fA-F]+)", camera_id)
    if m:
        return m.group(1)
    tail = re.sub(r"[^a-z0-9]+", "-", camera_id.lower()).strip("-")
    return tail[-12:] or "cam"


# -- configuration ---------------------------------------------------------

def capture_mode() -> str:
    """``loopback`` (default) or ``gst`` — how the daemon captures frames."""
    return os.environ.get("APRILCAM_LIBCAM_CAPTURE", "loopback").strip().lower()


def _loopback_label_prefix() -> str:
    return os.environ.get("APRILCAM_LIBCAM_LOOPBACK_LABEL", "aprilcam-")


def capture_size() -> tuple[int, int, int]:
    """(width, height, fps) for the gst-mode libcamera capture."""
    w = int(os.environ.get("APRILCAM_LIBCAM_WIDTH", "1280") or 1280)
    h = int(os.environ.get("APRILCAM_LIBCAM_HEIGHT", "720") or 720)
    fps = int(os.environ.get("APRILCAM_LIBCAM_FPS", "30") or 30)
    return w, h, fps


# -- enumeration -----------------------------------------------------------

_CACHE: dict = {"cams": None, "ts": 0.0}
_CACHE_TTL = 3.0  # retry interval when the last result was empty


def _list_loopback_cameras() -> List[LibcamCamera]:
    """Enumerate the labelled v4l2loopback devices via ``/sys`` (no subprocess).

    Reads ``/sys/class/video4linux/video*/name`` and keeps the devices whose
    card label starts with the configured prefix (default ``aprilcam-``). The
    bridge sets those labels (e.g. ``aprilcam-imx296-88000``).
    """
    prefix = _loopback_label_prefix()
    found: list[tuple[int, str]] = []
    for path in glob.glob("/sys/class/video4linux/video*/name"):
        try:
            name = open(path).read().strip()
        except Exception:
            continue
        if not name.startswith(prefix):
            continue
        m = re.search(r"/video(\d+)/name$", path)
        if not m:
            continue
        found.append((int(m.group(1)), name))
    found.sort()  # by device index
    cams: List[LibcamCamera] = []
    for pos, (dev_idx, name) in enumerate(found):
        slug = name[len(prefix):] if name.startswith(prefix) else name
        model = slug.split("-", 1)[0] or "camera"
        cams.append(
            LibcamCamera(
                position=pos,
                camera_id=name,           # stable label = unique id
                model=model,
                slug=slug,                # e.g. imx296-88000
                friendly_name=f"{model} ({_id_token(slug)})",
                device_index=dev_idx,     # /dev/video<dev_idx>
            )
        )
    return cams


def _cam_bin() -> Optional[str]:
    return shutil.which("cam")


def _list_cam_l_cameras() -> List[LibcamCamera]:
    """Enumerate via the libcamera ``cam -l`` tool (gst mode)."""
    cam = _cam_bin()
    if not cam:
        return []
    try:
        out = subprocess.run(
            [cam, "-l"], capture_output=True, text=True, timeout=20
        ).stdout
    except Exception:
        return []
    cams: List[LibcamCamera] = []
    for line in out.splitlines():
        m = re.match(r"\s*(\d+):\s+.*?'([^']+)'\s+\(([^)]+)\)", line)
        if not m:
            continue
        pos = int(m.group(1)) - 1
        model = m.group(2)
        cid = m.group(3)
        slug = f"{model}-{_id_token(cid)}"
        cams.append(
            LibcamCamera(
                position=pos,
                camera_id=cid,
                model=model,
                slug=slug,
                friendly_name=f"{model} ({_id_token(cid)})",
            )
        )
    cams.sort(key=lambda c: c.position)
    return cams


def list_cameras(*, use_cache: bool = True) -> List[LibcamCamera]:
    """Enumerate libcamera cameras for the active capture mode.

    A **non-empty** result is cached for the process lifetime (cameras are fixed
    hardware). An empty result is cached only briefly so a not-yet-ready bridge
    is retried. Loopback mode reads ``/sys`` (no subprocess); gst mode runs the
    ``cam`` tool.
    """
    if use_cache and _CACHE["cams"]:
        return _CACHE["cams"]
    if use_cache and _CACHE["cams"] == [] and (time.time() - _CACHE["ts"]) < _CACHE_TTL:
        return _CACHE["cams"]

    if capture_mode() == "loopback":
        cams = _list_loopback_cameras()
    else:
        cams = _list_cam_l_cameras()

    _CACHE["cams"] = cams
    _CACHE["ts"] = time.time()
    return cams


def camera_for_index(index: int) -> Optional[LibcamCamera]:
    for c in list_cameras():
        if c.position == index:
            return c
    return None


def backend_enabled() -> bool:
    """True when AprilCam should use the libcamera capture backend."""
    mode = os.environ.get("APRILCAM_CAMERA_BACKEND", "auto").strip().lower()
    if mode == "libcamera":
        return True
    if mode == "v4l2":
        return False
    return bool(list_cameras())  # auto


# -- capture ---------------------------------------------------------------

def loopback_index(position: int) -> int:
    """V4L2 device index for the loopback device backing the camera at *position*.

    Uses the device index discovered from ``/sys``; falls back to
    ``APRILCAM_LIBCAM_LOOPBACK_BASE + position`` (default base 70).
    """
    c = camera_for_index(position)
    if c is not None and c.device_index is not None:
        return c.device_index
    base = int(os.environ.get("APRILCAM_LIBCAM_LOOPBACK_BASE", "70") or 70)
    return base + position


def gst_pipeline(
    camera_id: str,
    width: Optional[int] = None,
    height: Optional[int] = None,
    fps: Optional[int] = None,
    fmt: str = "NV12",
) -> str:
    """Build the ``libcamerasrc`` → BGR ``appsink`` pipeline (gst mode)."""
    dw, dh, dfps = capture_size()
    width = width or dw
    height = height or dh
    fps = fps or dfps
    return (
        f'libcamerasrc camera-name="{camera_id}" '
        f"! video/x-raw,format={fmt},width={width},height={height},framerate={fps}/1 "
        f"! videoconvert ! video/x-raw,format=BGR "
        f"! appsink drop=1 max-buffers=1"
    )
