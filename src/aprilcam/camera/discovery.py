"""``CameraDiscovery`` — finding out what cameras exist (UC-25).

Converts the backend-enumeration and failure-diagnosis helpers from
``archive/src/aprilcam/camera/camutil.py`` (``list_cameras``,
``get_device_name``, ``diagnose_camera_failure``) into the shape of the
``CameraDiscovery`` base class (``camera/interfaces.py``): ``probe()``
(active, deep, on demand) and ``watch()`` (passive, continuous,
started once). Both build on ``camera/identity.py``'s enumeration
sources and ``camera/registry.py``'s persisted identity -> slug/number
mapping (tickets 002 and this one's own ``camera/camera.py``).

**Non-disturbance (UC-25.S-7).** ``probe()`` never opens a backend for
a camera the daemon is already streaming from. It cannot see the
daemon's live-subscriber table directly -- that table is owned by
``daemon/service.py`` (ticket 004, not yet built) -- so this module
takes an ``active_camera_state`` callback at construction: a slug ->
``Optional[CameraState]`` function. It defaults to "nothing is active"
(always ``None``), which is exactly right for a freshly constructed,
subscriber-less ``CameraDiscovery`` (this ticket's tests), and ticket
004 supplies its real accessor when it wires this class into the
daemon service. A slug for which the callback returns a real
``CameraState`` is held open, and ``probe()`` reports **that state's
own** ``present``/``usable`` values -- something is successfully
streaming from it, but "streaming" is not itself proof of health, so a
stalled/self-heal-exhausted held-open camera reports ``usable=False``
exactly as the daemon's own ``list_cameras()`` would (sprint 039
ticket 004, closing the gap where ``probe()`` used to hardcode
``usable=True`` for every active slug regardless of its real health).
Either way the slug is still named in ``ProbeReport.skipped`` --
healthy or stalled -- without any backend ever touching it; the skip
reason text itself distinguishes the two cases.

**Passive watching (UC-25.S-5) without any OS hotplug event source.**
Neither ``cv2-enumerate-cameras`` nor AVFoundation/V4L2 offer a
portable "device arrived/departed" callback this codebase can
subscribe to, so v1 did not have one and this module does not invent
one. ``watch()`` instead polls the same non-opening identity
enumeration ``probe()`` uses, diffs hardware keys against the previous
poll, and updates :meth:`CameraDiscovery.known_present` accordingly --
cheap and, because it is enumeration rather than an open device, never
disturbs a live stream either.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Dict, FrozenSet, List, Optional, Set

from aprilcam.camera.backends import (
    CaptureBackend,
    default_capture_backend_factory,
    resolve_capture_backend,
)
from aprilcam.camera.backends.libcamera import LibcameraDevice
from aprilcam.camera.camera import Camera
from aprilcam.camera.identity import CameraIdentity, resolve_all
from aprilcam.camera.interfaces import CameraDiscovery as _CameraDiscoveryBase
from aprilcam.camera.registry import CameraRegistry
from aprilcam.types import CameraState, ProbeReport, ProbeSkip

__all__ = ["CameraDiscovery"]

logger = logging.getLogger(__name__)

#: Seconds between passive-watch polls. Small enough that a hotplugged
#: camera shows up in ``camera list`` "within a few seconds" (UC-25.S-5).
DEFAULT_WATCH_INTERVAL = 2.0

#: Wall-clock bound (seconds) ``_probe_openable`` waits -- across as many
#: ``backend.read()`` attempts as fit within it -- for a real frame after
#: ``open()`` before giving up and reporting ``usable=False`` (sprint 038,
#: closing gap 1 of ``clasi/issues/camera-capture-session-wedge-is-silent-
#: and-needs-a-daemon-restart.md``). Before this, ``_probe_openable`` never
#: read at all: a device whose capture session is wedged -- ``open()``
#: succeeds, ``isOpened()``-equivalent state still reports true, but every
#: ``read()`` returns ``None`` indefinitely -- probed healthy anyway, the
#: single most misleading signal that issue identified. Deliberately
#: short: order of one frame interval, not ``daemon/DESIGN.md``'s
#: stall-watchdog window -- a healthy camera's first read after open
#: resolves quickly, and a wedged camera's read never resolves into a
#: real frame no matter how long this waits, so a longer bound would only
#: add latency, never catch anything a short one misses. This is an
#: explicit, accepted cost: ``probe()`` runs over every camera and is now
#: measurably slower per camera (``camera-DESIGN.md`` sprint 038 §3).
#: Overridable per :class:`CameraDiscovery` instance (``probe_read_timeout``)
#: so a test can inject a short bound rather than waiting out the default.
DEFAULT_PROBE_READ_TIMEOUT = 1.0

#: Pause between ``backend.read()`` attempts within
#: :data:`DEFAULT_PROBE_READ_TIMEOUT`'s bound -- gives a camera whose
#: first frame needs a moment (but is not actually wedged) more than one
#: instantaneous try, without busy-looping the probe thread.
_PROBE_READ_POLL_INTERVAL = 0.05


def _default_backend_factory(index: int) -> CaptureBackend:
    """``_probe_openable``'s default non-libcamera ``CaptureBackend``
    construction (sprint 038 ticket 005).

    Delegates to ``camera.backends.default_capture_backend_factory`` --
    the one place this selection (``WorkerBackend`` by default on
    macOS, ``OpenCVBackend`` elsewhere, either forced by
    ``APRILCAM_CAPTURE_BACKEND``) is decided, so this function and
    ``daemon/service.py``'s identically-named, identically-delegating
    ``_default_backend_factory`` cannot drift apart the way two
    independently-hardcoded ``OpenCVBackend(index)`` bodies did before
    this ticket -- both ``_probe_openable`` and ``_camera_for`` must
    agree on which backend a camera gets. See
    ``default_capture_backend_factory``'s own docstring for the full
    contract. Every existing test that constructs a ``CameraDiscovery``
    passes its own ``backend_factory=`` (a fake) and never reaches this
    default at all.
    """
    return default_capture_backend_factory(index)


class CameraDiscovery(_CameraDiscoveryBase):
    """Concrete ``camera.interfaces.CameraDiscovery``.

    Derives from ``camera.interfaces.CameraDiscovery`` (a plain base
    class, not ``Protocol`` -- see ``daemon.interfaces``'s module
    docstring for the design rule).

    Parameters
    ----------
    registry:
        Where identities resolve to stable slugs/numbers.
    active_camera_state:
        slug -> ``Optional[CameraState]``: ``None`` means the daemon
        does not currently hold this slug open (falls through to
        ``_probe_openable``, unchanged); a real ``CameraState`` means
        it is held open, and ``probe()`` reports *that* state's own
        ``usable`` value instead of assuming health (sprint 039 ticket
        004). See the module docstring's non-disturbance note. Defaults
        to "nothing is active" (always ``None``).
    backend_factory:
        index -> :class:`CaptureBackend`. Overridable so tests can
        inject a fake that raises on ``open()`` (AC6, UC-25.S-4)
        without touching real hardware; defaults to
        :func:`~aprilcam.camera.backends.default_capture_backend_factory`
        (sprint 038 ticket 005) --
        :class:`~aprilcam.camera.backends.worker_backend.WorkerBackend`
        on macOS, :class:`~aprilcam.camera.backends.opencv_backend.
        OpenCVBackend` elsewhere, either forced by
        ``APRILCAM_CAPTURE_BACKEND``.
    identity_source:
        () -> ``{index: CameraIdentity}``. Defaults to
        ``camera.identity.resolve_all`` -- fresh, non-caching, non-
        opening hardware enumeration. Overridable for headless tests.
    watch_interval:
        Seconds between ``watch()`` polls. Overridable so tests need
        not wait :data:`DEFAULT_WATCH_INTERVAL` seconds for a poll.
    probe_read_timeout:
        Seconds :meth:`_probe_openable` waits for a real frame after
        ``open()`` before reporting ``usable=False`` (sprint 038). See
        :data:`DEFAULT_PROBE_READ_TIMEOUT` for the bound and the
        tradeoff it accepts. Overridable so a test can inject a short
        bound rather than waiting out the default.
    list_loopback_cameras:
        () -> ``[LibcameraDevice, ...]``. Consulted only for a
        libcamera-identified camera (``identity.hardware_key`` prefixed
        ``"libcamera:"``) to resolve the real ``/dev/videoN`` device
        index a :class:`~aprilcam.camera.backends.v4l2_loopback.V4l2LoopbackBackend`
        needs -- see :func:`~aprilcam.camera.backends.resolve_capture_backend`
        (ticket 031-006, SUC-006). Defaults to the real, subprocess-free
        ``/sys`` scan (``camera.backends.libcamera.list_loopback_cameras``);
        overridable for headless tests exactly like *identity_source*
        above.
    """

    def __init__(
        self,
        registry: CameraRegistry,
        *,
        active_camera_state: Optional[Callable[[str], Optional[CameraState]]] = None,
        backend_factory: Callable[[int], CaptureBackend] = _default_backend_factory,
        identity_source: Callable[[], Dict[int, CameraIdentity]] = resolve_all,
        watch_interval: float = DEFAULT_WATCH_INTERVAL,
        probe_read_timeout: float = DEFAULT_PROBE_READ_TIMEOUT,
        list_loopback_cameras: Optional[Callable[[], List[LibcameraDevice]]] = None,
    ) -> None:
        self._registry = registry
        self._active_camera_state = active_camera_state or (lambda slug: None)
        self._backend_factory = backend_factory
        self._identity_source = identity_source
        self._watch_interval = watch_interval
        self._probe_read_timeout = probe_read_timeout
        self._list_loopback_cameras = list_loopback_cameras

        self._lock = threading.Lock()
        self._known_hardware_keys: FrozenSet[str] = frozenset()
        self._present_slugs: Set[str] = set()

        self._watch_thread: Optional[threading.Thread] = None
        self._watch_stop = threading.Event()

    # -- CameraDiscovery interface -------------------------------------------

    def probe(self) -> ProbeReport:
        """See ``camera.interfaces.CameraDiscovery.probe``.

        For every currently-enumerated identity: resolve its registry
        slug (registering it if new), then call
        ``active_camera_state(slug)``. ``None`` means the daemon does
        not hold this slug open -- briefly open a ``Camera`` over it to
        determine ``usable`` and immediately close it again. A camera
        that raises on open is still reported, ``usable=False``,
        ``present=True`` (UC-25.S-4), never omitted. A real
        ``CameraState`` means the slug is held open -- report *that*
        state's own ``present``/``usable`` values instead of assuming
        health, and record a ``ProbeSkip`` whose reason text
        distinguishes a healthy held-open camera from a stalled one
        (sprint 039 ticket 004: a held-open camera used to always probe
        ``usable=True`` even after its self-heal budget was exhausted).
        Registry records not seen in this enumeration pass are reported
        ``present=False`` (a config outliving its hardware --
        ``types.CameraState``'s own docstring -- not an error).
        """
        identities = self._safe_identities()

        seen_hardware_keys: Dict[str, str] = {}  # hardware_key -> slug
        cameras = []
        skipped = []

        for index, identity in identities.items():
            slug = self._registry.resolve(identity)
            seen_hardware_keys[identity.hardware_key] = slug
            number = self._number_for(slug)

            held_state = self._active_camera_state(slug)
            if held_state is not None:
                cameras.append(
                    CameraState(
                        name=slug,
                        present=held_state.present,
                        usable=held_state.usable,
                        playfield=None,
                        calibrated=False,
                        number=number,
                    )
                )
                reason = (
                    "in use by a live subscriber"
                    if held_state.usable
                    else "in use by a live subscriber (stalled)"
                )
                skipped.append(ProbeSkip(backend=slug, reason=reason))
                continue

            usable = self._probe_openable(slug, index, identity.hardware_key)
            cameras.append(
                CameraState(
                    name=slug,
                    present=True,
                    usable=usable,
                    playfield=None,
                    calibrated=False,
                    number=number,
                )
            )

        for record in self._registry.records():
            if record.slug in seen_hardware_keys.values():
                continue
            cameras.append(
                CameraState(
                    name=record.slug,
                    present=False,
                    usable=False,
                    playfield=None,
                    calibrated=False,
                    number=record.number,
                )
            )

        cameras.sort(key=lambda s: (s.number is None, s.number))

        with self._lock:
            self._known_hardware_keys = frozenset(seen_hardware_keys.keys())
            self._present_slugs = set(seen_hardware_keys.values())

        return ProbeReport(cameras=tuple(cameras), skipped=tuple(skipped))

    def watch(self) -> None:
        """See ``camera.interfaces.CameraDiscovery.watch``.

        Idempotent: a second call while a watch thread is already
        running is a no-op.
        """
        if self._watch_thread is not None and self._watch_thread.is_alive():
            return
        self._watch_stop.clear()
        self._watch_thread = threading.Thread(
            target=self._watch_loop, name="camera-discovery-watch", daemon=True
        )
        self._watch_thread.start()

    # -- beyond the Protocol: what watch() maintains, and test/shutdown hooks --

    def known_present(self) -> FrozenSet[str]:
        """Camera slugs this instance currently believes are attached.

        Refreshed by both ``probe()`` and the background ``watch()``
        poll. Not part of ``CameraDiscovery``'s declared interface -- ticket
        004's daemon service reads this to answer ``list_cameras``
        without re-probing on every call, which is the entire point of
        ``watch()`` existing (UC-25.S-5: hotplug needs no probe).
        """
        with self._lock:
            return frozenset(self._present_slugs)

    def stop_watch(self) -> None:
        """Stop the passive watch thread.

        Not part of ``CameraDiscovery``'s declared interface (which has no
        stop method -- process teardown is ``daemon/lifecycle.py``'s
        job, ticket 004). Exists for tests and for a clean shutdown
        path once that ticket wires one in.
        """
        self._watch_stop.set()
        thread, self._watch_thread = self._watch_thread, None
        if thread is not None:
            thread.join(timeout=self._watch_interval + 1.0)

    # -- internals -----------------------------------------------------

    def _safe_identities(self) -> Dict[int, CameraIdentity]:
        try:
            return self._identity_source()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("discovery: identity enumeration failed: %s", exc)
            return {}

    def _number_for(self, slug: str) -> Optional[int]:
        record = self._registry.get(slug)
        return record.number if record is not None else None

    def _probe_openable(self, slug: str, index: int, hardware_key: str) -> bool:
        """Open, one bounded frame read, then close a fresh ``Camera``
        over *index* to determine usability (sprint 038, closing gap 1 of
        ``clasi/issues/camera-capture-session-wedge-is-silent-and-needs-
        a-daemon-restart.md``). ``usable`` means "delivered a frame,"
        not merely "opened": a device whose capture session opens fine
        but is wedged -- every ``read()`` returns ``None`` forever -- must
        report ``usable=False``, where a bare open-then-close previously
        reported it healthy. Any exception from ``open()`` still means
        unusable, not a probe failure -- the camera still gets reported
        (UC-25.S-4); this ticket only adds a check *after* a successful
        open.

        *hardware_key* selects the backend via
        :func:`~aprilcam.camera.backends.resolve_capture_backend`
        (ticket 031-006): ``self._backend_factory`` (``OpenCVBackend`` by
        default) for every identity source except libcamera, which opens
        through :class:`~aprilcam.camera.backends.v4l2_loopback.V4l2LoopbackBackend`
        instead -- that branch point is unaffected by this ticket; the
        bounded read below runs uniformly afterward regardless of which
        backend was resolved.
        """
        backend = resolve_capture_backend(
            hardware_key,
            index,
            backend_factory=self._backend_factory,
            list_loopback_cameras=self._list_loopback_cameras,
        )
        camera = Camera(slug, backend)
        try:
            camera.open()
        except Exception as exc:
            logger.info("probe: %s present but unopenable: %s", slug, exc)
            return False
        try:
            usable = self._await_probe_frame(backend)
            if not usable:
                logger.info(
                    "probe: %s opened but delivered no frame within %.1fs -- reporting unusable",
                    slug,
                    self._probe_read_timeout,
                )
        finally:
            try:
                camera.close()
            except Exception:  # pragma: no cover - defensive
                pass
        return usable

    def _await_probe_frame(self, backend: CaptureBackend) -> bool:
        """Read *backend* directly -- bypassing ``Camera.read()``'s
        failure-counting/self-heal, exactly like :meth:`Camera.
        _settle_after_open`'s own direct-backend reads, since this
        ``Camera`` is discarded right after :meth:`_probe_openable`
        returns -- until a real frame arrives or :attr:`_probe_read_timeout`
        elapses, whichever comes first. See :data:`DEFAULT_PROBE_READ_TIMEOUT`
        for the bound and the tradeoff it accepts.
        """
        deadline = time.monotonic() + self._probe_read_timeout
        while True:
            try:
                frame = backend.read()
            except Exception as exc:  # pragma: no cover - defensive, read() should not raise
                logger.warning("probe: read() raised while probing usability: %s", exc)
                frame = None
            if frame is not None:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(_PROBE_READ_POLL_INTERVAL, remaining))

    def _watch_loop(self) -> None:
        while not self._watch_stop.is_set():
            try:
                self._watch_poll_once()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("watch: poll failed: %s", exc)
            self._watch_stop.wait(self._watch_interval)

    def _watch_poll_once(self) -> None:
        identities = self._safe_identities()
        current: Dict[str, CameraIdentity] = {
            identity.hardware_key: identity for identity in identities.values()
        }
        current_keys = frozenset(current.keys())

        with self._lock:
            previous_keys = self._known_hardware_keys

        new_keys = current_keys - previous_keys
        gone_keys = previous_keys - current_keys

        newly_present: Set[str] = set()
        for key in new_keys:
            try:
                newly_present.add(self._registry.resolve(current[key]))
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("watch: failed to register new camera: %s", exc)

        gone_slugs: Set[str] = set()
        if gone_keys:
            by_hardware_key = {r.hardware_key: r.slug for r in self._registry.records()}
            for key in gone_keys:
                slug = by_hardware_key.get(key)
                if slug is not None:
                    gone_slugs.add(slug)

        with self._lock:
            self._known_hardware_keys = current_keys
            self._present_slugs |= newly_present
            self._present_slugs -= gone_slugs
