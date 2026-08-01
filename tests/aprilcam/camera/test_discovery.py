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
from aprilcam.camera.camera import Camera
from aprilcam.camera.discovery import CameraDiscovery
from aprilcam.camera.identity import CameraIdentity
from aprilcam.camera.registry import CameraRegistry
from aprilcam.types import ProbeReport


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


class _FakeBackend:
    """Minimal CaptureBackend double for discovery's openability probe."""

    def __init__(self, *, fail_open: bool = False) -> None:
        self._is_open = False
        self.fail_open = fail_open
        self.open_calls = 0

    @property
    def is_open(self) -> bool:
        return self._is_open

    def open(self) -> None:
        self.open_calls += 1
        if self.fail_open:
            raise RuntimeError("simulated open failure")
        self._is_open = True

    def read(self) -> Optional[np.ndarray]:
        return None

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
# probe() -- non-disturbance via active_slugs (AC4, mechanism-level;
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
        active_slugs=lambda: {slug},
    )
    report = discovery.probe()

    assert opened == []  # backend_factory never invoked for the active camera
    assert len(report.cameras) == 1
    assert report.cameras[0].present is True
    assert report.cameras[0].usable is True
    assert len(report.skipped) == 1
    assert report.skipped[0].backend == slug


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
    live_camera.open()

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
            active_slugs=lambda: {slug},
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
