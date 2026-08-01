"""Raw V4L2 frame reader via a ``v4l2-ctl`` subprocess.

Converted from ``archive/src/aprilcam/daemon/v4l2_reader.py``
(``V4l2CtlCapture``) into the :class:`~aprilcam.camera.backends.CaptureBackend`
adapter shape — open/read/close/frame_size, lifecycle-managed by
``camera/camera.py`` rather than opened eagerly in ``__init__`` the way
v1's class was.

Why this exists (unchanged from v1): on a Raspberry Pi the daemon reads
frames from a v4l2loopback device that an out-of-process
``libcamerasrc`` bridge feeds. Pip ``opencv-contrib-python``'s V4L2
backend cannot read this loopback reliably — every ``read()`` reports a
``select()`` timeout — whereas ``v4l2-ctl`` reads it fine. So this
backend captures the raw stream with ``v4l2-ctl --stream-to=-`` and
reshapes each fixed-size frame into a BGR ``ndarray`` itself.

**Not exercised against real frames this ticket** — there is no Linux/
v4l2loopback hardware on this Mac to validate against, and the ticket's
"convert, don't rewrite blindly" instruction does not carve out a
deferral for this module the way it does for ``libcam.py``/
``screencap.py``. The format-parsing and frame-reshaping logic (the
parts with no OS/hardware dependency) are unit-tested headlessly
against a faked subprocess; the actual "does ``v4l2-ctl`` produce
correct bytes on a real loopback device" question is unverifiable here
and is carried forward to whichever sprint next brings up Pi hardware.
"""

from __future__ import annotations

import logging
import subprocess
from typing import Optional, Tuple

import cv2 as cv
import numpy as np

from . import CaptureBackend  # noqa: F401  (Protocol this module satisfies)

__all__ = ["V4l2LoopbackBackend"]

log = logging.getLogger(__name__)

# Pixel formats this backend knows how to convert to BGR, mapped to the
# cvtColor code. The YUY2 stream the libcamerasrc->videoconvert bridge
# writes comes out with red/blue swapped under COLOR_YUV2BGR_YUYV, so
# the _RGB_ variant is used to get true BGR frames (unchanged from v1).
_YUYV_CODES = {
    "YUYV": cv.COLOR_YUV2RGB_YUYV,
    "YUY2": cv.COLOR_YUV2RGB_YUYV,
}

_DEFAULT_WIDTH = 1280
_DEFAULT_HEIGHT = 720
_DEFAULT_PIXFMT = "YUYV"


def _query_format(device: str) -> Tuple[int, int, str]:
    """Return ``(width, height, pixelformat)`` for *device* via
    ``v4l2-ctl --get-fmt-video``. Degrades to the module defaults on
    any subprocess failure or unparseable output — never raises."""
    width, height, pixfmt = _DEFAULT_WIDTH, _DEFAULT_HEIGHT, _DEFAULT_PIXFMT
    try:
        out = subprocess.run(
            ["v4l2-ctl", "-d", device, "--get-fmt-video"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except Exception as exc:  # pragma: no cover - tooling/perms
        log.warning("v4l2_loopback: --get-fmt-video failed for %s: %s", device, exc)
        return width, height, pixfmt
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("Width/Height"):
            try:
                wh = s.split(":", 1)[1]
                width, height = (int(x) for x in wh.replace(" ", "").split("/")[:2])
            except Exception:
                pass
        elif s.startswith("Pixel Format"):
            try:
                pixfmt = s.split("'")[1]
            except Exception:
                pass
    return width, height, pixfmt


class V4l2LoopbackBackend:
    """Adapts ``v4l2-ctl --stream-to=-`` to :class:`CaptureBackend`.

    Parameters
    ----------
    device:
        The ``/dev/videoN`` loopback device path.
    """

    def __init__(self, device: str) -> None:
        self._device = device
        self._width = _DEFAULT_WIDTH
        self._height = _DEFAULT_HEIGHT
        self._pixfmt = _DEFAULT_PIXFMT
        self._cvt: Optional[int] = None
        self._frame_bytes = 0
        self._proc: Optional[subprocess.Popen] = None

    @property
    def is_open(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def open(self) -> None:
        """Query the current stream format and launch the reader.

        Raises ``RuntimeError`` if the queried pixel format has no
        known BGR conversion, or if the subprocess fails to launch —
        the two ways this backend can be "present but unopenable"
        (UC-25.S-4), matching :class:`CaptureBackend.open`'s contract.
        """
        if self.is_open:
            return
        self._width, self._height, self._pixfmt = _query_format(self._device)
        self._cvt = _YUYV_CODES.get(self._pixfmt.upper())
        if self._cvt is None:
            raise RuntimeError(
                f"unsupported pixel format {self._pixfmt!r} on {self._device} "
                f"(expected YUYV/YUY2)"
            )
        self._frame_bytes = self._width * self._height * 2  # YUYV: 2 bytes/pixel
        cmd = [
            "v4l2-ctl", "-d", self._device,
            "--stream-mmap", "--stream-count=0", "--stream-to=-",
        ]
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
            )
        except Exception as exc:
            self._proc = None
            raise RuntimeError(
                f"failed to launch v4l2-ctl for {self._device}: {exc}"
            ) from exc
        if not self.is_open:
            raise RuntimeError(f"v4l2-ctl exited immediately for {self._device}")

    def _read_exact(self, n: int) -> Optional[bytes]:
        assert self._proc is not None and self._proc.stdout is not None
        buf = bytearray()
        while len(buf) < n:
            chunk = self._proc.stdout.read(n - len(buf))
            if not chunk:
                return None
            buf.extend(chunk)
        return bytes(buf)

    def read(self) -> Optional[np.ndarray]:
        if not self.is_open or self._cvt is None:
            return None
        raw = self._read_exact(self._frame_bytes)
        if raw is None:
            return None
        yuyv = np.frombuffer(raw, dtype=np.uint8).reshape(self._height, self._width, 2)
        return cv.cvtColor(yuyv, self._cvt)

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def frame_size(self) -> Optional[Tuple[int, int]]:
        if not self.is_open:
            return None
        return (self._width, self._height)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        status = "open" if self.is_open else "closed"
        return f"V4l2LoopbackBackend(device={self._device!r}, {status})"
