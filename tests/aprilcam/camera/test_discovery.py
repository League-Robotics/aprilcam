"""Tests for ``aprilcam.camera.discovery`` (ticket 020-003).

Headless tests inject a fake ``identity_source`` and ``backend_factory``
so ``probe()``/``watch()``'s report-shape and non-disturbance logic are
exercised without hardware. ``@pytest.mark.camera`` tests exercise
``probe()`` against this Mac's two real cameras, including the
UC-25.S-7 non-disturbance guarantee against a genuine live read loop.

Every test that calls ``watch()`` stops it in a ``finally`` block —
the watch thread is a daemon thread but leaving it running across tests
would poll needlessly and could race a later test's own fake sources.
"""

from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional

import numpy as np
import pytest

from aprilcam.camera import identity as identity_module
from aprilcam.camera.backends import v4l2_loopback as v4l2_loopback_module
from aprilcam.camera.backends.libcamera import LibcameraDevice
from aprilcam.camera.camera import Camera
from aprilcam.camera.discovery import CameraDiscovery
from aprilcam.camera.identity import CameraIdentity
from aprilcam.camera.registry import CameraRegistry
from aprilcam.types import CameraState, ProbeReport


def _identity(hardware_key: str, device_name: str, **kw) -> CameraIdentity:
    base = dict(
        slug=identity_module.device_name_slug(device_name),
        device_name=device_name,
        hardware_key=hardware_key,
        source="vid_pid_location",
        is_fallback=False,
    )
    base.update(kw)
    return CameraIdentity(**base)


def _held_state(slug: str, *, usable: bool) -> CameraState:
    """A minimal ``CameraState`` for a held-open camera, standing in for
    what ``daemon/service.py`` would hand ``active_camera_state`` (ticket
    039-004): the real ``Camera.state`` of a camera the daemon already
    holds open, healthy or stalled."""
    return CameraState(
        name=slug,
        present=True,
        usable=usable,
        playfield=None,
        calibrated=False,
    )


_FAKE_FRAME = np.zeros((2, 2, 3), dtype=np.uint8)


class _FakeBackend:
    """Minimal CaptureBackend double for discovery's openability probe.

    Delivers a real frame promptly from ``read()`` by default -- since
    sprint 038's ``_probe_openable`` now performs a bounded frame read
    between ``open()`` and ``close()`` (usable means "delivered a
    frame," not merely "opened"), every existing test relying on this
    double reporting ``usable=True`` needs it to actually hand back a
    frame. Pass ``never_reads=True`` to simulate the wedged-session case
    this ticket exists to catch: ``open()`` succeeds but every
    subsequent ``read()`` returns ``None`` forever.
    """

    def __init__(self, *, fail_open: bool = False, never_reads: bool = False) -> None:
        self._is_open = False
        self.fail_open = fail_open
        self.never_reads = never_reads
        self.open_calls = 0
        self.read_calls = 0

    @property
    def is_open(self) -> bool:
        return self._is_open

    def open(self) -> None:
        self.open_calls += 1
        if self.fail_open:
            raise RuntimeError("simulated open failure")
        self._is_open = True

    def read(self) -> Optional[np.ndarray]:
        self.read_calls += 1
        if self.never_reads:
            return None
        return _FAKE_FRAME

    def close(self) -> None:
        self._is_open = False

    def frame_size(self):
        return None


@pytest.fixture
def registry(tmp_path):
    return CameraRegistry(tmp_path / "cameras")


def _names(report: ProbeReport) -> Dict[str, str]:
    return {c.name: c for c in report.cameras}  # type: ignore[misc]


# ---------------------------------------------------------------------------
# probe() -- report shape (AC3, AC6)
# ---------------------------------------------------------------------------


def test_probe_reports_present_usable_cameras_matching_registry_resolution(registry):
    identities = {
        0: _identity("hw:arducam", "Arducam OV9782 USB Camera"),
        1: _identity("hw:hd", "HD USB CAMERA"),
    }
    factories_called: List[int] = []

    def backend_factory(index):
        factories_called.append(index)
        return _FakeBackend()

    discovery = CameraDiscovery(
        registry,
        identity_source=lambda: identities,
        backend_factory=backend_factory,
    )
    report = discovery.probe()

    expected_slugs = {
        registry.resolve(identities[0]),
        registry.resolve(identities[1]),
    }
    assert {c.name for c in report.cameras} == expected_slugs
    assert all(c.present and c.usable for c in report.cameras)
    assert report.skipped == ()  # nothing was actually skipped (UC-25.S-7)
    assert sorted(factories_called) == [0, 1]


