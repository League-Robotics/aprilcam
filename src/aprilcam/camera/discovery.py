"""``CameraDiscovery`` — finding out what cameras exist (UC-25).

Converts the backend-enumeration and failure-diagnosis helpers from
``archive/src/aprilcam/camera/camutil.py`` (``list_cameras``,
``get_device_name``, ``diagnose_camera_failure``) into the shape of the
``CameraDiscovery`` Protocol (``camera/interfaces.py``): ``probe()``
(active, deep, on demand) and ``watch()`` (passive, continuous,
started once). Both build on ``camera/identity.py``'s enumeration
sources and ``camera/registry.py``'s persisted identity -> slug/number
mapping (tickets 002 and this one's own ``camera/camera.py``).

**Non-disturbance (UC-25.S-7).** ``probe()`` never opens a backend for
a camera the daemon is already streaming from. It cannot see the
daemon's live-subscriber table directly -- that table is owned by
``daemon/service.py`` (ticket 004, not yet built) -- so this module
takes an ``active_slugs`` callback at construction: a zero-argument
function returning the set of slugs currently held open. It defaults
to "nothing is active" (an empty set), which is exactly right for a
freshly constructed, subscriber-less ``CameraDiscovery`` (this
ticket's tests), and ticket 004 supplies its real table when it wires
this class into the daemon service. A camera in that set is reported
present/usable from what is already known about it (something is
successfully streaming from it, which is proof enough) and named in
``ProbeReport.skipped``, without any backend ever touching it.

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
from typing import AbstractSet, Callable, Dict, FrozenSet, Optional, Set

from aprilcam.camera.backends import CaptureBackend
from aprilcam.camera.backends.opencv_backend import OpenCVBackend
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


def _default_backend_factory(index: int) -> CaptureBackend:
    return OpenCVBackend(index)


class CameraDiscovery(_CameraDiscoveryBase):
    """Concrete ``camera.interfaces.CameraDiscovery``.

    Derives from ``camera.interfaces.CameraDiscovery`` (a plain base
    class, not ``Protocol`` -- see ``daemon.interfaces``'s module
    docstring for the design rule).

    Parameters
    ----------
    registry:
        Where identities resolve to stable slugs/numbers.
    active_slugs:
        () -> set of slugs the daemon currently holds open. See the
        module docstring's non-disturbance note. Defaults to "always
        empty".
    backend_factory:
        index -> :class:`CaptureBackend`. Overridable so tests can
        inject a fake that raises on ``open()`` (AC6, UC-25.S-4)
        without touching real hardware; defaults to
        :class:`~aprilcam.camera.backends.opencv_backend.OpenCVBackend`.
    identity_source:
        () -> ``{index: CameraIdentity}``. Defaults to
        ``camera.identity.resolve_all`` -- fresh, non-caching, non-
        opening hardware enumeration. Overridable for headless tests.
    watch_interval:
        Seconds between ``watch()`` polls. Overridable so tests need
        not wait :data:`DEFAULT_WATCH_INTERVAL` seconds for a poll.
    """

    def __init__(
        self,
        registry: CameraRegistry,
        *,
        active_slugs: Optional[Callable[[], AbstractSet[str]]] = None,
        backend_factory: Callable[[int], CaptureBackend] = _default_backend_factory,
        identity_source: Callable[[], Dict[int, CameraIdentity]] = resolve_all,
        watch_interval: float = DEFAULT_WATCH_INTERVAL,
    ) -> None:
        self._registry = registry
        self._active_slugs = active_slugs or (lambda: frozenset())
        self._backend_factory = backend_factory
        self._identity_source = identity_source
        self._watch_interval = watch_interval

        self._lock = threading.Lock()
        self._known_hardware_keys: FrozenSet[str] = frozenset()
        self._present_slugs: Set[str] = set()

        self._watch_thread: Optional[threading.Thread] = None
        self._watch_stop = threading.Event()

    # -- CameraDiscovery Protocol -------------------------------------------

    def probe(self) -> ProbeReport:
        """See ``camera.interfaces.CameraDiscovery.probe``.

        For every currently-enumerated identity: resolve its registry
        slug (registering it if new), then -- unless it is in
        ``active_slugs()`` -- briefly open a ``Camera`` over it to
        determine ``usable`` and immediately close it again. A camera
        that raises on open is still reported, ``usable=False``,
        ``present=True`` (UC-25.S-4), never omitted. Registry records
        not seen in this enumeration pass are reported ``present=False``
        (a config outliving its hardware -- ``types.CameraState``'s own
        docstring -- not an error).
        """
        identities = self._safe_identities()
        active = set(self._active_slugs())

        seen_hardware_keys: Dict[str, str] = {}  # hardware_key -> slug
        cameras = []
        skipped = []

        for index, identity in identities.items():
            slug = self._registry.resolve(identity)
            seen_hardware_keys[identity.hardware_key] = slug
            number = self._number_for(slug)

            if slug in active:
                cameras.append(
                    CameraState(
                        name=slug,
                        present=True,
                        usable=True,
                        playfield=None,
                        calibrated=False,
                        number=number,
                    )
                )
                skipped.append(
                    ProbeSkip(backend=slug, reason="in use by a live subscriber")
                )
                continue

            usable = self._probe_openable(slug, index)
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
        poll. Not part of the ``CameraDiscovery`` Protocol -- ticket
        004's daemon service reads this to answer ``list_cameras``
        without re-probing on every call, which is the entire point of
        ``watch()`` existing (UC-25.S-5: hotplug needs no probe).
        """
        with self._lock:
            return frozenset(self._present_slugs)

    def stop_watch(self) -> None:
        """Stop the passive watch thread.

        Not part of the ``CameraDiscovery`` Protocol (which has no
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

    def _probe_openable(self, slug: str, index: int) -> bool:
        """Open-then-close a fresh ``Camera`` over *index* to determine
        usability, per ``camera/discovery.py``'s module docstring
        ("delegates to Camera instances" -- sprint architecture module
        table). Any exception from ``open()`` means unusable, not a
        probe failure -- the camera still gets reported (UC-25.S-4).
        """
        camera = Camera(slug, self._backend_factory(index))
        try:
            camera.open()
        except Exception as exc:
            logger.info("probe: %s present but unopenable: %s", slug, exc)
            return False
        try:
            camera.close()
        except Exception:  # pragma: no cover - defensive
            pass
        return True

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
