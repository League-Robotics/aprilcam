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

**Reopen as a staleness trigger, notification only (ticket 023-007).**
A successful self-heal reopen is a real-world event that can move a
camera — someone unplugged it to swap a port, bumped it while
investigating a problem — which is exactly what
``calibration.interfaces.Calibration.stale`` exists to flag once a
calibration is established. ``Camera`` itself has no notion of
calibration (that stays a daemon-level concern, per this module's own
existing "beyond the interface" divisions below), so it only exposes an
optional, constructor-injected ``on_reopen`` callback — see
:meth:`Camera.__init__`'s docstring — invoked with no arguments at the
end of a *successful* :meth:`Camera._attempt_self_heal`, mirroring
``daemon.detection.DetectionEngine``'s existing ``calibration_lookup``
injection pattern. ``daemon.service.DaemonService`` is the one thing
that wires this to anything meaningful.

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
``settable_names``) in place of the primary capture backend, for every
key except resolution (below).

**Settle-after-open, before the first real UVC push (ticket 030-005,
reopened).** A hardware defect reopened this ticket: ``uvc-util``
writes against a *freshly opened* macOS camera (``_camera_for``'s
apply-on-open path, and any caller that opens a camera and immediately
pushes settings — exactly what ``get_frame`` followed by
``set_camera_config`` does) failed intermittently, at a real-world rate
this investigation measured at roughly 60% in genuinely cold processes
(matching a real ``pytest`` invocation or a real ``aprilcam`` CLI
process, each a fresh interpreter) — far worse than the "rare, ~1-in-5"
conclusion the ticket's first pass reached from a smaller, warm-process
sample. The stakeholder's own hypothesis for the cause
(``auto-exposure-priority``, a control v2 did not know existed) did
**not** hold up: live re-investigation found ``hd-usb-camera`` — the
camera that actually failed — does not expose that control at all
(``uvc-util -c`` lists it only on the arducam), so it cannot be what
gates a write on the camera that fails. The real cause, confirmed by
direct measurement, is v1's own already-documented quirk
(``archive/src/aprilcam/daemon/camera_pipeline.py``'s ``start()``:
"Drain a few frames so AVFoundation finishes initialising its capture
session before we apply UVC controls — otherwise the OS resets them"),
which v2 never ported: pushing UVC controls immediately after
``backend.open()``, before AVFoundation's capture session has finished
its own asynchronous initialisation, races that initialisation —
observed failing 6/10 genuinely-cold trials with no drain, 0/22 with
one. :meth:`open` (and a successful :meth:`_attempt_self_heal` reopen)
now marks :attr:`_pending_open_settle`; :meth:`apply_config` drains
:data:`_OPEN_SETTLE_READS` frames from *backend* the first time a push
after a fresh open actually has something to write, mirroring v1's own
mechanism exactly (plain frame reads, no explicit sleep — a sleep alone
also worked in this investigation's trials, but reads cost nothing a
caller wasn't already going to spend and match the one v1 itself
proved). A no-op whenever *control_backend* is ``None`` (every non-macOS
platform, every headless test that does not wire one) or the push has
nothing real to write (an empty or all-blank ``settings`` dict, e.g.
``DaemonService``'s own apply-on-open call with a config the camera
never actually needs to touch hardware for) — so no existing headless
test's frame-queue accounting is disturbed by this change.

**Bounded retry, belt-and-braces (ticket 030-005, reopened).**
Independently of the settle fix above — approved by the stakeholder
regardless of root cause, as insurance against whatever residual
transient the settle fix does not happen to cover —
``camera.backends.uvc_util.UvcUtilBackend`` now retries a failed *raw*
CLI write (``uvc-util -s ... exited nonzero``) a small bounded number of
times with a brief backoff before giving up. It deliberately does
**not** retry a write the CLI accepted but that then reads back wrong
(a mismatch — including the documented "silently clamped an
out-of-range value" case): that outcome is not transient, retrying it
cannot change it, and repeating it would only delay the honest
``UnsupportedSetting`` an out-of-range value or a genuinely absent
control still needs to raise promptly. See that module's docstring for
the mechanism.

**Self-describing template + resolution key (ticket 023-002).**
``effective_config`` reports the real, per-camera settable-key set —
the union of whichever backend :meth:`Camera._settable_names` resolves
to for most keys, plus ``frame_width``/``frame_height`` — rather than
the two-key ``device_name``/dimensions placeholder it reported before
this ticket. ``frame_width``/``frame_height`` are **always** routed
through *backend* (:meth:`Camera._backend_for_key`), never through
*control_backend*, because resolution is a capture property
(``OpenCVBackend.property_map``'s ``CAP_PROP_FRAME_WIDTH``/
``CAP_PROP_FRAME_HEIGHT`` entries), not a UVC control — the one
special-case the ticket-001 module docstring flagged as a future
ticket's job. Every settable key other than the two introspected
dimensions is always reported blank (``None``): ``Camera`` itself holds
no notion of "what was last pushed" (that is
``daemon.service.DaemonService._camera_configs``'s job, one layer up —
see :attr:`state`'s own docstring for the same division of
responsibility) or of what a control's current hardware value happens
to be, so ``effective_config`` is always the template a camera
configuration starts from, never a live-values dump — this also means
it never has to call a settings backend's ``get_property`` for those
keys, so fetching the template cannot itself disturb hardware state.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Set, Tuple

import numpy as np

from aprilcam.camera.backends import CaptureBackend, ControlBackend
from aprilcam.camera.interfaces import Camera as _CameraBase
from aprilcam.config.interfaces import CameraConfig
from aprilcam.config.interfaces import ConfigLoader as ConfigLoaderIface
from aprilcam.errors import UnsupportedSetting
from aprilcam.types import CameraState

#: Per-camera flat-field gain map, looked for at
#: ``<state_dir>/cameras/<slug>/flatfield.npy`` (``daemon.service.
#: DaemonService._flatfield_path_for``) -- a float ``(H, W, C)`` or
#: ``(H, W)`` NumPy array of per-pixel multipliers built from a white-sheet
#: reference capture (see ``docs/knowledge/flat-field-gain-map.md``).
#: When the file is present every frame :meth:`Camera.read` hands out is
#: multiplied by it and saturated back to the frame's own dtype, so
#: detection, ``get_frame``, streaming, and calibration capture all see
#: the illumination-flattened, colour-cast-corrected image without any
#: consumer needing to know the map exists. Absent file: frames pass
#: through untouched, exactly as before this file existed. Reloaded on
#: every :meth:`Camera.open` and :meth:`Camera.reset`, so replacing or
#: deleting the file takes effect live via ``ResetCameraConfig`` -- no
#: daemon restart, no new RPC.
FLATFIELD_FILENAME = "flatfield.npy"

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

#: The inverse relationship, key -> the gate that must be manual before
#: *key* can be expected to stick -- diagnostic-only (ticket 030-005).
#: Never used to block, reorder, or validate a push -- ``_GATE_KEYS``/
#: ``ordered`` above already handle ordering unconditionally regardless
#: of this map. Feeds :meth:`Camera._describe_set_failure`'s fallback
#: when a backend has no ``describe_set_failure`` of its own (e.g. a
#: bare ``FakeBackend``/``OpenCVBackend``) and can only report "the gate
#: this key needs was not part of this push" from the config's own
#: shape, not a live hardware readback.
_GATED_BY = {
    "exposure": "auto_exposure",
    "white_balance_temperature": "auto_white_balance",
}

#: Frames to read-and-discard from *backend* immediately before the
#: first real UVC control push following a fresh open (ticket 030-005,
#: reopened) -- see the module docstring's "Settle-after-open" section.
#: v1's own proven number (``archive/src/aprilcam/daemon/
#: camera_pipeline.py``'s ``for _ in range(5): cap.read()``), re-confirmed
#: live against this project's own two real cameras: 0 with no drain
#: failed 6/10 genuinely-cold trials, 5 passed 22/22.
_OPEN_SETTLE_READS = 5

#: Keys that are a capture property, not a UVC control (ticket 023-002).
#: Always routed through *backend* (the primary capture backend) for
#: get/set, never through *control_backend* -- see the module docstring's
#: "Real UVC control, validated live" section and :meth:`Camera._backend_for_key`.
_RESOLUTION_KEYS = frozenset({"frame_width", "frame_height"})


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
    config_loader, config_path:
        Optional (ticket 023-004): what :meth:`reset`/:meth:`stored_config`
        read *config_path* through, when both are given. ``daemon.
        service.DaemonService._camera_for`` wires a real
        ``config.loader.ConfigLoader`` plus this camera's resolved
        ``config_dir/cameras/<slug>/config.json`` path (sprint.md
        Decision 3: always this fixed location, never whatever was last
        pushed) into every ``Camera`` it constructs. ``None`` (the
        default -- every pre-existing test, and any ``Camera`` built by
        hand rather than through ``_camera_for``) means there is nothing
        to read: :meth:`reset` then falls back to the blank
        introspected-plus-blank template, exactly like a camera whose
        *config_path* does not exist on disk.
    on_reopen:
        Optional, constructor-injected notification (ticket 023-007,
        sprint.md Decision 6) invoked with no arguments at the end of a
        *successful* :meth:`_attempt_self_heal` reopen -- mirrors
        ``daemon.detection.DetectionEngine``'s existing
        ``calibration_lookup`` injection pattern: ``Camera`` gains no
        notion of calibration itself, it only tells whoever is
        listening "I just reopened." ``daemon.service.DaemonService``
        wires this to a closure marking the camera's calibration stale
        (a reopen correlates with a real-world event that can move a
        camera far better than elapsed time alone -- sprint.md Design
        Rationale Decision 6). **Not** invoked from :meth:`open`
        itself: the very first open of a freshly constructed ``Camera``
        (``DaemonService._camera_for``'s normal path, including for a
        calibration reloaded from disk at daemon startup) is not a
        reopen and must never mark a just-loaded, still-accurate
        calibration stale. ``None`` (the default) means nobody is
        listening -- every pre-existing test and any ``Camera`` built
        without this parameter behaves exactly as before.
    flatfield_path:
        Optional path to this camera's :data:`FLATFIELD_FILENAME` gain
        map. Loaded (if the file exists) on every :meth:`open` and
        :meth:`reset` and applied to every frame :meth:`read` returns
        (:meth:`_apply_flatfield`). ``None`` (the default) disables the
        feature entirely for this instance.
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
        config_loader: Optional[ConfigLoaderIface] = None,
        config_path: Optional[Path] = None,
        on_reopen: Optional[Callable[[], None]] = None,
        flatfield_path: Optional[Path] = None,
    ) -> None:
        self._slug = slug
        self._backend = backend
        self._control_backend = control_backend
        self._device_name = device_name or slug
        self._number = number
        self._playfield = playfield
        self._config_loader = config_loader
        self._config_path = config_path
        self._on_reopen = on_reopen
        #: Where :meth:`_load_flatfield` looks for :data:`FLATFIELD_FILENAME`
        #: -- ``None`` (every pre-existing test, any hand-built ``Camera``)
        #: means "no flat-field correction, ever", identical to the file
        #: simply not existing.
        self._flatfield_path = flatfield_path
        #: The loaded gain map (float32) or ``None``; see :meth:`_apply_flatfield`.
        self._flatfield: Optional[np.ndarray] = None
        #: Once-only guard for the shape-mismatch warning in
        #: :meth:`_apply_flatfield`, so a wrong-sized map does not log on
        #: every frame at 30 fps.
        self._flatfield_warned = False
        self._present = False
        self._consecutive_failures = 0
        self._reopens = 0
        self._exhausted = False
        #: Ticket 030-005 (reopened) -- set whenever *backend* was just
        #: (re)opened and has not yet had :meth:`apply_config` drain
        #: :data:`_OPEN_SETTLE_READS` frames from it before its first
        #: real push; see the module docstring's "Settle-after-open"
        #: section. Never true before the first :meth:`open`.
        self._pending_open_settle = False
        #: Guards the entire body of :meth:`read` -- including any
        #: self-heal reopen it triggers -- so a background stream
        #: producer (ticket 027-003/004) and a one-shot RPC read
        #: (``get_frame``/``get_tags``) never interleave calls to the
        #: same underlying ``CaptureBackend`` handle (ticket 027-002).
        #: A plain, non-reentrant ``Lock`` is safe here: nothing reached
        #: from within :meth:`read` (including :meth:`_attempt_self_heal`
        #: and *on_reopen*) calls back into :meth:`read` itself, so there
        #: is no same-thread re-acquisition to deadlock on. Deliberately
        #: *not* held by :meth:`open`/:meth:`close`/:meth:`apply_config`
        #: -- see :meth:`read`'s own docstring for the scope rationale.
        self._read_lock = threading.Lock()

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
        self._pending_open_settle = True
        self._load_flatfield()

    def close(self) -> None:
        self._backend.close()
        self._present = False
        self._consecutive_failures = 0
        self._reopens = 0
        self._exhausted = False
        self._pending_open_settle = False

    def read(self) -> Optional[np.ndarray]:
        """See ``camera.interfaces.Camera.read``.

        Never raises. A backend read failure -- or an exception the
        backend raises out of its own ``read()``, which by contract it
        should not, but this guards regardless -- counts the same as
        an ordinary dropped frame.

        **Read lock (ticket 027-002).** The entire method body runs
        under :attr:`_read_lock`, held for as long as this call touches
        the backend -- including a self-heal reopen (:meth:`
        _attempt_self_heal`'s ``close()``/``open()``) when five
        consecutive failures trip it, since that also drives the same
        ``CaptureBackend`` handle. This exists because sprint 027 adds a
        persistent background stream producer (tickets 003/004) that
        calls ``read()`` on a timer, coexisting with the one-shot RPC
        reads (``get_frame``/``get_tags`` via ``DetectionEngine.
        run_burst``) that were previously this method's only caller --
        without the lock, two threads could interleave calls into the
        same OpenCV/backend capture object. Deliberately narrow: the
        lock guards only ``read()``, not ``open()``/``close()``/
        ``apply_config()`` or read-only accessors like
        ``effective_config()`` -- nothing this sprint calls those
        concurrently with a background reader, so locking them would be
        unnecessary serialization (see this method's docstring and
        ticket 027-002's acceptance criteria).
        """
        return self._read_frame(apply_flatfield=True)

    def read_raw(self) -> Optional[np.ndarray]:
        """Exactly :meth:`read` -- same lock, same failure counting, same
        self-heal -- but **without** the flat-field gain map applied
        (:meth:`_apply_flatfield` is skipped). This is the read every
        *reference* capture must use: ``DaemonService.
        _capture_flatfield_frames`` builds a new gain map from these
        frames, and reading them through :meth:`read` while a map is
        already loaded would build a correction of already-corrected
        frames and then replace the good map with it (found live
        2026-09-03: a recapture with a map loaded produced a map that was
        only the residual). Not part of ``camera.interfaces.Camera``'s
        declared contract -- an addition specific to this concrete class,
        like :meth:`stored_config`.
        """
        return self._read_frame(apply_flatfield=False)

    def _read_frame(self, *, apply_flatfield: bool) -> Optional[np.ndarray]:
        with self._read_lock:
            frame = self._safe_read()
            if frame is not None:
                self._consecutive_failures = 0
                self._reopens = 0
                self._exhausted = False
                return self._apply_flatfield(frame) if apply_flatfield else frame
            self._consecutive_failures += 1
            if self._consecutive_failures >= FAILURE_THRESHOLD:
                self._consecutive_failures = 0
                self._attempt_self_heal()
            return None

    # -- flat-field gain map (FLATFIELD_FILENAME) ---------------------------

    def _load_flatfield(self) -> None:
        """(Re)load the gain map at :attr:`_flatfield_path`, or clear it.

        Never raises: an unreadable or malformed file is logged and
        ignored (frames then pass through uncorrected), because a
        camera must never become unopenable over an optional
        correction artefact. Accepts a float ``(H, W)`` or ``(H, W, C)``
        array of finite, non-negative multipliers; anything else is
        rejected by name so the operator can see why.
        """
        self._flatfield = None
        self._flatfield_warned = False
        path = self._flatfield_path
        if path is None or not path.exists():
            return
        try:
            gain = np.load(path)
        except Exception as exc:  # malformed/unreadable file -- never fatal
            logger.warning("%s: ignoring unreadable flat-field gain map %s: %s", self._slug, path, exc)
            return
        if (
            not isinstance(gain, np.ndarray)
            or gain.ndim not in (2, 3)
            or gain.size == 0
            or not np.issubdtype(gain.dtype, np.floating)
            or not np.isfinite(gain).all()
            or bool((gain < 0).any())
        ):
            logger.warning(
                "%s: ignoring malformed flat-field gain map %s -- expected a non-empty "
                "float (H, W) or (H, W, C) array of finite, non-negative gains, got %s %s",
                self._slug,
                path,
                getattr(gain, "dtype", type(gain)),
                getattr(gain, "shape", ""),
            )
            return
        self._flatfield = gain.astype(np.float32, copy=False)
        logger.info(
            "%s: flat-field gain map loaded from %s (shape %s, gain %.2f-%.2f)",
            self._slug,
            path,
            self._flatfield.shape,
            float(self._flatfield.min()),
            float(self._flatfield.max()),
        )

    def _apply_flatfield(self, frame: np.ndarray) -> np.ndarray:
        """Multiply *frame* by the loaded gain map and saturate back to
        the frame's own dtype. Pass-through when no map is loaded. A
        map whose height/width do not match the frame (a resolution
        change since capture) is skipped with a single warning rather
        than silently mis-corrected or crashing the read loop. A
        3-channel map is applied to a 2-D (mono) frame via its first
        channel; a 2-D map broadcasts across a colour frame's channels.
        """
        gain = self._flatfield
        if gain is None:
            return frame
        if gain.shape[:2] != frame.shape[:2]:
            if not self._flatfield_warned:
                self._flatfield_warned = True
                logger.warning(
                    "%s: flat-field gain map shape %s does not match frame shape %s -- "
                    "not applied (recapture the map at this resolution)",
                    self._slug,
                    gain.shape,
                    frame.shape,
                )
            return frame
        if frame.ndim == 2 and gain.ndim == 3:
            g: np.ndarray = gain[:, :, 0]
        elif frame.ndim == 3 and gain.ndim == 2:
            g = gain[:, :, None]
        elif frame.ndim == 3 and gain.ndim == 3 and gain.shape[2] not in (1, frame.shape[2]):
            if not self._flatfield_warned:
                self._flatfield_warned = True
                logger.warning(
                    "%s: flat-field gain map has %d channels but frames have %d -- not applied",
                    self._slug,
                    gain.shape[2],
                    frame.shape[2],
                )
            return frame
        else:
            g = gain
        out = frame.astype(np.float32) * g
        if np.issubdtype(frame.dtype, np.integer):
            info = np.iinfo(frame.dtype)
            np.clip(out, info.min, info.max, out=out)
        return out.astype(frame.dtype)

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

        **Staleness notification (ticket 023-007).** *on_reopen* fires
        only when ``self._backend.open()`` above genuinely succeeds --
        never on a failed reopen attempt (nothing physically changed
        about the camera's mount if the reopen itself didn't happen)
        and never when the budget is already exhausted (the ``return``
        above short-circuits before this point).
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
            self._pending_open_settle = True  # ticket 030-005: a fresh session again
        except Exception as exc:
            logger.warning("%s: reopen failed: %s", self._slug, exc)
            return
        if self._on_reopen is not None:
            self._on_reopen()

    # -- config --------------------------------------------------------

    def effective_config(self) -> CameraConfig:
        """See ``camera.interfaces.Camera.effective_config``.

        The self-describing template (UC-03.S-1, ticket 023-002):
        introspected facts (``device_name``, always; ``frame_width``/
        ``frame_height``, only while the backend is open -- ``None``
        otherwise, never guessed) plus every other key
        :meth:`_settable_names` reports for *this* camera's active
        backend(s), each blank. Unlike the two introspected dimensions,
        those other keys are never read back from hardware here --
        ``Camera`` has no notion of "what was last pushed" (that is
        ``DaemonService``'s job, one layer up; see :attr:`state`'s own
        docstring for the same division) -- so this is always the blank
        template a camera configuration starts from, never a live-values
        dump, and fetching it can never itself touch hardware state
        beyond the two dimension reads already made before this ticket.
        """
        size = self._backend.frame_size()
        settings: Dict[str, Any] = {
            "device_name": self._device_name,
            "frame_width": size[0] if size else None,
            "frame_height": size[1] if size else None,
        }
        for key in self._settable_names():
            settings.setdefault(key, None)
        return CameraConfig(name=self._slug, playfield=self._playfield, settings=settings)

    def apply_config(self, config: CameraConfig) -> None:
        """See ``camera.interfaces.Camera.apply_config``.

        Called by ``DaemonService.set_camera_config`` on every push to a
        held-open camera (ticket 023-001) -- no longer aspirational.
        Each key is read/written through whichever backend
        :meth:`_backend_for_key` resolves it to -- *control_backend* for
        every ordinary setting when one was given at construction, but
        *backend* (the primary capture backend) unconditionally for
        ``frame_width``/``frame_height`` (ticket 023-002 -- resolution is
        a capture property, never a UVC control, see the module
        docstring). Previous-value snapshotting and the rollback-on-
        mid-push-failure contract both now operate per key through that
        key's own backend, so a mixed push (some keys through
        *control_backend*, ``frame_width``/``frame_height`` through
        *backend*) still rolls back atomically as one whole-push unit.
        """
        if not self._backend.is_open:
            raise RuntimeError(f"{self._slug}: cannot apply_config while not open")

        settable = self._settable_names()
        for key in config.settings:
            if key not in settable:
                raise UnsupportedSetting(key, reason="not a setting this camera's backend(s) expose")

        # Resolve each key's get/set pair up front. A key whose backend
        # exposes no settings surface at all (e.g. v4l2 loopback -- the
        # stream format is fixed by its writer, per v1's comment on
        # V4l2CtlCapture.set()) fails immediately, per-key rather than
        # per-camera -- unchanged in effect from the prior whole-backend
        # check for every case this codebase actually exercises, since a
        # key can only be in *settable* if its own backend advertised it.
        accessors: Dict[str, Tuple[Any, Any, Any]] = {}
        for key in config.settings:
            backend = self._backend_for_key(key)
            get_property = getattr(backend, "get_property", None)
            set_property = getattr(backend, "set_property", None)
            if get_property is None or set_property is None:
                raise UnsupportedSetting(
                    key, reason="this camera's backend exposes no settings control surface"
                )
            accessors[key] = (backend, get_property, set_property)

        previous: Dict[str, float] = {}
        for key, (_backend, get_property, _set_property) in accessors.items():
            value = get_property(key)
            if value is not None:
                previous[key] = value

        ordered = [k for k in _GATE_KEYS if k in config.settings]
        ordered += [k for k in config.settings if k not in _GATE_KEYS]

        # Ticket 030-005 (reopened): the first real write following a
        # fresh open gets a settle drain first -- see the module
        # docstring's "Settle-after-open" section. Gated on there being
        # at least one real (non-None) value to push so an empty/blank
        # push (e.g. DaemonService._camera_for's apply-on-open call with
        # nothing stored yet) never pays this cost or disturbs a
        # headless test's frame-queue accounting for no reason.
        if (
            self._pending_open_settle
            and self._control_backend is not None
            and any(config.settings.get(k) is not None for k in ordered)
        ):
            self._settle_after_open()
            self._pending_open_settle = False

        applied: list = []
        for key in ordered:
            value = config.settings[key]
            if value is None:
                continue  # blank -- nothing to push for this key
            backend, _get_property, set_property = accessors[key]

            try:
                numeric_value = float(value)
            except (TypeError, ValueError) as exc:
                # A non-numeric settings value (e.g. a stray JSON string
                # like "100" -- ticket: camera 3's persisted config held
                # exactly this) must never reach set_property or
                # _describe_set_failure below: both assume a real float,
                # and a bare comparison against a str
                # (UvcUtilBackend.describe_set_failure's
                # ``minimum <= value <= maximum``) raises an untyped
                # TypeError that crosses the RPC boundary unclean
                # instead of a typed, diagnosable error (2026-08-22
                # hotfix). config/loader.py's ``_coerce_settings``
                # rejects this at load time for every document that goes
                # through it, but this is the one place every settings
                # value is actually used regardless of origin, so it is
                # also where the last-resort, always-clean guarantee has
                # to live.
                self._rollback_applied(applied, accessors, previous)
                raise UnsupportedSetting(
                    key,
                    reason=(
                        f"{self._slug}: value {value!r} for setting {key!r} is not a "
                        f"number ({type(value).__name__})"
                    ),
                ) from exc
            if not set_property(key, numeric_value):
                # Whole push fails; undo everything already applied
                # this call, each key through its own backend, so the
                # camera is left exactly as it was (interfaces.py's
                # Camera.apply_config contract).
                self._rollback_applied(applied, accessors, previous)
                raise UnsupportedSetting(
                    key, reason=self._describe_set_failure(key, numeric_value, backend)
                )
            applied.append(key)

    @staticmethod
    def _rollback_applied(
        applied: list,
        accessors: Dict[str, Tuple[Any, Any, Any]],
        previous: Dict[str, float],
    ) -> None:
        """Undo every key in *applied* through its own backend
        (:meth:`apply_config`'s whole-push, hard-error contract) --
        shared by both of that method's failure paths (a non-numeric
        settings value, and a hardware-rejected write) so the rollback
        behaviour cannot drift between them."""
        for applied_key in applied:
            _, _, applied_set_property = accessors[applied_key]
            applied_set_property(applied_key, previous.get(applied_key, 0.0))

    def _settle_after_open(self) -> None:
        """Read-and-discard :data:`_OPEN_SETTLE_READS` frames from
        *backend* (ticket 030-005, reopened) -- see the module
        docstring's "Settle-after-open" section for the real-hardware
        finding this exists to fix. Calls *backend* directly, never
        :meth:`read` -- this deliberately bypasses :attr:`_read_lock`
        and the consecutive-failure/self-heal counters: a frame that
        comes back ``None`` while the capture session is still settling
        is expected here, not a fault to count towards :data:
        `FAILURE_THRESHOLD`, and :meth:`apply_config` (this method's
        only caller) is documented as not running concurrently with a
        background reader in the first place (see :meth:`read`'s own
        docstring)."""
        for _ in range(_OPEN_SETTLE_READS):
            self._backend.read()

    def _describe_set_failure(self, key: str, value: float, backend: Any) -> Optional[str]:
        """Best-effort real cause behind ``set_property(key, value)``
        just returning ``False`` (ticket 030-005's investigation into
        ``UnsupportedSetting``'s previously-generic message).

        Prefers *backend*'s own :meth:`~camera.backends.ControlBackend.
        describe_set_failure` -- e.g. ``UvcUtilBackend``'s live
        readback of the control's hardware-advertised range and any
        gate control's current value, the most direct evidence
        available. Falls back to a config-shape-only check this class
        can make with no backend cooperation at all: whether *key* is
        one this codebase knows is gated by another key (:data:`
        _GATED_BY`) that this same push never included -- the one
        cause a bare ``FakeBackend``/``OpenCVBackend`` (no hardware
        readback) can still be right about. Returns ``None`` (today's
        plain ``UnsupportedSetting`` message, unchanged) only when
        neither source has anything more specific to add.
        """
        describe = getattr(backend, "describe_set_failure", None)
        if describe is not None:
            reason = describe(key, value)
            if reason is not None:
                return reason
        gate = _GATED_BY.get(key)
        if gate is not None:
            return (
                f"gate control {gate!r} was not part of this push -- if the "
                f"camera is currently in an automatic mode, {key!r} cannot "
                f"be written until {gate!r} is pushed to a manual value first"
            )
        return None

    def _settings_backend(self) -> Any:
        """The backend ordinary (non-resolution) settings are read and
        written through: *control_backend* when one was given at
        construction (ticket 023-001 -- real UVC control on macOS, see
        the module docstring), else *backend* itself (unchanged original
        behaviour -- every non-macOS platform and every headless test).
        ``frame_width``/``frame_height`` never go through this -- see
        :meth:`_backend_for_key`."""
        return self._control_backend if self._control_backend is not None else self._backend

    def _backend_for_key(self, key: str) -> Any:
        """Which backend :meth:`apply_config`/:meth:`_settable_names`
        read and write *key* through (ticket 023-002).

        ``frame_width``/``frame_height`` always resolve to *backend*
        (the primary capture backend) -- a capture property
        (``OpenCVBackend.property_map``'s ``CAP_PROP_FRAME_WIDTH``/
        ``CAP_PROP_FRAME_HEIGHT``), never a UVC control, so it stays on
        the primary capture path regardless of whether a narrower
        *control_backend* is wired for everything else. Every other key
        resolves to :meth:`_settings_backend`.
        """
        if key in _RESOLUTION_KEYS:
            return self._backend
        return self._settings_backend()

    def _names_from(self, backend: Any) -> Set[str]:
        """*backend*'s own ``settable_names()``, or an empty set if it
        has none (beyond the ``CaptureBackend``/``ControlBackend`` base
        surface -- a backend with neither, e.g. v4l2 loopback, is simply
        polled as having no settable keys at all)."""
        settable_names = getattr(backend, "settable_names", None)
        if settable_names is None:
            return set()
        return set(settable_names())

    def _settable_names(self) -> Set[str]:
        """The full settable-key set this camera reports (ticket
        023-002): whatever :meth:`_settings_backend` itself claims to
        support (*control_backend* when one was given, else *backend*),
        unioned with *backend*'s own ``frame_width``/``frame_height``
        support -- the resolution special case :meth:`_backend_for_key`
        also applies, so a narrower *control_backend*'s settable names
        (e.g. ``UvcUtilBackend``'s five UVC controls, which do not
        include resolution at all) never hides ``frame_width``/
        ``frame_height`` from the template.
        """
        names = self._names_from(self._settings_backend())
        names |= self._names_from(self._backend) & _RESOLUTION_KEYS
        return names

    def reset(self) -> None:
        """See ``camera.interfaces.Camera.reset`` (ticket 023-004).

        The real on-disk document at *config_path* (:meth:`_on_disk_config`),
        re-applied through :meth:`apply_config` -- the exact same
        whole-push, ordered, hard-error, rollback-on-failure contract a
        live push gets, since this is nothing more than ``apply_config``
        fed a freshly-read document instead of an RPC-supplied one. No
        separate, weaker code path exists here.

        Nothing installed on disk (sprint.md Decision 3's "nothing to
        revert to" case): applies an explicitly *empty* settings push --
        a true no-op on hardware, never :meth:`effective_config`'s
        template. That template's ``device_name`` is introspected-only,
        never a settable key -- pushing it through ``apply_config``
        would hard-error every time, which is exactly backwards for a
        code path whose entire point is "nothing to revert to, not a
        failure." :meth:`stored_config` is the accessor that reports
        the *template* for display/config-service purposes; this method
        only ever touches hardware with real, settable values.
        """
        config = self._on_disk_config()
        if config is None:
            config = CameraConfig(name=self._slug, playfield=self._playfield, settings={})
        self.apply_config(config)
        # Same "re-read exactly what is installed on disk" contract for the
        # flat-field gain map: a replaced file is picked up, a deleted file
        # clears the correction -- live, no restart (FLATFIELD_FILENAME).
        self._load_flatfield()

    def stored_config(self) -> CameraConfig:
        """The document that describes what is now in force after
        :meth:`reset` (ticket 023-004): the real on-disk document at
        *config_path* when one exists, else the blank
        introspected-plus-blank template (:meth:`effective_config`, the
        same document an unconfigured camera reports) -- sprint.md
        Decision 3: a live override with nothing installed in
        ``config_dir`` falls back to this blank template rather than
        erroring, "discard the override, nothing to revert to" being an
        honest answer. Unlike :meth:`reset`'s own fallback (an
        explicitly empty push, since ``effective_config``'s
        ``device_name`` is not a settable key), this method's fallback
        is the full template -- this is what a caller reports back to a
        client asking "what does this camera's config look like now,"
        not what gets pushed to hardware.

        Not part of ``camera.interfaces.Camera``'s declared contract --
        an addition specific to this concrete class, the same way
        ``DaemonService.active_slugs`` adds a capability ``Daemon``
        itself does not declare -- so a caller
        (``DaemonService.reset_camera_config``) can learn what is now in
        force to keep its own config-service state in sync, without
        re-deriving the same on-disk resolution itself.
        """
        config = self._on_disk_config()
        return config if config is not None else self.effective_config()

    def _on_disk_config(self) -> Optional[CameraConfig]:
        """The real document at *config_path*, re-read fresh through
        *config_loader* every call -- never cached, so a file edited
        between resets is always picked up. ``None`` when either was
        not injected at construction, or the resolved path does not
        currently exist on disk -- the shared "is there really
        something installed" check :meth:`reset`/:meth:`stored_config`
        both build on, so the two methods can never disagree about
        *whether* an on-disk document exists, only about what to do
        when one does not."""
        if (
            self._config_loader is not None
            and self._config_path is not None
            and self._config_path.exists()
        ):
            return self._config_loader.load_camera_config(self._config_path)
        return None

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"Camera(slug={self._slug!r}, present={self._present}, usable={self._backend.is_open})"