def test_probe_reports_registry_record_absent_when_not_currently_enumerated(registry):
    identity = _identity("hw:gone", "Ghost Camera")
    slug = registry.resolve(identity)  # previously seen, persisted
    record = registry.get(slug)

    discovery = CameraDiscovery(
        registry,
        identity_source=lambda: {},  # nothing attached this pass
        backend_factory=lambda index: _FakeBackend(),
    )
    report = discovery.probe()

    assert len(report.cameras) == 1
    state = report.cameras[0]
    assert state.name == slug
    assert state.present is False
    assert state.usable is False
    assert state.number == record.number


def test_probe_reports_present_but_unopenable_camera_never_omitted(registry):
    """AC6 / UC-25.S-4: a backend that raises on open() still yields a
    reported camera, usable=False, present=True, with its derived name."""
    identity = _identity("hw:broken", "Broken Camera")

    discovery = CameraDiscovery(
        registry,
        identity_source=lambda: {0: identity},
        backend_factory=lambda index: _FakeBackend(fail_open=True),
    )
    report = discovery.probe()

    assert len(report.cameras) == 1
    state = report.cameras[0]
    assert state.name == registry.resolve(identity)
    assert state.present is True
    assert state.usable is False
    assert report.skipped == ()  # unopenable is reported, not "skipped"


def test_probe_reports_unusable_when_backend_never_delivers_a_frame(registry):
    """038-001 AC2: open() succeeding is no longer enough -- a backend
    whose capture session is wedged (opens fine, but every read() comes
    back None forever) must probe unusable, not healthy."""
    identity = _identity("hw:wedged", "Wedged Camera")

    discovery = CameraDiscovery(
        registry,
        identity_source=lambda: {0: identity},
        backend_factory=lambda index: _FakeBackend(never_reads=True),
        probe_read_timeout=0.05,
    )
    report = discovery.probe()

    assert len(report.cameras) == 1
    state = report.cameras[0]
    assert state.name == registry.resolve(identity)
    assert state.present is True
    assert state.usable is False
    assert report.skipped == ()  # unusable is reported, not "skipped"


def test_probe_reports_usable_when_backend_delivers_a_frame_promptly(registry):
    """038-001 AC3: a backend whose read() returns a real frame right
    away still reports usable=True, unchanged from before this ticket."""
    identity = _identity("hw:healthy", "Healthy Camera")
    backend = _FakeBackend()

    discovery = CameraDiscovery(
        registry,
        identity_source=lambda: {0: identity},
        backend_factory=lambda index: backend,
    )
    report = discovery.probe()

    assert len(report.cameras) == 1
    assert report.cameras[0].usable is True
    assert backend.read_calls >= 1  # the probe actually read a frame


def test_probe_openable_bounded_read_does_not_hang_the_test_suite(registry):
    """038-001 AC4/testing plan: the read bound is a real, injectable
    timeout, not an unbounded retry loop -- a wedged backend with a
    short injected probe_read_timeout must return promptly rather than
    waiting out DEFAULT_PROBE_READ_TIMEOUT (or hanging forever)."""
    identity = _identity("hw:wedged-timed", "Wedged Timed Camera")

    discovery = CameraDiscovery(
        registry,
        identity_source=lambda: {0: identity},
        backend_factory=lambda index: _FakeBackend(never_reads=True),
        probe_read_timeout=0.05,
    )

    started = time.monotonic()
    report = discovery.probe()
    elapsed = time.monotonic() - started

    assert report.cameras[0].usable is False
    # generous margin above the injected 0.05s bound -- proves the probe
    # did not fall back to DEFAULT_PROBE_READ_TIMEOUT (1.0s) or hang.
    assert elapsed < 0.5


def test_probe_populates_known_present(registry):
    identity = _identity("hw:a", "Cam A")
    discovery = CameraDiscovery(
        registry,
        identity_source=lambda: {0: identity},
        backend_factory=lambda index: _FakeBackend(),
    )
    assert discovery.known_present() == frozenset()
    discovery.probe()
    assert discovery.known_present() == {registry.resolve(identity)}


# ---------------------------------------------------------------------------
# probe() -- non-disturbance via active_camera_state (AC4, mechanism-level;
# see the @pytest.mark.camera test below for the real-read-loop version)
# ---------------------------------------------------------------------------


