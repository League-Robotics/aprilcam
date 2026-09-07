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
Real-hardware validation (ticket 023-001) found ``cv2.VideoCapture.set()``
does not actually work for exposure/gain/white-balance on this project's
macOS cameras at all -- see ``camera/backends/uvc_util.py`` for the
mechanism that replaces this map for those controls on macOS
(``Camera``'s optional ``control_backend``). This map still backs
resolution (``frame_width``/``frame_height``, ticket 023-002 -- always
routed through *this* backend regardless of ``control_backend``, since
it is a capture property, not a UVC control) and remains the only
settings surface at all on non-macOS platforms.

**Identity-safe reopen (sprint 039).** ``OpenCVBackend``'s addressing
unit -- an OS-assigned positional index -- is unstable across USB
replugs/enumeration churn (the source incident: a sibling camera's
departure/return shifted indices enough that a reopen silently served a
*different* physical camera's frames under the original slot's slug).
``__init__`` accepts two additive, optional keyword parameters:
*index_resolver* (called fresh on every :meth:`OpenCVBackend.open` --
including a self-heal/stall-triggered reopen of this same object --
instead of reusing a stale index; ``None`` means "not currently
enumerated", which raises rather than opening anything) and
*expected_hardware_key* (a post-open AVFoundation ``uniqueID`` identity
check, macOS only, refusing and closing a mismatched open rather than
completing it). Both default to ``None``, preserving today's exact
behaviour for every existing direct construction. ``daemon/service.py``'s
``_camera_for`` is the sole production wiring point -- see that
method's own comments -- reusing its existing ``_index_for`` helper
rather than a second implementation of "find the current index for this
hardware key". See :meth:`OpenCVBackend.open`'s docstring for the full
mechanism.
"""

from __future__ import annotations

import logging
import sys
from typing import Callable, Dict, Optional, Set, Tuple

import cv2 as cv
import numpy as np

from aprilcam.camera.identity import resolve_identity

from . import CameraNotEnumeratedError, CaptureBackend

__all__ = ["OpenCVBackend", "default_backend_priority", "property_map"]

logger = logging.getLogger(__name__)


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

    ``frame_width``/``frame_height`` (ticket 023-002) are a capture
    property, not a UVC control -- included here, not in
    ``camera.backends.uvc_util``'s ``_CONTROLS`` table, so
    ``Camera.apply_config``/``effective_config`` always route them
    through this backend even when a narrower ``control_backend`` (e.g.
    ``UvcUtilBackend``) is wired for everything else (``camera/camera.py``'s
    module docstring).
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
        "frame_width": getattr(cv, "CAP_PROP_FRAME_WIDTH", 3),
        "frame_height": getattr(cv, "CAP_PROP_FRAME_HEIGHT", 4),
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
        enumeration index). Only the initial value when *index_resolver*
        is given -- :meth:`open` overwrites it with whatever the
        resolver returns, every call (see *index_resolver* below).
    width, height:
        Requested capture resolution, applied after a successful open.
        Best-effort: a camera that ignores the request keeps whatever
        ``frame_size()`` reports afterward.
    backend:
        A specific OpenCV backend constant to force (mainly for
        tests). ``None`` tries :func:`default_backend_priority` in
        order, matching v1.
    index_resolver:
        Optional (sprint 039, "identity-safe reopen" -- see
        ``camera-DESIGN.md``). When given, :meth:`open` calls it
        immediately before opening -- on *every* call, including a
        self-heal/stall-triggered reopen (``camera.camera.Camera.
        _attempt_self_heal``/``check_stall`` close and reopen this same
        backend object) -- instead of reusing *index* from construction
        or from whatever the previous open resolved. ``None`` (the
        default -- every existing direct ``OpenCVBackend(index)``
        construction, including ``testdev/capture_matrix.py``) preserves
        today's behaviour exactly: *index* is fixed at construction and
        never re-resolved. A resolver returning ``None`` (the hardware
        key it is bound to is not currently enumerated) makes
        :meth:`open` raise :class:`~aprilcam.camera.backends.
        CameraNotEnumeratedError` rather than attempting to open
        anything -- never a stale index. ``daemon/service.py``'s
        ``_camera_for`` wires this to a closure over its existing
        ``_index_for(record)`` helper, so this is the *only* place that
        re-resolution logic lives -- see that method's own comments.
    expected_hardware_key:
        Optional (sprint 039). When given *and* this process can verify
        identity after opening (macOS AVFoundation only -- see
        :meth:`open`), a mismatch between this value and the just-opened
        index's freshly-resolved ``hardware_key`` closes the just-opened
        session and raises loudly rather than completing the open --
        turning a silent camera substitution into an explicit failure.
        Every other platform/identity-source combination skips the
        check (degrades to "re-resolved, not verified"), never raises
        because of this parameter alone. ``None`` (the default) disables
        the check entirely.

    Both *index_resolver* and *expected_hardware_key* are purely
    additive, ``OpenCVBackend``-constructor-only parameters -- they are
    not part of :class:`~aprilcam.camera.backends.CaptureBackend`'s
    five-method behavioural interface.
    """

    def __init__(
        self,
        index: int,
        *,
        width: Optional[int] = None,
        height: Optional[int] = None,
        backend: Optional[int] = None,
        index_resolver: Optional[Callable[[], Optional[int]]] = None,
        expected_hardware_key: Optional[str] = None,
    ) -> None:
        self._index = index
        self._width = width
        self._height = height
        self._backend = backend
        self._index_resolver = index_resolver
        self._expected_hardware_key = expected_hardware_key
        self._cap: Optional[cv.VideoCapture] = None

    @property
    def index(self) -> int:
        return self._index

    @property
    def is_open(self) -> bool:
        return self._cap is not None and self._cap.isOpened()

    def open(self) -> None:
        """Re-resolve the index (if an *index_resolver* was given), then
        try each candidate backend in turn; raise if none opens.

        **Fresh re-resolution, every call (sprint 039).** When
        *index_resolver* is set, it is called here -- unconditionally,
        on every :meth:`open`, including a self-heal/stall reopen that
        closes and reopens this exact object -- and :attr:`index` is
        updated to whatever it returns *before* any ``cv2.VideoCapture``
        call is made. A resolver reporting ``None`` (hardware key not
        currently enumerated) raises :class:`~aprilcam.camera.backends.
        CameraNotEnumeratedError` immediately, without attempting to
        open the last-known (now stale) index -- this is the fix for the
        source incident (a sibling camera's departure/return shifted
        AVFoundation's positional indices enough that a reopen silently
        served a different physical camera's frames under the original
        slot's slug).

        Matches v1's backend-priority loop exactly (``camera.py.open()``):
        the first backend that reports ``isOpened()`` wins, every failed
        candidate is released immediately rather than left dangling.

        **Post-open identity verification (sprint 039).** After a
        successful open, when *expected_hardware_key* is given and this
        platform/identity-source combination can verify it (macOS only,
        and only when *expected_hardware_key* is itself an
        ``avfoundation_unique_id``-tier key -- see
        :meth:`_can_verify_identity`), the just-opened index's identity
        is re-resolved fresh (the same ``cv2-enumerate-cameras``/
        ``system_profiler`` machinery ``camera/identity.py`` already
        uses) and compared. A mismatch closes the just-opened session
        and raises -- logged at WARNING naming both identities -- rather
        than completing the open, so a silent substitution becomes an
        explicit, loud failure instead.
        """
        if self.is_open:
            return
        if self._index_resolver is not None:
            resolved = self._index_resolver()
            if resolved is None:
                raise CameraNotEnumeratedError(
                    f"camera hardware key is not currently enumerated "
                    f"(last known index {self._index})"
                )
            self._index = resolved
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
        if self._expected_hardware_key is not None and self._can_verify_identity():
            observed = resolve_identity(self._index)
            if observed.hardware_key != self._expected_hardware_key:
                c_index = self._index
                cap.release()
                logger.warning(
                    "OpenCVBackend: post-open identity mismatch at index %d -- "
                    "expected hardware_key %r, observed %r; refusing to complete "
                    "the open (closing the just-opened session rather than "
                    "silently serving a different physical camera's frames)",
                    c_index,
                    self._expected_hardware_key,
                    observed.hardware_key,
                )
                raise RuntimeError(
                    f"post-open identity mismatch at index {c_index}: expected "
                    f"hardware_key {self._expected_hardware_key!r}, observed "
                    f"{observed.hardware_key!r}"
                )
        self._cap = cap

    def _can_verify_identity(self) -> bool:
        """Whether this platform/*expected_hardware_key* combination can
        be checked post-open (sprint 039).

        Only macOS AVFoundation's ``uniqueID`` tier is reliable enough
        to re-resolve per-index after the fact -- ``camera/identity.py``'s
        other tiers (``vidpid``/``loc``/``name``) are documented fallback
        tiers that can legitimately change value across a re-enumeration
        for reasons unrelated to a substitution bug (a USB re-plug moving
        *this* camera's own ``location``), so checking them here would
        produce false-positive mismatches for exactly the cameras the
        module docstring already calls out as fallback-tier/port-move
        sensitive. See :meth:`open`'s docstring and the module docstring's
        "Post-open identity verification" note.
        """
        return sys.platform == "darwin" and (self._expected_hardware_key or "").startswith("avf:")

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
