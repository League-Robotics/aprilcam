"""libcamera (Raspberry Pi CSI) camera backend.

On a Raspberry Pi the CSI cameras (e.g. the IMX296 global-shutter modules) are
**not** reachable through OpenCV ``VideoCapture(index)`` / V4L2 — the kernel
exposes dozens of ISP pipeline ``/dev/video*`` nodes, none of which is a plain
capturable device. The cameras are driven by **libcamera**; the supported way
to grab frames into OpenCV is a GStreamer ``libcamerasrc`` pipeline.

This module is the seam that lets the rest of AprilCam keep its index-based
camera model while actually talking to libcamera:

* :func:`list_cameras` enumerates the real cameras via the ``cam`` tool, so the
  daemon reports *exactly* the physical cameras (two on a dual-CSI Pi 5), not
  the V4L2 node soup.
* :func:`gst_pipeline` builds the ``libcamerasrc`` → BGR ``appsink`` pipeline
  that ``cv2.VideoCapture(..., cv2.CAP_GSTREAMER)`` consumes.
* :func:`backend_enabled` selects this backend. Default ``auto`` turns it on
  only when ``cam`` lists cameras *and* the ``libcamerasrc`` GStreamer element
  is present — so non-Pi hosts (macOS, USB-webcam Linux) are unaffected.

Select explicitly with ``APRILCAM_CAMERA_BACKEND`` = ``auto`` | ``libcamera``
| ``v4l2``. Resolution/fps via ``APRILCAM_LIBCAM_WIDTH`` / ``_HEIGHT`` / ``_FPS``.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class LibcamCamera:
    """One libcamera-enumerated camera."""

    position: int     # 0-based enumeration order (stable per i2c address)
    camera_id: str    # libcamera id path, e.g. /base/axi/.../imx296@1a
    model: str        # sensor model, e.g. "imx296"

    @property
    def slug(self) -> str:
        """Stable, unique, filesystem-safe per-camera key (the dir name)."""
        return f"{self.model}-{_id_token(self.camera_id)}"

    @property
    def friendly_name(self) -> str:
        return f"{self.model} ({_id_token(self.camera_id)})"


def _id_token(camera_id: str) -> str:
    """A short, stable token distinguishing cameras of the same model.

    Prefers the I2C address (``i2c@88000`` -> ``88000``), which is fixed per
    physical CSI port; falls back to a slug of the tail of the id path.
    """
    m = re.search(r"i2c@([0-9a-fA-F]+)", camera_id)
    if m:
        return m.group(1)
    tail = re.sub(r"[^a-z0-9]+", "-", camera_id.lower()).strip("-")
    return tail[-12:] or "cam"


# -- enumeration -----------------------------------------------------------

_CACHE: dict = {"cams": None, "ts": 0.0}
_CACHE_TTL = 3.0  # seconds; cameras don't change at runtime, avoid re-running `cam`


def _cam_bin() -> Optional[str]:
    return shutil.which("cam")


def list_cameras(*, use_cache: bool = True) -> List[LibcamCamera]:
    """Enumerate libcamera cameras via the ``cam -l`` tool (cached briefly)."""
    if use_cache and _CACHE["cams"] is not None and (time.time() - _CACHE["ts"]) < _CACHE_TTL:
        return _CACHE["cams"]

    cams: List[LibcamCamera] = []
    cam = _cam_bin()
    if cam:
        try:
            out = subprocess.run(
                [cam, "-l"], capture_output=True, text=True, timeout=20
            ).stdout
            # Lines like: "1: External camera 'imx296' (/base/axi/.../imx296@1a)"
            for line in out.splitlines():
                m = re.match(r"\s*(\d+):\s+.*?'([^']+)'\s+\(([^)]+)\)", line)
                if m:
                    cams.append(
                        LibcamCamera(
                            position=int(m.group(1)) - 1,  # `cam` is 1-based
                            model=m.group(2),
                            camera_id=m.group(3),
                        )
                    )
            cams.sort(key=lambda c: c.position)
        except Exception:
            cams = []

    _CACHE["cams"] = cams
    _CACHE["ts"] = time.time()
    return cams


def camera_for_index(index: int) -> Optional[LibcamCamera]:
    for c in list_cameras():
        if c.position == index:
            return c
    return None


def _gst_has_libcamerasrc() -> bool:
    gi = shutil.which("gst-inspect-1.0")
    if not gi:
        return False
    try:
        return subprocess.run(
            [gi, "libcamerasrc"], capture_output=True, timeout=10
        ).returncode == 0
    except Exception:
        return False


def backend_enabled() -> bool:
    """True when AprilCam should use the libcamera/GStreamer capture backend."""
    mode = os.environ.get("APRILCAM_CAMERA_BACKEND", "auto").strip().lower()
    if mode == "libcamera":
        return True
    if mode == "v4l2":
        return False
    # auto: only when libcamera actually has cameras and the gst element exists.
    return bool(list_cameras()) and _gst_has_libcamerasrc()


# -- capture ---------------------------------------------------------------

def capture_size() -> tuple[int, int, int]:
    """(width, height, fps) for the libcamera capture, from env or defaults."""
    w = int(os.environ.get("APRILCAM_LIBCAM_WIDTH", "1280") or 1280)
    h = int(os.environ.get("APRILCAM_LIBCAM_HEIGHT", "720") or 720)
    fps = int(os.environ.get("APRILCAM_LIBCAM_FPS", "30") or 30)
    return w, h, fps


def gst_pipeline(
    camera_id: str,
    width: Optional[int] = None,
    height: Optional[int] = None,
    fps: Optional[int] = None,
    fmt: str = "NV12",
) -> str:
    """Build the ``libcamerasrc`` → BGR ``appsink`` pipeline for OpenCV.

    The explicit ``format``/``width``/``height`` caps are required — without
    them ``libcamerasrc`` fixates to a raw Bayer format and fails to negotiate.
    """
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