def test_probe_skips_active_camera_without_touching_its_backend(registry):
    identity = _identity("hw:live", "Live Camera")
    slug = registry.resolve(identity)
    opened: List[int] = []

    def backend_factory(index):
        opened.append(index)
        return _FakeBackend()

    discovery = CameraDiscovery(
        registry,
        identity_source=lambda: {0: identity},
        backend_factory=backend_factory,
        active_camera_state=lambda s: _held_state(s, usable=True) if s == slug else None,
    )
    report = discovery.probe()

    assert opened == []  # backend_factory never invoked for the active camera
    assert len(report.cameras) == 1
    assert report.cameras[0].present is True
    assert report.cameras[0].usable is True
    assert len(report.skipped) == 1
    assert report.skipped[0].backend == slug
    assert report.skipped[0].reason == "in use by a live subscriber"


def test_probe_reports_a_stalled_active_camera_as_unusable(registry):
    """Core regression test (ticket 039-004): ``probe()`` must consult the
    held-open camera's actual health via ``active_camera_state`` instead of
    hardcoding ``usable=True`` for any active slug -- this is the bug the
    incident report (``camera-probe-reports-a-held-open-camera-as-usable-
    without-checking-it.md``) filed: a stalled self-heal-exhausted camera
    still probed ``usable``. Fails against pre-fix code (which returns
    ``usable=True`` unconditionally for an active slug); passes once
    ``probe()`` reads the fake's returned ``CameraState.usable`` instead."""
    identity = _identity("hw:stalled", "Stalled Camera")
    slug = registry.resolve(identity)
    opened: List[int] = []

    def backend_factory(index):
        opened.append(index)
        return _FakeBackend()

    discovery = CameraDiscovery(
        registry,
        identity_source=lambda: {0: identity},
        backend_factory=backend_factory,
        active_camera_state=lambda s: _held_state(s, usable=False) if s == slug else None,
    )
    report = discovery.probe()

    assert opened == []  # still non-disturbing -- no backend touched
    assert len(report.cameras) == 1
    assert report.cameras[0].present is True
    assert report.cameras[0].usable is False
    assert len(report.skipped) == 1
    assert report.skipped[0].backend == slug
    assert report.skipped[0].reason == "in use by a live subscriber (stalled)"


def test_probe_skip_reason_distinguishes_healthy_from_stalled_active_cameras(registry):
    healthy_identity = _identity("hw:healthy", "Healthy Camera")
    stalled_identity = _identity("hw:stalled-2", "Stalled Camera Two")
    healthy_slug = registry.resolve(healthy_identity)
    stalled_slug = registry.resolve(stalled_identity)

    def active_camera_state(slug):
        if slug == healthy_slug:
            return _held_state(slug, usable=True)
        if slug == stalled_slug:
            return _held_state(slug, usable=False)
        return None

    discovery = CameraDiscovery(
        registry,
        identity_source=lambda: {0: healthy_identity, 1: stalled_identity},
        backend_factory=lambda index: _FakeBackend(),
        active_camera_state=active_camera_state,
    )
    report = discovery.probe()

    reasons = {skip.backend: skip.reason for skip in report.skipped}
    assert reasons[healthy_slug] != reasons[stalled_slug]
    assert "stalled" not in reasons[healthy_slug]
    assert "stalled" in reasons[stalled_slug]

    states = {state.name: state for state in report.cameras}
    assert states[healthy_slug].usable is True
    assert states[stalled_slug].usable is False


# ---------------------------------------------------------------------------
# watch() -- passive hotplug (AC5 mechanism; headless, fully automated)
# ---------------------------------------------------------------------------


def test_watch_detects_hotplug_and_unplug_without_probe(registry):
    identity = _identity("hw:hotplug", "Hotplug Camera")
    present: Dict[int, CameraIdentity] = {}  # starts empty -- "unplugged"

    discovery = CameraDiscovery(
        registry,
        identity_source=lambda: dict(present),
        backend_factory=lambda index: _FakeBackend(),
        watch_interval=0.02,
    )
    try:
        discovery.watch()
        time.sleep(0.1)
        assert discovery.known_present() == frozenset()

        present[0] = identity  # simulate plugging the camera in
        time.sleep(0.15)
        slug = registry.resolve(identity)
        assert discovery.known_present() == {slug}

        del present[0]  # simulate unplugging it
        time.sleep(0.15)
        assert discovery.known_present() == frozenset()
    finally:
        discovery.stop_watch()


