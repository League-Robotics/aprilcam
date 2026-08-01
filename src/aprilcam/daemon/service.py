"""``DaemonService`` -- the ``Daemon`` base class's RPC-body
implementation for ``list_cameras``, ``probe_cameras``, ``get_frame``,
``status`` (ticket 020-004), plus ``get_tags``/``get_tag``/
``get_tag_history`` (ticket 021-004).

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

import logging
import threading
import time
from dataclasses import replace
from typing import Callable, Dict, Optional, Tuple

import cv2 as cv

from aprilcam.camera.backends import CaptureBackend
from aprilcam.camera.backends.opencv_backend import OpenCVBackend
from aprilcam.camera.camera import Camera
from aprilcam.camera.discovery import CameraDiscovery
from aprilcam.camera.identity import CameraIdentity, resolve_all
from aprilcam.camera.registry import CameraRecord, CameraRegistry
from aprilcam.config.interfaces import Directories
from aprilcam.daemon.detection import DetectionEngine
from aprilcam.daemon.interfaces import Daemon, DaemonStatus
from aprilcam.errors import CameraNotPresent, NotCalibrated, UnknownCamera
from aprilcam.types import CameraState, ImageFrame, ProbeReport, TagFrame, TagId, TagRecord

__all__ = ["DaemonService"]

logger = logging.getLogger(__name__)

#: What ``get_frame``'s ``encoding`` parameter accepts this sprint --
#: matches ``daemon.interfaces.Daemon.get_frame``'s docstring exactly
#: ("jpeg" default, "png" lossless).
VALID_ENCODINGS = ("jpeg", "png")

_CV_EXTENSION = {"jpeg": ".jpg", "png": ".png"}


def _default_backend_factory(index: int) -> CaptureBackend:
    return OpenCVBackend(index)


class DaemonService(Daemon):
    """Concrete ``daemon.interfaces.Daemon`` (this sprint's slice only).

    Derives from ``Daemon`` (see that class's module docstring for the
    base-class-vs-``Protocol`` design rule): the 23 methods this sprint
    does not implement are inherited as-is, so calling one raises
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
    detection:
        The per-camera ring buffer plus burst orchestration
        (``daemon/detection.py``, ticket 021-004) that
        :meth:`get_tags`/:meth:`get_tag`/:meth:`get_tag_history`/
        :meth:`get_frame` delegate to. Defaults to a fresh
        ``DetectionEngine()`` (real pipeline, real detector) when not
        supplied -- tests that need to pin exactly how many detection
        bursts ran construct their own ``DetectionEngine`` with a
        substitutable ``detector_factory`` and pass it here.
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
        detection: Optional[DetectionEngine] = None,
    ) -> None:
        self._registry = registry
        self._discovery = discovery
        self._directories = directories
        self._cameras: Dict[str, Camera] = cameras if cameras is not None else {}
        self._identity_source = identity_source
        self._backend_factory = backend_factory
        self._detection = detection if detection is not None else DetectionEngine()
        self._lock = threading.Lock()

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
        used for everything else.
        """
        present = self._discovery.known_present()
        states = []
        for record in self._registry.records():
            held = self._cameras.get(record.slug)
            if held is not None:
                states.append(held.state)
                continue
            is_present = record.slug in present
            states.append(
                CameraState(
                    name=record.slug,
                    present=is_present,
                    usable=is_present,
                    playfield=None,
                    calibrated=False,
                    number=record.number,
                )
            )
        return tuple(states)

    def probe_cameras(self) -> ProbeReport:
        """See ``daemon.interfaces.Daemon.probe_cameras``. Pure
        delegation to ``CameraDiscovery.probe()`` -- no business logic
        here (sprint architecture's module table)."""
        return self._discovery.probe()

    def get_frame(
        self, camera: str, *, deskewed: bool = False, encoding: str = "jpeg"
    ) -> ImageFrame:
        """See ``daemon.interfaces.Daemon.get_frame``.

        ``deskewed=True`` fails explicitly with :class:`NotCalibrated`
        every time this sprint -- no calibration subsystem exists yet,
        so every camera is uncalibrated by construction (matches the
        base class docstring's "never silently substitutes a raw frame").
        An unrecognised ``encoding`` is a plain ``ValueError`` (not one
        of ``errors.py``'s named types -- it is a caller-side argument
        mistake, not a domain failure the RPC boundary needs client
        code to dispatch on).

        ``tags`` embeds *camera*'s latest ring-buffer ``TagFrame`` when
        one exists (``daemon.detection.DetectionEngine.latest`` -- a
        pure read) and stays ``None`` when it does not -- asking for
        pixels never triggers a detection burst of its own (sprint.md
        Decision 4, ticket 021-004).
        """
        if encoding not in VALID_ENCODINGS:
            raise ValueError(
                f"unsupported encoding {encoding!r}; expected one of {VALID_ENCODINGS}"
            )
        if deskewed:
            raise NotCalibrated(
                f"{camera!r} is not calibrated (no calibration subsystem this sprint)"
            )

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
            device_name=record.device_name,
            number=record.number,
        )
        cam.open()
        with self._lock:
            self._cameras[record.slug] = cam
        return cam

    def _record_for(self, token: str) -> Optional[CameraRecord]:
        """Resolve *token* against the registry: an all-digits token by
        number, anything else by slug. See :meth:`_camera_for`."""
        if token.isdigit():
            return self._registry.get_by_number(int(token))
        return self._registry.get(token)

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
