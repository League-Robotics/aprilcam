"""``Camera`` — one physical camera, owned end to end (``camera.interfaces.Camera``).

Converted from ``archive/src/aprilcam/camera/video_camera.py`` +
``camera.py``'s open/read glue and ``camera/camera_config.py``'s
introspection role, but restructured around composition over a
:class:`~aprilcam.camera.backends.CaptureBackend` rather than owning a
``cv2.VideoCapture`` directly — this is what lets one ``Camera`` class
work uniformly across the USB/AVFoundation, v4l2-loopback, and (future)
libcamera backends in ``camera/backends/`` (module table, sprint 020
architecture: "each backend changes independently... this is why they
stay separate small adapters behind the ``Camera`` base class").

**Self-heal reopen, unified across backends (a deliberate simplification
of v1).** v1's ``daemon/camera_pipeline.py`` split self-heal in two:
loopback captures (``.reopen()``) retried *forever*, plain USB captures
were re-opened at most 10 times. That asymmetry existed because v1's
two capture classes had genuinely different reopen primitives. Here,
every :class:`~aprilcam.camera.backends.CaptureBackend` exposes the same
``close()``/``open()`` pair, so "reopen" is uniformly "close then open"
for any backend, and the single v1 threshold this ticket's acceptance
criteria actually name — 5 consecutive failures trigger a reopen
attempt, capped at 10 reopens, budget restored on any healthy frame —
applies the same way everywhere. What happens when the budget is
exhausted (an open question in ``interfaces.py``'s ``Camera.read``
docstring) is decided here: the camera stops attempting further
reopens and ``state.usable`` goes (and stays) ``False``, while
``state.present`` stays ``True`` — this is deliberately the "plugged in
and broken" case (``types.CameraState``'s own docstring), not
"unplugged", since a stopped self-heal loop is not evidence the
hardware is gone.

**Real UVC control, validated live (ticket 023-001).** ``apply_config``
was originally implemented only against the small set of controls
``camera.backends.opencv_backend.property_map()`` exposes generically
through OpenCV (``cv2.VideoCapture.set()``) — not a verified
per-camera-model settable-key list. Ticket 023-001 validated that
generic path live against camera 3 (the OV9782 this project's real
playfield uses): ``cv2.VideoCapture.set()`` returns ``False`` for
**every one** of ``auto_exposure``/``exposure``/``gain``/
``auto_white_balance``/``white_balance_temperature`` on this exact
hardware's AVFoundation capture session — matching archived v1 evidence
that a generic capture-library property setter is not to be trusted here
(``archive/src/aprilcam/camera/DESIGN.md`` §4). ``Camera`` therefore
accepts an optional second backend, *control_backend* — a
:class:`~aprilcam.camera.backends.ControlBackend` (e.g.
``camera.backends.uvc_util.UvcUtilBackend``, a ``uvc-util`` subprocess
adapter) — that, when given, becomes the **exclusive** source for
``apply_config``'s settings surface (``get_property``/``set_property``/
``settable_names``) in place of the primary capture backend. Resolution
(``frame_width``/``frame_height``, ticket 002) is not a UVC control and
is meant to stay on the primary (``OpenCVBackend``) capture path
regardless of whether a *control_backend* is wired — a future ticket
adding it as a settable key should special-case it rather than routing
it through *control_backend*.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import numpy as np

from aprilcam.camera.backends import CaptureBackend, ControlBackend
from aprilcam.camera.interfaces import Camera as _CameraBase
from aprilcam.config.interfaces import CameraConfig
from aprilcam.errors import UnsupportedSetting
from aprilcam.types import CameraState

__all__ = ["Camera"]

logger = logging.getLogger(__name__)

#: v1's threshold (``daemon/camera_pipeline.py``): this many consecutive
#: failed reads trigger one reopen attempt.
FAILURE_THRESHOLD = 5

#: v1's threshold: at most this many reopen attempts before giving up
#: (budget restored to 0 on any healthy read).
MAX_REOPENS = 10

#: Settings that gate another setting and must therefore be pushed
#: first (interfaces.py's ``apply_config`` docstring: "auto-exposure
#: must be switched to manual before an exposure value will stick, and
#: auto-white-balance off before WB temperature").
_GATE_KEYS = ("auto_exposure", "auto_white_balance")


class Camera(_CameraBase):
    """Concrete ``camera.interfaces.Camera``.

    Derives from ``camera.interfaces.Camera`` (a plain base class, not
    ``Protocol`` -- see ``daemon.interfaces``'s module docstring for the
    design rule).

    Parameters
    ----------
    slug:
        The registry-assigned stable name (``camera.registry.CameraRegistry.resolve``'s
        return value) -- this is what :attr:`slug` and
        :meth:`effective_config`'s document ``name`` report.
    backend:
        The :class:`~aprilcam.camera.backends.CaptureBackend` this
        camera reads through. One backend per ``Camera`` instance,
        chosen by whoever constructs it (``camera/discovery.py`` for
        USB/AVFoundation hardware; a Pi bring-up sprint would pass a
        ``V4l2LoopbackBackend`` instead).
    control_backend:
        Optional :class:`~aprilcam.camera.backends.ControlBackend` used
        **exclusively** for ``apply_config``'s settings surface
        (``get_property``/``set_property``/``settable_names``) in place
        of *backend*, when given (ticket 023-001 -- see the module
        docstring). ``None`` (the default) preserves the original
        behaviour of reading/writing settings through *backend* itself,
        which is what every non-macOS platform and every headless test
        still does.
    device_name:
        OS-reported descriptor (``CameraIdentity.device_name``), carried
        through for ``effective_config``'s introspected facts. Purely
        diagnostic -- never the identity (see ``camera/identity.py``).
    number, playfield:
        Static facts from the registry/config that ``state()`` reports
        verbatim; ``playfield`` is ``None`` this sprint (no config-push
        loop yet to link one).
    """

    def __init__(
        self,
        slug: str,
        backend: CaptureBackend,
        *,
        control_backend: Optional[ControlBackend] = None,
        device_name: str = "",
        number: Optional[int] = None,
        playfield: Optional[str] = None,
    ) -> None:
        self._slug = slug
        self._backend = backend
        self._control_backend = control_backend
        self._device_name = device_name or slug
        self._number = number
        self._playfield = playfield
        self._present = False
        self._consecutive_failures = 0
        self._reopens = 0
        self._exhausted = False

    @property
    def slug(self) -> str:
        return self._slug

    @property
    def state(self) -> CameraState:
        """``playfield``/``calibrated``/``calibration_stale`` are always
        this instance's own defaults (``None``/``False``/``False``) --
        ``Camera`` has no notion of a config-service link or a
        calibration store (both are daemon-level state, per the module
        boundary; see ``daemon.service.DaemonService``'s module
        docstring). ``DaemonService.list_cameras`` overlays the real
        values for all three onto whatever this property returns
        (``dataclasses.replace``) for a held-open camera -- the same
        overlay it already applies for every not-held-open camera it
        reports. Do not treat these three fields as authoritative when
        reading ``Camera.state`` directly outside ``list_cameras``
        (ticket 022-007: a prior version hardcoded ``calibrated=False``
        here as if it were, which combined with an identical hardcode in
        ``list_cameras`` itself to make a genuinely calibrated camera
        permanently report ``uncalibrated``).
        """
        return CameraState(
            name=self._slug,
            present=self._present,
            usable=self._backend.is_open and not self._exhausted,
            playfield=self._playfield,
            calibrated=False,
            calibration_stale=False,
            frame_dark=False,  # no dark-frame heuristic wired this sprint
            number=self._number,
        )

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> None:
        """Open the backend. Propagates whatever the backend raises.

        Deliberately does not catch: ``CameraDiscovery.probe()``
        depends on this raising for "present but unopenable" detection
        (UC-25.S-4) -- swallowing it here would hide exactly the
        signal that AC exists to surface.
        """
        self._backend.open()
        self._present = True
        self._consecutive_failures = 0
        self._reopens = 0
        self._exhausted = False

    def close(self) -> None:
        self._backend.close()
        self._present = False
        self._consecutive_failures = 0
        self._reopens = 0
        self._exhausted = False

    def read(self) -> Optional[np.ndarray]:
        """See ``camera.interfaces.Camera.read``.

        Never raises. A backend read failure -- or an exception the
        backend raises out of its own ``read()``, which by contract it
        should not, but this guards regardless -- counts the same as
        an ordinary dropped frame.
        """
        frame = self._safe_read()
        if frame is not None:
            self._consecutive_failures = 0
            self._reopens = 0
            self._exhausted = False
            return frame
        self._consecutive_failures += 1
        if self._consecutive_failures >= FAILURE_THRESHOLD:
            self._consecutive_failures = 0
            self._attempt_self_heal()
        return None

    def _safe_read(self) -> Optional[np.ndarray]:
        try:
            return self._backend.read()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("%s: backend.read() raised: %s", self._slug, exc)
            return None

    def _attempt_self_heal(self) -> None:
        """Close and reopen the backend, within the reopen budget.

        See the module docstring for the budget-exhausted decision:
        once spent, this stops trying and the camera's ``usable``
        stays ``False`` (``present`` stays ``True``) until an external
        caller closes and reopens it explicitly. ``_exhausted`` is
        what carries that even if the backend's own ``is_open`` would
        otherwise say ``True`` (e.g. a backend whose ``open()`` keeps
        trivially succeeding while it never yields another frame) --
        ``usable`` must reflect "we gave up", not just "is a handle
        technically open".
        """
        if self._reopens >= MAX_REOPENS:
            self._exhausted = True
            return
        self._reopens += 1
        logger.warning(
            "%s: read failing, reopen attempt %d/%d", self._slug, self._reopens, MAX_REOPENS
        )
        try:
            self._backend.close()
        except Exception:  # pragma: no cover - defensive
            pass
        try:
            self._backend.open()
            self._present = True
        except Exception as exc:
            logger.warning("%s: reopen failed: %s", self._slug, exc)

    # -- config --------------------------------------------------------

    def effective_config(self) -> CameraConfig:
        """See ``camera.interfaces.Camera.effective_config``.

        Reports only what is genuinely introspectable: the device name
        (always) and current frame dimensions (only while the backend
        is open -- ``None`` otherwise, never guessed). The fuller
        "every settable key, blank" self-describing template
        (UC-25.S-1/S-2) needs real per-model UVC-control enumeration
        this sprint does not implement (see the module docstring) --
        reporting a fabricated key list would be a worse answer than
        reporting none.
        """
        size = self._backend.frame_size()
        settings: Dict[str, Any] = {
            "device_name": self._device_name,
            "frame_width": size[0] if size else None,
            "frame_height": size[1] if size else None,
        }
        return CameraConfig(name=self._slug, playfield=self._playfield, settings=settings)

    def apply_config(self, config: CameraConfig) -> None:
        """See ``camera.interfaces.Camera.apply_config``.

        Called by ``DaemonService.set_camera_config`` on every push to a
        held-open camera (ticket 023-001) -- no longer aspirational.
        Settings are read/written through :meth:`_settings_backend`:
        *control_backend* when one was given at construction, else
        *backend* itself, exactly as before this ticket (see the module
        docstring).
        """
        if not self._backend.is_open:
            raise RuntimeError(f"{self._slug}: cannot apply_config while not open")

        settable = self._settable_names()
        for key in config.settings:
            if key not in settable:
                raise UnsupportedSetting(key)

        settings_backend = self._settings_backend()
        get_property = getattr(settings_backend, "get_property", None)
        set_property = getattr(settings_backend, "set_property", None)
        if get_property is None or set_property is None:
            # Backend exposes no adjustable settings surface at all
            # (e.g. v4l2 loopback -- the stream format is fixed by its
            # writer, per v1's comment on V4l2CtlCapture.set()).
            if config.settings:
                raise UnsupportedSetting(next(iter(config.settings)))
            return

        previous: Dict[str, float] = {}
        for key in config.settings:
            value = get_property(key)
            if value is not None:
                previous[key] = value

        ordered = [k for k in _GATE_KEYS if k in config.settings]
        ordered += [k for k in config.settings if k not in _GATE_KEYS]

        applied: list = []
        for key in ordered:
            value = config.settings[key]
            if value is None:
                continue  # blank -- nothing to push for this key
            if not set_property(key, float(value)):
                # Whole push fails; undo everything already applied
                # this call so the camera is left exactly as it was
                # (interfaces.py's Camera.apply_config contract).
                for applied_key in applied:
                    set_property(applied_key, previous.get(applied_key, 0.0))
                raise UnsupportedSetting(key)
            applied.append(key)

    def _settings_backend(self) -> Any:
        """The backend :meth:`apply_config`/:meth:`_settable_names` read
        and write settings through: *control_backend* when one was given
        at construction (ticket 023-001 -- real UVC control on macOS,
        see the module docstring), else *backend* itself (unchanged
        original behaviour -- every non-macOS platform and every
        headless test)."""
        return self._control_backend if self._control_backend is not None else self._backend

    def _settable_names(self) -> set:
        """Setting names :meth:`_settings_backend` itself claims to
        support.

        Delegates to that backend's own ``settable_names()`` (beyond the
        ``CaptureBackend`` base class, like ``get_property``/``set_property``
        -- a backend with none of these, e.g. v4l2 loopback, is simply
        polled as having no settable keys at all).
        """
        settable_names = getattr(self._settings_backend(), "settable_names", None)
        if settable_names is None:
            return set()
        return set(settable_names())

    def reset(self) -> None:
        """See ``camera.interfaces.Camera.reset``.

        No ``ConfigLoader`` is wired this sprint (the config-push
        ticket owns reading ``config.json`` back off disk) -- there is
        nothing stored to discard live overrides back to yet. A
        truthful no-op: leaving the camera exactly as it is, rather
        than pretending to re-apply a document that does not exist.
        """
        return None

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"Camera(slug={self._slug!r}, present={self._present}, usable={self._backend.is_open})"
