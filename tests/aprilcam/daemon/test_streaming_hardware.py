"""Real-hardware verification for sprint 027 (Streams) -- ticket 008.

**Written, correctly marked, and expected to skip cleanly this
sprint -- not silently omitted.** Every test in this module is
``@pytest.mark.camera`` (module-wide, via ``pytestmark``) and skips via
``_skip_if_arducam_not_attached`` on a machine with no camera genuinely
configured and calibrated against ``main-playfield`` (discovered at run
time via ``tests/aprilcam/hardware_camera.py``, never a hardcoded device
name), matching every other ``@pytest.mark.camera`` module in this
project (``test_detection_hardware.py``, ``test_calibration_hardware.py``,
``test_mobile_tags_hardware.py``) -- ``uv run pytest -m "not camera"``
(the sprint's own headless baseline) is unaffected either way.

**Blocked this sprint, by a physical condition, not a code gap.**
sprint.md's "Hardware constraint this sprint" section: the physical
``main-playfield`` is currently obstructed (a cloth draped over part of
it) and the room is dark -- zero tags detect right now, and (in this
particular execution environment) no camera is attached at all. Every
test below is written against the real, unmodified sprint 027 code path
and *will* exercise real hardware the moment both conditions clear
(camera attached, field visible, lit) -- see this ticket's own
completion notes for the exact acceptance criteria this blocks and what
running them later requires.

**What each test verifies, and why it is shaped the way it is:**

* :func:`test_two_real_subscribers_on_deskewed_stream_share_one_warp_per_tick`
  -- SUC-002's UC-07.S-1 claim, against a real camera and a real,
  calibrated ``Deskewer``, exactly as ``test_streaming_integration.py``'s
  headless real-clock counterpart proves it, minus the fixture backend.
  Uses a directly-constructed ``DaemonService`` (not
  ``daemon.__main__.build``) around a real ``OpenCVBackend`` -- the same
  "direct construction for internal-state access" pattern
  ``test_detection_hardware.py``'s occlusion/false-accept tests already
  use, needed here so the test can assert on ``Deskewer.deskew``'s real
  call count via a monkeypatch, not just observed behaviour.
* :func:`test_real_detection_cadence_against_main_playfield` -- UC-26's
  wall-clock-bounded ring buffer claim, against real capture and real
  detection cost (not the trivial 4x4 blank-frame fixture headless tests
  use) -- uses the ``daemon`` fixture (a real ``daemon.__main__.build``
  stack reached over a real gRPC channel, the same assembly a real
  ``python -m aprilcam.daemon`` process wires up) and only the public
  ``stream_tags``/``get_tag_history`` API, since this claim needs no
  internal state.
* :func:`test_viewer_visibly_beats_default_poll_hz_on_real_hardware` --
  ticket 007's "beats 2 Hz" success criterion, against a real Tk window
  and a real camera together -- both real-hardware preconditions this
  sandbox lacks (no display *and* no camera), so this is the one test in
  this module with two independent skip conditions.
* :func:`test_daemon_idle_cpu_budget_with_camera_open_and_zero_stream_subscribers`
  -- development-plan.md Stage 8's CPU-idle-budget exit criterion,
  extended to this sprint's new streaming subsystem: a camera held open
  with zero ``stream_images``/``stream_tags`` subscribers ever created
  must do no background capture/detection/encode work. Follows
  ``test_detection_hardware.py``'s
  ``test_idle_daemon_runs_zero_detections_over_ten_seconds`` precedent
  exactly (same reasoning, restated there, for why real CPU-seconds
  measured in-process alongside pytest's own gRPC threads is recorded as
  informational only, never a hard assertion) -- the ring buffer's
  zero-growth over a real, unsubscribed window is the deterministic
  proxy.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path
from typing import Dict, Tuple

import grpc
import pytest

from aprilcam.calibration.deskew import Deskewer
from aprilcam.camera.backends.opencv_backend import OpenCVBackend
from aprilcam.camera.backends.uvc_util import UvcUtilBackend
from aprilcam.camera.camera import Camera
from aprilcam.camera.discovery import CameraDiscovery
from aprilcam.camera.identity import resolve_all
from aprilcam.camera.registry import CameraRegistry
from aprilcam.client.daemon_client import GrpcDaemon
from aprilcam.config.directories import Directories
from aprilcam.daemon import __main__ as daemon_main
from aprilcam.daemon.service import DaemonService
from aprilcam.proto import aprilcam_v2_pb2_grpc
from aprilcam.types import ImageFrame
from aprilcam.view import viewer
from tests.aprilcam.calibration.test_calibration import main_playfield_definition
from tests.aprilcam.daemon.conftest import calibration_diagnosis, hardware_test_camera_config
from tests.aprilcam.hardware_camera import resolve_hardware_camera_slug

pytestmark = pytest.mark.camera

#: Resolved lazily, once per test, by :func:`_skip_if_arducam_not_attached`
#: against this machine's real, persisted system state (see
#: tests/aprilcam/hardware_camera.py) -- never hardcoded.
ARDUCAM_SLUG: str = ""


def _skip_if_arducam_not_attached() -> None:
    global ARDUCAM_SLUG
    ARDUCAM_SLUG = resolve_hardware_camera_slug()


def _short_tmp_dir(prefix: str) -> Path:
    """A short, top-level ``/tmp`` directory -- unix domain sockets have
    a ~103-character path limit, and pytest's ``tmp_path`` nests too
    deeply (matches every other ``@pytest.mark.camera`` module's
    identically-named workaround)."""
    return Path(tempfile.mkdtemp(prefix=prefix, dir="/tmp"))


# ---------------------------------------------------------------------------
# Direct DaemonService construction around a real OpenCVBackend -- for
# tests that need internal-state assertions (producer/warp call counts),
# not reachable through the public Daemon interface alone. Mirrors
# tests.aprilcam.daemon.test_service._service_with_camera exactly, with a
# real backend in place of FakeBackend.
# ---------------------------------------------------------------------------


def _real_arducam_service(tmp_path: Path) -> Tuple[DaemonService, str, Camera]:
    identities = resolve_all()
    index = next((idx for idx, ident in identities.items() if ident.slug == ARDUCAM_SLUG), None)
    assert index is not None  # guaranteed by _skip_if_arducam_not_attached
    identity = identities[index]

    registry = CameraRegistry(tmp_path / "state" / "cameras")
    slug = registry.resolve(identity)
    number = registry.get(slug).number
    # ticket 031-009: a real control_backend, matching
    # DaemonService._camera_for's own production wiring exactly --
    # without it, _calibrate_against_main_playfield's exposure push
    # below would raise UnsupportedSetting on this exact hardware
    # (camera.camera module docstring, ticket 023-001: cv2.VideoCapture.
    # set() does not work for exposure/gain/etc on this camera's
    # AVFoundation capture session).
    cam = Camera(slug, OpenCVBackend(index), control_backend=UvcUtilBackend(identity.device_name), number=number)
    cam.open()

    cameras: Dict[str, Camera] = {slug: cam}
    discovery = CameraDiscovery(
        registry,
        active_slugs=lambda: frozenset(cameras.keys()),
        identity_source=lambda: {index: identity},
        backend_factory=lambda idx: OpenCVBackend(idx),
    )
    directories = Directories(config_dir=tmp_path / "config", state_dir=tmp_path / "state")
    service = DaemonService(
        registry=registry,
        discovery=discovery,
        directories=directories,
        cameras=cameras,
        identity_source=lambda: {index: identity},
    )
    return service, slug, cam


def _calibrate_against_main_playfield(service: DaemonService, slug: str, camera: Camera) -> None:
    """Calibrate *slug* against the real, physical ``main-playfield``.

    *camera* is *slug*'s already-open ``Camera`` (from
    :func:`_real_arducam_service`) -- passed through to
    :func:`hardware_test_camera_config` so its exposure sweep drives this
    already-open handle directly instead of opening a second, conflicting
    one on the same device index (``tests/aprilcam/daemon/conftest.py``'s
    "avoiding a double-open" section).

    Skips cleanly (rather than failing) when calibration itself does not
    succeed -- sprint.md's documented hardware constraint ("the physical
    playfield is currently obstructed... and the room is dark") means
    this is an *expected* outcome this sprint, not a code defect. A
    clean, informative skip is this ticket's own preferred outcome over
    a hard failure for exactly this reason (ticket 008: "skip cleanly
    (or fail with a clear, non-misleading message)") -- once the field
    is genuinely clear this same call will succeed and every test built
    on top of it starts running for real, with no code change needed.
    """
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(
        hardware_test_camera_config(slug, playfield="main-playfield", camera=camera)
    )
    result = service.calibrate(slug)
    if not result.succeeded:
        pytest.skip(
            f"real-hardware calibration against main-playfield did not succeed -- "
            f"{calibration_diagnosis(result, slug)} -- if the field is genuinely "
            f"obstructed or dark (sprint.md's documented hardware constraint), "
            f"clear it and re-run"
        )


# ---------------------------------------------------------------------------
# The daemon fixture -- a real daemon.__main__.build() stack reached over
# a real gRPC channel, matching test_detection_hardware.py's own fixture
# exactly (fresh per test function, tmp_path-isolated).
# ---------------------------------------------------------------------------


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    _skip_if_arducam_not_attached()
    short_dir = _short_tmp_dir("ac-008-")
    try:
        # daemon.__main__.build() always binds sprint 027's stream-transport
        # listeners under directories.socket_dir (independent of the
        # lock_path/socket_path overrides below, which only relocate the
        # gRPC control-plane socket) -- unix socket paths have a
        # ~103-character OS limit, routinely exceeded by the real default
        # (e.g. macOS's deep /var/folders/.../T/aprilcam-<uid>/ path), so
        # this test module's use of stream_images/stream_tags needs
        # XDG_RUNTIME_DIR relocated too, matching every other
        # @pytest.mark.camera module's identical fix for the control
        # socket (test_detection_hardware.py's cli_env fixture).
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(short_dir))
        directories = Directories(config_dir=tmp_path / "config", state_dir=tmp_path / "state")
        lock_path = short_dir / "daemon.lock"
        socket_path = short_dir / "control.sock"
        lifecycle = daemon_main.build(directories, lock_path=lock_path, socket_path=socket_path)
        lifecycle.start()
        try:
            channel = grpc.insecure_channel(f"unix:{socket_path}")
            grpc.channel_ready_future(channel).result(timeout=5)
            stub = aprilcam_v2_pb2_grpc.AprilCamV2Stub(channel)
            yield GrpcDaemon(stub, target="localhost")
        finally:
            lifecycle.stop()
    finally:
        shutil.rmtree(short_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# AC: two real subscribers on the same deskewed image stream receive
# identical frames with the warp routine called once per tick, not twice
# (UC-07.S-1, against actual hardware)
# ---------------------------------------------------------------------------


def test_two_real_subscribers_on_deskewed_stream_share_one_warp_per_tick(tmp_path, monkeypatch):
    _skip_if_arducam_not_attached()
    service, slug, camera = _real_arducam_service(tmp_path)
    try:
        _calibrate_against_main_playfield(service, slug, camera)

        calls = {"n": 0}
        original_deskew = Deskewer.deskew

        def _counting_deskew(self, frame):  # noqa: ANN001, ANN202
            calls["n"] += 1
            return original_deskew(self, frame)

        monkeypatch.setattr(Deskewer, "deskew", _counting_deskew)

        stream1 = service.stream_images(slug, deskewed=True)
        stream2 = service.stream_images(slug, deskewed=True)
        try:
            received1: Dict[float, ImageFrame] = {}
            received2: Dict[float, ImageFrame] = {}
            it1 = iter(stream1)
            it2 = iter(stream2)
            deadline = time.monotonic() + 15.0
            while (len(received1) < 5 or len(received2) < 5) and time.monotonic() < deadline:
                if len(received1) < 5:
                    f = next(it1)
                    received1[f.timestamp] = f
                if len(received2) < 5:
                    f = next(it2)
                    received2[f.timestamp] = f

            assert len(received1) >= 3 and len(received2) >= 3, (
                "did not collect enough real deskewed frames within the bounded "
                "window -- check the arducam is attached and streaming"
            )
            common = set(received1) & set(received2)
            assert len(common) >= 2, f"expected overlap between two real subscribers, got {len(common)}"
            for ts in common:
                a, b = received1[ts], received2[ts]
                assert a.data == b.data
                assert a.px_per_cm == b.px_per_cm
                assert a.world_extent == b.world_extent

            distinct_ticks = len(set(received1) | set(received2))
            assert calls["n"] >= distinct_ticks
            assert calls["n"] <= distinct_ticks + 3, (
                f"warp called {calls['n']} times for {distinct_ticks} distinct real "
                f"ticks -- looks like more than one warp per tick on real hardware"
            )
        finally:
            stream1.close()
            stream2.close()
    finally:
        service.close()


# ---------------------------------------------------------------------------
# AC: the tag-history ring buffer fills at the configured detection
# cadence against real capture + real detection cost (UC-26), not the
# trivial synthetic fixture headless tests use
# ---------------------------------------------------------------------------


def test_real_detection_cadence_against_main_playfield(daemon):
    baseline = daemon.get_tag_history(ARDUCAM_SLUG)

    stream = daemon.stream_tags(ARDUCAM_SLUG)
    it = iter(stream)
    try:
        next(it)  # confirm the subscription is really flowing
        start = time.monotonic()
        time.sleep(2.0)  # a real, measured wall-clock window
        elapsed = time.monotonic() - start
    finally:
        stream.close()

    history = daemon.get_tag_history(ARDUCAM_SLUG)[len(baseline):]
    assert len(history) > 1, "ring buffer did not grow against real hardware while a tag-stream subscriber was open"

    span = history[-1].timestamp - history[0].timestamp
    print(
        f"\n[ticket 008 real-hardware data] {len(history)} ring-buffer entries "
        f"over a real {elapsed:.2f}s window (span={span:.2f}s) -- real detection "
        f"cadence against main-playfield"
    )
    # Generous bands: real capture + real AprilTag/ArUco detection cost is
    # unknown and untuned this sprint (sprint.md Open Question 1) -- this
    # is exactly the measurement that tuning will later be based on, not
    # a pre-judged tight assertion.
    assert span > elapsed * 0.2, (
        f"ring buffer span {span:.2f}s is far shorter than the measured "
        f"{elapsed:.2f}s real window -- detection may be stalling on real hardware"
    )
    assert span < elapsed * 1.5


# ---------------------------------------------------------------------------
# AC: the viewer visibly beats 2 Hz on a real camera (ticket 007's
# success criterion, against actual hardware and a real Tk window)
# ---------------------------------------------------------------------------


class _TimedTkWindow(viewer.TkWindow):
    """Wraps the real :class:`viewer.TkWindow`, closing itself once
    *duration_s* of real wall-clock time have elapsed and counting real
    ``show()`` calls -- the real-window counterpart of
    ``test_viewer.py``'s ``_TimedFakeWindow``, used only here because
    this test's whole point is real-hardware, real-toolkit throughput."""

    def __init__(self, title: str, *, duration_s: float) -> None:
        super().__init__(title)
        self._duration_s = duration_s
        self._start = time.monotonic()
        self.shown_count = 0

    def show(self, data: bytes) -> None:
        super().show(data)
        self.shown_count += 1

    def pump(self) -> bool:
        if time.monotonic() - self._start >= self._duration_s:
            self.close()
            return False
        return super().pump()

    def elapsed(self) -> float:
        return time.monotonic() - self._start


def test_viewer_visibly_beats_default_poll_hz_on_real_hardware(monkeypatch):
    _skip_if_arducam_not_attached()
    short_dir = _short_tmp_dir("ac-008-view-")
    try:
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(short_dir))  # see the daemon fixture's comment above
        directories = Directories(config_dir=short_dir / "config", state_dir=short_dir / "state")
        lock_path = short_dir / "daemon.lock"
        socket_path = short_dir / "control.sock"
        lifecycle = daemon_main.build(directories, lock_path=lock_path, socket_path=socket_path)
        lifecycle.start()
        try:
            channel = grpc.insecure_channel(f"unix:{socket_path}")
            grpc.channel_ready_future(channel).result(timeout=5)
            stub = aprilcam_v2_pb2_grpc.AprilCamV2Stub(channel)
            daemon = GrpcDaemon(stub, target="localhost")

            window_holder: Dict[str, _TimedTkWindow] = {}

            def _factory(title: str) -> _TimedTkWindow:
                # A generous window: _start begins ticking at window
                # construction, *before* daemon.stream_images's RPC +
                # real-socket connect + first real camera open/capture --
                # opening a real USB camera device for the first time can
                # itself take a couple of real seconds, which must not
                # starve the measurement of any time to actually observe
                # streamed frames once capture is flowing.
                window = _TimedTkWindow(title, duration_s=8.0)
                window_holder["window"] = window
                return window

            try:
                viewer.run_raw_view(ARDUCAM_SLUG, daemon, window_factory=_factory)
            except viewer.NoDisplayAvailable as exc:
                pytest.skip(f"no real display available on this machine: {exc}")

            window = window_holder["window"]
            elapsed = window.elapsed()
            assert elapsed > 0.0
            throughput_hz = window.shown_count / elapsed
            print(
                f"\n[ticket 008 real-hardware data] viewer real throughput: "
                f"{window.shown_count} frames / {elapsed:.2f}s = {throughput_hz:.2f} Hz "
                f"(DEFAULT_POLL_HZ={viewer.DEFAULT_POLL_HZ})"
            )
            assert throughput_hz > viewer.DEFAULT_POLL_HZ, (
                f"real viewer throughput {throughput_hz:.2f} Hz did not beat "
                f"DEFAULT_POLL_HZ={viewer.DEFAULT_POLL_HZ} on real hardware"
            )
        finally:
            lifecycle.stop()
    finally:
        shutil.rmtree(short_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# AC: development-plan.md Stage 8's CPU-idle-budget exit criterion --
# camera open, zero stream subscribers, near-idle CPU
# ---------------------------------------------------------------------------


def test_daemon_idle_cpu_budget_with_camera_open_and_zero_stream_subscribers(daemon):
    """See the module docstring's explanation of why the ring buffer's
    zero-growth is this test's hard assertion and real CPU-seconds is
    recorded as informational only -- identical reasoning to
    ``test_detection_hardware.py``'s
    ``test_idle_daemon_runs_zero_detections_over_ten_seconds``, applied
    here to a window with zero ``stream_images``/``stream_tags`` calls
    in addition to zero one-shot ``get_tags``/``get_tag``/
    ``get_tag_history`` calls -- this module never calls either
    streaming method during the idle window, so sprint 027's own
    idle-gating design (no producer thread exists without a subscriber)
    makes "no stream producer ever ran" true by construction; what this
    test measures is whether that structural guarantee also holds up as
    *zero observable work* on real hardware.
    """
    daemon.get_tags(ARDUCAM_SLUG)  # warm up: opens + caches the camera, one detection burst
    history_before = daemon.get_tag_history(ARDUCAM_SLUG)
    cpu_before = time.process_time()

    time.sleep(10.0)  # a real 10s idle window -- zero stream subscribers, zero further RPCs

    cpu_after = time.process_time()
    history_after = daemon.get_tag_history(ARDUCAM_SLUG)

    assert len(history_after) == len(history_before), (
        f"the ring buffer grew from {len(history_before)} to {len(history_after)} "
        "entries during a 10s window with zero stream subscribers and zero "
        "get_tags/get_tag/get_tag_history calls in flight -- something ran a "
        "detection burst on its own"
    )

    cpu_delta = cpu_after - cpu_before
    print(
        f"\n[ticket 008 real-hardware data, informational only -- see docstring] "
        f"in-process pytest+daemon combined CPU over a real 10s wall-clock idle "
        f"window with zero stream subscribers: {cpu_delta:.3f}s"
    )
