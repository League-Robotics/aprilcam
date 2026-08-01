"""``DaemonService`` -- the ``Daemon`` base class's RPC-body
implementation for this sprint's narrow slice (ticket 020-004):
``list_cameras``, ``probe_cameras``, ``get_frame``, ``status``.

Delegates everything camera-shaped to ``camera/registry.py`` (ticket
002) and ``Camera``/``CameraDiscovery`` (ticket 003) -- no identity
derivation, no registry persistence, no probe/backend logic is
duplicated here (sprint architecture's module table: this module's
boundary is "RPC method bodies... Outside: camera hardware access").

**Only this sprint's four methods are implemented.** ``daemon.
interfaces.Daemon`` is a much larger base class (playfields, calibration,
tags, streams, annotations, tag parameters, guides) -- those belong to
later sprints per the sprint architecture's Out-of-Scope section, and
are simply absent from :class:`DaemonService`, inherited as-is from
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
from typing import Callable, Dict, Optional, Tuple

import cv2 as cv

from aprilcam.camera.backends import CaptureBackend
from aprilcam.camera.backends.opencv_backend import OpenCVBackend
from aprilcam.camera.camera import Camera
from aprilcam.camera.discovery import CameraDiscovery
from aprilcam.camera.identity import CameraIdentity, resolve_all
from aprilcam.camera.registry import CameraRecord, CameraRegistry
from aprilcam.config.interfaces import Directories
from aprilcam.daemon.interfaces import Daemon, DaemonStatus
from aprilcam.errors import NotCalibrated, UnknownCamera
from aprilcam.types import CameraState, ImageFrame, ProbeReport

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
    ) -> None:
        self._registry = registry
        self._discovery = discovery
        self._directories = directories
        self._cameras: Dict[str, Camera] = cameras if cameras is not None else {}
        self._identity_source = identity_source
        self._backend_factory = backend_factory
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
        )

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

    def _camera_for(self, slug: str) -> Camera:
        """The held-open ``Camera`` for *slug*, opening it on first use.

        Raises :class:`UnknownCamera` (naming every slug the registry
        knows) both for a slug the registry has never seen and for one
        it has but cannot currently locate among enumerated backends
        (unplugged, or a transient enumeration miss) -- ``errors.py``
        has no third category for "known but not present right now",
        and inventing one is outside this ticket's scope (see the
        ticket's Open Question 1 resolution for the same reasoning
        applied to ``get_camera_config``).
        """
        with self._lock:
            existing = self._cameras.get(slug)
        if existing is not None:
            return existing

        record = self._registry.get(slug)
        available = tuple(r.slug for r in self._registry.records())
        if record is None:
            raise UnknownCamera(slug, available)

        index = self._index_for(record)
        if index is None:
            raise UnknownCamera(slug, available)

        cam = Camera(
            record.slug,
            self._backend_factory(index),
            device_name=record.device_name,
            number=record.number,
        )
        cam.open()
        with self._lock:
            self._cameras[slug] = cam
        return cam

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
