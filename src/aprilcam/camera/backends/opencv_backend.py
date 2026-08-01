"""``cv2.VideoCapture``-backed capture — USB/AVFoundation/V4L2 hardware.

Converted from ``archive/src/aprilcam/camera/video_camera.py`` (the
``Camera``-wraps-``cv2.VideoCapture`` shape) and the backend-priority
loop in ``archive/src/aprilcam/camera/camera.py``'s ``open()``
(itself sourced from ``camutil.default_backends()``), reshaped to the
:class:`~aprilcam.camera.backends.CaptureBackend` adapter surface.

This is the backend for both of this Mac's fixture cameras
(``arducam-ov9782-usb-camera``, ``hd-usb-camera``) and the only backend
exercised against real hardware by this ticket's ``@pytest.mark.camera``
tests.

Also exposes :func:`property_map`: a **best-effort** name → OpenCV
property-id table for the handful of UVC-ish controls OpenCV's
``VideoCapture`` generically exposes a ``set``/``get`` for (exposure,
gain, white balance, ...). This is *not* a per-camera-model "what does
this hardware actually support" answer — OpenCV's ``get``/``set`` do
not reliably report that, and pretending otherwise would be exactly the
kind of plausible-looking wrong answer ``errors.py`` exists to kill.
Real per-model settable-key enumeration (UC-25.S-2's "each camera gets
its own key set") needs a proper UVC introspection tool (v1 used
``uvc-util`` on macOS) and is deferred to the config-push ticket; this
map only backs the modest, best-effort ``apply_config`` this sprint
scopes to (see ``camera/camera.py``).
"""

from __future__ import annotations

import sys
from typing import Dict, Optional, Set, Tuple

import cv2 as cv
import numpy as np

from . import CaptureBackend

__all__ = ["OpenCVBackend", "default_backend_priority", "property_map"]


def default_backend_priority() -> list:
    """OpenCV backend constants to try, in order, for this platform.

    Converted from ``camutil.default_backends()``. ``CAP_ANY`` is
    always the last resort so an unrecognised platform still gets a
    chance via OpenCV's own auto-detection.
    """
    if sys.platform == "win32":
        return [getattr(cv, "CAP_MSMF", 1400), getattr(cv, "CAP_DSHOW", 700), getattr(cv, "CAP_ANY", 0)]
    if sys.platform == "darwin":
        return [getattr(cv, "CAP_AVFOUNDATION", 1200), getattr(cv, "CAP_ANY", 0)]
    return [getattr(cv, "CAP_V4L2", 200), getattr(cv, "CAP_ANY", 0)]


def property_map() -> Dict[str, int]:
    """Setting name -> ``cv2.CAP_PROP_*`` id, for the controls OpenCV
    exposes a generic accessor for.

    ``getattr`` with a fallback numeric id throughout: these constants
    have been stable across OpenCV releases for years, but resolving
    them defensively means a very old/new binding missing one symbol
    degrades that one entry rather than failing this whole module.
    """
    return {
        "brightness": getattr(cv, "CAP_PROP_BRIGHTNESS", 10),
        "contrast": getattr(cv, "CAP_PROP_CONTRAST", 11),
        "saturation": getattr(cv, "CAP_PROP_SATURATION", 12),
        "gain": getattr(cv, "CAP_PROP_GAIN", 14),
        "exposure": getattr(cv, "CAP_PROP_EXPOSURE", 15),
        "auto_exposure": getattr(cv, "CAP_PROP_AUTO_EXPOSURE", 21),
        "white_balance_temperature": getattr(cv, "CAP_PROP_WB_TEMPERATURE", 45),
        "auto_white_balance": getattr(cv, "CAP_PROP_AUTO_WB", 44),
        "focus": getattr(cv, "CAP_PROP_FOCUS", 28),
        "autofocus": getattr(cv, "CAP_PROP_AUTOFOCUS", 39),
    }


