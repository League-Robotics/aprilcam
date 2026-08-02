"""``DaemonService`` -- the ``Daemon`` base class's RPC-body
implementation for ``list_cameras``, ``probe_cameras``, ``get_frame``,
``status`` (ticket 020-004), plus ``get_tags``/``get_tag``/
``get_tag_history`` (ticket 021-004), plus ``set_playfield``/
``get_playfield``/``list_playfields``/``set_camera_config``/
``get_camera_config`` (ticket 022-001), plus ``calibrate``/
``get_calibration`` (ticket 022-003).

**Calibration persistence** (ticket 022-003, UC-01's "calibration is
file-backed, unlike pushed config"). All vision/homography work is
delegated to ``calibration.calibration.Calibrator`` -- this class's job
is exactly the three things that module boundary reserves for the
daemon: resolving the camera and handing it frames (the ``capture_frames``
callable ``Calibrator`` is constructed with), writing a successful
result to ``<state_dir>/cameras/<camera>/calibration.json`` (atomic
tmp+fsync+rename, mirroring ``camera/registry.py``'s ``_save``), and
reloading every camera's ``calibration.json`` at construction time --
"loaded at daemon startup", the same file-backed rule the module
docstring above already describes for ``config.json``/registry.json.

Delegates everything camera-shaped to ``camera/registry.py`` (ticket
002) and ``Camera``/``CameraDiscovery`` (ticket 003) -- no identity
derivation, no registry persistence, no probe/backend logic is
duplicated here (sprint architecture's module table: this module's
boundary is "RPC method bodies... Outside: camera hardware access").
Everything vision-shaped -- the ring buffer, detection bursts, corner
handling -- is delegated the same way to ``daemon/detection.py``
(ticket 021-004): no vision logic lives inline here, matching sprint
021's module boundary table exactly as sprint 020's did for cameras.

**Only a subset of ``Daemon``'s methods are implemented.** ``daemon.
interfaces.Daemon`` is a much larger base class (playfields, calibration,
streams, annotations, tag parameters, guides) -- those belong to later
sprints per the sprint architecture's Out-of-Scope section, and are
simply absent from :class:`DaemonService`, inherited as-is from
``Daemon``. Calling one of them here raises ``NotImplementedError``
naming the missing method (``daemon.interfaces``'s module docstring
explains why this is a plain base class and not ``abc.ABC``:
un-instantiable until all 27 methods existed would force 23 hollow
stubs, which is worse than an honest partial implementation).
``isinstance(service, Daemon)`` is ``True`` -- :class:`DaemonService`
really is a ``Daemon``, just an incomplete one; the per-method
``NotImplementedError`` is what keeps that honest rather than
pretending full conformance.

**Long-lived ``Camera`` instances, held open.** ``get_frame`` opens a
camera *at most once*: the first successful open is cached in
``self._cameras`` and reused for every subsequent call, which is what
lets ``Camera``'s self-heal (5 consecutive failures -> reopen, capped
at 10) actually accumulate state across calls rather than resetting on
every request. This cache **is** the daemon's live-subscriber table:
:meth:`active_slugs` exposes its keys to ``CameraDiscovery`` as the
``active_slugs`` callback (``camera/discovery.py``'s constructor;
ticket 003 left this defaulting to "nothing is active" and named this
exact wiring as this ticket's job) so ``probe_cameras()``'s
non-disturbance guarantee (UC-25.S-7) holds for real work, not just the
mechanism-level unit test ticket 003 could write without a service to
wire into.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import cv2 as cv
import numpy as np

from aprilcam.calibration.calibration import Calibration, Calibrator
from aprilcam.calibration.deskew import Deskewer
from aprilcam.calibration.interfaces import CalibrationResult
from aprilcam.camera.backends import CaptureBackend, ControlBackend
from aprilcam.camera.backends.opencv_backend import OpenCVBackend
from aprilcam.camera.backends.uvc_util import UvcUtilBackend
from aprilcam.camera.camera import Camera
from aprilcam.camera.discovery import CameraDiscovery
from aprilcam.camera.identity import CameraIdentity, resolve_all
from aprilcam.camera.registry import CameraRecord, CameraRegistry
from aprilcam.config.interfaces import CameraConfig, Directories
from aprilcam.config.interfaces import ConfigLoader as ConfigLoaderIface
from aprilcam.config.interfaces import PlayfieldDefinition as PlayfieldDefinitionIface
from aprilcam.config.loader import ConfigLoader as JsonConfigLoader
from aprilcam.daemon.detection import DetectionEngine
from aprilcam.daemon.interfaces import Daemon, DaemonStatus
from aprilcam.errors import CameraNotPresent, NotCalibrated, UnknownCamera, UnknownPlayfield
from aprilcam.playfield.playfield import Playfield
from aprilcam.types import CameraState, ImageFrame, ProbeReport, TagFrame, TagId, TagRecord

__all__ = ["DaemonService"]

#: Name every camera's calibration file gets under
#: ``<state_dir>/cameras/<camera>/`` -- design.md §7's storage layout.
CALIBRATION_FILENAME = "calibration.json"

#: How many frames :meth:`DaemonService._capture_calibration_frames`
#: reads for one :meth:`DaemonService.calibrate` call -- see
#: ``calibration.calibration.DEFAULT_CALIBRATION_FRAMES``'s docstring
#: for provenance (mined from v1's real-camera-tuned default). Frames
#: the camera fails to deliver (``Camera.read()`` returning ``None``)
#: are simply not counted towards this budget's worth of usable frames.
CALIBRATION_FRAME_COUNT = 30

logger = logging.getLogger(__name__)

#: What ``get_frame``'s ``encoding`` parameter accepts this sprint --
#: matches ``daemon.interfaces.Daemon.get_frame``'s docstring exactly
#: ("jpeg" default, "png" lossless).
VALID_ENCODINGS = ("jpeg", "png")

_CV_EXTENSION = {"jpeg": ".jpg", "png": ".png"}


def _default_backend_factory(index: int) -> CaptureBackend:
    return OpenCVBackend(index)


def _default_control_backend_factory(device_name: str) -> Optional[ControlBackend]:
    """The real per-control mechanism for a newly-opened camera's
    settings surface (ticket 023-001, sprint.md Decision 2).

    macOS only, unconditionally: real-hardware validation against camera
    3 (an OV9782) found ``cv2.VideoCapture.set()`` -- ``OpenCVBackend``'s
    settings path -- returns ``False`` for every one of
    ``auto_exposure``/``exposure``/``gain``/``auto_white_balance``/
    ``white_balance_temperature`` on this platform's AVFoundation capture
    session, so ``UvcUtilBackend`` (``camera/backends/uvc_util.py``) is
    unconditionally what a macOS ``Camera`` uses for those controls, not
    a fallback tried only if something else fails first. Deliberately
    **not** wrapped in a try/except that would swallow a missing
    ``uvc-util`` binary into ``None`` -- sprint.md's Migration Concerns
    are explicit that its absence "must surface as a clear, typed
    error... never a silent fallback to a path that... may not actually
    work", so :class:`~aprilcam.camera.backends.uvc_util.UvcUtilBackend`'s
    constructor is left to raise straight through when ``uvc-util`` is
    not on ``PATH``, rather than this factory quietly degrading to the
    already-proven-broken ``OpenCVBackend`` control surface.

    Returns ``None`` on every other platform, unchanged from before this
    ticket -- Linux/Raspberry Pi UVC control is out of this sprint's
    scope (sprint.md Scope); ``Camera`` falls back to *backend* itself
    for settings exactly as it always has (``camera/camera.py``'s
    module docstring).
    """
    if sys.platform != "darwin":
        return None
    return UvcUtilBackend(device_name)


class DaemonService(Daemon):
    """Concrete ``daemon.interfaces.Daemon`` (this sprint's slice only).

    Derives from ``Daemon`` (see that class's module docstring for the
    base-class-vs-``Protocol`` design rule): the 14 methods this sprint
    does not implement (streams, annotations, tag parameters, guides,
    ``find_feature``) are inherited as-is, so calling one raises
    ``NotImplementedError`` naming the missing method rather than an
    ``AttributeError`` -- ``isinstance(service, Daemon)`` is also simply
    true now, mechanically, rather than a Protocol structural guess.

    Parameters
    ----------
    registry:
        The persistent identity -> slug/number mapping (ticket 002).
    discovery:
        Passive-watch + active-probe camera discovery (ticket 003).
        Its ``active_slugs`` callback should be wired to this service's
        :meth:`active_slugs` at construction (``daemon/__main__.py``
        does this) so probe's non-disturbance guarantee holds.
    directories:
        Reported verbatim by :meth:`status` (``config_dir``/
        ``state_dir``) -- UC-01's "which roots were chosen is never a
        mystery," now answerable over the wire too.
    cameras:
        The held-open-camera cache, keyed by slug. Accepts an
        externally-owned dict (rather than always constructing its own)
        so ``daemon/__main__.py`` can hand the *same* dict object to
        both this service and ``CameraDiscovery``'s ``active_slugs``
        closure -- see the module docstring. Defaults to a fresh, empty
        dict when not supplied (tests that do not care about this
        wiring).
    identity_source / backend_factory:
        Resolve a registered camera's *current* OS-probe index on
        demand (only needed the first time a given slug is requested
        via :meth:`get_frame` -- see :meth:`_camera_for`). Overridable
        for headless tests exactly like ``CameraDiscovery``'s own
        constructor; default to real hardware enumeration.
    control_backend_factory:
        Builds the optional :class:`~aprilcam.camera.backends.ControlBackend`
        a newly-constructed ``Camera`` uses for ``apply_config``'s
        settings surface (ticket 023-001 -- real UVC control validated
        live on camera 3; see :func:`_default_control_backend_factory`).
        Defaults to that real, macOS-only factory; headless tests
        override with ``lambda device_name: None`` so a ``FakeBackend``
        stands in for settings too, exactly like *backend_factory*
        above.
    detection:
        The per-camera ring buffer plus burst orchestration
        (``daemon/detection.py``, ticket 021-004) that
        :meth:`get_tags`/:meth:`get_tag`/:meth:`get_tag_history`/
        :meth:`get_frame` delegate to. Defaults to a fresh
        ``DetectionEngine()`` (real pipeline, real detector) when not
        supplied -- tests that need to pin exactly how many detection
        bursts ran construct their own ``DetectionEngine`` with a
        substitutable ``detector_factory`` and pass it here.
    calibrator:
        The homography-solving engine (``calibration.calibration.
        Calibrator``, ticket 022-003) that :meth:`calibrate` delegates
        all vision/geometry work to. Defaults to a real ``Calibrator``
        wired against this service's own ``_camera_configs``/
        ``_playfields`` stores and :meth:`_capture_calibration_frames`
        -- tests that want a substitutable frame source or camera-config
        lookup construct their own ``Calibrator`` and pass it here
        instead, the same override pattern ``detection`` above uses.
    config_loader:
        The :class:`~aprilcam.config.interfaces.ConfigLoader` every
        newly-constructed ``Camera`` is given (ticket 023-004), for
        :meth:`Camera.reset`/:meth:`Camera.stored_config` to read a
        camera's on-disk ``config_dir/cameras/<slug>/config.json`` back.
        This service never calls it itself -- passing it through to
        ``Camera`` at construction is the one piece of "where configs
        come from" knowledge this service carries, targeted reads only,
        never the boot-time directory scan (that stays in
        ``daemon.__main__.build()``, sprint.md Architecture "Modules"
        point 1). Defaults to a real ``config.loader.ConfigLoader``;
        tests substitute their own when they need to.
    """

    def __init__(
        self,
        *,
        registry: CameraRegistry,
        discovery: CameraDiscovery,
        directories: Directories,
        cameras: Optional[Dict[str, Camera]] = None,
        identity_source: Callable[[], Dict[int, CameraIdentity]] = resolve_all,
        backend_factory: Callable[[int], CaptureBackend] = _default_backend_factory,
        control_backend_factory: Callable[[str], Optional[ControlBackend]] = (
            _default_control_backend_factory
        ),
        detection: Optional[DetectionEngine] = None,
        calibrator: Optional[Calibrator] = None,
        config_loader: Optional[ConfigLoaderIface] = None,
    ) -> None:
        self._registry = registry
        self._discovery = discovery
        self._directories = directories
        self._cameras: Dict[str, Camera] = cameras if cameras is not None else {}
        self._identity_source = identity_source
        self._backend_factory = backend_factory
        self._control_backend_factory = control_backend_factory
        self._config_loader: ConfigLoaderIface = (
            config_loader if config_loader is not None else JsonConfigLoader()
        )
        # ``calibration_lookup=self.get_calibration``: a bound-method
        # reference, resolved at call time, not at this line -- safe to
        # pass before ``self._calibrations``/``self._lock`` exist below,
        # since ``DetectionEngine`` never calls it during construction,
        # only from a later ``run_burst`` (ticket 022-004, module
        # docstring's "threaded through" section). Callers that pass
        # their own ``detection=`` (tests pinning burst counts) own their
        # engine's ``calibration_lookup`` themselves.
        self._detection = (
            detection if detection is not None else DetectionEngine(calibration_lookup=self.get_calibration)
        )
        self._playfields: Dict[str, PlayfieldDefinitionIface] = {}
        self._camera_configs: Dict[str, CameraConfig] = {}
        self._lock = threading.Lock()
        self._calibrator = calibrator if calibrator is not None else Calibrator(
            get_camera_config=self._camera_config_for_calibration,
            get_playfield=self._playfield_for_calibration,
            capture_frames=self._capture_calibration_frames,
        )
        self._calibrations: Dict[str, Calibration] = {}
        self._load_calibrations()

    # -- beyond the interface: CameraDiscovery's active_slugs wiring -----

    def active_slugs(self) -> frozenset:
        """Slugs this service currently holds a ``Camera`` open for.

        Passed as ``CameraDiscovery(..., active_slugs=service.
        active_slugs)`` so ``probe_cameras()`` never opens a backend
        already held here (UC-25.S-7). See the module docstring.
        """
        with self._lock:
            return frozenset(self._cameras.keys())

    # -- daemon.interfaces.Daemon (this sprint's slice) --------------------

    def list_cameras(self) -> Tuple[CameraState, ...]:
        """See ``daemon.interfaces.Daemon.list_cameras``.

        Answered from what is already known -- the registry (every
        camera ever seen) plus ``discovery.known_present()`` (updated by
        passive watching and by the last ``probe()``) -- never a fresh
        probe. A slug this service already holds open reports its
        *live* ``Camera.state`` (which reflects self-heal exhaustion)
        rather than the coarser present/usable-from-enumeration guess
        used for everything else. ``playfield`` is overlaid from
        whatever ``CameraConfig`` :meth:`set_camera_config` last held
        for that slug (``None`` if never configured) -- SUC-001's "``
        camera list`` shows the camera linked" -- rather than stored on
        ``Camera`` itself, keeping the config-service link a pure
        ``DaemonService``-level concern (ticket 022-001).

        ``calibrated``/``calibration_stale`` are overlaid the same way,
        from :meth:`get_calibration` -- **not** read off ``Camera.state``
        (``Camera`` has no notion of calibration; that state lives here,
        in ``self._calibrations``, per the module boundary that keeps
        calibration a daemon-level concern rather than something
        ``camera/camera.py`` knows about). Bug fix, ticket 022-007: this
        method previously hardcoded ``calibrated=False`` unconditionally
        in both branches below (and, in the ``held`` branch, inherited a
        second independent hardcoded ``False`` from ``Camera.state``
        itself) -- a genuinely calibrated camera reported
        ``uncalibrated`` forever in ``camera list``/``daemon status``,
        even though ``get_frame(deskewed=True)``/``get_tags``/
        ``playfield view`` all read the real calibration correctly. Pure
        display/state-reporting bug: the calibration itself was never
        wrong, only what this method told a caller about it.
        """
        present = self._discovery.known_present()
        states = []
        for record in self._registry.records():
            playfield = self._playfield_link(record.slug)
            calibrated, calibration_stale = self._calibration_flags(record.slug)
            held = self._cameras.get(record.slug)
            if held is not None:
                states.append(
                    replace(
                        held.state,
                        playfield=playfield,
                        calibrated=calibrated,
                        calibration_stale=calibration_stale,
                    )
                )
                continue
            is_present = record.slug in present
            states.append(
                CameraState(
                    name=record.slug,
                    present=is_present,
                    usable=is_present,
                    playfield=playfield,
                    calibrated=calibrated,
                    calibration_stale=calibration_stale,
                    number=record.number,
                )
            )
        return tuple(states)

    def _calibration_flags(self, slug: str) -> Tuple[bool, bool]:
        """``(calibrated, calibration_stale)`` for *slug*, from whatever
        :meth:`get_calibration` currently holds -- ``(False, False)``
        for a camera that has never been calibrated. The single overlay
        point :meth:`list_cameras` uses for both its branches, so a
        camera's reported calibration status can never drift between
        the "held open" and "not held open" code paths."""
        calibration = self.get_calibration(slug)
        if calibration is None:
            return False, False
        return True, calibration.stale

    def probe_cameras(self) -> ProbeReport:
        """See ``daemon.interfaces.Daemon.probe_cameras``. Pure
        delegation to ``CameraDiscovery.probe()`` -- no business logic
        here (sprint architecture's module table)."""
        return self._discovery.probe()

    def get_frame(
        self, camera: str, *, deskewed: bool = False, encoding: str = "jpeg"
    ) -> ImageFrame:
        """See ``daemon.interfaces.Daemon.get_frame``.

        An unrecognised ``encoding`` is a plain ``ValueError`` (not one
        of ``errors.py``'s named types -- it is a caller-side argument
        mistake, not a domain failure the RPC boundary needs client
        code to dispatch on), checked before either branch below touches
        a camera or calibration lookup.

        ``deskewed=True`` delegates to :meth:`_get_deskewed_frame`
        (ticket 022-005): :class:`NotCalibrated` for an uncalibrated
        camera, a real warped frame for a calibrated one -- see that
        method's docstring. The raw (``deskewed=False``) branch is
        unchanged from sprint 021: ``tags`` embeds *camera*'s latest
        ring-buffer ``TagFrame`` when one exists
        (``daemon.detection.DetectionEngine.latest`` -- a pure read) and
        stays ``None`` when it does not -- asking for pixels never
        triggers a detection burst of its own (sprint.md Decision 4,
        ticket 021-004).
        """
        if encoding not in VALID_ENCODINGS:
            raise ValueError(
                f"unsupported encoding {encoding!r}; expected one of {VALID_ENCODINGS}"
            )
        if deskewed:
            return self._get_deskewed_frame(camera, encoding=encoding)

        cam = self._camera_for(camera)
        frame = cam.read()
        if frame is None:
            raise RuntimeError(f"no frame available from camera {camera!r}")

        ok, buf = cv.imencode(_CV_EXTENSION[encoding], frame)
        if not ok:
            raise RuntimeError(f"failed to encode frame from {camera!r} as {encoding}")

        height, width = frame.shape[:2]
        return ImageFrame(
            data=buf.tobytes(),
            width=width,
            height=height,
            encoding=encoding,
            timestamp=time.time(),
            deskewed=False,
            tags=self._detection.latest(cam.slug),
        )

    def _get_deskewed_frame(self, camera: str, *, encoding: str) -> ImageFrame:
        """``get_frame(deskewed=True)``'s real body (ticket 022-005;
        was an unconditional :class:`NotCalibrated` since sprint 020).

        **Calibration is checked, and this raises, before
        :meth:`_camera_for` is ever called** -- exactly the same
        "precondition fails before any hardware access" discipline
        :meth:`calibrate` already gets from ``Calibrator`` (module
        docstring). A deskewed request on an uncalibrated camera must
        never substitute a raw frame (``daemon.interfaces.Daemon.get_frame``'s
        base-class docstring) -- checking first, not merely returning
        early, is what makes "no raw frame is substituted" true by
        construction rather than by an easily-broken ordering.

        *camera* is resolved to its canonical slug via
        :meth:`_resolve_slug_if_known` up front (SUC-005 number-or-slug
        addressing -- the same hotfix :meth:`calibrate`/
        :meth:`get_calibration` already apply) *before* the calibration
        lookup, so a numbered camera's calibration is found the same way
        a slug-addressed one is.

        The playfield dimensions the warp needs come from
        ``calibration.playfield`` (the definition this *homography* was
        actually solved against) via :meth:`_playfield_for_calibration`,
        not from the camera's *current* live playfield link -- those can
        diverge (a camera re-linked to a different playfield after
        calibrating), and the homography's own world coordinates are
        only meaningful against the definition it was solved from.
        """
        slug = self._resolve_slug_if_known(camera)
        calibration = self.get_calibration(slug)
        if calibration is None:
            raise NotCalibrated(f"{camera!r} is not calibrated")

        definition = self._playfield_for_calibration(calibration.playfield)
        if definition is None:
            raise NotCalibrated(
                f"{camera!r} was calibrated against playfield "
                f"{calibration.playfield!r}, which this daemon no longer holds"
            )

        cam = self._camera_for(camera)
        frame = cam.read()
        if frame is None:
            raise RuntimeError(f"no frame available from camera {camera!r}")

        deskewer = Deskewer(
            calibration=calibration,
            width_cm=definition.width_cm,
            height_cm=definition.height_cm,
        )
        warped = deskewer.deskew(frame)

        ok, buf = cv.imencode(_CV_EXTENSION[encoding], warped)
        if not ok:
            raise RuntimeError(f"failed to encode deskewed frame from {camera!r} as {encoding}")

        height, width = warped.shape[:2]
        return ImageFrame(
            data=buf.tobytes(),
            width=width,
            height=height,
            encoding=encoding,
            timestamp=time.time(),
            deskewed=True,
            px_per_cm=deskewer.px_per_cm,
            world_extent=deskewer.world_extent,
            tags=self._detection.latest(cam.slug),
        )

    def get_tags(self, camera: str) -> TagFrame:
        """See ``daemon.interfaces.Daemon.get_tags``.

        Resolves *camera* via :meth:`_camera_for` (SUC-005's
        number-or-slug addressing, and ``UnknownCamera``/
        ``CameraNotPresent`` both apply, inherited for free -- sprint.md
        Decision 5) and delegates the actual burst entirely to
        ``daemon.detection.DetectionEngine.run_burst`` -- no vision logic
        lives here (module docstring). Exactly one detection burst runs
        per call; nothing here caches or shares a result across calls
        (SUC-002's "twice with nothing in view = exactly two bursts"
        pin).
        """
        cam = self._camera_for(camera)
        return self._detection.run_burst(cam.slug, cam)

    def get_tag(self, camera: str, tag: TagId) -> Optional[TagRecord]:
        """See ``daemon.interfaces.Daemon.get_tag``.

        Runs the same single burst :meth:`get_tags` would (via
        :meth:`get_tags` itself, so the "exactly one burst per call"
        contract is inherited rather than re-implemented) and filters it
        for *tag*. Returns ``None`` -- never a zero-valued record -- when
        *tag* is absent from this burst's results (UC-09.S-4).
        """
        tag_frame = self.get_tags(camera)
        for record in tag_frame.tags:
            if record.tag == tag:
                return record
        return None

    def get_tag_history(
        self, camera: str, tag: Optional[TagId] = None, frames: Optional[int] = None
    ) -> Tuple[TagFrame, ...]:
        """See ``daemon.interfaces.Daemon.get_tag_history``.

        A pure buffer read: *camera* is still resolved via
        :meth:`_camera_for` (SUC-005 addressing and
        ``UnknownCamera``/``CameraNotPresent`` apply here too -- sprint.md
        Decision 5), but no detection burst runs
        (``DetectionEngine.history`` never calls ``run_burst``) -- the
        buffer only grows from prior :meth:`get_tags`/:meth:`get_tag`
        calls (sprint.md Decision 1).

        A buffer with fewer entries than *frames* requested returns
        everything available, not an error. Filtering by *tag* preserves
        every frame's real ``timestamp``/``frame_index`` even where that
        frame's ``.tags`` is empty for the requested tag -- gaps are
        explicit, never dropped (UC-26.S-4).
        """
        cam = self._camera_for(camera)
        buffered = self._detection.history(cam.slug)

        if frames is not None:
            buffered = buffered[-frames:] if frames > 0 else ()

        if tag is not None:
            buffered = tuple(
                replace(frame, tags=tuple(r for r in frame.tags if r.tag == tag))
                for frame in buffered
            )

        return buffered

    def status(self) -> DaemonStatus:
        """See ``daemon.interfaces.Daemon.status``.

        ``running`` is always ``True`` here: this method only executes
        inside a live daemon process answering a live RPC, so by
        construction the daemon *is* running (UC-05's remote-readable
        service view -- ``DaemonLifecycle.status()`` is the separate,
        local-only "is anything running at all" check).
        """
        return DaemonStatus(
            running=True,
            cameras=self.list_cameras(),
            config_dir=str(self._directories.config_dir),
            state_dir=str(self._directories.state_dir),
        )

    # -- playfields / camera config (ticket 022-001; reset, ticket 023-004) -
    #
    # The minimal config-service slice sprint.md's Architecture Decision 1
    # scopes for this sprint: playfields in full (held zero-or-more,
    # keyed by name -- Decision 4), plus a camera-config slice carrying
    # only the playfield link. Both stores are plain in-memory dicts,
    # never written to disk (UC-01's ephemeral rule is structural here,
    # not a policy this class has to remember -- there is simply no
    # write path). ``reset_camera_config`` (below, ticket 023-004) is the
    # one deliberate exception to "never written to disk" -- it *reads*
    # disk, never writes it, so the ephemeral rule for RPC-pushed state
    # is undisturbed.

    def set_playfield(self, definition: PlayfieldDefinitionIface) -> None:
        """See ``daemon.interfaces.Daemon.set_playfield``.

        A single dict assignment, keyed by the document's own ``name``
        -- atomic by construction, so there is no partial-apply case to
        guard against here. Whole-document *validation* (malformed
        JSON, no name, duplicate ArUco ids, field-size inconsistency)
        already happened client-side in ``config.loader.ConfigLoader``
        before *definition* was ever built (UC-23.S-4) -- by the time an
        object reaches here it is a well-formed ``PlayfieldDefinition``.
        Replacing an existing definition of the same name does not
        (yet) mark any calibration against it stale -- no calibration
        subsystem exists until ticket 003.
        """
        with self._lock:
            self._playfields[definition.name] = definition

    def get_playfield(self, name: str) -> Playfield:
        """See ``daemon.interfaces.Daemon.get_playfield``. Raises
        :class:`~aprilcam.errors.UnknownPlayfield`, naming every
        playfield this daemon currently holds, for a name it does not
        recognise."""
        with self._lock:
            definition = self._playfields.get(name)
            camera_configs = tuple(self._camera_configs.values())
        if definition is None:
            raise UnknownPlayfield(name, tuple(sorted(self._playfields.keys())))
        return Playfield(name=definition.name, definition=definition, camera_configs=camera_configs)

    def list_playfields(self) -> Tuple[Playfield, ...]:
        """See ``daemon.interfaces.Daemon.list_playfields``. Every held
        definition (Decision 4: no single-slot limit), each wrapped
        with the *same* camera-config snapshot so every entry's
        ``linked_cameras()`` reflects one consistent moment."""
        with self._lock:
            definitions = tuple(self._playfields.values())
            camera_configs = tuple(self._camera_configs.values())
        return tuple(
            Playfield(name=d.name, definition=d, camera_configs=camera_configs) for d in definitions
        )

    def get_camera_config(self, camera: str) -> CameraConfig:
        """See ``daemon.interfaces.Daemon.get_camera_config``.

        Answers from whatever :meth:`set_camera_config` last held for
        *camera* when one exists -- UC-03.S-2's "re-fetching the config
        returns the pushed values," exactly the document, never a live
        hardware readback merged in. This is deliberate, not an
        omission: the dump -> install -> restart -> dump round trip
        (UC-24.S-1) must reproduce exactly what was pushed or installed
        on disk, and the blank-template case just below must report
        ``None`` for every unset key even when the hardware itself has
        real factory-default values in force -- a live readback would
        get both of those wrong. Capturing what a camera's hardware is
        *actually* doing right now (as opposed to what document is on
        record for it) is a different, unbuilt capability -- ticket
        023-005 (this ticket) deliberately leaves it that way; see that
        ticket's notes.

        A camera with no stored document but **held open** in this
        process (:attr:`_cameras` -- populated by ``get_frame``/
        ``tags``/a config-push attempt/... via :meth:`_camera_for`)
        falls back to that camera's own introspected-plus-blank
        template (:meth:`~aprilcam.camera.camera.Camera.effective_config`,
        made real per-camera by ticket 023-002) -- UC-03.S-1's "freshly
        enumerated... never configured" scenario and UC-24.S-3's
        dump-as-template scenario, ticket 023-005's whole reason for
        depending on 023-002. A camera merely *registered* (known from
        a previous session or a probe, but never opened by this
        process) still reports :class:`~aprilcam.errors.UnknownCamera`
        here, matching UC-24.S-4 and every restart test's existing
        expectation -- config, pushed or blank-template, is exactly as
        ephemeral as everything else UC-01/UC-02 already establish;
        only calibration survives a restart.

        *camera* accepts a persistent number or a slug (SUC-005),
        resolved via :meth:`_resolve_slug_if_known` before either
        lookup -- both ``_camera_configs`` and ``_cameras`` are keyed by
        slug only, so an unresolved number would otherwise never match
        a real entry in either. The error still names whatever *camera*
        the caller actually typed, not the resolved slug.
        """
        slug = self._resolve_slug_if_known(camera)
        with self._lock:
            config = self._camera_configs.get(slug)
            held = self._cameras.get(slug)
            available = tuple(sorted(self._camera_configs.keys()))
        if config is not None:
            return config
        if held is not None:
            return held.effective_config()
        raise UnknownCamera(camera, available)

    def set_camera_config(self, config: CameraConfig) -> None:
        """See ``daemon.interfaces.Daemon.set_camera_config``.

        Stores *config* whole, keyed by its own ``name``, so
        :meth:`get_camera_config`/:meth:`list_cameras`/a linked
        playfield's ``linked_cameras()`` all see the update immediately.
        Accepts a config for a camera this daemon has never enumerated
        (a config outliving, or arriving before, its hardware is normal
        -- UC-25) and for a camera not currently held open -- there is
        nothing to reconfigure on a closed handle, only the link to
        record (ticket 023-001 AC1). A config stored here for a camera
        not yet open still takes effect the moment that camera is first
        opened, via :meth:`_camera_for`'s apply-on-open step (ticket
        023-003) -- the same mechanism that also applies a
        boot-time-loaded config (``daemon.__main__.build()``), so a
        pushed-while-absent config and a config installed on disk before
        the daemon ever started are handled identically once the camera
        actually appears.

        **Applies to hardware when the camera is held open** (ticket
        023-001, closing sprint 022's Decision 1 deferral -- UC-03's
        "applies... immediately, not at next open"). ``Camera.
        apply_config`` is called **before** the store, deliberately:
        its whole-push, hard-error contract
        (``camera.interfaces.Camera.apply_config``'s docstring) means an
        ``UnsupportedSetting`` propagates straight out of this method
        too, leaving *both* the hardware **and** the stored document
        exactly as they were (UC-03.S-3's "re-fetching the config
        returns the previous state") -- storing first and applying
        after would leave the stored document ahead of the hardware on
        exactly the failure path this rule exists to prevent.
        """
        with self._lock:
            cam = self._cameras.get(config.name)
        if cam is not None:
            cam.apply_config(config)
        with self._lock:
            self._camera_configs[config.name] = config

    def reset_camera_config(self, camera: str) -> None:
        """See ``daemon.interfaces.Daemon.reset_camera_config`` --
        "the RPC face of ``Camera.reset()``" (that base-class docstring's
        own words). Ticket 023-004.

        Opens *camera* if it is not already held (:meth:`_camera_for`,
        the same on-demand-open discipline every other camera-addressed
        method here uses -- ``UnknownCamera``/``CameraNotPresent``
        propagate exactly as they would from ``get_frame``/``calibrate``).
        Every ``Camera`` :meth:`_camera_for` constructs already carries
        the ``config_loader``/``config_path`` :meth:`Camera.reset` needs
        (wired at construction, see :meth:`_config_path_for`), so this
        method never touches ``config.loader`` or ``self._directories``
        itself -- disk access for reset stays entirely inside ``Camera``,
        this service only orchestrates (sprint.md Architecture "Modules"
        point 1: this service stays ignorant of disk beyond handing the
        already-resolved path/loader to ``Camera`` at construction).

        :meth:`Camera.reset` re-reads and re-applies whatever is at
        `config_dir/cameras/<slug>/config.json` (or the blank
        introspected-plus-blank template when nothing is installed --
        sprint.md Decision 3), discarding whatever live override this
        service held. The resulting document
        (:meth:`Camera.stored_config`) then replaces this service's
        ``_camera_configs`` entry for the camera, so
        :meth:`get_camera_config`/:meth:`list_cameras` reflect the reset
        immediately -- exactly the way a live :meth:`set_camera_config`
        push already would (SUC-003's postcondition: fetching the config
        after a reset returns the on-disk document, or the blank
        template, never the discarded override).
        """
        cam = self._camera_for(camera)
        cam.reset()
        config = cam.stored_config()
        with self._lock:
            self._camera_configs[cam.slug] = config

    def _playfield_link(self, slug: str) -> Optional[str]:
        """The playfield *slug* is currently linked to, per the last
        :meth:`set_camera_config` push -- ``None`` if never configured.
        """
        with self._lock:
            config = self._camera_configs.get(slug)
        return config.playfield if config is not None else None

    # -- calibration (ticket 022-003) --------------------------------------

    def calibrate(self, camera: str) -> CalibrationResult:
        """See ``daemon.interfaces.Daemon.calibrate``.

        A thin wrapper: every vision/geometry decision (multi-frame
        averaging, id matching, all-or-nothing acceptance, homography
        solve, degenerate-geometry rejection) lives in
        ``calibration.calibration.Calibrator``, injected as
        ``self._calibrator`` -- this method's only job is to run it and,
        on success, persist the result. ``NoPlayfieldLinked`` propagates
        straight through from the calibrator (raised there before it
        ever calls :meth:`_capture_calibration_frames`, so an unlinked
        camera never reaches the camera backend at all -- UC-15.S-2).

        *camera* accepts a persistent number or a slug (SUC-005) --
        resolved to the canonical slug via :meth:`_resolve_slug_if_known`
        **before** anything else happens, and that resolved slug is what
        gets passed to the calibrator and used as the
        ``self._calibrations``/``calibration.json`` key. Without this,
        a number token never matches ``_camera_configs`` (keyed by slug
        only) and every numbered camera reports the misleading
        ``NoPlayfieldLinked`` regardless of whether it is actually
        linked -- ticket 022-003's addressing regression, fixed here so
        every other camera-config-addressed method inherits the same
        resolution rather than re-implementing it (see
        :meth:`_resolve_slug_if_known`'s docstring).

        A failed attempt (``result.succeeded is False``) leaves
        ``calibration.json`` and this service's in-memory
        ``self._calibrations`` entry for *camera* completely untouched
        -- there is simply no write path taken on that branch, so "a
        prior-good calibration survives a failed retry" (UC-04.S-2,
        UC-15.S-3) holds by construction rather than by an explicit
        guard.
        """
        slug = self._resolve_slug_if_known(camera)
        result = self._calibrator.calibrate(slug)
        if result.succeeded:
            calibration = self._calibrator.get_calibration(slug)
            assert calibration is not None, (
                "Calibrator.calibrate() reported succeeded=True but "
                "get_calibration() has nothing stored -- a Calibrator bug"
            )
            with self._lock:
                self._calibrations[slug] = calibration
            self._persist_calibration(slug, calibration)
        return result

    def get_calibration(self, camera: str) -> Optional[Calibration]:
        """See ``daemon.interfaces.Daemon.get_calibration``.

        Answers from this service's in-memory cache -- populated by a
        successful :meth:`calibrate` call in this process, or by
        :meth:`_load_calibrations` at construction time from whatever
        ``calibration.json`` files already existed on disk (UC-01: a
        calibrated camera stays calibrated across a daemon restart).
        ``None`` for a camera that has never been calibrated -- never a
        fabricated/default ``Calibration``, mirroring
        :meth:`get_tag`'s "``None`` means not detected, never a
        zero-valued record" discipline.

        *camera* accepts a persistent number or a slug (SUC-005),
        resolved via :meth:`_resolve_slug_if_known` -- ``self.
        _calibrations`` is always keyed by the resolved slug (see
        :meth:`calibrate`), so looking it up by the raw token would
        silently report "never calibrated" for a numbered camera that
        actually is.
        """
        slug = self._resolve_slug_if_known(camera)
        with self._lock:
            return self._calibrations.get(slug)

    def _camera_config_for_calibration(self, camera: str) -> Optional[CameraConfig]:
        """``Calibrator``'s ``get_camera_config`` callable -- a locked
        read of :attr:`_camera_configs`, the same store
        :meth:`get_camera_config`/:meth:`_playfield_link` already read
        under :attr:`_lock`."""
        with self._lock:
            return self._camera_configs.get(camera)

    def _playfield_for_calibration(self, name: str) -> Optional[PlayfieldDefinitionIface]:
        """``Calibrator``'s ``get_playfield`` callable -- a locked read
        of :attr:`_playfields`."""
        with self._lock:
            return self._playfields.get(name)

    def _capture_calibration_frames(self, camera: str) -> Tuple[np.ndarray, ...]:
        """``Calibrator``'s ``capture_frames`` callable -- the one place
        calibration touches a camera backend (the module boundary's
        "camera frame access, handed in" rule). Resolves *camera* via
        :meth:`_camera_for` (opening it on first use, exactly like
        :meth:`get_frame`/:meth:`get_tags`) and reads up to
        :data:`CALIBRATION_FRAME_COUNT` frames, skipping any individual
        read that returns ``None`` (a transient capture miss) rather
        than failing the whole capture -- ``Calibrator.calibrate`` is
        the one that decides whether the frames actually collected are
        enough (it raises if this returns nothing at all).

        Never called for a camera with no playfield link: ``Calibrator.
        calibrate`` raises ``NoPlayfieldLinked`` before this callable is
        invoked, so :meth:`_camera_for` -- which can open a backend --
        is never reached on that path (UC-15.S-2's "zero calls to the
        camera backend").
        """
        cam = self._camera_for(camera)
        frames: List[np.ndarray] = []
        for _ in range(CALIBRATION_FRAME_COUNT):
            frame = cam.read()
            if frame is not None:
                frames.append(frame)
        return tuple(frames)

    def _calibration_path(self, camera: str) -> Path:
        """``<state_dir>/cameras/<camera>/calibration.json`` -- design.md
        §7's storage layout, matching ``camera/registry.py``'s
        ``<state_dir>/cameras/registry.json`` sibling layout (a
        per-camera subdirectory, not a shared file, so one camera's
        calibration can never collide with another's)."""
        return self._directories.state_dir / "cameras" / camera / CALIBRATION_FILENAME

    def _persist_calibration(self, camera: str, calibration: Calibration) -> None:
        """Atomically write *calibration* to *camera*'s
        ``calibration.json``: tmp file, ``fsync``, ``os.replace`` --
        the same tmp+fsync+rename pattern ``camera/registry.py``'s
        ``_save`` uses, so a crash between those steps leaves the prior
        file (if any) exactly as it was, never half-written."""
        path = self._calibration_path(camera)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise OSError(
                f"cannot create calibration directory {path.parent}: {exc}"
            ) from exc

        text = json.dumps(calibration.to_dict(), indent=2, sort_keys=True) + "\n"
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except OSError as exc:
            raise OSError(f"cannot write calibration file {path}: {exc}") from exc
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:  # pragma: no cover - best-effort cleanup
                pass

    def _load_calibrations(self) -> None:
        """Reload every camera's ``calibration.json`` at construction
        time (UC-01: "reloads it at startup like any other file").
        Scans ``<state_dir>/cameras/*/calibration.json`` directly rather
        than going through the camera registry, so a calibration is
        found regardless of whether this particular ``DaemonService``
        instance has re-enumerated that camera yet -- the file, not the
        registry, is the source of truth here. A missing ``state_dir``
        (first run) or a corrupt/malformed calibration file is tolerated
        by skipping it (logged as a warning), never by failing daemon
        construction -- mirrors ``camera.registry.CameraRegistry``'s
        "a corrupt file must never crash the daemon" stance.
        """
        cameras_dir = self._directories.state_dir / "cameras"
        if not cameras_dir.is_dir():
            return
        for entry in sorted(cameras_dir.iterdir()):
            if not entry.is_dir():
                continue
            path = entry / CALIBRATION_FILENAME
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                calibration = Calibration.from_dict(data)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                logger.warning(
                    "corrupt calibration file for %s at %s: %s -- ignoring",
                    entry.name,
                    path,
                    exc,
                )
                continue
            self._calibrations[entry.name] = calibration

    # -- beyond the interface: shutdown cleanup ----------------------------

    def close(self) -> None:
        """Close every held-open camera. Not part of the ``Daemon``
        base class -- ``daemon/lifecycle.py``'s ``on_stop`` hook calls
        this during a clean shutdown so no backend handle outlives the
        process that opened it."""
        with self._lock:
            held = list(self._cameras.values())
            self._cameras.clear()
        for cam in held:
            try:
                cam.close()
            except Exception:  # pragma: no cover - defensive
                logger.exception("error closing camera %s during shutdown", cam.slug)

    # -- internals -----------------------------------------------------

    def _camera_for(self, token: str) -> Camera:
        """The held-open ``Camera`` addressed by *token*, opening it on
        first use.

        *token* is either form ``camera list``/``camera probe`` print
        (ticket 021-002, SUC-005): an **all-digits token resolves by
        persistent registry number** (``CameraRegistry.get_by_number``);
        anything else resolves by slug, exactly as before
        (``CameraRegistry.get``). No ambiguity is possible between the
        two forms -- ``camera/identity.py``'s ``device_name_slug``
        guarantees every slug contains at least one non-digit character,
        so an all-digits token can never *be* a real slug.

        Raises :class:`UnknownCamera` (naming every known camera as
        ``"{number} {slug}"`` pairs, teaching the addressing scheme
        rather than just rejecting) for a token -- number or slug -- the
        registry has never seen. Raises :class:`CameraNotPresent`
        (naming the resolved slug) for a token that *does* resolve to a
        registry record but cannot currently be located among enumerated
        backends (unplugged, or a transient enumeration miss) -- a
        registry record outliving its hardware is normal (UC-25), so
        this is distinct from "never registered," not folded into
        :class:`UnknownCamera` the way it was before this ticket.

        **Apply-on-open (ticket 023-003).** A freshly-opened camera
        checks :attr:`_camera_configs` for a config already held under
        its slug -- populated either by a live :meth:`set_camera_config`
        push that arrived before this camera did, or by
        ``daemon.__main__.build()``'s boot-time load from ``config_dir``
        -- and applies it via :meth:`Camera.apply_config` *before* the
        camera is cached into :attr:`_cameras` and handed back to the
        caller. This is what makes a config installed on disk (or
        pushed while the camera was absent) take effect the instant the
        camera is actually opened, not only on a subsequent
        :meth:`set_camera_config` call -- closing UC-01.S-4's
        previously-broken playfield-link half. An ``UnsupportedSetting``
        from a stored-but-now-invalid config propagates straight through,
        exactly like a live push would; :attr:`_cameras` is only updated
        after this succeeds, so a camera that fails to apply its stored
        config is not left half-registered as held-open.
        """
        with self._lock:
            existing = self._cameras.get(token)
        if existing is not None:
            return existing

        record = self._record_for(token)
        if record is None:
            raise UnknownCamera(token, self._available())

        with self._lock:
            existing = self._cameras.get(record.slug)
        if existing is not None:
            return existing

        index = self._index_for(record)
        if index is None:
            raise CameraNotPresent(record.slug)

        cam = Camera(
            record.slug,
            self._backend_factory(index),
            control_backend=self._control_backend_factory(record.device_name),
            device_name=record.device_name,
            number=record.number,
            config_loader=self._config_loader,
            config_path=self._config_path_for(record.slug),
        )
        cam.open()
        with self._lock:
            config = self._camera_configs.get(record.slug)
        if config is not None:
            cam.apply_config(config)
        with self._lock:
            self._cameras[record.slug] = cam
        return cam

    def _config_path_for(self, slug: str) -> Path:
        """``config_dir/cameras/<slug>/config.json`` -- sprint.md
        Decision 3's fixed reset target (``ResetCameraConfig`` always
        means re-reading exactly this location, never whatever was last
        pushed), matching ``configuration.md``'s documented per-camera
        layout and ``config.loader.ConfigLoader.load_config_dir``'s own
        boot-time scan path for the same file. Computed fresh per call
        from :attr:`_directories`, set once at construction -- every
        ``Camera`` :meth:`_camera_for` builds gets this wired in
        (ticket 023-004), whether or not anything is actually installed
        there yet."""
        return self._directories.config_dir / "cameras" / slug / "config.json"

    def _record_for(self, token: str) -> Optional[CameraRecord]:
        """Resolve *token* against the registry: an all-digits token by
        number, anything else by slug. See :meth:`_camera_for`."""
        if token.isdigit():
            return self._registry.get_by_number(int(token))
        return self._registry.get(token)

    def _resolve_slug_if_known(self, token: str) -> str:
        """*token* (a persistent number or a slug, SUC-005 addressing)
        resolved to its canonical registry slug when the registry
        recognises it -- otherwise *token* itself, unchanged.

        The shared number-or-slug resolution point every
        camera-**config**-addressed method needs (:meth:`get_camera_config`,
        :meth:`calibrate`, :meth:`get_calibration`) -- built on the same
        :meth:`_record_for` :meth:`_camera_for` already uses, so a token
        resolves identically everywhere rather than each method
        re-deciding "is this a number" for itself.

        **Hotfix, ticket 022-003.** ``calibrate``/``get_calibration``/
        ``get_camera_config`` originally looked up ``self._camera_configs``/
        ``self._calibrations`` keyed by the *raw* token -- both stores
        are keyed by slug only (``set_camera_config``'s ``self.
        _camera_configs[config.name] = config``, ``config.name`` always
        a slug; :meth:`_persist_calibration`/:meth:`_load_calibrations`
        likewise). A valid persistent number therefore matched nothing,
        and ``calibrate`` reported the misleading ``NoPlayfieldLinked``
        for a camera that *was* linked, addressed the only way that
        happened not to work.

        Deliberately **soft**: unlike :meth:`_camera_for` (which raises
        :class:`~aprilcam.errors.UnknownCamera` for an unresolvable
        token, appropriate once hardware access follows), this never
        raises. A token the registry has never seen at all is passed
        through unchanged, so a downstream not-found error still names
        exactly what the caller typed and existing "unknown token"
        error shapes are undisturbed -- e.g. :meth:`get_camera_config`'s
        ``available`` lists cameras with a pushed config, not every
        registered camera, and stays that way for a token this method
        cannot resolve either.
        """
        record = self._record_for(token)
        return record.slug if record is not None else token

    def _available(self) -> Tuple[str, ...]:
        """Every known camera, formatted ``"{number} {slug}"`` for
        :class:`UnknownCamera`'s ``available`` list -- falls back to the
        bare slug for a record somehow missing a number (not reachable
        through normal construction, since ``CameraRecord.number`` is a
        required field, but defensive rather than assuming a stored
        record can never be malformed)."""
        return tuple(
            f"{r.number} {r.slug}" if r.number is not None else r.slug
            for r in self._registry.records()
        )

    def _index_for(self, record: CameraRecord) -> Optional[int]:
        """The current OS-probe index for *record*, or ``None`` if it is
        not enumerated right now. Re-resolves fresh every call (like
        ``CameraDiscovery.probe()`` does) rather than caching -- an
        index is only ever used once, immediately, to open a backend
        (it is explicitly unstable across replugs)."""
        for index, identity in self._identity_source().items():
            if identity.hardware_key == record.hardware_key:
                return index
        return None