def test_watch_is_idempotent(registry):
    discovery = CameraDiscovery(
        registry,
        identity_source=lambda: {},
        backend_factory=lambda index: _FakeBackend(),
        watch_interval=0.05,
    )
    try:
        discovery.watch()
        thread_first = discovery._watch_thread
        discovery.watch()
        assert discovery._watch_thread is thread_first
    finally:
        discovery.stop_watch()


def test_stop_watch_joins_the_thread(registry):
    discovery = CameraDiscovery(
        registry,
        identity_source=lambda: {},
        backend_factory=lambda index: _FakeBackend(),
        watch_interval=0.02,
    )
    discovery.watch()
    assert discovery._watch_thread is not None
    discovery.stop_watch()
    assert discovery._watch_thread is None


# ---------------------------------------------------------------------------
# Backend selection -- libcamera-identified cameras (ticket 031-006, SUC-006)
# ---------------------------------------------------------------------------


class _RecordingV4l2Backend:
    """Stands in for the real ``V4l2LoopbackBackend`` so these tests
    never shell out to ``v4l2-ctl`` -- records the device path it was
    constructed with and behaves like an always-openable
    ``CaptureBackend``."""

    def __init__(self, device: str) -> None:
        self.device = device
        self._is_open = False

    @property
    def is_open(self) -> bool:
        return self._is_open

    def open(self) -> None:
        self._is_open = True

    def read(self) -> Optional[np.ndarray]:
        return _FAKE_FRAME

    def close(self) -> None:
        self._is_open = False

    def frame_size(self):
        return None


def _libcamera_loopback_device(
    *, position: int = 0, device_index: int = 70, camera_id: str = "aprilcam-imx296-88000"
) -> LibcameraDevice:
    return LibcameraDevice(
        position=position,
        slug="imx296-88000",
        model="imx296",
        device_index=device_index,
        camera_id=camera_id,
    )


def test_probe_opens_libcamera_identified_camera_through_v4l2loopback_backend(registry, monkeypatch):
    """AC1 (SUC-006): a libcamera-identified camera's probe openability
    check constructs V4l2LoopbackBackend at the real /dev/videoN device
    index, not OpenCVBackend(index) -- even though identity_source's key
    (the enumeration "position") differs from the loopback device_index
    this asserts against (0 vs. 70)."""
    monkeypatch.setattr(v4l2_loopback_module, "V4l2LoopbackBackend", _RecordingV4l2Backend)
    identity = _identity("libcamera:aprilcam-imx296-88000", "imx296 camera")
    device = _libcamera_loopback_device(position=0, device_index=70, camera_id="aprilcam-imx296-88000")
    opencv_calls: List[int] = []

    def backend_factory(index):
        opencv_calls.append(index)
        return _FakeBackend()

    discovery = CameraDiscovery(
        registry,
        identity_source=lambda: {0: identity},
        backend_factory=backend_factory,
        list_loopback_cameras=lambda: [device],
    )
    report = discovery.probe()

    assert opencv_calls == []  # the OpenCVBackend stand-in was never constructed
    assert len(report.cameras) == 1
    assert report.cameras[0].present is True
    assert report.cameras[0].usable is True  # _RecordingV4l2Backend.open() always succeeds


def test_probe_uses_opencv_backend_for_non_libcamera_identity_unchanged(registry, monkeypatch):
    """AC2 (SUC-006): regression -- a non-libcamera identity source is
    unaffected by this ticket; backend_factory (OpenCVBackend in
    production) is still invoked exactly as before, even with a loopback
    device present in the (irrelevant) list_loopback_cameras() result."""
    monkeypatch.setattr(v4l2_loopback_module, "V4l2LoopbackBackend", _RecordingV4l2Backend)
    identity = _identity("hw:arducam", "Arducam OV9782 USB Camera")
    opencv_calls: List[int] = []

    def backend_factory(index):
        opencv_calls.append(index)
        return _FakeBackend()

    discovery = CameraDiscovery(
        registry,
        identity_source=lambda: {0: identity},
        backend_factory=backend_factory,
        list_loopback_cameras=lambda: [_libcamera_loopback_device()],
    )
    report = discovery.probe()

    assert opencv_calls == [0]
    assert report.cameras[0].present is True
    assert report.cameras[0].usable is True


# ---------------------------------------------------------------------------
# @pytest.mark.camera -- real hardware (AC3, AC4)
# ---------------------------------------------------------------------------


def _real_identities() -> Dict[int, CameraIdentity]:
    from aprilcam.camera.identity import resolve_all

    return resolve_all()


