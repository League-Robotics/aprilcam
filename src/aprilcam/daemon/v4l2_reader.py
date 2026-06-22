"""Raw V4L2 frame reader via a ``v4l2-ctl`` subprocess.

Why this exists
---------------
On the Raspberry Pi the daemon reads camera frames from a v4l2loopback device
that an out-of-process ``libcamerasrc`` bridge feeds (see
``docs/knowledge/raspberry-pi-camera-setup.md``).  The daemon venv uses **pip**
``opencv-contrib-python`` (required: the system OpenCV 4.6 ABI-clashes with the
pip ``grpcio`` wheel and segfaults in ``cv2.aruco``).  But pip OpenCV's V4L2
backend **cannot read this loopback** — it opens the device yet every
``read()`` returns ``select() timeout`` (0 frames), whereas ``v4l2-ctl`` and the
system OpenCV read it fine.

So we capture the raw stream with ``v4l2-ctl`` (proven reliable) and only use
pip OpenCV for the cheap colour conversion + AprilTag/ArUco detection.

``V4l2CtlCapture`` mimics the slice of the ``cv2.VideoCapture`` API the pipeline
and ``AprilCam`` core use (``isOpened``/``read``/``get``/``set``/``release``),
so it is a drop-in for the loopback branch.
"""

from __future__ import annotations

import logging
import subprocess
from typing import Optional, Tuple

import cv2 as cv
import numpy as np

log = logging.getLogger(__name__)

# Pixel formats we know how to convert to BGR, mapped to the cvtColor code.
# NOTE: the YUY2 stream the libcamerasrc->videoconvert bridge writes comes out
# with red/blue swapped under COLOR_YUV2BGR_YUYV, so we use the _RGB_ variant to
# get true BGR frames (the daemon's frame contract). Detection is luma-based and
# unaffected; this corrects the colour seen by get_frame / the live view.
_YUYV_CODES = {
    "YUYV": cv.COLOR_YUV2RGB_YUYV,
    "YUY2": cv.COLOR_YUV2RGB_YUYV,
}


def _query_format(device: str) -> Tuple[int, int, str]:
    """Return (width, height, pixelformat) for *device* via ``v4l2-ctl``."""
    width, height, pixfmt = 1280, 720, "YUYV"
    try:
        out = subprocess.run(
            ["v4l2-ctl", "-d", device, "--get-fmt-video"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception as exc:  # pragma: no cover - tooling/perms
        log.warning("v4l2_reader: --get-fmt-video failed for %s: %s", device, exc)
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
            # e.g.  Pixel Format      : 'YUYV' (YUYV 4:2:2)
            try:
                pixfmt = s.split("'")[1]
            except Exception:
                pass
    return width, height, pixfmt


class V4l2CtlCapture:
    """A ``cv2.VideoCapture``-compatible reader backed by ``v4l2-ctl``.

    Streams raw uncompressed frames from ``v4l2-ctl --stream-to=-`` and reshapes
    each fixed-size frame into a BGR ndarray.  Only YUYV/YUY2 (2 bytes/pixel) is
    supported — that is what the libcamera bridge writes.
    """

    def __init__(self, device: str):
        self._device = device
        self._width, self._height, self._pixfmt = _query_format(device)
        self._cvt = _YUYV_CODES.get(self._pixfmt.upper())
        if self._cvt is None:
            raise RuntimeError(
                f"V4l2CtlCapture: unsupported pixel format {self._pixfmt!r} on "
                f"{device} (expected YUYV)"
            )
        # YUYV packs 2 bytes per pixel.
        self._frame_bytes = self._width * self._height * 2
        self._proc: Optional[subprocess.Popen] = None
        self._open()

    def _open(self) -> None:
        cmd = [
            "v4l2-ctl", "-d", self._device,
            "--stream-mmap", "--stream-count=0", "--stream-to=-",
        ]
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
            )
            log.info(
                "V4l2CtlCapture: streaming %s (%dx%d %s) via v4l2-ctl",
                self._device, self._width, self._height, self._pixfmt,
            )
        except Exception as exc:
            log.error("V4l2CtlCapture: failed to launch v4l2-ctl for %s: %s",
                      self._device, exc)
            self._proc = None

    # -- cv2.VideoCapture-compatible surface ------------------------------
    def isOpened(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _read_exact(self, n: int) -> Optional[bytes]:
        assert self._proc is not None and self._proc.stdout is not None
        buf = bytearray()
        while len(buf) < n:
            chunk = self._proc.stdout.read(n - len(buf))
            if not chunk:
                return None
            buf.extend(chunk)
        return bytes(buf)

    def read(self) -> Tuple[bool, Optional[np.ndarray]]:
        if not self.isOpened():
            return False, None
        raw = self._read_exact(self._frame_bytes)
        if raw is None:
            return False, None
        yuyv = np.frombuffer(raw, dtype=np.uint8).reshape(self._height, self._width, 2)
        return True, cv.cvtColor(yuyv, self._cvt)

    def get(self, prop: int) -> float:
        if prop == cv.CAP_PROP_FRAME_WIDTH:
            return float(self._width)
        if prop == cv.CAP_PROP_FRAME_HEIGHT:
            return float(self._height)
        return 0.0

    def set(self, prop: int, value: float) -> bool:  # noqa: D401 - no-op shim
        # Format is fixed by the writer; the daemon never changes it.
        return True

    def release(self) -> None:
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

    def reopen(self) -> bool:
        """Relaunch the ``v4l2-ctl`` subprocess and report whether it is running.

        Used to recover after the loopback feed drops out and returns — e.g.
        when the out-of-process ``libcamerasrc`` bridge crashes and its watchdog
        restarts it. The old subprocess has exited (its reads return EOF), so we
        terminate it, re-query the format (a new writer may have reconfigured the
        device), and start a fresh stream.
        """
        self.release()
        try:
            self._width, self._height, self._pixfmt = _query_format(self._device)
            self._cvt = _YUYV_CODES.get(self._pixfmt.upper())
            if self._cvt is None:
                log.warning(
                    "V4l2CtlCapture: reopen of %s saw unsupported format %r",
                    self._device, self._pixfmt,
                )
                return False
            self._frame_bytes = self._width * self._height * 2
        except Exception as exc:  # pragma: no cover - tooling/perms
            log.warning("V4l2CtlCapture: reopen format query failed for %s: %s",
                        self._device, exc)
            return False
        self._open()
        return self.isOpened()