class OpenCVBackend(CaptureBackend):
    """Adapts ``cv2.VideoCapture`` to :class:`CaptureBackend`.

    Derives from ``CaptureBackend`` (a plain base class, not
    ``Protocol`` -- see ``daemon.interfaces``'s module docstring for the
    design rule).

    Parameters
    ----------
    index:
        The OpenCV camera index (matches ``camera/identity.py``'s
        enumeration index).
    width, height:
        Requested capture resolution, applied after a successful open.
        Best-effort: a camera that ignores the request keeps whatever
        ``frame_size()`` reports afterward.
    backend:
        A specific OpenCV backend constant to force (mainly for
        tests). ``None`` tries :func:`default_backend_priority` in
        order, matching v1.
    """

    def __init__(
        self,
        index: int,
        *,
        width: Optional[int] = None,
        height: Optional[int] = None,
        backend: Optional[int] = None,
    ) -> None:
        self._index = index
        self._width = width
        self._height = height
        self._backend = backend
        self._cap: Optional[cv.VideoCapture] = None

    @property
    def index(self) -> int:
        return self._index

    @property
    def is_open(self) -> bool:
        return self._cap is not None and self._cap.isOpened()

    def open(self) -> None:
        """Try each candidate backend in turn; raise if none opens.

        Matches v1's loop exactly (``camera.py.open()``): the first
        backend that reports ``isOpened()`` wins, every failed
        candidate is released immediately rather than left dangling.
        """
        if self.is_open:
            return
        candidates = [self._backend] if self._backend is not None else default_backend_priority()
        cap: Optional[cv.VideoCapture] = None
        for be in candidates:
            c = cv.VideoCapture(self._index, be)
            if c.isOpened():
                cap = c
                break
            c.release()
        if cap is None:
            raise RuntimeError(f"failed to open camera at index {self._index}")
        if self._width is not None:
            cap.set(cv.CAP_PROP_FRAME_WIDTH, self._width)
        if self._height is not None:
            cap.set(cv.CAP_PROP_FRAME_HEIGHT, self._height)
        # Match v1's capture_loop: one buffered frame, so read() always
        # returns the most recent frame rather than a stale queued one.
        cap.set(cv.CAP_PROP_BUFFERSIZE, 1)
        self._cap = cap

    def read(self) -> Optional[np.ndarray]:
        if self._cap is None:
            return None
        ok, frame = self._cap.read()
        if not ok or frame is None:
            return None
        return frame

    def close(self) -> None:
        if self._cap is not None:
            try:
                self._cap.release()
            finally:
                self._cap = None

    def frame_size(self) -> Optional[Tuple[int, int]]:
        if self._cap is None:
            return None
        w = int(self._cap.get(cv.CAP_PROP_FRAME_WIDTH))
        h = int(self._cap.get(cv.CAP_PROP_FRAME_HEIGHT))
        if w <= 0 or h <= 0:
            return None
        return (w, h)

    # -- best-effort settings, beyond the CaptureBackend base class -----
    # Used only by Camera.apply_config()/effective_config() (camera.py),
    # never called directly by discovery or by anything client-facing.

    def get_property(self, name: str) -> Optional[float]:
        """Current value of a named property, or ``None`` if unopened
        or the name is not in :func:`property_map`."""
        prop = property_map().get(name)
        if prop is None or self._cap is None:
            return None
        return float(self._cap.get(prop))

    def set_property(self, name: str, value: float) -> bool:
        """Attempt to set a named property.

        Returns whether OpenCV's ``set()`` reported success. Unknown
        names or an unopened device return ``False`` rather than
        raising — ``Camera.apply_config`` is what turns a ``False``
        into the typed ``UnsupportedSetting`` its callers see.
        """
        prop = property_map().get(name)
        if prop is None or self._cap is None:
            return False
        return bool(self._cap.set(prop, value))

    def settable_names(self) -> Set[str]:
        """Setting names this backend can attempt (see :func:`property_map`).

        Read by ``Camera.apply_config`` to validate a config document's
        keys before touching hardware -- see that module's docstring
        for why this is a generic "OpenCV exposes a set() for these"
        list rather than a verified per-camera-model answer.
        """
        return set(property_map())

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        status = "open" if self.is_open else "closed"
        return f"OpenCVBackend(index={self._index}, {status})"