@pytest.mark.camera
def test_probe_matches_registry_resolution_for_attached_hardware(registry):
    identities = _real_identities()
    if not identities:
        pytest.skip("no cameras enumerated on this machine")

    discovery = CameraDiscovery(registry, identity_source=lambda: identities)
    report = discovery.probe()

    expected = {registry.resolve(i) for i in identities.values()}
    assert {c.name for c in report.cameras} == expected
    assert all(c.present for c in report.cameras)


@pytest.mark.camera
def test_probe_does_not_disturb_a_live_read_loop():
    """UC-25.S-7: probe() must not interrupt an in-flight read loop.

    Opens a real camera directly (outside discovery), drives a
    background read loop against it, and runs probe() concurrently
    with that camera's slug registered as active -- discovery must
    never touch its backend, so the read loop cannot be disturbed by
    construction, not by luck of OS-level concurrent-open behaviour.
    """
    import tempfile

    from aprilcam.camera.backends.opencv_backend import OpenCVBackend
    from tests.aprilcam.camera.test_camera import _real_camera_index

    identities = _real_identities()
    if not identities:
        pytest.skip("no cameras enumerated on this machine")

    index = _real_camera_index("Arducam OV9782")
    if index is None or index not in identities:
        index, identity = next(iter(identities.items()))
    else:
        identity = identities[index]

    live_camera = Camera(identity.slug, OpenCVBackend(index), device_name=identity.device_name)
    # Enumeration (identities/_real_identities, above) is not availability
    # -- this process may enumerate the device fine and still be unable
    # to open it (e.g. an agent session with no macOS camera TCC grant --
    # docs/knowledge/daemon-camera-grant-and-detection-priming.md). Gate
    # on the real open itself; re-raise any RuntimeError that is not this
    # specific known camera-open failure so a genuine bug still fails
    # loudly.
    try:
        live_camera.open()
    except RuntimeError as exc:
        if "failed to open camera" not in str(exc):
            raise
        pytest.skip(
            f"{identity.slug!r} is attached but could not be opened just "
            f"now -- {exc}. Expected when this process has no camera TCC "
            f"grant (e.g. an agent session) or the device is genuinely busy "
            f"elsewhere; re-run from an operator Terminal session to "
            f"exercise this test for real."
        )

    frame_counts: List[int] = [0]
    errors: List[BaseException] = []
    stop = threading.Event()

    def read_loop():
        while not stop.is_set():
            try:
                frame = live_camera.read()
                if frame is not None:
                    frame_counts[0] += 1
            except BaseException as exc:  # pragma: no cover - defensive
                errors.append(exc)
                break
            time.sleep(0.01)

    reader = threading.Thread(target=read_loop, daemon=True)
    reader.start()
    try:
        time.sleep(0.2)
        count_before = frame_counts[0]
        assert count_before > 0  # the read loop is genuinely producing frames

        registry = CameraRegistry(tempfile.mkdtemp())
        slug = registry.resolve(identity)
        discovery = CameraDiscovery(
            registry,
            identity_source=lambda: {index: identity},
            active_camera_state=lambda s: _held_state(s, usable=True) if s == slug else None,
        )
        report = discovery.probe()

        time.sleep(0.2)
        count_after = frame_counts[0]
    finally:
        stop.set()
        reader.join(timeout=2.0)
        live_camera.close()

    assert not errors
    assert count_after > count_before  # the read loop kept running through probe()
    assert len(report.skipped) == 1
    assert report.skipped[0].backend == slug
    matching = [c for c in report.cameras if c.name == slug]
    assert len(matching) == 1
    assert matching[0].present is True
    assert matching[0].usable is True


# ---------------------------------------------------------------------------
# CameraDiscovery's default backend_factory (sprint 038 ticket 005): must
# agree with daemon/service.py's own default (AC2) -- both now delegate to
# the one shared camera.backends.default_capture_backend_factory rather
# than each hardcoding its own OpenCVBackend(index), so they cannot drift
# apart independently.
# ---------------------------------------------------------------------------


def test_default_backend_factory_delegates_to_the_shared_selection(monkeypatch):
    from aprilcam.camera.backends.opencv_backend import OpenCVBackend
    from aprilcam.camera.backends.worker_backend import WorkerBackend
    from aprilcam.camera.discovery import _default_backend_factory

    monkeypatch.delenv("APRILCAM_CAPTURE_BACKEND", raising=False)

    monkeypatch.setattr("sys.platform", "darwin")
    assert isinstance(_default_backend_factory(0), WorkerBackend)

    monkeypatch.setattr("sys.platform", "linux")
    assert isinstance(_default_backend_factory(0), OpenCVBackend)
