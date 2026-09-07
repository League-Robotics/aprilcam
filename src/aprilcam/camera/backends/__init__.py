"""One small adapter per capture technology (sprint 020 architecture,
module table).

``camera/camera.py``'s ``Camera`` class owns a physical camera end to
end — self-heal reopen, config application, state assembly — and does
so uniformly across whatever hardware it is backed by. A **backend** is
the small, swappable piece underneath that: it knows how to open, read,
and close *one kind* of capture device and nothing else. This keeps a
libcamera bring-up or a v4l2 quirk fix from touching ``camera.py``, and
vice versa (module table: "each backend changes independently of the
others").

:class:`CaptureBackend` is deliberately smaller than the public
``Camera`` base class (``camera/interfaces.py``) — no ``apply_config``,
no ``state``, no self-heal policy. Those live one layer up. A backend
does not itself derive from ``Camera`` and is never handed directly to
a client; ``camera.py`` is the only consumer.

**Backends in this package, and what shipped this ticket (020-003):**

* :mod:`opencv_backend` — ``cv2.VideoCapture`` over USB/AVFoundation/
  V4L2 hardware. This is the backend both of this Mac's fixture
  cameras (``arducam-ov9782-usb-camera``, ``hd-usb-camera``) use, and
  the only one exercised against real hardware this ticket.
* :mod:`v4l2_loopback` — the ``v4l2-ctl`` subprocess reader for a Pi's
  v4l2loopback bridge device. Converted in full from v1's
  ``daemon/v4l2_reader.py`` (the ticket names no deferral option for
  it), but untestable against real frames on this Mac (no Linux/v4l2
  hardware) — its unit tests exercise the pure parsing/framing logic
  with a faked subprocess.
* :mod:`libcamera` — **enumeration only**. The ``/sys``-based
  v4l2loopback-label scan from v1's ``camera/libcam.py`` is ported
  (safe and headlessly testable: it degrades to an empty list on any
  non-Linux machine, this Mac included, via a plain ``glob`` miss).
  The ``cam``-tool / GStreamer "gst mode" half of v1's module, and an
  actual libcamera *capture* backend, are **not** ported this ticket —
  per the ticket's explicit allowance ("may be stubbed/deferred if it
  adds no test coverage this sprint"), since there is no Pi/libcamera
  device attached here to validate either against. The enumeration
  function is wired into ``camera/identity.py``'s ``_probe_libcamera``
  injection point (a safe, no-behaviour-change-on-this-Mac swap, since
  it is empty everywhere the v1 stub was empty); real capture is left
  for a future Raspberry Pi bring-up sprint.
* **Screen capture is not ported this ticket** — v1's
  ``camera/screencap.py`` (``mss``-based) is the lowest-priority item
  the ticket names and is not needed for any of this ticket's
  hardware-backed acceptance criteria. Deferred, no stub file; convert
  it if/when a use case actually needs a screen-capture "camera".
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Callable, List, Optional, Set, Tuple

import numpy as np

from aprilcam.camera.backends.libcamera import LibcameraDevice
from aprilcam.camera.backends.libcamera import list_loopback_cameras as _list_loopback_cameras

__all__ = [
    "CAPTURE_BACKEND_ENV",
    "CameraNotEnumeratedError",
    "CaptureBackend",
    "ControlBackend",
    "default_capture_backend_factory",
    "resolve_capture_backend",
]

logger = logging.getLogger(__name__)

#: The ``CameraIdentity``/``CameraRecord`` ``hardware_key`` prefix
#: ``camera/identity.py``'s ``_probe_libcamera`` stamps onto a
#: libcamera-sourced camera (``f"libcamera:{lc.camera_id}"``). See
#: :func:`resolve_capture_backend`.
_LIBCAMERA_HARDWARE_KEY_PREFIX = "libcamera:"

#: Env var naming which non-libcamera ``CaptureBackend`` construction
#: :func:`default_capture_backend_factory` picks -- ``"worker"`` forces
#: :class:`~aprilcam.camera.backends.worker_backend.WorkerBackend`,
#: ``"opencv"`` forces
#: :class:`~aprilcam.camera.backends.opencv_backend.OpenCVBackend`, unset
#: or any other value falls through to the platform default (sprint 038
#: ticket 005). Follows the project's established
#: ``APRILCAM_*``-env-var-plus-named-constant convention (e.g.
#: ``camera.backends.libcamera._LOOPBACK_LABEL_ENV``,
#: ``config.directories.APRILCAM_CONFIG_DIR_ENV``).
CAPTURE_BACKEND_ENV = "APRILCAM_CAPTURE_BACKEND"


class CameraNotEnumeratedError(RuntimeError):
    """Raised by :meth:`CaptureBackend.open` when an injected
    ``index_resolver`` reports that this camera's ``hardware_key`` is
    not currently enumerated (the resolver returned ``None``) -- sprint
    039's "identity-safe reopen" (see ``camera-DESIGN.md``'s section of
    that name and :mod:`aprilcam.camera.backends.opencv_backend`).

    A distinct type, not a bare :class:`RuntimeError`, specifically so
    :meth:`aprilcam.camera.camera.Camera._attempt_self_heal` can tell
    "this hardware key is not enumerated right now" apart from an
    ordinary open failure ("enumerated, but opening it failed") and
    react differently: the former is budget-neutral (does not spend a
    :data:`~aprilcam.camera.camera.MAX_REOPENS` attempt) and marks the
    camera not :attr:`~aprilcam.types.CameraState.present`, since a
    resolver reporting "not found" is positive evidence the hardware is
    gone, not merely "read failed for some reason" -- see
    ``camera-DESIGN.md``'s "Not-currently-enumerated is a distinct state
    from budget-exhausted" section for the stakeholder-fixed contract
    this implements.
    """


class CaptureBackend:
    """The internal surface every ``backends/*.py`` adapter provides.

    A plain base class, not ``Protocol`` — see ``daemon.interfaces``'s
    module docstring for the design rule. ``OpenCVBackend`` and
    ``V4l2LoopbackBackend`` derive from it explicitly.

    Three methods, on purpose: a backend is *just* device I/O. Nothing
    here reports settings, nothing here decides how many times to
    retry — that policy is ``Camera``'s (``camera/camera.py``), applied
    uniformly across every backend so self-heal behaviour does not
    fork per capture technology.
    """

    @property
    def is_open(self) -> bool:
        """Whether the device is currently open."""
        raise NotImplementedError(f"{type(self).__name__} does not implement CaptureBackend.is_open")

    def open(self) -> None:
        """Open the device.

        **Raises on failure** — never returns a sentinel "not open"
        value. This is the hook ``CameraDiscovery.probe()`` depends on
        to tell "present but unopenable" (UC-25.S-4) apart from a
        clean open: it calls this inside a ``try``/``except`` and
        reports ``usable=False`` on any exception, ``present`` coming
        from enumeration rather than from this call succeeding.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement CaptureBackend.open")

    def read(self) -> Optional[np.ndarray]:
        """One frame, or ``None`` on a read failure.

        Never raises for an ordinary failed read (a dropped frame, a
        momentarily wedged driver) — only ``open()`` raises. This
        mirrors ``Camera.read()``'s own contract one layer up, so
        ``Camera`` can pass failures straight through into its
        consecutive-failure counter without translating exception
        types per backend.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement CaptureBackend.read")

    def close(self) -> None:
        """Release the device. Safe to call when not open (no-op)."""
        raise NotImplementedError(f"{type(self).__name__} does not implement CaptureBackend.close")

    def frame_size(self) -> Optional[Tuple[int, int]]:
        """``(width, height)`` once open, else ``None``.

        Feeds ``Camera.effective_config()``'s introspected-facts
        reporting (AC3) — never guessed, only read back from the open
        device.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement CaptureBackend.frame_size")

    def heartbeat_age(self) -> Optional[float]:
        """Seconds since this backend last confirmed its capture session
        is alive, or ``None`` if this backend has no such concept
        (sprint 038 ticket 005).

        Unlike this class's other four methods, this one has a sensible
        default (``None``, "not supported") rather than
        ``NotImplementedError`` — a best-effort, opt-in capability, not
        part of the core capture contract every backend must honour, the
        same relationship :class:`ControlBackend`'s
        ``describe_set_failure`` has to *its* base class. ``OpenCVBackend``
        and ``V4l2LoopbackBackend`` inherit this default unmodified (they
        expose no way to tell "still alive" apart from "the next
        ``read()`` happens to work"), so they and every hand-rolled fake
        backend in the test suite are untouched by this method existing.
        :class:`~aprilcam.camera.backends.worker_backend.WorkerBackend` is
        the one override: it reports the age of its shared-memory
        ``FrameRing``'s last published sequence
        (``FrameRing.peek()`` — a cheap ``(sequence, timestamp)`` snapshot,
        no frame copy), which keeps advancing as long as the child
        process is actually capturing, independent of whether anything is
        currently calling :meth:`CaptureBackend.read`.

        ``camera.camera.Camera.check_stall`` duck-types this the same way
        ``apply_config`` already duck-types ``describe_set_failure`` —
        ``getattr(backend, "heartbeat_age", None)`` — so a backend that
        predates this method (impossible now that it lives on this base
        class, but true of any pre-existing hand-rolled fake that does
        not derive from ``CaptureBackend`` at all) is indistinguishable
        from one that inherits the ``None`` default.
        """
        return None


class ControlBackend:
    """The settings-only surface a ``Camera`` can optionally use for
    ``get_property``/``set_property``/``settable_names``, decoupled from
    :class:`CaptureBackend`'s capture-only contract (``is_open``/
    ``open``/``read``/``close``/``frame_size``).

    A plain base class, not ``Protocol`` — see the module docstring for
    the design rule. Exists because a control mechanism and a capture
    mechanism are not always the same object (ticket 023-001's
    real-hardware finding): :mod:`opencv_backend`'s ``OpenCVBackend``
    happens to be both — its ``get_property``/``set_property``/
    ``settable_names`` sit "beyond the ``CaptureBackend`` base class",
    per that module's own docstring, duck-typed rather than declared —
    but :mod:`uvc_util`'s ``UvcUtilBackend`` is control-only: it never
    opens a capture stream or reads a frame, so it derives from this
    instead of :class:`CaptureBackend`.

    ``camera.camera.Camera`` accepts an optional ``control_backend`` of
    this shape and, when given one, routes ``apply_config``'s settings
    surface through it exclusively rather than through the primary
    capture backend — see that module's docstring for why real hardware
    (camera 3, an OV9782) needed this: ``cv2.VideoCapture.set()`` reports
    failure for every exposure/gain/white-balance control on that
    hardware's AVFoundation capture session (ticket 023-001).
    """

    def get_property(self, name: str) -> Optional[float]:
        """Current value of *name*, or ``None`` if unsupported or
        unreadable right now. Never raises for an ordinary failure —
        mirrors ``CaptureBackend.read()``'s "only construction/``open``
        raise" discipline."""
        raise NotImplementedError(f"{type(self).__name__} does not implement ControlBackend.get_property")

    def set_property(self, name: str, value: float) -> bool:
        """Attempt to set *name* to *value*. Returns whether the
        hardware actually reports the new value afterward — **not**
        merely whether the underlying call claimed success (UC-03: "must
        never report success for a setting that did not take effect").
        Unknown names return ``False`` rather than raising; ``Camera.
        apply_config`` is what turns a ``False`` into the typed
        ``UnsupportedSetting`` its callers see."""
        raise NotImplementedError(f"{type(self).__name__} does not implement ControlBackend.set_property")

    def settable_names(self) -> Set[str]:
        """Setting names this backend can attempt."""
        raise NotImplementedError(f"{type(self).__name__} does not implement ControlBackend.settable_names")

    def describe_set_failure(self, name: str, value: float) -> Optional[str]:
        """Best-effort, human-readable reason the most recent
        ``set_property(name, value)`` returned ``False`` (ticket
        030-005) -- e.g. "300 is outside exposure-time-abs's supported
        range 1..100" or "auto_exposure is currently 8, not the manual
        value 1". ``Camera.apply_config`` folds a non-``None`` result
        straight into ``UnsupportedSetting.reason``.

        Unlike this class's other three methods, this one has a
        sensible default (``None``, meaning "nothing more specific
        available") rather than ``NotImplementedError`` -- it is a
        best-effort diagnostic add-on, not part of the core get/set
        contract every backend must honour. ``Camera.apply_config``
        resolves it with ``getattr(backend, "describe_set_failure",
        None)``, the same duck-typed pattern already used for
        ``get_property``/``set_property`` so that ``OpenCVBackend``
        (which does not derive from this class -- see the module
        docstring) is called identically to a real subclass."""
        return None


def resolve_capture_backend(
    hardware_key: str,
    index: int,
    *,
    backend_factory: Callable[[int], CaptureBackend],
    list_loopback_cameras: Optional[Callable[[], List[LibcameraDevice]]] = None,
) -> CaptureBackend:
    """Construct the right :class:`CaptureBackend` for a resolved camera
    (sprint 031 ticket 006, SUC-006).

    The shared branch point both ``camera/discovery.py``'s
    ``_probe_openable`` and ``daemon/service.py``'s ``_camera_for`` call,
    so the ``"libcamera:"``-prefix decision lives in exactly one place
    rather than being duplicated across the two backend-construction call
    sites.

    *hardware_key* is ``CameraIdentity.hardware_key``/
    ``CameraRecord.hardware_key`` -- the same field ``camera/identity.py``'s
    ``_probe_libcamera`` stamps ``f"libcamera:{lc.camera_id}"`` onto for a
    Raspberry Pi CSI camera reached through a labelled v4l2loopback
    device. For every other identity source this is a plain pass-through
    to *backend_factory(index)*, byte-for-byte the call both sites made
    before this ticket (their own ``_default_backend_factory`` ->
    ``OpenCVBackend(index)``) -- no behaviour change for any non-libcamera
    camera.

    For a libcamera-sourced *hardware_key*, *index* is the enumeration
    "position" ``camera/identity.py``'s ``resolve_all``/``_probe_libcamera``
    assign (0-based enumeration order -- see ``LibcameraDevice.position``'s
    own docstring) -- **not** a ``/dev/videoN`` device index, so it is
    never handed to *backend_factory* in this branch. Instead this
    re-enumerates ``camera/backends/libcamera.py``'s
    ``list_loopback_cameras()`` (the same, unmodified function
    ``_probe_libcamera`` already consumes) and matches on ``camera_id`` --
    the raw ``/sys`` card label embedded after the ``"libcamera:"``
    prefix -- to recover the real ``device_index``, then constructs
    ``V4l2LoopbackBackend(f"/dev/video{device_index}")``: the backend that
    can actually read a v4l2loopback device reliably (see that class's
    own module docstring for why ``OpenCVBackend`` cannot -- "every
    read() reports a stale frame").

    *list_loopback_cameras* is overridable so tests can inject a fixed
    device list without a real ``/sys/class/video4linux`` tree; defaults
    to the real, subprocess-free ``/sys`` scan.

    If no enumerated loopback device matches (a stale registry entry, or
    a race between this call and whatever earlier enumeration pass
    produced *hardware_key*/*index* -- ``DaemonService._index_for``'s own
    docstring already documents "explicitly unstable across replugs" as
    an accepted limitation of *index* generally), this logs a warning and
    falls back to *backend_factory(index)* rather than raising --
    consistent with every other function in this identity/discovery
    family (``_probe_libcamera``, ``CameraDiscovery._safe_identities``)
    degrading instead of throwing on an enumeration miss. That fallback
    is not expected to work any better than it does today (the whole
    reason this ticket exists is that ``OpenCVBackend`` cannot read a
    loopback device reliably), but it is never silent -- the warning
    names exactly what happened -- and it is strictly no worse than the
    unconditional ``OpenCVBackend(index)`` behaviour every camera got
    before this ticket.
    """
    if hardware_key.startswith(_LIBCAMERA_HARDWARE_KEY_PREFIX):
        camera_id = hardware_key[len(_LIBCAMERA_HARDWARE_KEY_PREFIX) :]
        lister = list_loopback_cameras or _list_loopback_cameras
        for device in lister():
            if device.camera_id == camera_id:
                from aprilcam.camera.backends.v4l2_loopback import (  # noqa: PLC0415
                    V4l2LoopbackBackend,
                )

                return V4l2LoopbackBackend(f"/dev/video{device.device_index}")
        logger.warning(
            "resolve_capture_backend: hardware_key %r is libcamera-sourced but no "
            "matching loopback device was enumerated; falling back to "
            "backend_factory(%d)",
            hardware_key,
            index,
        )
    return backend_factory(index)


def default_capture_backend_factory(index: int) -> CaptureBackend:
    """The one ``backend_factory``-shaped default both
    ``camera/discovery.py``'s ``_probe_openable`` and
    ``daemon/service.py``'s ``_camera_for`` construct their non-libcamera
    ``CaptureBackend`` from (sprint 038 ticket 005) -- so the two call
    sites cannot disagree about which backend an unspecified camera gets
    the way two independently-hardcoded copies of the same function
    could drift.

    **Selection (team-lead override of this ticket's original "always
    ``OpenCVBackend``" default, recorded on the stakeholder_approval
    gate).** ``camera-DESIGN.md``'s "AVFoundation capture-session wedge"
    finding (see ``worker_backend.py``'s module docstring) is
    ``sys.platform == "darwin"``-specific, so the *default* -- with no
    :data:`CAPTURE_BACKEND_ENV` override -- now differs by platform:

    * ``darwin`` -- :func:`~aprilcam.camera.backends.worker_backend.
      default_worker_backend_factory` (``WorkerBackend``), the only
      backend that can actually clear the wedge (a fresh process, not a
      fresh in-process handle).
    * every other platform -- :class:`~aprilcam.camera.backends.
      opencv_backend.OpenCVBackend`, unchanged -- no Linux/Raspberry Pi
      hardware has shown this symptom, and ``V4l2LoopbackBackend``
      already has its own, unrelated backend (``resolve_capture_backend``'s
      libcamera branch, upstream of this function).

    :data:`CAPTURE_BACKEND_ENV` (``APRILCAM_CAPTURE_BACKEND``), read
    fresh on every call rather than cached, overrides either way:
    ``"worker"`` or ``"opencv"`` (case-insensitive) forces that backend
    on any platform -- an operator or a test can force ``OpenCVBackend``
    back on a Mac, or exercise ``WorkerBackend`` on a non-Mac CI runner.
    Any other non-empty value is logged and ignored (falls through to
    the platform default) rather than raising -- consistent with every
    other env-var reader in this codebase (e.g.
    ``config.directories._resolve_dir``) tolerating an unrecognised
    value instead of taking a working daemon down over it.

    Lazy imports below (not module-level) avoid a circular import:
    ``opencv_backend.py`` and ``worker_backend.py`` both do
    ``from aprilcam.camera.backends import CaptureBackend`` -- importing
    either of them back into this ``__init__.py`` at module load time
    would re-enter this package's own not-yet-finished initialization,
    exactly the reason :func:`resolve_capture_backend` already imports
    ``V4l2LoopbackBackend`` the same way, lazily, inside its libcamera
    branch above.
    """
    override = os.environ.get(CAPTURE_BACKEND_ENV, "").strip().lower()
    if override == "worker":
        return _worker_backend_factory(index)
    if override == "opencv":
        return _opencv_backend_factory(index)
    if override:
        logger.warning(
            "default_capture_backend_factory: unrecognised %s=%r (expected "
            "'worker' or 'opencv'); falling back to the platform default",
            CAPTURE_BACKEND_ENV,
            override,
        )
    if sys.platform == "darwin":
        return _worker_backend_factory(index)
    return _opencv_backend_factory(index)


def _opencv_backend_factory(index: int) -> CaptureBackend:
    from aprilcam.camera.backends.opencv_backend import OpenCVBackend  # noqa: PLC0415

    return OpenCVBackend(index)


def _worker_backend_factory(index: int) -> CaptureBackend:
    from aprilcam.camera.backends.worker_backend import (  # noqa: PLC0415
        default_worker_backend_factory,
    )

    return default_worker_backend_factory(index)
