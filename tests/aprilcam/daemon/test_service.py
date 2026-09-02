"""Tests for ``aprilcam.daemon.service`` and ``aprilcam.daemon.rpc``
(ticket 020-004; ``get_tags``/``get_tag``/``get_tag_history`` and the
``get_frame`` tag-embed added by ticket 021-004).

Fully headless: a :class:`FakeBackend` (reused from
``tests.aprilcam.camera.test_camera``, matching ``test_discovery.py``'s
established cross-module-fixture convention) stands in for real camera
hardware. The RPC round-trip tests bind a real ``grpc.Server`` to a
``tmp_path`` unix socket and drive it with a real generated stub, so
they exercise genuine (de)serialization through ``daemon/rpc.py`` and
the compiled ``proto/aprilcam_v2.proto`` messages, not just
``DaemonService``'s Python-level method bodies.

The tag-detection tests below use a substitutable ``TagDetector``-shaped
double (``_CountingDetector``/``_ScriptedDetector``), injected via
``DetectionEngine(detector_factory=...)`` and ``DaemonService(...,
detection=...)`` -- the CPU-budget "exactly one burst per call" pins and
the explicit-gap history test need an exact, controllable call count and
tag sequence, not real pixel detection (``daemon/detection.py``'s own
tests, ``test_detection.py``, cover real end-to-end synthetic-image
detection instead).
"""

from __future__ import annotations

import json
import logging
import math
import re
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List, Optional

import cv2 as cv
import grpc
import numpy as np
import pytest

from aprilcam.calibration.calibration import Calibration, Calibrator
from aprilcam.calibration.geometry import apply_mount_offset
from aprilcam.calibration.interfaces import FlatFieldInfo, FlatFieldResult
from aprilcam.camera import identity as identity_module
from aprilcam.camera.camera import FAILURE_THRESHOLD, Camera
from aprilcam.camera.discovery import CameraDiscovery
from aprilcam.camera.identity import CameraIdentity
from aprilcam.camera.registry import CameraRegistry
from aprilcam.client.guides import Guides
from aprilcam.config.directories import Directories
from aprilcam.config.interfaces import CameraConfig, CameraPosition
from aprilcam.config.loader import ConfigLoader, PlayfieldDefinition, PlayfieldFeatureEntry
from aprilcam.daemon import __main__ as daemon_main
from aprilcam.daemon.detection import CONFIRMATION_STREAK, DetectionEngine
from aprilcam.daemon.interfaces import Daemon, MountRegistration
from aprilcam.daemon.lifecycle import DaemonLifecycle
from aprilcam.daemon.rpc import build_server
from aprilcam.daemon.service import (
    CALIBRATION_FRAME_COUNT,
    FLATFIELD_FRAME_COUNT,
    VALID_ENCODINGS,
    _MAX_ANNOTATIONS_PER_LAYER,
    _MAX_POINTS_PER_SHAPE,
    DaemonService,
)
from aprilcam.errors import (
    AnnotationLimitExceeded,
    CameraNotPresent,
    MalformedDocument,
    NoFieldMask,
    NoPlayfieldLinked,
    NotCalibrated,
    UnknownCamera,
    UnknownGuide,
    UnknownPlayfield,
    UnsupportedSetting,
)
from aprilcam.guides import AVAILABLE_GUIDES
from aprilcam.proto import aprilcam_v2_pb2, aprilcam_v2_pb2_grpc
from aprilcam.types import (
    Annotation,
    Circle,
    ImageFrame,
    Marker,
    MountParameters,
    PathShape,
    PixelPoint,
    Polygon,
    Style,
    TagFamily,
    TagFrame,
    TagId,
    TagRecord,
    Text,
    WorldPoint,
)
from aprilcam.vision.detector import Detection
from tests.aprilcam.calibration.test_calibration import (
    MAIN_PLAYFIELD_ARUCO_POSITIONS,
    main_playfield_definition,
    render_playfield_frame,
    render_projective_frame,
)
from tests.aprilcam.camera.test_camera import FakeBackend
from tests.aprilcam.daemon.conftest import hardware_test_camera_config


def _playfield_definition(
    name: str = "main-playfield", aruco_ids: tuple = (1, 2, 3)
) -> PlayfieldDefinition:
    """A minimal but real ``PlayfieldDefinition`` -- enough for the
    playfield/camera-config service tests below (ticket 022-001), which
    care about the daemon's storage/dispatch behaviour, not document
    parsing (``tests/aprilcam/config/test_loader.py`` covers that)."""
    return PlayfieldDefinition(
        name=name,
        width_cm=100.0,
        height_cm=80.0,
        aruco_tags=tuple(
            PlayfieldFeatureEntry(slug=f"u{i}", x=float(i), y=float(-i), tag_id=i)
            for i in aruco_ids
        ),
    )


def _frame() -> np.ndarray:
    return np.zeros((4, 4, 3), dtype=np.uint8)


def _identity(hardware_key: str, device_name: str) -> CameraIdentity:
    return CameraIdentity(
        slug=identity_module.device_name_slug(device_name),
        device_name=device_name,
        hardware_key=hardware_key,
        source="vid_pid_location",
        is_fallback=False,
    )


@pytest.fixture
def directories(tmp_path: Path) -> Directories:
    return Directories(config_dir=tmp_path / "config", state_dir=tmp_path / "state")


@pytest.fixture
def registry(tmp_path: Path) -> CameraRegistry:
    return CameraRegistry(tmp_path / "state" / "cameras")


@pytest.fixture
def held_camera(registry: CameraRegistry):
    """A registered camera already held open (as ``get_frame`` leaves
    one) with two frames queued -- the "fake in-process camera fixture"
    the ticket's Testing section asks for."""
    identity = _identity("hw:fake", "Fake Camera")
    slug = registry.resolve(identity)
    number = registry.get(slug).number
    cam = Camera(slug, FakeBackend(read_queue=[_frame(), _frame(), _frame()]), number=number)
    cam.open()
    return identity, slug, cam


@pytest.fixture
def service(directories, registry, held_camera):
    identity, slug, cam = held_camera
    cameras: Dict[str, Camera] = {slug: cam}
    discovery = CameraDiscovery(
        registry,
        active_slugs=lambda: frozenset(cameras.keys()),
        identity_source=lambda: {0: identity},
        backend_factory=lambda index: FakeBackend(),
    )
    return DaemonService(
        registry=registry,
        discovery=discovery,
        directories=directories,
        cameras=cameras,
        # Headless-safe: without this, DaemonService's own
        # identity_source defaults to real hardware enumeration
        # (aprilcam.camera.identity.resolve_all). Only the held camera
        # is "present" as far as _camera_for's _index_for lookup is
        # concerned -- anything else registered but not in this dict
        # is a registered-but-absent camera (CameraNotPresent tests
        # below).
        identity_source=lambda: {0: identity},
    )


@pytest.fixture
def absent_camera(registry: CameraRegistry):
    """A camera the registry remembers (assigned a slug and a number)
    but that ``service``'s identity source does not currently
    enumerate -- simulates an unplugged camera (UC-25), never held open
    in ``service``'s ``cameras`` cache."""
    identity = _identity("hw:absent", "Absent Camera")
    slug = registry.resolve(identity)
    number = registry.get(slug).number
    return slug, number


# ---------------------------------------------------------------------------
# DaemonService -- direct method tests
# ---------------------------------------------------------------------------


def test_list_cameras_reports_the_held_open_camera(service, held_camera):
    _, slug, _ = held_camera

    states = service.list_cameras()

    assert len(states) == 1
    assert states[0].name == slug
    assert states[0].present is True
    assert states[0].usable is True


def test_active_slugs_reflects_the_held_camera_cache(service, held_camera):
    _, slug, _ = held_camera
    assert service.active_slugs() == {slug}


def test_probe_cameras_skips_the_held_open_camera(service, held_camera):
    """UC-25.S-7, wired for real: probe_cameras() must not touch a
    camera get_frame already holds open."""
    _, slug, _ = held_camera

    report = service.probe_cameras()

    assert len(report.skipped) == 1
    assert report.skipped[0].backend == slug


def test_get_frame_jpeg_default(service, held_camera):
    _, slug, _ = held_camera

    frame = service.get_frame(slug)

    assert frame.encoding == "jpeg"
    assert frame.width == 4 and frame.height == 4
    assert frame.data[:2] == b"\xff\xd8"  # JPEG SOI marker
    assert frame.deskewed is False


def test_get_frame_png_lossless(service, held_camera):
    _, slug, _ = held_camera

    frame = service.get_frame(slug, encoding="png")

    assert frame.encoding == "png"
    assert frame.data[:8] == b"\x89PNG\r\n\x1a\n"  # PNG signature


def test_get_frame_unknown_camera_raises_unknown_camera(service):
    with pytest.raises(UnknownCamera) as excinfo:
        service.get_frame("no-such-camera")
    assert excinfo.value.requested == "no-such-camera"


# ---------------------------------------------------------------------------
# Camera addressing by persistent number (ticket 021-002, SUC-005)
# ---------------------------------------------------------------------------


def test_get_frame_by_number_and_by_slug_return_frames_from_the_same_camera(
    service, held_camera, registry
):
    _, slug, _ = held_camera
    number = registry.get(slug).number

    by_slug = service.get_frame(slug)
    by_number = service.get_frame(str(number))

    assert by_slug.data == by_number.data
    assert by_slug.width == by_number.width and by_slug.height == by_number.height


def test_get_frame_unknown_number_raises_unknown_camera_with_number_slug_available(
    service, held_camera, registry
):
    _, slug, _ = held_camera
    number = registry.get(slug).number

    with pytest.raises(UnknownCamera) as excinfo:
        service.get_frame("99")

    assert excinfo.value.requested == "99"
    assert excinfo.value.available == (f"{number} {slug}",)


def test_get_frame_absent_registered_camera_by_slug_raises_camera_not_present(
    service, absent_camera
):
    slug, _number = absent_camera

    with pytest.raises(CameraNotPresent) as excinfo:
        service.get_frame(slug)

    assert excinfo.value.slug == slug


def test_get_frame_absent_registered_camera_by_number_raises_camera_not_present(
    service, absent_camera
):
    slug, number = absent_camera

    with pytest.raises(CameraNotPresent) as excinfo:
        service.get_frame(str(number))

    assert excinfo.value.slug == slug


def _service_for_backend_selection(
    directories, registry, *, identity, index, backend_factory, list_loopback_cameras=None
):
    """A fresh, standalone ``DaemonService`` (not the shared ``service``
    fixture -- that one's camera is already held open, bypassing
    ``_camera_for``'s backend construction entirely) whose only
    enumerated camera is *identity* at *index*, for exercising the
    libcamera backend-selection branch (ticket 031-006, SUC-006)."""
    discovery = CameraDiscovery(
        registry, identity_source=lambda: {}, backend_factory=lambda i: FakeBackend()
    )
    return DaemonService(
        registry=registry,
        discovery=discovery,
        directories=directories,
        identity_source=lambda: {index: identity},
        backend_factory=backend_factory,
        control_backend_factory=lambda device_name: None,
        list_loopback_cameras=list_loopback_cameras,
    )


class _RecordingV4l2Backend:
    """Stands in for the real ``V4l2LoopbackBackend`` so this test never
    shells out to ``v4l2-ctl`` -- records the device path it was
    constructed with and returns a real frame on ``read()``."""

    def __init__(self, device: str) -> None:
        self.device = device
        self._is_open = False

    @property
    def is_open(self) -> bool:
        return self._is_open

    def open(self) -> None:
        self._is_open = True

    def read(self):
        return _frame()

    def close(self) -> None:
        self._is_open = False

    def frame_size(self):
        return (4, 4)


def test_get_frame_opens_libcamera_identified_camera_through_v4l2loopback_backend(
    directories, registry, monkeypatch
):
    """AC1 (SUC-006): DaemonService._camera_for constructs
    V4l2LoopbackBackend at the real /dev/videoN device index for a
    libcamera-identified camera, not OpenCVBackend(index) -- the second
    of this ticket's two call sites (``camera/discovery.py`` is the
    first, covered in ``tests/aprilcam/camera/test_discovery.py``).
    Deliberately uses index=0 (the enumeration "position") while the
    loopback device's real device_index is 70, to prove the two are not
    conflated."""
    from aprilcam.camera.backends import v4l2_loopback as v4l2_loopback_module
    from aprilcam.camera.backends.libcamera import LibcameraDevice

    monkeypatch.setattr(v4l2_loopback_module, "V4l2LoopbackBackend", _RecordingV4l2Backend)

    identity = _identity("libcamera:aprilcam-imx296-88000", "imx296 camera")
    slug = registry.resolve(identity)
    device = LibcameraDevice(
        position=0,
        slug="imx296-88000",
        model="imx296",
        device_index=70,
        camera_id="aprilcam-imx296-88000",
    )
    opencv_calls: List[int] = []

    def backend_factory(index):
        opencv_calls.append(index)
        return FakeBackend()

    service = _service_for_backend_selection(
        directories,
        registry,
        identity=identity,
        index=0,
        backend_factory=backend_factory,
        list_loopback_cameras=lambda: [device],
    )

    frame = service.get_frame(slug)

    assert opencv_calls == []  # the OpenCVBackend stand-in was never constructed
    cam = service._cameras[slug]
    assert isinstance(cam._backend, _RecordingV4l2Backend)
    assert cam._backend.device == "/dev/video70"
    assert frame.width == 4 and frame.height == 4


def test_get_frame_uses_opencv_backend_for_non_libcamera_identity_unchanged(
    directories, registry
):
    """AC2 (SUC-006): regression -- a non-libcamera identity source is
    unaffected by this ticket; backend_factory (OpenCVBackend in
    production) is invoked exactly as before."""
    identity = _identity("hw:arducam", "Arducam OV9782 USB Camera")
    slug = registry.resolve(identity)
    opencv_calls: List[int] = []

    def backend_factory(index):
        opencv_calls.append(index)
        return FakeBackend(read_queue=[_frame()])

    service = _service_for_backend_selection(
        directories, registry, identity=identity, index=0, backend_factory=backend_factory
    )

    frame = service.get_frame(slug)

    assert opencv_calls == [0]
    cam = service._cameras[slug]
    assert not isinstance(cam._backend, _RecordingV4l2Backend)
    assert frame.width == 4 and frame.height == 4


def test_get_frame_deskewed_raises_not_calibrated(service, held_camera):
    _, slug, _ = held_camera
    with pytest.raises(NotCalibrated):
        service.get_frame(slug, deskewed=True)


def test_get_frame_unsupported_encoding_raises_value_error(service, held_camera):
    _, slug, _ = held_camera
    with pytest.raises(ValueError):
        service.get_frame(slug, encoding="bmp")


def test_status_reports_running_cameras_and_directories(service, directories, held_camera):
    _, slug, _ = held_camera

    status = service.status()

    assert status.running is True
    assert [c.name for c in status.cameras] == [slug]
    assert status.config_dir == str(directories.config_dir)
    assert status.state_dir == str(directories.state_dir)


def test_close_closes_every_held_camera(service, held_camera):
    _, _, cam = held_camera

    service.close()

    assert cam.state.usable is False
    assert service.active_slugs() == frozenset()


# ---------------------------------------------------------------------------
# stream_tags (ticket 027-003, UC-11/SUC-001) -- in-process only this
# ticket: no socket/gRPC endpoint, just DaemonService.stream_tags wired
# to daemon.streaming.TagStreamProducer/BroadcasterTagStream.
# ---------------------------------------------------------------------------


def test_stream_tags_returns_a_tag_stream_that_publishes_real_frames(service, held_camera):
    _, slug, _ = held_camera

    stream = service.stream_tags(slug)
    try:
        frame = next(iter(stream))
        assert isinstance(frame, TagFrame)
    finally:
        stream.close()


def test_stream_tags_reuses_the_same_producer_across_repeated_calls(service, held_camera):
    _, slug, _ = held_camera

    stream1 = service.stream_tags(slug)
    stream2 = service.stream_tags(slug)
    try:
        producer = service._tag_stream_producers[slug]  # noqa: SLF001 - internal, pinning reuse
        assert producer.broadcaster.subscriber_count == 2
    finally:
        stream1.close()
        stream2.close()


def test_close_stops_every_tracked_tag_stream_producer(service, held_camera):
    _, slug, _ = held_camera

    service.stream_tags(slug)
    producer = service._tag_stream_producers[slug]  # noqa: SLF001 - internal, pinning shutdown
    deadline = time.monotonic() + 2.0
    while not producer.running and time.monotonic() < deadline:
        time.sleep(0.005)
    assert producer.running is True

    service.close()

    assert producer.running is False
    assert service._tag_stream_producers == {}  # noqa: SLF001


# ---------------------------------------------------------------------------
# stream_images (ticket 027-004, UC-07/SUC-002) -- in-process only this
# ticket: no socket/gRPC endpoint, just DaemonService.stream_images wired
# to daemon.streaming.ImageStreamProducer/BroadcasterImageStream.
# ---------------------------------------------------------------------------


def _open_camera_looping(
    registry: CameraRegistry,
    frame: np.ndarray,
    *,
    hardware_key: str = "hw:stream-image",
    device_name: str = "Stream Image Camera",
):
    """Like ``_open_camera_with_frames`` above, but with ``loop=True`` --
    a background stream producer keeps reading for as long as it has a
    subscriber, so (unlike the one-shot calibration fixtures) it must
    never run dry."""
    identity = _identity(hardware_key, device_name)
    slug = registry.resolve(identity)
    number = registry.get(slug).number
    backend = FakeBackend(read_queue=[frame], loop=True)
    cam = Camera(slug, backend, number=number)
    cam.open()
    return identity, slug, cam, backend


def _calibrated_streaming_service(directories, registry):
    """A ``DaemonService`` with one camera, calibrated against
    ``main-playfield``, backed by a *looping* fake camera -- the shared
    setup every deskewed ``stream_images`` test below needs."""
    frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    identity, slug, cam, _backend = _open_camera_looping(registry, frame)
    service = _service_with_camera(directories, registry, identity, slug, cam)
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))
    assert service.calibrate(slug).succeeded is True
    return service, slug


def test_stream_images_raw_returns_a_stream_that_publishes_real_frames(service, held_camera):
    _, slug, _ = held_camera

    stream = service.stream_images(slug)
    try:
        frame = next(iter(stream))
        assert isinstance(frame, ImageFrame)
        assert frame.deskewed is False
    finally:
        stream.close()


def test_stream_images_deskewed_raises_not_calibrated(service, held_camera):
    _, slug, _ = held_camera

    with pytest.raises(NotCalibrated):
        service.stream_images(slug, deskewed=True)

    # No producer was created for the rejected request.
    assert service._image_stream_producers == {}  # noqa: SLF001


def test_stream_images_deskewed_returns_a_stream_that_publishes_real_deskewed_frames(
    directories, registry
):
    service, slug = _calibrated_streaming_service(directories, registry)

    stream = service.stream_images(slug, deskewed=True)
    try:
        frame = next(iter(stream))
        assert frame.deskewed is True
        assert frame.width == 800 and frame.height == 532
        assert frame.px_per_cm == pytest.approx(800 / 134.3)
        assert frame.world_extent == pytest.approx((134.3, 89.3))
        assert frame.annotations is not None  # deskewed always embeds a real set
    finally:
        stream.close()


def test_stream_images_deskewed_embed_matches_get_frame_for_the_same_moment(directories, registry):
    """The literal 'embedded tags/annotations match what get_frame would
    return for the same moment' check: neither :meth:`DaemonService.
    stream_images` nor :meth:`DaemonService.get_frame` ever triggers its
    own detection burst *synchronously as part of serving that call*
    (sprint 021 Decision 4) -- both still only ever read
    ``DetectionEngine.latest``. Both therefore embed content from the
    *same* shared ring buffer, not two independently-arrived-at,
    coincidentally-equal answers.

    Unlike before this ticket's fix, a subscribed deskewed image stream
    now keeps a real ``TagStreamProducer`` alive for the camera in the
    background (:meth:`DaemonService._build_deskewed_image_producer` --
    the fix for a confirmed real-hardware bug where a `playfield view`
    session's embedded tags never updated at all). That producer's own
    bursts land on a real cadence while this test runs, so ``streamed``
    and ``one_shot`` are no longer guaranteed to be the exact same
    ``TagFrame`` *object* (``timestamp``/``frame_index`` may have ticked
    between the two reads, taken microseconds apart) -- this asserts
    content equality of the buffered detection result instead, which is
    what "matches for the same moment" actually means once a live
    background loop is in the picture.
    """
    service, slug = _calibrated_streaming_service(directories, registry)

    stream = service.stream_images(slug, deskewed=True)
    try:
        # The keep-alive TagStreamProducer's very first burst has not
        # necessarily landed by the time the image producer's own first
        # tick runs (two independently-scheduled background threads) --
        # wait for the first frame whose ``tags`` is actually populated,
        # rather than assuming ``next(iter(stream))`` already is one.
        streamed = None
        deadline = time.monotonic() + 5.0
        for candidate in stream:
            if candidate.tags is not None:
                streamed = candidate
                break
            if time.monotonic() > deadline:
                break
        assert streamed is not None, "no frame with populated tags within 5s"
        # Taken immediately after, while the stream (and its keep-alive
        # TagStreamProducer) is still open -- both reads see the same
        # live detection state, modulo an at-most-one-tick race.
        one_shot = service.get_frame(slug, deskewed=True)
    finally:
        stream.close()

    assert streamed.tags is not None and one_shot.tags is not None
    assert streamed.tags.tags == one_shot.tags.tags
    assert streamed.tags.calibration_stale == one_shot.tags.calibration_stale
    # Content, not wall-clock ``timestamp`` (each ``get_annotations`` call
    # stamps its own -- content equality is what "matches" means here).
    assert streamed.annotations is not None and one_shot.annotations is not None
    assert streamed.annotations.annotations == one_shot.annotations.annotations
    assert streamed.annotations.revision == one_shot.annotations.revision
    assert streamed.px_per_cm == pytest.approx(one_shot.px_per_cm)
    assert streamed.world_extent == pytest.approx(one_shot.world_extent)
    assert streamed.deskewed is True and one_shot.deskewed is True


def test_stream_images_deskewed_alone_keeps_detection_running_and_tags_update(directories, registry):
    """Regression test for a confirmed real-hardware bug (aprilcam
    `playfield view --camera arducam-ov9281-usb-camera`): a viewer
    session subscribes to ``stream_images(camera, deskewed=True)`` only
    -- ``view.viewer.run_playfield_view``'s own acceptance criteria
    (ticket 027-007) forbid it from ever calling ``get_tags``/
    ``stream_tags`` too. Before this ticket's fix,
    ``ImageStreamProducer`` embedding tags via ``DetectionEngine.latest``
    (a pure read -- it never calls ``run_burst`` itself, by ticket
    027-004's own design) meant *nothing* ever advanced detection for a
    camera whose only active consumer was a deskewed image stream: the
    embedded ``tags`` froze at whatever state existed when the stream
    started (typically stale calibration-time state) for the entire life
    of the view, even while a real, already-confirmed tag was newly
    visible and correctly reported by a concurrent ``get_tags`` poll on
    the same camera -- exactly the divergence reported live.

    Proven here with a scripted detector standing in for real vision (no
    real tag needs to be found in the synthetic frame -- the frame's
    pixel content is irrelevant to a scripted double): absent for the
    first few detection bursts, present from then on. This test never
    calls ``get_tags``/``get_tag``/``stream_tags`` anywhere -- the only
    thing that can possibly be advancing detection is
    ``stream_images(deskewed=True)``'s own keep-alive wiring.
    """
    frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    identity, slug, cam, _backend = _open_camera_looping(registry, frame)

    new_tag = TagId(family=TagFamily.APRILTAG, number=7)
    calls_before_hit = 4

    class _NewTagAfterAFewBursts:
        def __init__(self) -> None:
            self.calls = 0

        def detect(self, gray: np.ndarray, *, scale: float = 1.0) -> List[Detection]:
            hits = [] if self.calls < calls_before_hit else [_detection(7)]
            self.calls += 1
            return hits

    scripted_detector = _NewTagAfterAFewBursts()
    # confirmation_streak=1: this test is about "does detection run at
    # all for a deskewed-image-stream-only subscriber", not about
    # temporal confirmation (test_detection.py's own concern) -- matches
    # this file's established pattern (``_build_tag_service``'s
    # docstring) for tests genuinely about something else entirely.
    detection = DetectionEngine(detector_factory=lambda _slug: scripted_detector, confirmation_streak=1)

    cameras: Dict[str, Camera] = {slug: cam}
    discovery = CameraDiscovery(
        registry,
        active_slugs=lambda: frozenset(cameras.keys()),
        identity_source=lambda: {0: identity},
        backend_factory=lambda index: FakeBackend(),
    )
    service = DaemonService(
        registry=registry,
        discovery=discovery,
        directories=directories,
        cameras=cameras,
        identity_source=lambda: {0: identity},
        detection=detection,
    )
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))
    assert service.calibrate(slug).succeeded is True  # Calibrator has its own detector -- unaffected

    stream = service.stream_images(slug, deskewed=True)
    seen_new_tag = False
    try:
        deadline = time.monotonic() + 10.0
        for streamed in stream:
            if streamed.tags is not None and any(
                record.tag == new_tag for record in streamed.tags.tags
            ):
                seen_new_tag = True
                break
            if time.monotonic() > deadline:
                break
    finally:
        stream.close()

    assert seen_new_tag, (
        "stream_images(deskewed=True) never showed the new tag within "
        "10s of real time even though nothing else in this test ever "
        "called get_tags/stream_tags -- the deskewed image stream's "
        "keep-alive TagStreamProducer wiring "
        "(DaemonService._build_deskewed_image_producer) is not driving "
        "detection as it should"
    )
    # And the scripted detector really did run more than once -- proof
    # this was driven by an active background loop, not a lucky single
    # call that happened to already know about the new tag.
    assert scripted_detector.calls > calls_before_hit


def test_stream_images_reuses_the_same_producer_across_repeated_calls(service, held_camera):
    _, slug, _ = held_camera

    stream1 = service.stream_images(slug)
    stream2 = service.stream_images(slug)
    try:
        producer = service._image_stream_producers[(slug, False)]  # noqa: SLF001
        assert producer.broadcaster.subscriber_count == 2
    finally:
        stream1.close()
        stream2.close()


def test_stream_images_raw_and_deskewed_are_independently_tracked(directories, registry):
    service, slug = _calibrated_streaming_service(directories, registry)

    raw_stream = service.stream_images(slug, deskewed=False)
    deskewed_stream = service.stream_images(slug, deskewed=True)
    try:
        producers = service._image_stream_producers  # noqa: SLF001
        assert set(producers.keys()) == {(slug, False), (slug, True)}
        assert producers[(slug, False)] is not producers[(slug, True)]
    finally:
        raw_stream.close()
        deskewed_stream.close()


def test_close_stops_every_tracked_image_stream_producer(service, held_camera):
    _, slug, _ = held_camera

    service.stream_images(slug)
    producer = service._image_stream_producers[(slug, False)]  # noqa: SLF001
    deadline = time.monotonic() + 2.0
    while not producer.running and time.monotonic() < deadline:
        time.sleep(0.005)
    assert producer.running is True

    service.close()

    assert producer.running is False
    assert service._image_stream_producers == {}  # noqa: SLF001


def test_valid_encodings_matches_daemon_get_frame_contract():
    assert VALID_ENCODINGS == ("jpeg", "png")


# ---------------------------------------------------------------------------
# daemon/rpc.py -- round trips over a real gRPC unix-socket server
# ---------------------------------------------------------------------------


@contextmanager
def _bind_stub(service):
    """Bind a real ``grpc.Server`` to a short top-level ``/tmp`` unix
    socket (not pytest's deeply nested ``tmp_path``, which routinely
    exceeds unix domain sockets' ~103-character path limit on
    macOS/Linux) and yield a connected, real generated stub. Factored
    out of the ``stub`` fixture below (ticket 021-005) so the
    tag-detection round-trip tests can bind a *different* service per
    test (each needs its own scripted detector) without duplicating
    the bind/connect/teardown dance."""
    socket_dir = Path(tempfile.mkdtemp(prefix="ac-", dir="/tmp"))
    socket_path = socket_dir / "control.sock"
    try:
        listener = build_server(service, socket_path)
        listener.start()
        channel = grpc.insecure_channel(f"unix:{socket_path}")
        try:
            grpc.channel_ready_future(channel).result(timeout=5)
            yield aprilcam_v2_pb2_grpc.AprilCamV2Stub(channel)
        finally:
            channel.close()
            listener.stop(1.0)
    finally:
        shutil.rmtree(socket_dir, ignore_errors=True)


@pytest.fixture
def stub(service):
    with _bind_stub(service) as s:
        yield s


@pytest.fixture
def tag_stub_factory(directories, registry):
    """Factory fixture: bind a real ``grpc.Server`` to a fresh
    ``DaemonService`` wired to a caller-supplied scripted/counting
    detector (see ``_build_tag_service`` above), yielding
    ``(stub, slug)``. Each RPC round-trip test below needs its own
    detection script, so this is a factory rather than a single
    fixture instance."""

    @contextmanager
    def _make(detector, *, frame_count: int = 10):
        service, slug = _build_tag_service(
            directories, registry, detector=detector, frame_count=frame_count
        )
        with _bind_stub(service) as stub:
            yield stub, slug

    return _make


def test_rpc_list_cameras_round_trip(stub, held_camera):
    _, slug, _ = held_camera

    response = stub.ListCameras(aprilcam_v2_pb2.ListCamerasRequest())

    assert len(response.cameras) == 1
    assert response.cameras[0].name == slug
    assert response.cameras[0].present is True
    assert response.cameras[0].HasField("number")


def test_rpc_probe_cameras_round_trip(stub, held_camera):
    _, slug, _ = held_camera

    response = stub.ProbeCameras(aprilcam_v2_pb2.ProbeCamerasRequest())

    assert len(response.skipped) == 1
    assert response.skipped[0].backend == slug


def test_rpc_get_frame_jpeg_round_trip(stub, held_camera):
    _, slug, _ = held_camera

    response = stub.GetFrame(aprilcam_v2_pb2.GetFrameRequest(camera=slug))

    assert response.encoding == "jpeg"
    assert response.width == 4
    assert response.height == 4
    assert response.data[:2] == b"\xff\xd8"


def test_rpc_get_frame_png_round_trip(stub, held_camera):
    _, slug, _ = held_camera

    response = stub.GetFrame(aprilcam_v2_pb2.GetFrameRequest(camera=slug, encoding="png"))

    assert response.encoding == "png"
    assert response.data[:8] == b"\x89PNG\r\n\x1a\n"


def test_rpc_get_status_round_trip(stub, directories, held_camera):
    _, slug, _ = held_camera

    response = stub.GetStatus(aprilcam_v2_pb2.GetStatusRequest())

    assert response.running is True
    assert [c.name for c in response.cameras] == [slug]
    assert response.config_dir == str(directories.config_dir)
    assert response.state_dir == str(directories.state_dir)


def test_rpc_get_frame_unknown_camera_maps_to_not_found_with_typed_metadata(stub):
    with pytest.raises(grpc.RpcError) as excinfo:
        stub.GetFrame(aprilcam_v2_pb2.GetFrameRequest(camera="no-such-camera"))

    err = excinfo.value
    assert err.code() == grpc.StatusCode.NOT_FOUND
    metadata = dict(err.trailing_metadata())
    assert metadata["aprilcam-error-type"] == "UnknownCamera"
    assert metadata["aprilcam-requested"] == "no-such-camera"


def test_rpc_get_frame_deskewed_maps_to_failed_precondition(stub, held_camera):
    _, slug, _ = held_camera

    with pytest.raises(grpc.RpcError) as excinfo:
        stub.GetFrame(aprilcam_v2_pb2.GetFrameRequest(camera=slug, deskewed=True))

    assert excinfo.value.code() == grpc.StatusCode.FAILED_PRECONDITION
    metadata = dict(excinfo.value.trailing_metadata())
    assert metadata["aprilcam-error-type"] == "NotCalibrated"


def test_rpc_get_frame_by_number_round_trip(stub, held_camera, registry):
    _, slug, _ = held_camera
    number = registry.get(slug).number

    response = stub.GetFrame(aprilcam_v2_pb2.GetFrameRequest(camera=str(number)))

    assert response.encoding == "jpeg"
    assert response.data[:2] == b"\xff\xd8"


def test_rpc_get_frame_camera_not_present_maps_to_failed_precondition_with_slug(
    stub, absent_camera
):
    slug, _number = absent_camera

    with pytest.raises(grpc.RpcError) as excinfo:
        stub.GetFrame(aprilcam_v2_pb2.GetFrameRequest(camera=slug))

    err = excinfo.value
    assert err.code() == grpc.StatusCode.FAILED_PRECONDITION
    metadata = dict(err.trailing_metadata())
    assert metadata["aprilcam-error-type"] == "CameraNotPresent"
    assert metadata["aprilcam-slug"] == slug


def test_rpc_get_frame_camera_not_present_by_number_maps_to_failed_precondition(
    stub, absent_camera
):
    slug, number = absent_camera

    with pytest.raises(grpc.RpcError) as excinfo:
        stub.GetFrame(aprilcam_v2_pb2.GetFrameRequest(camera=str(number)))

    err = excinfo.value
    assert err.code() == grpc.StatusCode.FAILED_PRECONDITION
    metadata = dict(err.trailing_metadata())
    assert metadata["aprilcam-error-type"] == "CameraNotPresent"
    assert metadata["aprilcam-slug"] == slug


# ---------------------------------------------------------------------------
# Full stack, zero cameras (AC1 / UC-01.S-2) -- proves the whole
# lifecycle+service+discovery assembly (not just the bare lock/listener
# mechanics test_lifecycle.py covers) succeeds and reports ready when
# nothing is enumerated -- headless, no hardware.
# ---------------------------------------------------------------------------


def test_full_stack_start_with_zero_cameras_and_zero_config_succeeds(tmp_path, directories):
    registry = CameraRegistry(directories.state_dir / "cameras")
    cameras: Dict[str, Camera] = {}
    discovery = CameraDiscovery(
        registry,
        active_slugs=lambda: frozenset(cameras.keys()),
        identity_source=lambda: {},  # nothing enumerated -- no hardware at all
    )
    service = DaemonService(registry=registry, discovery=discovery, directories=directories, cameras=cameras)

    socket_dir = Path(tempfile.mkdtemp(prefix="ac-zero-", dir="/tmp"))
    try:
        lifecycle = DaemonLifecycle(
            directories=directories,
            build_listener=lambda: build_server(service, socket_dir / "control.sock"),
            status_source=service.status,
            watch=discovery.watch,
            stop_watch=discovery.stop_watch,
            on_stop=service.close,
            lock_path=socket_dir / "daemon.lock",
            socket_path=socket_dir / "control.sock",
        )

        lifecycle.start()
        try:
            status = lifecycle.status()
            assert status.running is True
            assert status.cameras == ()
            assert status.config_dir == str(directories.config_dir)
            assert status.state_dir == str(directories.state_dir)
        finally:
            lifecycle.stop()
    finally:
        shutil.rmtree(socket_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# get_tags / get_tag / get_tag_history (ticket 021-004)
# ---------------------------------------------------------------------------


class _CountingDetector:
    """A ``TagDetector``-shaped double: counts ``detect()`` calls and
    returns a fixed (empty, by default) set of hits every time. The
    CPU-budget "exactly one burst per call" pins below need an accurate
    call count, not real pixel detection."""

    def __init__(self, hits: Optional[List[Detection]] = None) -> None:
        self.calls = 0
        self._hits = hits if hits is not None else []

    def detect(self, gray: np.ndarray, *, scale: float = 1.0) -> List[Detection]:
        self.calls += 1
        return list(self._hits)


class _ScriptedDetector:
    """A ``TagDetector``-shaped double returning a different, prescripted
    set of hits on each successive call -- builds an exact
    present/absent-tag sequence across bursts, for the explicit-gap
    history test (UC-26.S-4). Calls past the end of the script return no
    hits."""

    def __init__(self, script: List[List[Detection]]) -> None:
        self._script = [list(hits) for hits in script]
        self.calls = 0

    def detect(self, gray: np.ndarray, *, scale: float = 1.0) -> List[Detection]:
        result = self._script[self.calls] if self.calls < len(self._script) else []
        self.calls += 1
        return result


def _detection(number: int, family: TagFamily = TagFamily.APRILTAG) -> Detection:
    corners = np.array([[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]], dtype=np.float32)
    return Detection(family=family, number=number, corners=corners, center=PixelPoint(x=5.0, y=5.0))


def _build_tag_service(directories, registry, *, detector, frame_count: int = 10):
    """Assemble a headless ``DaemonService`` wired to *detector* via a
    fresh ``DetectionEngine`` -- the shared setup every burst-counting /
    scripted-detection test below needs, factored out so each test states
    only what is different about it (the detector's script and how many
    times it queries). Returns ``(service, slug)``.

    ``confirmation_streak=1`` (ticket 024-006): every test built on this
    helper is about burst-counting, ring-buffer/history shape, or the
    RPC wire contract -- not about temporal confirmation itself (that is
    ``test_detection.py``'s dedicated suite) -- so this override restores
    exactly pre-ticket, single-burst-reports-immediately behaviour,
    matching the ticket's own sanctioned pattern for tests "genuinely
    about something else entirely."""
    identity = _identity("hw:fake-tags", "Fake Tag Camera")
    slug = registry.resolve(identity)
    number = registry.get(slug).number
    cam = Camera(
        slug, FakeBackend(read_queue=[_frame() for _ in range(frame_count)]), number=number
    )
    cam.open()
    cameras: Dict[str, Camera] = {slug: cam}
    discovery = CameraDiscovery(
        registry,
        active_slugs=lambda: frozenset(cameras.keys()),
        identity_source=lambda: {0: identity},
        backend_factory=lambda index: FakeBackend(),
    )
    detection = DetectionEngine(detector_factory=lambda _slug: detector, confirmation_streak=1)
    service = DaemonService(
        registry=registry,
        discovery=discovery,
        directories=directories,
        cameras=cameras,
        identity_source=lambda: {0: identity},
        detection=detection,
    )
    return service, slug


@pytest.fixture
def counting_detector():
    return _CountingDetector()


@pytest.fixture
def tag_service(directories, registry, counting_detector):
    return _build_tag_service(directories, registry, detector=counting_detector)


def test_get_tags_triggers_exactly_one_detection_burst_per_call(tag_service, counting_detector):
    service, slug = tag_service

    service.get_tags(slug)
    assert counting_detector.calls == 1

    service.get_tags(slug)
    assert counting_detector.calls == 2


def test_get_tags_called_twice_with_nothing_in_view_triggers_exactly_two_bursts(
    tag_service, counting_detector
):
    """SUC-002's CPU-budget pin: never zero, never more than two."""
    service, slug = tag_service

    first = service.get_tags(slug)
    second = service.get_tags(slug)

    assert first.tags == () and second.tags == ()
    assert counting_detector.calls == 2


def test_get_tag_triggers_exactly_one_detection_burst_per_call(tag_service, counting_detector):
    service, slug = tag_service
    tag = TagId(family=TagFamily.APRILTAG, number=7)

    service.get_tag(slug, tag)
    assert counting_detector.calls == 1

    service.get_tag(slug, tag)
    assert counting_detector.calls == 2


def test_get_tag_returns_none_for_an_absent_tag_never_a_zero_valued_record(tag_service):
    service, slug = tag_service

    result = service.get_tag(slug, TagId(family=TagFamily.APRILTAG, number=99))

    assert result is None


def test_get_tags_on_an_empty_field_returns_an_empty_tag_frame_not_an_error(tag_service):
    service, slug = tag_service

    frame = service.get_tags(slug)

    assert frame.tags == ()
    assert frame.calibration_stale is False


def test_get_tag_history_with_no_prior_queries_triggers_zero_bursts(tag_service, counting_detector):
    service, slug = tag_service

    history = service.get_tag_history(slug)

    assert history == ()
    assert counting_detector.calls == 0


def test_get_tag_history_reads_the_buffer_without_triggering_new_bursts(
    tag_service, counting_detector
):
    service, slug = tag_service

    service.get_tags(slug)
    service.get_tags(slug)
    assert counting_detector.calls == 2

    history = service.get_tag_history(slug)

    assert len(history) == 2
    assert counting_detector.calls == 2  # unchanged -- a pure read


def test_get_tag_history_returns_everything_when_fewer_than_requested(tag_service):
    service, slug = tag_service

    service.get_tags(slug)
    service.get_tags(slug)

    history = service.get_tag_history(slug, frames=100)

    assert len(history) == 2  # not an error, not padded/truncated to 100


def test_get_tag_history_by_frames_returns_the_most_recent_n(tag_service):
    service, slug = tag_service

    frames = [service.get_tags(slug) for _ in range(5)]

    history = service.get_tag_history(slug, frames=2)

    assert history == tuple(frames[-2:])


def test_idle_daemon_triggers_zero_detections_over_a_simulated_window(
    tag_service, counting_detector
):
    """No ``get_tags``/``get_tag``/``get_tag_history`` call at all --
    trivially zero detections, since there is no background loop to idle
    (sprint.md Decision 1). Pinned as a named regression test per
    dev-plan Stage 8 rather than left as an unstated assumption; a brief
    sleep stands in for "the daemon sat idle" without slowing the suite
    down to the full 10 s window the acceptance criterion names -- the
    assertion holds at any duration, since nothing but an explicit call
    increments the counter."""
    service, _slug = tag_service
    time.sleep(0.05)
    assert counting_detector.calls == 0


def test_get_tags_every_record_is_honestly_uncalibrated(directories, registry):
    """The Description's field-by-field uncalibrated contract, exercised
    at the ``DaemonService`` layer (``test_detection.py`` covers the same
    contract one layer down, against a real detector)."""
    detector = _ScriptedDetector([[_detection(7)]])
    service, slug = _build_tag_service(directories, registry, detector=detector, frame_count=1)

    tag_frame = service.get_tags(slug)

    assert len(tag_frame.tags) == 1
    record = tag_frame.tags[0]
    assert record.calibrated is False
    assert record.world is None
    assert record.heading_rad is None
    assert record.world_velocity is None
    assert record.speed is None
    assert record.yaw_rad is None
    assert record.world_corners is None
    assert record.pixel is not None
    assert record.pixel_corners is not None
    assert len(record.pixel_corners) == 4


def test_get_tag_history_preserves_explicit_gaps_when_filtered_by_tag(directories, registry):
    """UC-26.S-4: a tag absent from the middle of the requested window
    shows an explicit gap -- the frame is retained, with its real
    timestamp/frame_index, and an empty ``.tags`` -- never interpolated
    and never silently dropped."""
    target = TagId(family=TagFamily.APRILTAG, number=7)
    script = [[_detection(7)], [_detection(9)], [_detection(7)]]
    detector = _ScriptedDetector(script)
    service, slug = _build_tag_service(directories, registry, detector=detector, frame_count=3)

    service.get_tags(slug)
    service.get_tags(slug)
    service.get_tags(slug)

    history = service.get_tag_history(slug, tag=target)

    assert len(history) == 3  # no frame dropped, even though frame 1 lacks the tag
    assert [f.frame_index for f in history] == [0, 1, 2]
    assert len(history[0].tags) == 1 and history[0].tags[0].tag == target
    assert history[1].tags == ()  # the explicit gap -- frame retained, .tags empty
    assert len(history[2].tags) == 1 and history[2].tags[0].tag == target
    assert history[0].timestamp <= history[1].timestamp <= history[2].timestamp


def test_get_tags_by_number_and_by_slug_share_the_same_camera_buffer(
    tag_service, registry, counting_detector
):
    """SUC-005: ``_camera_for``'s number-or-slug resolution is reused,
    unmodified, by the new tag-query methods -- both forms land in the
    same per-camera ring buffer."""
    service, slug = tag_service
    number = registry.get(slug).number

    service.get_tags(slug)
    service.get_tags(str(number))

    history = service.get_tag_history(slug)
    assert len(history) == 2
    assert counting_detector.calls == 2


def test_get_tags_unknown_camera_raises_unknown_camera(service):
    with pytest.raises(UnknownCamera) as excinfo:
        service.get_tags("no-such-camera")
    assert excinfo.value.requested == "no-such-camera"


def test_get_tag_unknown_camera_raises_unknown_camera(service):
    with pytest.raises(UnknownCamera):
        service.get_tag("no-such-camera", TagId(family=TagFamily.APRILTAG, number=1))


def test_get_tag_history_unknown_camera_raises_unknown_camera(service):
    with pytest.raises(UnknownCamera):
        service.get_tag_history("no-such-camera")


def test_get_tags_absent_registered_camera_raises_camera_not_present(service, absent_camera):
    slug, _number = absent_camera
    with pytest.raises(CameraNotPresent) as excinfo:
        service.get_tags(slug)
    assert excinfo.value.slug == slug


def test_get_tag_history_absent_registered_camera_raises_camera_not_present(
    service, absent_camera
):
    slug, _number = absent_camera
    with pytest.raises(CameraNotPresent) as excinfo:
        service.get_tag_history(slug)
    assert excinfo.value.slug == slug


# ---------------------------------------------------------------------------
# get_frame's tag embed (ticket 021-004, sprint.md Decision 4)
# ---------------------------------------------------------------------------


def test_get_frame_tags_is_none_when_the_ring_buffer_is_empty(tag_service):
    service, slug = tag_service

    frame = service.get_frame(slug)

    assert frame.tags is None


def test_get_frame_embeds_the_latest_ring_buffer_entry_without_triggering_a_new_burst(
    tag_service, counting_detector
):
    service, slug = tag_service

    tag_frame = service.get_tags(slug)
    assert counting_detector.calls == 1

    frame = service.get_frame(slug)

    assert frame.tags == tag_frame
    assert counting_detector.calls == 1  # get_frame itself triggered no new burst


def test_get_frame_deskewed_behavior_is_unchanged_by_the_tag_embed(tag_service):
    """``get_frame``'s existing ``deskewed=True`` -> ``NotCalibrated``
    behavior must survive the ``tags=`` extension unchanged."""
    service, slug = tag_service
    with pytest.raises(NotCalibrated):
        service.get_frame(slug, deskewed=True)


# ---------------------------------------------------------------------------
# GetTags / GetTag / GetTagHistory -- RPC round trips over a real
# gRPC unix-socket server (ticket 021-005)
# ---------------------------------------------------------------------------


class _StaticTagsDaemon(Daemon):
    """A minimal ``Daemon`` double (plain base class, derived from --
    see ``daemon.interfaces``'s module docstring) that answers
    ``get_tags`` with a pre-built ``TagFrame`` regardless of the
    camera named. Lets a test hand the wire a ``TagRecord`` carrying a
    *genuine* ``0.0``/``(0, 0)`` for fields that ``DaemonService`` never
    actually produces this sprint (every real record is honestly
    uncalibrated -- see ``test_get_tags_every_record_is_honestly_
    uncalibrated`` above) -- proving the marshalling code's *other*
    failure mode: a real zero must survive as a real zero, not collapse
    into "unset" the same way ``None`` must never coerce to ``0.0``
    (ticket 021-005's marshalling contract cuts both ways)."""

    def __init__(self, tag_frame: TagFrame) -> None:
        self._tag_frame = tag_frame

    def get_tags(self, camera: str) -> TagFrame:
        return self._tag_frame


def test_rpc_get_tags_round_trip_found_tag_is_honestly_uncalibrated(tag_stub_factory):
    detector = _ScriptedDetector([[_detection(7)]])
    with tag_stub_factory(detector, frame_count=1) as (stub, slug):
        response = stub.GetTags(aprilcam_v2_pb2.GetTagsRequest(camera=slug))

    assert len(response.tags.tags) == 1
    record = response.tags.tags[0]
    assert record.tag.family == aprilcam_v2_pb2.APRILTAG
    assert record.tag.number == 7
    assert record.calibrated is False
    # Absence, not a 0.0/empty placeholder -- the wire contract this
    # sprint's uncalibrated records must never violate.
    assert record.HasField("world") is False
    assert record.HasField("heading_rad") is False
    assert record.HasField("world_velocity") is False
    assert record.HasField("speed") is False
    assert record.HasField("yaw_rad") is False
    assert record.world_corners == []
    assert record.HasField("pixel") is True
    assert len(record.pixel_corners) == 4


def test_rpc_get_tags_round_trip_aruco_family_and_number_survive_intact(tag_stub_factory):
    """The v1 ``-(N+1)`` internal ArUco encoding must never appear on
    the wire -- family and a non-negative number cross intact."""
    detector = _ScriptedDetector([[_detection(3, family=TagFamily.ARUCO)]])
    with tag_stub_factory(detector, frame_count=1) as (stub, slug):
        response = stub.GetTags(aprilcam_v2_pb2.GetTagsRequest(camera=slug))

    record = response.tags.tags[0]
    assert record.tag.family == aprilcam_v2_pb2.ARUCO
    assert record.tag.number == 3


def test_rpc_get_tags_round_trip_empty_field_returns_empty_tag_frame(tag_stub_factory):
    detector = _ScriptedDetector([[]])
    with tag_stub_factory(detector, frame_count=1) as (stub, slug):
        response = stub.GetTags(aprilcam_v2_pb2.GetTagsRequest(camera=slug))

    assert list(response.tags.tags) == []
    assert response.tags.calibration_stale is False


def test_rpc_get_tags_unknown_camera_maps_to_not_found(tag_stub_factory):
    detector = _CountingDetector()
    with tag_stub_factory(detector, frame_count=1) as (stub, _slug):
        with pytest.raises(grpc.RpcError) as excinfo:
            stub.GetTags(aprilcam_v2_pb2.GetTagsRequest(camera="no-such-camera"))

    assert excinfo.value.code() == grpc.StatusCode.NOT_FOUND
    metadata = dict(excinfo.value.trailing_metadata())
    assert metadata["aprilcam-error-type"] == "UnknownCamera"
    assert metadata["aprilcam-requested"] == "no-such-camera"


def test_rpc_get_tag_round_trip_found(tag_stub_factory):
    detector = _ScriptedDetector([[_detection(7)]])
    with tag_stub_factory(detector, frame_count=1) as (stub, slug):
        request = aprilcam_v2_pb2.GetTagRequest(
            camera=slug, tag=aprilcam_v2_pb2.TagId(family=aprilcam_v2_pb2.APRILTAG, number=7)
        )
        response = stub.GetTag(request)

    assert response.HasField("record") is True
    assert response.record.tag.number == 7


def test_rpc_get_tag_round_trip_not_found_is_an_absent_record_not_an_error(tag_stub_factory):
    detector = _ScriptedDetector([[]])
    with tag_stub_factory(detector, frame_count=1) as (stub, slug):
        request = aprilcam_v2_pb2.GetTagRequest(
            camera=slug, tag=aprilcam_v2_pb2.TagId(family=aprilcam_v2_pb2.APRILTAG, number=99)
        )
        response = stub.GetTag(request)

    assert response.HasField("record") is False


def test_rpc_get_tag_unknown_camera_maps_to_not_found(tag_stub_factory):
    detector = _CountingDetector()
    with tag_stub_factory(detector, frame_count=1) as (stub, _slug):
        request = aprilcam_v2_pb2.GetTagRequest(
            camera="no-such-camera",
            tag=aprilcam_v2_pb2.TagId(family=aprilcam_v2_pb2.APRILTAG, number=1),
        )
        with pytest.raises(grpc.RpcError) as excinfo:
            stub.GetTag(request)

    assert excinfo.value.code() == grpc.StatusCode.NOT_FOUND


def test_rpc_get_tag_history_round_trip_preserves_explicit_gap(tag_stub_factory):
    target = aprilcam_v2_pb2.TagId(family=aprilcam_v2_pb2.APRILTAG, number=7)
    script = [[_detection(7)], [_detection(9)], [_detection(7)]]
    detector = _ScriptedDetector(script)
    with tag_stub_factory(detector, frame_count=3) as (stub, slug):
        stub.GetTags(aprilcam_v2_pb2.GetTagsRequest(camera=slug))
        stub.GetTags(aprilcam_v2_pb2.GetTagsRequest(camera=slug))
        stub.GetTags(aprilcam_v2_pb2.GetTagsRequest(camera=slug))

        response = stub.GetTagHistory(
            aprilcam_v2_pb2.GetTagHistoryRequest(camera=slug, tag=target)
        )

    history = response.history
    assert len(history) == 3
    assert [f.frame_index for f in history] == [0, 1, 2]
    assert len(history[0].tags) == 1 and history[0].tags[0].tag.number == 7
    assert list(history[1].tags) == []  # explicit gap -- retained, empty
    assert len(history[2].tags) == 1 and history[2].tags[0].tag.number == 7


def test_rpc_get_tag_history_round_trip_frames_limit(tag_stub_factory):
    detector = _ScriptedDetector([[_detection(7)] for _ in range(5)])
    with tag_stub_factory(detector, frame_count=5) as (stub, slug):
        for _ in range(5):
            stub.GetTags(aprilcam_v2_pb2.GetTagsRequest(camera=slug))

        response = stub.GetTagHistory(
            aprilcam_v2_pb2.GetTagHistoryRequest(camera=slug, frames=2)
        )

    assert len(response.history) == 2
    assert [f.frame_index for f in response.history] == [3, 4]


def test_rpc_get_tag_history_unknown_camera_maps_to_not_found(tag_stub_factory):
    detector = _CountingDetector()
    with tag_stub_factory(detector, frame_count=1) as (stub, _slug):
        with pytest.raises(grpc.RpcError) as excinfo:
            stub.GetTagHistory(aprilcam_v2_pb2.GetTagHistoryRequest(camera="no-such-camera"))

    assert excinfo.value.code() == grpc.StatusCode.NOT_FOUND


def test_rpc_get_tags_camera_not_present_maps_to_failed_precondition(stub, absent_camera):
    """Reuses the plain ``service``/``stub`` fixtures (no detection
    script needed -- the camera is never reached) to prove
    ``CameraNotPresent`` (ticket 021-002) round-trips through the new
    ``GetTags`` RPC exactly as it already does for ``GetFrame``."""
    slug, _number = absent_camera

    with pytest.raises(grpc.RpcError) as excinfo:
        stub.GetTags(aprilcam_v2_pb2.GetTagsRequest(camera=slug))

    err = excinfo.value
    assert err.code() == grpc.StatusCode.FAILED_PRECONDITION
    metadata = dict(err.trailing_metadata())
    assert metadata["aprilcam-error-type"] == "CameraNotPresent"
    assert metadata["aprilcam-slug"] == slug


def test_rpc_get_tags_round_trip_distinguishes_a_real_zero_from_absent():
    """The wire's other marshalling failure mode: a genuinely
    calibrated-looking ``0.0``/``(0, 0)`` must survive as a real value,
    never collapsing into "unset" -- the mirror image of ``None`` never
    coercing to ``0.0``. Drives ``build_server`` with a
    ``_StaticTagsDaemon`` double instead of ``DaemonService`` because no
    real detection burst this sprint ever produces a non-``None`` value
    for these fields to exercise this the other way."""
    zeroed = TagRecord(
        tag=TagId(family=TagFamily.APRILTAG, number=1),
        world=WorldPoint(x=0.0, y=0.0),
        heading_rad=0.0,
        world_velocity=WorldPoint(x=0.0, y=0.0),
        speed=0.0,
        timestamp=1.0,
        calibrated=True,
        yaw_rad=0.0,
    )
    unset = TagRecord(
        tag=TagId(family=TagFamily.APRILTAG, number=2),
        world=None,
        heading_rad=None,
        world_velocity=None,
        speed=None,
        timestamp=1.0,
        calibrated=False,
    )
    tag_frame = TagFrame(tags=(zeroed, unset), timestamp=1.0, frame_index=0)
    service = _StaticTagsDaemon(tag_frame)

    with _bind_stub(service) as stub:
        response = stub.GetTags(aprilcam_v2_pb2.GetTagsRequest(camera="any"))

    zero_record, unset_record = response.tags.tags

    assert zero_record.HasField("heading_rad") is True
    assert zero_record.heading_rad == 0.0
    assert zero_record.HasField("world") is True
    assert zero_record.world.x == 0.0 and zero_record.world.y == 0.0
    assert zero_record.HasField("speed") is True
    assert zero_record.speed == 0.0
    assert zero_record.HasField("yaw_rad") is True

    assert unset_record.HasField("heading_rad") is False
    assert unset_record.HasField("world") is False
    assert unset_record.HasField("world_velocity") is False
    assert unset_record.HasField("speed") is False
    assert unset_record.HasField("yaw_rad") is False


# ---------------------------------------------------------------------------
# GetFrame's tag embed over the wire: absent vs. present-but-empty
# (ticket 021-005) -- the three-way distinction ImageFrame.tags
# documents must survive real (de)serialization, not just the
# same-process DaemonService assertions above.
# ---------------------------------------------------------------------------


def test_rpc_get_frame_tags_is_absent_when_no_detection_has_run(tag_stub_factory):
    detector = _CountingDetector()
    with tag_stub_factory(detector, frame_count=1) as (stub, slug):
        response = stub.GetFrame(aprilcam_v2_pb2.GetFrameRequest(camera=slug))

    assert response.HasField("tags") is False


def test_rpc_get_frame_tags_is_present_but_empty_after_detection_saw_nothing(tag_stub_factory):
    detector = _ScriptedDetector([[]])
    with tag_stub_factory(detector, frame_count=2) as (stub, slug):
        stub.GetTags(aprilcam_v2_pb2.GetTagsRequest(camera=slug))
        response = stub.GetFrame(aprilcam_v2_pb2.GetFrameRequest(camera=slug))

    assert response.HasField("tags") is True
    assert list(response.tags.tags) == []


def test_rpc_get_frame_embeds_tags_round_trip(tag_stub_factory):
    detector = _ScriptedDetector([[_detection(7)]])
    with tag_stub_factory(detector, frame_count=2) as (stub, slug):
        tags_response = stub.GetTags(aprilcam_v2_pb2.GetTagsRequest(camera=slug))
        frame_response = stub.GetFrame(aprilcam_v2_pb2.GetFrameRequest(camera=slug))

    assert frame_response.HasField("tags") is True
    assert frame_response.tags.frame_index == tags_response.tags.frame_index
    assert len(frame_response.tags.tags) == 1
    assert frame_response.tags.tags[0].tag.number == 7


# ---------------------------------------------------------------------------
# Playfields / camera config -- the minimal config-service slice
# (ticket 022-001, sprint.md Architecture Decision 1 & 4)
# ---------------------------------------------------------------------------


def test_set_playfield_and_get_playfield_round_trip_reports_all_features(service):
    definition = _playfield_definition()

    service.set_playfield(definition)
    fetched = service.get_playfield("main-playfield")

    assert fetched.name == "main-playfield"
    assert fetched.definition.features() == definition.features()
    assert fetched.definition.required_aruco_ids() == (1, 2, 3)


def test_set_playfield_with_the_real_main_playfield_document_round_trips_through_the_daemon(
    service,
):
    """AC1 (UC-02.S-1), end to end through ``DaemonService`` -- not just
    the loader (``tests/aprilcam/config/test_loader.py`` covers the
    loader in isolation): push the real shipped document, fetch it
    back, and check the exact acceptance-criteria coordinates."""
    repo_root = Path(__file__).resolve().parents[3]
    main_playfield_path = repo_root / "config" / "aprilcam-old" / "playfields" / "main-playfield.json"
    definition = ConfigLoader().load_playfield(main_playfield_path)

    service.set_playfield(definition)
    fetched = service.get_playfield("main-playfield")

    features = fetched.definition.features()
    assert len(features) == 25
    by_slug = {f.slug: f for f in features}
    assert by_slug["aruco-northwest-u1"].position == WorldPoint(x=-67, y=44.65)
    assert by_slug["aruco-southeast-u5"].position == WorldPoint(x=67, y=-44.65)
    assert fetched.definition.required_aruco_ids() == tuple(range(1, 11))
    assert fetched.linked_cameras() == ()


def test_a_good_playfield_survives_a_later_rejected_push_attempt(service, tmp_path):
    """AC2's full scenario (UC-02.S-2), end to end: a definition already
    held is untouched by a later push whose document is malformed --
    ``ConfigLoader.load_playfield`` raises ``MalformedDocument`` before
    ``set_playfield`` is ever called, so the daemon's held state simply
    never had a chance to change."""
    good = _playfield_definition("main-playfield", aruco_ids=(1, 2, 3))
    service.set_playfield(good)

    bad_path = tmp_path / "bad-main-playfield.json"
    bad_path.write_text(
        json.dumps(
            {
                "name": "main-playfield",
                "playfield": {"width_cm": 100.0, "height_cm": 80.0},
                "aruco_tags": [
                    {"slug": "u3", "id": 3, "x": -1, "y": -1},
                    {"slug": "u3-dup", "id": 3, "x": 1, "y": 1},
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(MalformedDocument):
        definition = ConfigLoader().load_playfield(bad_path)
        service.set_playfield(definition)  # never reached

    fetched = service.get_playfield("main-playfield")
    assert fetched.definition.required_aruco_ids() == (1, 2, 3)  # unchanged


def test_list_playfields_includes_a_pushed_definition(service):
    service.set_playfield(_playfield_definition())

    playfields = service.list_playfields()

    assert [p.name for p in playfields] == ["main-playfield"]


def test_get_playfield_unknown_name_raises_unknown_playfield_listing_held_playfields(service):
    service.set_playfield(_playfield_definition("main-playfield"))

    with pytest.raises(UnknownPlayfield) as excinfo:
        service.get_playfield("no-such-field")

    assert excinfo.value.requested == "no-such-field"
    assert excinfo.value.available == ("main-playfield",)


def test_get_playfield_on_a_service_with_no_playfields_lists_none_available(service):
    with pytest.raises(UnknownPlayfield) as excinfo:
        service.get_playfield("main-playfield")
    assert excinfo.value.available == ()


def test_daemon_holds_two_differently_named_playfields_without_interference(service):
    """Sprint.md Decision 4: no single-slot cardinality limit."""
    field_a = _playfield_definition("field-a", aruco_ids=(1, 2))
    field_b = _playfield_definition("field-b", aruco_ids=(5, 6, 7))

    service.set_playfield(field_a)
    service.set_playfield(field_b)

    assert {p.name for p in service.list_playfields()} == {"field-a", "field-b"}
    assert service.get_playfield("field-a").definition.required_aruco_ids() == (1, 2)
    assert service.get_playfield("field-b").definition.required_aruco_ids() == (5, 6, 7)

    # Replacing one by name never disturbs the other.
    service.set_playfield(_playfield_definition("field-a", aruco_ids=(9,)))
    assert service.get_playfield("field-a").definition.required_aruco_ids() == (9,)
    assert service.get_playfield("field-b").definition.required_aruco_ids() == (5, 6, 7)


# ---------------------------------------------------------------------------
# find_feature (ticket 025-002, UC-08)
# ---------------------------------------------------------------------------


def _real_main_playfield_definition() -> PlayfieldDefinition:
    """The real ``main-playfield.json`` on disk -- includes dots and
    rectangles ``_playfield_definition()`` above (ArUco-only) does not,
    which ``find_feature``'s worked example (UC-08.S-1/S-2/S-3:
    ``dot-west-blue``, the four orange dots) needs. Same fixture file
    ``tests/aprilcam/playfield/test_query.py``'s
    ``real_main_playfield_query`` loads."""
    path = Path(__file__).resolve().parents[3] / "config" / "aprilcam-old" / "playfields" / "main-playfield.json"
    assert path.exists(), f"fixture missing: {path}"
    return ConfigLoader().load_playfield(path)


def test_find_feature_delegates_to_feature_query_blue_dot_west(service):
    service.set_playfield(_real_main_playfield_definition())

    matches = service.find_feature("main-playfield", "the blue dot")

    assert len(matches) == 1
    assert matches[0].slug == "dot-west-blue"
    assert matches[0].position == WorldPoint(x=-50, y=0)


def test_find_feature_ambiguous_query_returns_all_four_orange_dots(service):
    service.set_playfield(_real_main_playfield_definition())

    matches = service.find_feature("main-playfield", "the orange dot")

    assert {m.slug for m in matches} == {
        "dot-northwest-orange",
        "dot-northeast-orange",
        "dot-southeast-orange",
        "dot-southwest-orange",
    }


def test_find_feature_no_match_returns_empty_tuple_not_nearest_guess(service):
    service.set_playfield(_real_main_playfield_definition())

    matches = service.find_feature("main-playfield", "the orange triangle")

    assert matches == ()


def test_find_feature_unknown_playfield_raises_unknown_playfield_listing_held_playfields(service):
    service.set_playfield(_real_main_playfield_definition())

    with pytest.raises(UnknownPlayfield) as excinfo:
        service.find_feature("no-such-field", "the blue dot")

    assert excinfo.value.requested == "no-such-field"
    assert excinfo.value.available == ("main-playfield",)


def test_find_feature_on_a_service_with_no_playfields_lists_none_available(service):
    with pytest.raises(UnknownPlayfield) as excinfo:
        service.find_feature("main-playfield", "the blue dot")
    assert excinfo.value.available == ()


def test_find_feature_opens_no_camera(directories, registry):
    """UC-08's own claim: answered from the surveyed definition, never
    vision -- no camera is opened and no calibration is required. Built
    against a service holding *zero* cameras (unlike the ``service``
    fixture used above, which has one held open for other tests) so
    there is nothing ``find_feature`` could open even by accident."""
    discovery = CameraDiscovery(
        registry,
        active_slugs=lambda: frozenset(),
        identity_source=lambda: {},
        backend_factory=lambda index: FakeBackend(),
    )
    camera_free_service = DaemonService(
        registry=registry,
        discovery=discovery,
        directories=directories,
        cameras={},
        identity_source=lambda: {},
    )
    camera_free_service.set_playfield(_real_main_playfield_definition())

    matches = camera_free_service.find_feature("main-playfield", "the blue dot")

    assert len(matches) == 1
    assert matches[0].slug == "dot-west-blue"
    assert camera_free_service._cameras == {}  # no camera was opened as a side effect


def test_set_camera_config_links_camera_and_get_camera_config_echoes_it_back(service, held_camera):
    _, slug, _ = held_camera
    service.set_playfield(_playfield_definition())

    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))
    fetched = service.get_camera_config(slug)

    assert fetched.playfield == "main-playfield"


def test_camera_geometry_for_unconfigured_camera_returns_the_exact_pre_sprint_stopgap(
    service, held_camera
):
    """Ticket 029-001: a camera with no config at all held by this
    service, and a camera configured but never located, both fall back
    to the exact pre-029 stopgap -- ``(WorldPoint(0, 0), 0.0)``."""
    _, slug, _ = held_camera

    position, height_cm = service._camera_geometry_for(slug)
    assert position == WorldPoint(x=0.0, y=0.0)
    assert height_cm == 0.0

    # Configured, but never located -- camera_position still None.
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))
    position, height_cm = service._camera_geometry_for(slug)
    assert position == WorldPoint(x=0.0, y=0.0)
    assert height_cm == 0.0

    # A slug this service has never even heard of -- config is None.
    position, height_cm = service._camera_geometry_for("never-registered-camera")
    assert position == WorldPoint(x=0.0, y=0.0)
    assert height_cm == 0.0


def test_camera_geometry_for_configured_camera_returns_its_real_nadir_and_height(
    service, held_camera
):
    """The real fixture (sprint.md's Problem section): camera 3's
    solved nadir/height."""
    _, slug, _ = held_camera
    service.set_camera_config(
        CameraConfig(
            name=slug,
            playfield="main-playfield",
            settings={},
            camera_position=CameraPosition(
                nadir=WorldPoint(x=3.05, y=-2.75), height_cm=128.25
            ),
        )
    )

    position, height_cm = service._camera_geometry_for(slug)

    assert position == WorldPoint(x=3.05, y=-2.75)
    assert height_cm == 128.25


# ---------------------------------------------------------------------------
# _any_camera_located / _mount_z_applied (ticket 030-004, SUC-004) -- pure,
# hand-picked-geometry unit tests of the two new reads register_tag's
# warning and list_tag_parameters' mount_z_applied both share, exercised
# directly against the service fixture's own camera-config store (no
# calibration or detection needed -- these are plain reads of
# _camera_configs, mirroring _camera_geometry_for's own tests just above).
# ---------------------------------------------------------------------------


def test_any_camera_located_false_with_no_camera_configured_at_all(service):
    assert service._any_camera_located() is False


def test_any_camera_located_false_when_configured_but_never_located(service, held_camera):
    _, slug, _ = held_camera
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))

    assert service._any_camera_located() is False


def test_any_camera_located_true_once_one_camera_has_camera_position(service, held_camera):
    _, slug, _ = held_camera
    service.set_camera_config(
        CameraConfig(
            name=slug,
            playfield="main-playfield",
            settings={},
            camera_position=CameraPosition(nadir=WorldPoint(x=0.0, y=0.0), height_cm=100.0),
        )
    )

    assert service._any_camera_located() is True


def test_mount_z_applied_is_vacuously_true_for_zero_or_negative_mount_z(service):
    """No elevation to project -- True regardless of any camera's
    located state (mirrors project_tag_to_field_plane's own
    mount_z_cm <= 0.0 no-op sentinel)."""
    assert service._mount_z_applied(0.0) is True
    assert service._mount_z_applied(-5.0) is True


def test_mount_z_applied_false_then_true_as_the_observing_camera_is_located(
    service, held_camera
):
    _, slug, _ = held_camera

    assert service._mount_z_applied(15.0) is False

    service.set_camera_config(
        CameraConfig(
            name=slug,
            playfield="main-playfield",
            settings={},
            camera_position=CameraPosition(nadir=WorldPoint(x=0.0, y=0.0), height_cm=100.0),
        )
    )

    assert service._mount_z_applied(15.0) is True


def test_get_camera_config_by_persistent_number_reaches_the_same_config_as_by_slug(
    service, held_camera, registry
):
    """Regression (team-lead verification, ticket 022-003 hotfix):
    ``_camera_configs`` is keyed by slug only, so looking a config up by
    the raw token silently missed a valid persistent number. Number and
    slug addressing must reach the identical config (SUC-005)."""
    _, slug, _ = held_camera
    number = registry.get(slug).number
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))

    by_number = service.get_camera_config(str(number))
    by_slug = service.get_camera_config(slug)

    assert by_number == by_slug
    assert by_number.playfield == "main-playfield"


def test_linked_playfield_reports_the_configured_camera(service, held_camera):
    _, slug, _ = held_camera
    service.set_playfield(_playfield_definition())
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))

    assert service.get_playfield("main-playfield").linked_cameras() == (slug,)
    assert service.list_playfields()[0].linked_cameras() == (slug,)


def test_list_cameras_reports_the_playfield_link_after_set_camera_config(service, held_camera):
    _, slug, _ = held_camera
    assert service.list_cameras()[0].playfield is None  # not configured yet

    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))

    assert service.list_cameras()[0].playfield == "main-playfield"


def test_camera_config_settings_round_trip_and_apply_to_held_open_camera(directories, registry):
    """Ticket 023-001: a pushed ``settings`` dict survives the round trip
    through ``get_camera_config`` unchanged (as before), and a spy
    ``set_property`` now proves the settings **do** reach the backend --
    sprint 022's ``Decision 1`` scoping ("accepted and echoed back but
    never applied") is exactly what this ticket closes. Superseded
    version of the old
    ``test_camera_config_settings_round_trip_without_hardware_application``,
    which asserted the opposite (``calls == []``)."""
    identity = _identity("hw:spy", "Spy Camera")
    slug = registry.resolve(identity)
    number = registry.get(slug).number
    backend = FakeBackend(read_queue=[_frame()])
    calls: List[tuple] = []
    backend.set_property = (  # type: ignore[method-assign]
        lambda name, value: calls.append((name, value)) or True
    )
    cam = Camera(slug, backend, number=number)
    cam.open()
    cameras: Dict[str, Camera] = {slug: cam}
    discovery = CameraDiscovery(
        registry,
        active_slugs=lambda: frozenset(cameras.keys()),
        identity_source=lambda: {0: identity},
        backend_factory=lambda index: FakeBackend(),
    )
    service = DaemonService(
        registry=registry,
        discovery=discovery,
        directories=directories,
        cameras=cameras,
        identity_source=lambda: {0: identity},
    )

    service.set_camera_config(
        CameraConfig(name=slug, playfield=None, settings={"exposure": 100, "gain": 2.5})
    )
    fetched = service.get_camera_config(slug)

    assert fetched.settings == {"exposure": 100, "gain": 2.5}
    assert calls == [("exposure", 100.0), ("gain", 2.5)]


def test_set_camera_config_for_a_not_held_open_camera_stores_without_applying(
    service, absent_camera
):
    """AC1's other half: a config for a camera not currently held open
    (registered but absent -- UC-25) is still stored, unchanged from
    before this ticket, with nothing to apply yet since there is no
    open handle to push settings to."""
    slug, _number = absent_camera

    service.set_camera_config(CameraConfig(name=slug, playfield=None, settings={"gain": 5}))

    assert service.get_camera_config(slug).settings == {"gain": 5}


def test_set_camera_config_unsupported_key_fails_whole_push_leaves_config_unset(
    service, held_camera
):
    """UC-03.S-3 at the service level: an unsupported key on a
    held-open camera's *first-ever* push raises ``UnsupportedSetting``
    (propagated straight through from ``Camera.apply_config``) and
    leaves nothing stored. ``get_camera_config`` reports the camera as
    never configured via the held-open blank template (ticket 023-005),
    not the rejected document -- exactly as if the push never
    happened."""
    _, slug, _ = held_camera

    with pytest.raises(UnsupportedSetting) as exc_info:
        service.set_camera_config(
            CameraConfig(name=slug, playfield=None, settings={"not_a_real_key": 1})
        )
    assert exc_info.value.key == "not_a_real_key"

    fetched = service.get_camera_config(slug)
    assert fetched.playfield is None
    assert fetched.settings["device_name"] == slug
    assert fetched.settings["frame_width"] == 640
    assert all(
        value is None
        for key, value in fetched.settings.items()
        if key not in ("device_name", "frame_width", "frame_height")
    )


def test_set_camera_config_unsupported_key_leaves_prior_config_untouched(service, held_camera):
    """UC-03.S-3's defining postcondition: "re-fetching the config
    returns the previous state." A camera already configured, then
    pushed a second, failing document, must still report the *first*
    document afterward -- not the rejected one, and not an unconfigured
    state either."""
    _, slug, _ = held_camera

    service.set_camera_config(
        CameraConfig(name=slug, playfield="main-playfield", settings={"gain": 3})
    )
    with pytest.raises(UnsupportedSetting):
        service.set_camera_config(
            CameraConfig(name=slug, playfield="other-field", settings={"not_a_real_key": 1})
        )

    fetched = service.get_camera_config(slug)
    assert fetched.playfield == "main-playfield"
    assert fetched.settings == {"gain": 3}


def test_set_camera_config_gate_key_ordering_reaches_the_real_backend(directories, registry):
    """Ticket 023-001 AC5: ``_GATE_KEYS`` ordering (``camera/camera.py``)
    is exercised through the *service* entry point, not just directly
    against ``Camera`` (``test_camera.py`` already covers that unit in
    isolation) -- proving the ordering discipline survives being wired
    through ``DaemonService.set_camera_config``."""
    identity = _identity("hw:order", "Order Camera")
    slug = registry.resolve(identity)
    number = registry.get(slug).number
    backend = FakeBackend(read_queue=[_frame()])
    order: List[str] = []
    original_set = backend.set_property

    def spy_set(name, value):
        order.append(name)
        return original_set(name, value)

    backend.set_property = spy_set  # type: ignore[method-assign]
    cam = Camera(slug, backend, number=number)
    cam.open()
    cameras: Dict[str, Camera] = {slug: cam}
    discovery = CameraDiscovery(
        registry,
        active_slugs=lambda: frozenset(cameras.keys()),
        identity_source=lambda: {0: identity},
        backend_factory=lambda index: FakeBackend(),
    )
    service = DaemonService(
        registry=registry,
        discovery=discovery,
        directories=directories,
        cameras=cameras,
        identity_source=lambda: {0: identity},
    )

    service.set_camera_config(
        CameraConfig(
            name=slug, playfield=None, settings={"exposure": 100, "auto_exposure": 1}
        )
    )

    assert order == ["auto_exposure", "exposure"]


# ---------------------------------------------------------------------------
# Apply-on-open (ticket 023-003 AC5): a config stored before a camera is
# ever opened -- whether it arrived via a live push while the camera was
# absent, or (daemon/__main__.py's job) was boot-loaded from config_dir
# -- takes effect the moment _camera_for opens that camera for the first
# time.
# ---------------------------------------------------------------------------


def test_camera_for_applies_a_previously_stored_config_when_opening_for_the_first_time(
    directories, registry
):
    """AC5 / UC-01.S-4's previously-broken half: a config already held
    in ``_camera_configs`` for a camera that has never been opened (here,
    simulated the same way ``set_camera_config`` already allows -- a push
    while the camera is not held open, per
    ``test_set_camera_config_for_a_not_held_open_camera_stores_without_applying``
    above) reaches the real backend the instant that camera *is* first
    opened, via ``_camera_for``. ``daemon.__main__.build()``'s boot-time
    load feeds ``_camera_configs`` through this exact same
    ``set_camera_config`` entry point (this ticket's other half), so this
    also stands in for "a boot-loaded config takes effect on first
    open" without needing a real ``config_dir`` fixture here."""
    identity = _identity("hw:preconfigured", "Preconfigured Camera")
    slug = registry.resolve(identity)
    backend = FakeBackend(read_queue=[_frame()])
    cameras: Dict[str, Camera] = {}
    discovery = CameraDiscovery(
        registry,
        active_slugs=lambda: frozenset(cameras.keys()),
        identity_source=lambda: {0: identity},
        backend_factory=lambda index: FakeBackend(),
    )
    service = DaemonService(
        registry=registry,
        discovery=discovery,
        directories=directories,
        cameras=cameras,
        identity_source=lambda: {0: identity},
        backend_factory=lambda index: backend,
        control_backend_factory=lambda device_name: None,
    )
    service.set_camera_config(CameraConfig(name=slug, playfield=None, settings={"gain": 7}))
    assert backend.get_property("gain") is None  # not applied yet -- camera not open

    service.get_frame(slug)  # first-ever open -- _camera_for must apply the stored config

    assert backend.get_property("gain") == 7.0
    assert service.active_slugs() == {slug}


def test_camera_for_with_no_stored_config_opens_without_touching_the_backend_settings(
    directories, registry
):
    """The common case (no config pushed or boot-loaded for this camera
    at all): opening it for the first time must not call ``apply_config``
    -- nothing to apply, and no spurious backend interaction."""
    identity = _identity("hw:unconfigured", "Unconfigured Camera")
    slug = registry.resolve(identity)
    backend = FakeBackend(read_queue=[_frame()])
    cameras: Dict[str, Camera] = {}
    discovery = CameraDiscovery(
        registry,
        active_slugs=lambda: frozenset(cameras.keys()),
        identity_source=lambda: {0: identity},
        backend_factory=lambda index: FakeBackend(),
    )
    service = DaemonService(
        registry=registry,
        discovery=discovery,
        directories=directories,
        cameras=cameras,
        identity_source=lambda: {0: identity},
        backend_factory=lambda index: backend,
        control_backend_factory=lambda device_name: None,
    )

    frame = service.get_frame(slug)

    assert frame.width == 4 and frame.height == 4
    assert service.active_slugs() == {slug}


def test_get_camera_config_unknown_camera_raises_unknown_camera(service):
    with pytest.raises(UnknownCamera) as excinfo:
        service.get_camera_config("never-configured")
    assert excinfo.value.requested == "never-configured"
    assert excinfo.value.available == ()


def test_get_camera_config_held_open_but_never_pushed_returns_blank_template(
    service, held_camera
):
    """UC-03.S-1 / UC-24.S-3 at the service level (ticket 023-005): a
    camera this process has opened (``held_camera`` -- as ``get_frame``
    leaves one) but that has never had a config successfully pushed
    reports the introspected-plus-blank template
    (``Camera.effective_config``, ticket 023-002's real per-camera
    settable-key set) rather than ``UnknownCamera`` -- the gap ticket
    023-002 built ``effective_config`` for and this ticket closes."""
    _, slug, _ = held_camera

    fetched = service.get_camera_config(slug)

    assert fetched.name == slug
    assert fetched.playfield is None
    assert fetched.settings["device_name"] == slug
    assert fetched.settings["frame_width"] == 640
    assert fetched.settings["frame_height"] == 480
    non_dimension_keys = {
        key for key in fetched.settings if key not in ("device_name", "frame_width", "frame_height")
    }
    assert non_dimension_keys  # the real per-camera settable-key set, not empty
    assert all(fetched.settings[key] is None for key in non_dimension_keys)


def test_get_camera_config_registered_but_never_opened_still_raises_unknown_camera(
    directories, registry, held_camera
):
    """A camera merely *registered* (known to this process's registry,
    e.g. from a prior ``probe``) but never actually opened this process
    -- ``held_camera``'s own identity is resolved into *registry* by its
    fixture, but this service's ``cameras``/``identity_source`` never
    include it -- still reports ``UnknownCamera``, not the blank
    template: the held-open fallback above is deliberately narrower than
    "known to the registry" (see ``get_camera_config``'s docstring)."""
    _, slug, _ = held_camera
    discovery = CameraDiscovery(
        registry,
        active_slugs=lambda: frozenset(),
        identity_source=lambda: {},
        backend_factory=lambda index: FakeBackend(),
    )
    service = DaemonService(registry=registry, discovery=discovery, directories=directories)

    with pytest.raises(UnknownCamera):
        service.get_camera_config(slug)


# ---------------------------------------------------------------------------
# reset_camera_config (ticket 023-004) -- SUC-003, "an experiment ends
# without a daemon restart".
# ---------------------------------------------------------------------------


def _fresh_service_for_reset(directories, registry, identity, backend):
    """A ``DaemonService`` whose one camera opens through the real
    ``_camera_for`` path -- unlike the ``service``/``held_camera``
    fixtures (whose ``Camera`` is constructed by hand, bypassing
    ``_camera_for``'s ``config_loader``/``config_path`` wiring), so
    ``reset_camera_config`` actually exercises a real on-disk read."""
    slug = registry.resolve(identity)
    cameras: Dict[str, Camera] = {}
    discovery = CameraDiscovery(
        registry,
        active_slugs=lambda: frozenset(cameras.keys()),
        identity_source=lambda: {0: identity},
        backend_factory=lambda index: FakeBackend(),
    )
    service = DaemonService(
        registry=registry,
        discovery=discovery,
        directories=directories,
        cameras=cameras,
        identity_source=lambda: {0: identity},
        backend_factory=lambda index: backend,
        control_backend_factory=lambda device_name: None,
    )
    return service, slug


def test_reset_camera_config_reapplies_the_real_on_disk_document_discarding_a_live_override(
    directories, registry
):
    """The round trip that matters (ticket 023-004's Definition of Done
    #2): push an experimental setting, confirm it took, reset, confirm
    the *on-disk* values are back in force -- verified by backend
    readback, never a setter's return value."""
    identity = _identity("hw:resettable", "Resettable Camera")
    backend = FakeBackend(read_queue=[_frame()])
    service, slug = _fresh_service_for_reset(directories, registry, identity, backend)

    config_path = directories.config_dir / "cameras" / slug / "config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "name": slug,
                "playfield": "main-playfield",
                "settings": {"auto_exposure": 1, "exposure": 50, "gain": 3},
            }
        )
    )

    service.get_frame(slug)  # first open -- wires config_loader/config_path onto Camera
    service.set_camera_config(
        CameraConfig(
            name=slug,
            playfield="main-playfield",
            settings={"auto_exposure": 1, "exposure": 999, "gain": 99},
        )
    )
    assert backend.get_property("gain") == 99.0  # the live override took

    service.reset_camera_config(slug)

    assert backend.get_property("gain") == 3.0  # on-disk value back in force
    assert backend.get_property("exposure") == 50.0

    fetched = service.get_camera_config(slug)
    assert fetched.playfield == "main-playfield"
    assert fetched.settings == {"auto_exposure": 1, "exposure": 50, "gain": 3}


def test_reset_camera_config_with_no_installed_file_falls_back_to_blank_template(
    directories, registry
):
    """SUC-003's other acceptance criterion: a live override with
    nothing installed in ``config_dir`` falls back to the blank
    template rather than erroring -- and does not touch hardware
    settings it has nothing real to revert them to (Decision 3)."""
    identity = _identity("hw:unreset", "Unreset Camera")
    backend = FakeBackend(read_queue=[_frame()])
    service, slug = _fresh_service_for_reset(directories, registry, identity, backend)

    service.get_frame(slug)
    service.set_camera_config(
        CameraConfig(name=slug, playfield="main-playfield", settings={"gain": 99})
    )
    assert backend.get_property("gain") == 99.0

    service.reset_camera_config(slug)  # no error -- nothing installed on disk

    assert backend.get_property("gain") == 99.0  # nothing to revert to -- unchanged
    fetched = service.get_camera_config(slug)
    assert fetched.playfield is None  # the discarded override is gone
    assert fetched.settings["gain"] is None
    assert fetched.settings["device_name"] == "Unreset Camera"


def test_reset_camera_config_no_daemon_restart_involved(directories, registry):
    """SUC-003's third acceptance criterion, made concrete: the same
    already-open backend handle serves both before and after reset --
    the backend is never closed/reopened, unlike a real daemon
    restart."""
    identity = _identity("hw:samecam", "Same Camera")
    backend = FakeBackend(read_queue=[_frame()])
    service, slug = _fresh_service_for_reset(directories, registry, identity, backend)
    service.get_frame(slug)
    assert backend.open_calls == 1

    service.reset_camera_config(slug)

    assert backend.open_calls == 1  # no reopen
    assert backend.close_calls == 0  # no close
    assert service.active_slugs() == {slug}


def test_reset_camera_config_unknown_camera_raises_unknown_camera(service):
    with pytest.raises(UnknownCamera) as excinfo:
        service.reset_camera_config("never-registered")
    assert excinfo.value.requested == "never-registered"


def test_reset_camera_config_absent_registered_camera_raises_camera_not_present(
    service, absent_camera
):
    slug, _number = absent_camera
    with pytest.raises(CameraNotPresent) as excinfo:
        service.reset_camera_config(slug)
    assert excinfo.value.slug == slug


def test_fresh_daemon_service_instance_has_no_playfields_or_camera_links(
    directories, registry, held_camera
):
    """UC-02's ephemeral rule: a simulated restart (fresh ``DaemonService``
    over the same registry/directories) starts with zero pushed
    playfields and zero camera links -- disk, not RPC, is the source of
    record, and this ticket writes nothing to disk."""
    _, slug, cam = held_camera
    cameras: Dict[str, Camera] = {slug: cam}
    discovery = CameraDiscovery(
        registry,
        active_slugs=lambda: frozenset(cameras.keys()),
        identity_source=lambda: {},
        backend_factory=lambda index: FakeBackend(),
    )
    original = DaemonService(registry=registry, discovery=discovery, directories=directories, cameras=cameras)
    original.set_playfield(_playfield_definition())
    original.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))
    assert original.list_playfields() != ()

    restarted = DaemonService(
        registry=registry, discovery=discovery, directories=directories, cameras=cameras
    )

    assert restarted.list_playfields() == ()
    assert restarted.list_cameras()[0].playfield is None
    # The pushed config itself is gone (UC-02's ephemeral rule) -- this
    # test shares `cameras` with `original` on purpose, to isolate "is
    # the config-service map ephemeral" from camera-open state, so the
    # still-held-open camera correctly reports the blank template
    # (ticket 023-005's held-open fallback), not the discarded push and
    # not an error.
    fetched = restarted.get_camera_config(slug)
    assert fetched.playfield is None
    assert all(
        value is None
        for key, value in fetched.settings.items()
        if key not in ("device_name", "frame_width", "frame_height")
    )


# ---------------------------------------------------------------------------
# Playfields / camera config -- RPC round trips (ticket 022-001)
# ---------------------------------------------------------------------------


def _playfield_definition_msg(
    name: str = "main-playfield", aruco_ids: tuple = (1, 2, 3)
) -> aprilcam_v2_pb2.PlayfieldDefinition:
    """A ``PlayfieldDefinition`` proto message, built by hand -- matches
    this file's existing convention of constructing request messages
    directly (e.g. ``aprilcam_v2_pb2.TagId(...)`` above) rather than
    importing ``daemon/rpc.py``'s private converters into a test."""
    return aprilcam_v2_pb2.PlayfieldDefinition(
        name=name,
        width_cm=100.0,
        height_cm=80.0,
        aruco_tags=[
            aprilcam_v2_pb2.PlayfieldFeatureEntry(slug=f"u{i}", x=float(i), y=float(-i), id=i)
            for i in aruco_ids
        ],
    )


def _camera_config_msg(
    name: str, *, playfield: Optional[str] = None, settings: Optional[dict] = None
) -> aprilcam_v2_pb2.CameraConfig:
    msg = aprilcam_v2_pb2.CameraConfig(name=name, settings_json=json.dumps(settings or {}))
    if playfield is not None:
        msg.playfield = playfield
    return msg


def test_rpc_set_playfield_and_get_playfield_round_trip(stub):
    stub.SetPlayfield(aprilcam_v2_pb2.SetPlayfieldRequest(definition=_playfield_definition_msg()))

    response = stub.GetPlayfield(aprilcam_v2_pb2.GetPlayfieldRequest(name="main-playfield"))

    assert response.playfield.name == "main-playfield"
    definition = response.playfield.definition
    assert definition.width_cm == 100.0 and definition.height_cm == 80.0
    assert sorted(e.id for e in definition.aruco_tags) == [1, 2, 3]
    assert list(response.playfield.linked_cameras) == []


def test_rpc_list_playfields_round_trip_holds_two(stub):
    stub.SetPlayfield(
        aprilcam_v2_pb2.SetPlayfieldRequest(definition=_playfield_definition_msg("field-a", (1,)))
    )
    stub.SetPlayfield(
        aprilcam_v2_pb2.SetPlayfieldRequest(definition=_playfield_definition_msg("field-b", (2, 3)))
    )

    response = stub.ListPlayfields(aprilcam_v2_pb2.ListPlayfieldsRequest())

    assert {p.name for p in response.playfields} == {"field-a", "field-b"}


def test_rpc_get_playfield_unknown_name_maps_to_not_found(stub):
    with pytest.raises(grpc.RpcError) as excinfo:
        stub.GetPlayfield(aprilcam_v2_pb2.GetPlayfieldRequest(name="no-such-field"))

    assert excinfo.value.code() == grpc.StatusCode.NOT_FOUND
    metadata = dict(excinfo.value.trailing_metadata())
    assert metadata["aprilcam-error-type"] == "UnknownPlayfield"
    assert metadata["aprilcam-requested"] == "no-such-field"


def test_rpc_get_playfield_reports_a_linked_camera(stub, held_camera):
    _, slug, _ = held_camera
    stub.SetPlayfield(aprilcam_v2_pb2.SetPlayfieldRequest(definition=_playfield_definition_msg()))
    stub.SetCameraConfig(
        aprilcam_v2_pb2.SetCameraConfigRequest(
            config=_camera_config_msg(slug, playfield="main-playfield")
        )
    )

    response = stub.GetPlayfield(aprilcam_v2_pb2.GetPlayfieldRequest(name="main-playfield"))

    assert list(response.playfield.linked_cameras) == [slug]


# ---------------------------------------------------------------------------
# FindFeature -- RPC round trips (ticket 025-002, UC-08)
# ---------------------------------------------------------------------------


def _playfield_definition_msg_with_dots() -> aprilcam_v2_pb2.PlayfieldDefinition:
    """A hand-built proto message (mirroring ``_playfield_definition_msg``'s
    existing convention above) that includes the ``dots`` section that
    helper does not -- ``FindFeature``'s round trip needs findable
    coloured features, not just ArUco ids, to exercise UC-08.S-1/S-2/S-3
    over the real wire."""
    return aprilcam_v2_pb2.PlayfieldDefinition(
        name="main-playfield",
        width_cm=134.3,
        height_cm=89.3,
        dots=[
            aprilcam_v2_pb2.PlayfieldFeatureEntry(slug="dot-west-blue", x=-50.0, y=0.0, colour="blue"),
            aprilcam_v2_pb2.PlayfieldFeatureEntry(
                slug="dot-northwest-orange", x=-50.0, y=30.0, colour="orange"
            ),
            aprilcam_v2_pb2.PlayfieldFeatureEntry(
                slug="dot-northeast-orange", x=50.0, y=30.0, colour="orange"
            ),
            aprilcam_v2_pb2.PlayfieldFeatureEntry(
                slug="dot-southeast-orange", x=50.0, y=-30.0, colour="orange"
            ),
            aprilcam_v2_pb2.PlayfieldFeatureEntry(
                slug="dot-southwest-orange", x=-50.0, y=-30.0, colour="orange"
            ),
        ],
    )


def test_rpc_find_feature_unique_match_round_trips_slug_kind_colour_and_position(stub):
    stub.SetPlayfield(
        aprilcam_v2_pb2.SetPlayfieldRequest(definition=_playfield_definition_msg_with_dots())
    )

    response = stub.FindFeature(
        aprilcam_v2_pb2.FindFeatureRequest(playfield="main-playfield", query="the blue dot")
    )

    assert len(response.features) == 1
    match = response.features[0]
    assert match.slug == "dot-west-blue"
    assert match.kind == "dot"
    assert match.colour == "blue"
    assert (match.position.x, match.position.y) == (-50.0, 0.0)


def test_rpc_find_feature_ambiguous_query_round_trips_all_four_orange_dots(stub):
    stub.SetPlayfield(
        aprilcam_v2_pb2.SetPlayfieldRequest(definition=_playfield_definition_msg_with_dots())
    )

    response = stub.FindFeature(
        aprilcam_v2_pb2.FindFeatureRequest(playfield="main-playfield", query="the orange dot")
    )

    assert {f.slug for f in response.features} == {
        "dot-northwest-orange",
        "dot-northeast-orange",
        "dot-southeast-orange",
        "dot-southwest-orange",
    }


def test_rpc_find_feature_no_match_round_trips_an_empty_list(stub):
    stub.SetPlayfield(
        aprilcam_v2_pb2.SetPlayfieldRequest(definition=_playfield_definition_msg_with_dots())
    )

    response = stub.FindFeature(
        aprilcam_v2_pb2.FindFeatureRequest(playfield="main-playfield", query="the orange triangle")
    )

    assert list(response.features) == []


def test_rpc_find_feature_unknown_playfield_maps_to_not_found(stub):
    with pytest.raises(grpc.RpcError) as excinfo:
        stub.FindFeature(
            aprilcam_v2_pb2.FindFeatureRequest(playfield="no-such-field", query="the blue dot")
        )

    assert excinfo.value.code() == grpc.StatusCode.NOT_FOUND
    metadata = dict(excinfo.value.trailing_metadata())
    assert metadata["aprilcam-error-type"] == "UnknownPlayfield"
    assert metadata["aprilcam-requested"] == "no-such-field"


def test_rpc_set_camera_config_and_get_camera_config_round_trip_settings(stub, held_camera):
    _, slug, _ = held_camera
    pushed = _camera_config_msg(slug, playfield="main-playfield", settings={"exposure": 100, "gain": 2.5})
    stub.SetCameraConfig(aprilcam_v2_pb2.SetCameraConfigRequest(config=pushed))

    response = stub.GetCameraConfig(aprilcam_v2_pb2.GetCameraConfigRequest(camera=slug))

    assert response.config.playfield == "main-playfield"
    assert json.loads(response.config.settings_json) == {"exposure": 100, "gain": 2.5}


def test_rpc_set_camera_config_and_get_camera_config_round_trip_camera_position(stub, held_camera):
    """Ticket 029-001, SUC-002: ``camera_position`` round-trips over a
    real gRPC channel."""
    _, slug, _ = held_camera
    pushed = _camera_config_msg(slug, playfield="main-playfield", settings={})
    pushed.camera_position.CopyFrom(
        aprilcam_v2_pb2.CameraPosition(
            nadir=aprilcam_v2_pb2.WorldPoint(x=3.05, y=-2.75), height_cm=128.25
        )
    )
    stub.SetCameraConfig(aprilcam_v2_pb2.SetCameraConfigRequest(config=pushed))

    response = stub.GetCameraConfig(aprilcam_v2_pb2.GetCameraConfigRequest(camera=slug))

    assert response.config.HasField("camera_position")
    assert response.config.camera_position.nadir.x == pytest.approx(3.05)
    assert response.config.camera_position.nadir.y == pytest.approx(-2.75)
    assert response.config.camera_position.height_cm == pytest.approx(128.25)


def test_rpc_get_camera_config_camera_position_absent_when_unset(stub, held_camera):
    """``HasField("camera_position")`` is false on the wire when
    unset -- never a fabricated zero-valued message."""
    _, slug, _ = held_camera
    pushed = _camera_config_msg(slug, playfield="main-playfield", settings={})
    stub.SetCameraConfig(aprilcam_v2_pb2.SetCameraConfigRequest(config=pushed))

    response = stub.GetCameraConfig(aprilcam_v2_pb2.GetCameraConfigRequest(camera=slug))

    assert not response.config.HasField("camera_position")


def test_rpc_get_camera_config_unknown_camera_maps_to_not_found(stub):
    with pytest.raises(grpc.RpcError) as excinfo:
        stub.GetCameraConfig(aprilcam_v2_pb2.GetCameraConfigRequest(camera="never-configured"))

    assert excinfo.value.code() == grpc.StatusCode.NOT_FOUND
    metadata = dict(excinfo.value.trailing_metadata())
    assert metadata["aprilcam-error-type"] == "UnknownCamera"


def test_rpc_reset_camera_config_round_trip_falls_back_to_blank_template(stub, held_camera):
    """``held_camera``'s ``Camera`` is constructed directly (bypassing
    ``_camera_for``'s ``config_loader``/``config_path`` wiring), so this
    exercises the "nothing installed on disk" branch over the real wire
    -- the on-disk-document branch is covered headlessly, at the
    ``DaemonService`` level, by
    ``test_reset_camera_config_reapplies_the_real_on_disk_document_discarding_a_live_override``
    above, where a ``Camera`` opened through the real ``_camera_for``
    path is needed for a file to actually be read."""
    _, slug, _ = held_camera
    pushed = _camera_config_msg(
        slug, playfield="main-playfield", settings={"exposure": 100, "gain": 2.5}
    )
    stub.SetCameraConfig(aprilcam_v2_pb2.SetCameraConfigRequest(config=pushed))

    stub.ResetCameraConfig(aprilcam_v2_pb2.ResetCameraConfigRequest(camera=slug))

    response = stub.GetCameraConfig(aprilcam_v2_pb2.GetCameraConfigRequest(camera=slug))
    assert not response.config.HasField("playfield")  # the discarded override's link is gone
    settings = json.loads(response.config.settings_json)
    assert settings["gain"] is None
    assert settings["exposure"] is None


def test_rpc_reset_camera_config_unknown_camera_maps_to_not_found(stub):
    with pytest.raises(grpc.RpcError) as excinfo:
        stub.ResetCameraConfig(
            aprilcam_v2_pb2.ResetCameraConfigRequest(camera="never-registered")
        )

    assert excinfo.value.code() == grpc.StatusCode.NOT_FOUND
    metadata = dict(excinfo.value.trailing_metadata())
    assert metadata["aprilcam-error-type"] == "UnknownCamera"


# ---------------------------------------------------------------------------
# Calibration (ticket 022-003) -- DaemonService.calibrate/get_calibration,
# calibration.json persistence/reload, RPC round trips.
#
# Real ArUco detection + a real homography solve throughout: every frame
# below is rendered by ``tests/aprilcam/calibration/test_calibration.py``'s
# ``render_playfield_frame`` (real cv2.aruco markers at known pixel
# positions), read back through a ``FakeBackend``-driven ``Camera`` exactly
# the way a real one would be -- the only thing "fake" here is the camera
# hardware, never the vision or the geometry.
# ---------------------------------------------------------------------------


def _open_camera_with_frames(
    registry: CameraRegistry,
    frames: List[np.ndarray],
    *,
    hardware_key: str = "hw:cal",
    device_name: str = "Cal Camera",
):
    """Like ``held_camera`` above, but with caller-chosen frames (real
    rendered ArUco images, for calibration) instead of blank ones --
    and returns the ``FakeBackend`` too, so a test can queue additional
    frames for a second ``calibrate()`` call (e.g. the failed-retry
    tests) without reaching into ``Camera``'s private internals."""
    identity = _identity(hardware_key, device_name)
    slug = registry.resolve(identity)
    number = registry.get(slug).number
    backend = FakeBackend(read_queue=list(frames))
    cam = Camera(slug, backend, number=number)
    cam.open()
    return identity, slug, cam, backend


def _service_with_camera(directories, registry, identity, slug, cam) -> DaemonService:
    cameras: Dict[str, Camera] = {slug: cam}
    discovery = CameraDiscovery(
        registry,
        active_slugs=lambda: frozenset(cameras.keys()),
        identity_source=lambda: {0: identity},
        backend_factory=lambda index: FakeBackend(),
    )
    return DaemonService(
        registry=registry,
        discovery=discovery,
        directories=directories,
        cameras=cameras,
        identity_source=lambda: {0: identity},
    )


def _calibration_path(directories: Directories, slug: str) -> Path:
    return directories.state_dir / "cameras" / slug / "calibration.json"


def test_calibrate_end_to_end_succeeds_and_persists_calibration_json(directories, registry):
    frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    identity, slug, cam, _backend = _open_camera_with_frames(registry, [frame])
    service = _service_with_camera(directories, registry, identity, slug, cam)
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))

    result = service.calibrate(slug)

    assert result.succeeded is True
    assert result.matched_ids == tuple(range(1, 11))
    assert result.missing_ids == ()

    calibration = service.get_calibration(slug)
    assert calibration is not None
    assert calibration.playfield == "main-playfield"
    assert calibration.matched_ids == tuple(range(1, 11))

    path = _calibration_path(directories, slug)
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["playfield"] == "main-playfield"
    assert data["matched_ids"] == list(range(1, 11))
    assert len(data["homography"]) == 3
    assert all(len(row) == 3 for row in data["homography"])


def test_calibrate_by_persistent_number_reaches_the_same_camera_as_by_slug(directories, registry):
    """Regression (team-lead verification, ticket 022-003 hotfix):
    ``camera calibrate 3`` failed with a misleading ``NoPlayfieldLinked``
    against a camera that *was* linked (per ``camera list``), because
    ``calibrate``/``get_calibration`` looked ``_camera_configs``/
    ``_calibrations`` up by the raw token -- both slug-keyed only, so a
    persistent number never matched. Number and slug addressing must
    reach the identical camera, the identical stored ``Calibration``,
    and the identical ``calibration.json`` file (not two separate
    entries, one keyed "3")."""
    frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    identity, slug, cam, _backend = _open_camera_with_frames(registry, [frame])
    number = registry.get(slug).number
    service = _service_with_camera(directories, registry, identity, slug, cam)
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))

    result = service.calibrate(str(number))

    assert result.succeeded is True
    assert result.matched_ids == tuple(range(1, 11))

    by_number = service.get_calibration(str(number))
    by_slug = service.get_calibration(slug)
    assert by_number is not None
    assert by_number == by_slug

    # Exactly one file, under the canonical slug -- never a second one
    # keyed by the literal number token.
    assert _calibration_path(directories, slug).exists()
    assert not _calibration_path(directories, str(number)).exists()


def test_calibrate_failure_does_not_write_calibration_json(directories, registry):
    """UC-04.S-2: one id occluded -> explicit failure, no calibration
    stored on disk or in memory."""
    positions = dict(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    del positions[6]
    frame = render_playfield_frame(positions)
    identity, slug, cam, _backend = _open_camera_with_frames(registry, [frame])
    service = _service_with_camera(directories, registry, identity, slug, cam)
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))

    result = service.calibrate(slug)

    assert result.succeeded is False
    assert result.missing_ids == (6,)
    assert service.get_calibration(slug) is None
    assert not _calibration_path(directories, slug).exists()


def test_a_failed_retry_leaves_calibration_json_byte_identical(directories, registry):
    """The Definition of Done's own acceptance criterion: a failed
    retry never overwrites or clears an existing good
    ``calibration.json`` -- checked at the byte level, not merely "some
    calibration still exists"."""
    good_frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    identity, slug, cam, backend = _open_camera_with_frames(registry, [good_frame])
    service = _service_with_camera(directories, registry, identity, slug, cam)
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))

    first_result = service.calibrate(slug)
    assert first_result.succeeded is True
    path = _calibration_path(directories, slug)
    before = path.read_bytes()

    partial_positions = dict(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    del partial_positions[3]
    backend._queue.append(render_playfield_frame(partial_positions))

    retry_result = service.calibrate(slug)
    assert retry_result.succeeded is False
    assert retry_result.missing_ids == (3,)

    after = path.read_bytes()
    assert after == before
    # The in-memory cache is untouched too, not just the file.
    assert service.get_calibration(slug) == Calibration.from_dict(json.loads(before))


def test_calibrate_with_no_playfield_link_raises_before_touching_camera_backend(
    directories, registry
):
    """UC-15.S-2: a precondition failure, not a calibration failure --
    proven with an exploding ``backend_factory`` so any attempt to open
    a camera backend fails the test loudly rather than being merely
    unasserted."""

    def exploding_backend_factory(index: int):
        raise AssertionError("must not open a camera backend when NoPlayfieldLinked applies")

    discovery = CameraDiscovery(
        registry,
        active_slugs=lambda: frozenset(),
        identity_source=lambda: {},
        backend_factory=exploding_backend_factory,
    )
    service = DaemonService(
        registry=registry,
        discovery=discovery,
        directories=directories,
        backend_factory=exploding_backend_factory,
    )

    with pytest.raises(NoPlayfieldLinked) as excinfo:
        service.calibrate("never-configured-camera")

    assert excinfo.value.camera == "never-configured-camera"


def test_calibrate_camera_linked_but_playfield_never_pushed_raises_no_playfield_linked(
    directories, registry, held_camera
):
    """A camera config names a playfield the daemon does not currently
    hold -- the same precondition class as no link at all."""
    _, slug, cam = held_camera
    cameras: Dict[str, Camera] = {slug: cam}
    discovery = CameraDiscovery(
        registry,
        active_slugs=lambda: frozenset(cameras.keys()),
        identity_source=lambda: {},
        backend_factory=lambda index: FakeBackend(),
    )
    service = DaemonService(
        registry=registry, discovery=discovery, directories=directories, cameras=cameras
    )
    service.set_camera_config(CameraConfig(name=slug, playfield="ghost-field", settings={}))

    with pytest.raises(NoPlayfieldLinked):
        service.calibrate(slug)


def test_fresh_daemon_service_reloads_calibration_json_after_a_simulated_restart(
    directories, registry
):
    """UC-01: calibration is file-backed and survives a restart, unlike
    pushed playfields/camera configs (in-memory, ephemeral)."""
    frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    identity, slug, cam, _backend = _open_camera_with_frames(registry, [frame])
    service = _service_with_camera(directories, registry, identity, slug, cam)
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))
    service.calibrate(slug)
    original = service.get_calibration(slug)
    assert original is not None

    fresh_discovery = CameraDiscovery(
        registry,
        active_slugs=lambda: frozenset(),
        identity_source=lambda: {},
        backend_factory=lambda index: FakeBackend(),
    )
    restarted = DaemonService(registry=registry, discovery=fresh_discovery, directories=directories)

    reloaded = restarted.get_calibration(slug)

    assert reloaded == original  # provenance (calibrated_at, playfield) and homography, byte for byte
    assert reloaded.calibrated_at == original.calibrated_at
    assert reloaded.playfield == "main-playfield"
    # UC-02's ephemeral rule still holds for everything else -- only
    # calibration is file-backed.
    assert restarted.list_playfields() == ()
    with pytest.raises(UnknownCamera):
        restarted.get_camera_config(slug)


def test_fresh_daemon_service_with_no_calibration_file_reports_none(directories, registry):
    discovery = CameraDiscovery(
        registry, active_slugs=lambda: frozenset(), identity_source=lambda: {}, backend_factory=lambda index: FakeBackend()
    )
    service = DaemonService(registry=registry, discovery=discovery, directories=directories)

    assert service.get_calibration("never-calibrated-camera") is None


# ---------------------------------------------------------------------------
# Calibration staleness reachable (ticket 023-007) -- the two triggers
# (opportunistic drift, camera reopen), _mark_stale's locking, staleness
# reaching ImageFrame, and re-calibration clearing it.
# ---------------------------------------------------------------------------


def test_camera_reopen_via_self_heal_marks_the_calibration_stale_through_real_wiring(
    directories, registry
):
    """Sprint.md Decision 6, exercised through ``DaemonService._camera_for``'s
    own ``Camera(...)`` construction -- not a directly-injected ``Camera``
    the way ``held_camera``/``_open_camera_with_frames`` build one for
    every other test in this file. Those fixtures bypass ``_camera_for``
    entirely, so they would never exercise the
    ``on_reopen=lambda: self._mark_stale(...)`` wiring this ticket adds
    there -- ``test_camera.py``'s own ``on_reopen`` tests already cover
    the hook's *mechanism* in isolation; this is the one place the real
    production wiring between ``Camera`` and ``DaemonService`` is
    checked."""
    frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    identity = _identity("hw:reopen", "Reopen Camera")
    slug = registry.resolve(identity)
    backend = FakeBackend(read_queue=[frame] * CALIBRATION_FRAME_COUNT)
    discovery = CameraDiscovery(
        registry,
        active_slugs=lambda: frozenset(),
        identity_source=lambda: {0: identity},
        backend_factory=lambda index: backend,
    )
    service = DaemonService(
        registry=registry,
        discovery=discovery,
        directories=directories,
        identity_source=lambda: {0: identity},
        backend_factory=lambda index: backend,
    )
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))
    assert service.calibrate(slug).succeeded is True
    assert service.get_calibration(slug).stale is False

    cam = service._camera_for(slug)  # noqa: SLF001 - internal; the same Camera calibrate() opened
    opens_before = backend.open_calls
    for _ in range(FAILURE_THRESHOLD):
        cam.read()  # the queue is now empty -- every read fails; the 5th triggers self-heal

    assert backend.open_calls == opens_before + 1  # the reopen genuinely happened
    calibration = service.get_calibration(slug)
    assert calibration is not None
    assert calibration.stale is True


def test_mark_stale_is_thread_safe_under_concurrent_calls(directories, registry):
    """AC: 'Concurrent mark-stale calls... do not corrupt
    self._calibrations -- guarded by self._lock.' Hammers ``_mark_stale``
    from several threads at once (the drift check and the reopen hook
    can both fire from whatever RPC thread is servicing them, per
    ``daemon/rpc.py``'s ``ThreadPoolExecutor``) and checks both that
    nothing raises and that the store ends up with exactly one, fully-
    formed, stale entry for the camera -- never a torn or duplicated
    one."""
    frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    identity, slug, cam, _backend = _open_camera_with_frames(registry, [frame])
    service = _service_with_camera(directories, registry, identity, slug, cam)
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))
    assert service.calibrate(slug).succeeded is True

    errors: List[BaseException] = []

    def hammer() -> None:
        try:
            for _ in range(200):
                service._mark_stale(slug)  # noqa: SLF001 - internal; the concurrency target itself
        except BaseException as exc:  # pragma: no cover - would fail the test either way
            errors.append(exc)

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    calibration = service.get_calibration(slug)
    assert calibration is not None
    assert calibration.stale is True
    assert list(service._calibrations.keys()) == [slug]  # noqa: SLF001 - exactly one entry, no corruption


def test_get_frame_raw_reports_calibration_stale_after_mark_stale(directories, registry):
    frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    identity, slug, cam, _backend = _open_camera_with_frames(
        registry, [frame] * (CALIBRATION_FRAME_COUNT + 2)
    )
    service = _service_with_camera(directories, registry, identity, slug, cam)
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))
    assert service.calibrate(slug).succeeded is True

    before = service.get_frame(slug)
    assert before.calibration_stale is False

    service._mark_stale(slug)  # noqa: SLF001

    after = service.get_frame(slug)
    assert after.calibration_stale is True


def test_get_frame_deskewed_reports_calibration_stale_after_mark_stale(directories, registry):
    frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    identity, slug, cam, _backend = _open_camera_with_frames(
        registry, [frame] * (CALIBRATION_FRAME_COUNT + 2)
    )
    service = _service_with_camera(directories, registry, identity, slug, cam)
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))
    assert service.calibrate(slug).succeeded is True

    before = service.get_frame(slug, deskewed=True)
    assert before.calibration_stale is False

    service._mark_stale(slug)  # noqa: SLF001

    after = service.get_frame(slug, deskewed=True)
    assert after.calibration_stale is True


def test_recalibrating_a_stale_camera_clears_stale(directories, registry):
    """AC: re-calibrating a stale camera clears ``stale`` -- already true
    by construction (``calibrate()`` always writes ``stale=False`` on a
    fresh success), pinned explicitly now that staleness is actually
    reachable rather than assumed permanently ``False``."""
    frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    identity, slug, cam, _backend = _open_camera_with_frames(
        registry, [frame] * (CALIBRATION_FRAME_COUNT * 2)
    )
    service = _service_with_camera(directories, registry, identity, slug, cam)
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))
    assert service.calibrate(slug).succeeded is True

    service._mark_stale(slug)  # noqa: SLF001
    stale_calibration = service.get_calibration(slug)
    assert stale_calibration is not None
    assert stale_calibration.stale is True

    result = service.calibrate(slug)

    assert result.succeeded is True
    fresh_calibration = service.get_calibration(slug)
    assert fresh_calibration is not None
    assert fresh_calibration.stale is False


def test_end_to_end_drift_via_the_real_daemon_service_wiring_flips_stale(directories, registry):
    """SUC-006's own scenario, headless: a bumped camera's calibration is
    flagged stale rather than continuing to serve confident wrong
    positions. This is the ticket's headless equivalent of physically
    moving camera 3 (a corner-shifted fixture frame, per the ticket's own
    documented alternative -- real hardware was not disturbed for this
    ticket; see the ticket notes), run through ``DaemonService``'s real,
    production-wired ``DetectionEngine`` (no ``detection=`` override --
    every other drift-check test in ``test_detection.py`` substitutes its
    own engine; this is the one place the default constructor wiring
    itself, ``playfield_lookup``/``mark_stale``, is exercised end to
    end)."""
    good_frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    shifted_positions = {
        tag_id: (x + 5.0, y) for tag_id, (x, y) in MAIN_PLAYFIELD_ARUCO_POSITIONS.items()
    }
    bumped_frame = render_playfield_frame(shifted_positions)
    identity, slug, cam, backend = _open_camera_with_frames(
        registry, [good_frame] * CALIBRATION_FRAME_COUNT
    )
    service = _service_with_camera(directories, registry, identity, slug, cam)
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))
    assert service.calibrate(slug).succeeded is True
    assert service.get_calibration(slug).stale is False

    backend._queue.append(bumped_frame)
    tag_frame = service.get_tags(slug)

    assert tag_frame.calibration_stale is True
    calibration = service.get_calibration(slug)
    assert calibration is not None
    assert calibration.stale is True
    state = next(s for s in service.list_cameras() if s.name == slug)
    assert state.calibration_stale is True


# ---------------------------------------------------------------------------
# World coordinates through DaemonService's real DetectionEngine wiring
# (ticket 022-004) -- get_tags on a calibrated camera
# ---------------------------------------------------------------------------


def test_get_tags_on_a_calibrated_camera_reports_real_world_coordinates(directories, registry):
    """The one-line production wiring this ticket adds --
    ``DaemonService``'s default ``DetectionEngine`` receives
    ``calibration_lookup=self.get_calibration`` -- exercised end to end:
    calibrate a camera, then ask ``get_tags`` for the very next burst
    against that same now-calibrated camera, and confirm the returned
    records carry real world coordinates rather than sprint 021's
    pixel-only shape. ``test_detection.py`` covers ``DetectionEngine`` in
    isolation with an injected ``calibration_lookup``; this is the one
    place the actual ``DaemonService`` wiring itself is checked, using
    the real (default, no ``detection=`` override) engine a production
    daemon process constructs.

    Ticket 024-006: the production ``DetectionEngine`` uses the default
    ``CONFIRMATION_STREAK`` (3, raised from 2 by ticket 024-007's
    real-hardware run), and ``calibrate()`` does not itself run any
    bursts through ``DetectionEngine`` (it captures its own frames
    directly), so the first ``get_tags`` call against this camera is
    this tag's first-ever sighting through the engine -- unconfirmed.
    ``get_tags`` is therefore called ``CONFIRMATION_STREAK`` times,
    against identical rendered frames, with the assertions made against
    the last (confirmed) call -- the "more honest fix" pattern the
    ticket prescribes for tests specifically about world-field
    behaviour."""
    frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    # CALIBRATION_FRAME_COUNT reads for calibrate(), plus CONFIRMATION_STREAK
    # more for get_tags' own bursts (all but the last unconfirmed) -- the
    # same rendered image serves all of them, since real ArUco
    # detection/homography is what's under test here, not distinct
    # per-call imagery.
    identity, slug, cam, _backend = _open_camera_with_frames(
        registry, [frame] * (CALIBRATION_FRAME_COUNT + CONFIRMATION_STREAK)
    )
    service = _service_with_camera(directories, registry, identity, slug, cam)
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))

    result = service.calibrate(slug)
    assert result.succeeded is True

    for _ in range(CONFIRMATION_STREAK - 1):
        service.get_tags(slug)
    tag_frame = service.get_tags(slug)

    aruco_1 = next(r for r in tag_frame.tags if r.tag == TagId(family=TagFamily.ARUCO, number=1))
    assert aruco_1.calibrated is True
    assert aruco_1.world is not None
    expected_x, expected_y = MAIN_PLAYFIELD_ARUCO_POSITIONS[1]
    assert aruco_1.world.x == pytest.approx(expected_x, abs=0.5)
    assert aruco_1.world.y == pytest.approx(expected_y, abs=0.5)
    assert aruco_1.world_corners is not None
    assert aruco_1.yaw_rad is not None


# ---------------------------------------------------------------------------
# Mobile-tag registry (ticket 025-004, UC-12): register_tag/unregister_tag/
# list_tag_parameters against the lightweight `service` fixture (held
# camera, no calibration held) -- pure registry behaviour, no detection
# burst involved.
# ---------------------------------------------------------------------------


def test_register_tag_then_list_tag_parameters_round_trips(service):
    tag = TagId(family=TagFamily.APRILTAG, number=7)
    params = MountParameters(mount_x=8.0, mount_y=3.0, mount_yaw_rad=0.5)

    service.register_tag(tag, params)

    # mount_z is 0.0 (unset) -- mount_z_applied is vacuously True
    # (ticket 030-004, SUC-004): nothing for parallax to withhold.
    assert service.list_tag_parameters() == {
        tag: MountRegistration(params=params, mount_z_applied=True)
    }


def test_register_tag_upserts_replacing_a_prior_registration_outright(service):
    tag = TagId(family=TagFamily.APRILTAG, number=7)
    service.register_tag(tag, MountParameters(mount_x=1.0))

    service.register_tag(tag, MountParameters(mount_x=2.0))

    assert service.list_tag_parameters() == {
        tag: MountRegistration(params=MountParameters(mount_x=2.0), mount_z_applied=True)
    }


def test_unregister_tag_removes_the_registration(service):
    tag = TagId(family=TagFamily.APRILTAG, number=7)
    service.register_tag(tag, MountParameters(mount_x=1.0))

    service.unregister_tag(tag)

    assert service.list_tag_parameters() == {}


def test_unregister_tag_absent_is_a_no_op(service):
    """Matches ``clear_annotations``'s established no-op-on-absent
    convention (sprint 024) -- must not raise."""
    tag = TagId(family=TagFamily.APRILTAG, number=7)

    service.unregister_tag(tag)

    assert service.list_tag_parameters() == {}


def test_list_tag_parameters_returns_a_snapshot_not_a_live_reference(service):
    tag = TagId(family=TagFamily.APRILTAG, number=7)
    service.register_tag(tag, MountParameters(mount_x=1.0))

    snapshot = service.list_tag_parameters()
    snapshot[TagId(family=TagFamily.ARUCO, number=1)] = MountRegistration(
        params=MountParameters(), mount_z_applied=True
    )

    assert service.list_tag_parameters() == {
        tag: MountRegistration(params=MountParameters(mount_x=1.0), mount_z_applied=True)
    }


def test_register_tag_mount_z_with_no_calibration_anywhere_is_rejected(service):
    """UC-12.S-5: mount_z set with no calibration present anywhere the
    tag could be observed -> reject (NotCalibrated), never silently
    stored-but-inert."""
    tag = TagId(family=TagFamily.APRILTAG, number=7)

    with pytest.raises(NotCalibrated):
        service.register_tag(tag, MountParameters(mount_z=20.0))

    assert service.list_tag_parameters() == {}


def test_register_tag_zero_mount_z_is_accepted_even_uncalibrated(service):
    """A *zero* mount_z (``MountParameters``' own default) never
    triggers the uncalibrated-rejection -- there is nothing for
    parallax to project in that case. ``mount_z_applied`` is vacuously
    True for the same reason (ticket 030-004, SUC-004)."""
    tag = TagId(family=TagFamily.APRILTAG, number=7)
    params = MountParameters(mount_x=8.0, mount_y=3.0, mount_z=0.0)

    warning = service.register_tag(tag, params)

    assert warning is None
    assert service.list_tag_parameters() == {
        tag: MountRegistration(params=params, mount_z_applied=True)
    }


def test_register_tag_partial_spec_mount_z_set_xy_unset_is_accepted_as_zero_offset(
    directories, registry
):
    """Partial mount spec (``mount_z`` set, ``mount_x``/``mount_y``
    unset) is accepted, treated as zero offset, not an error -- once a
    calibration exists to make ``mount_z`` meaningful in the first
    place (UC-12.S-5 is the only thing that can reject this spec).

    Ticket 030-004, SUC-004: this camera is calibrated but never
    ``locate``\\ d (no ``camera_position`` ever set) -- the exact
    calibrated-but-unlocated scenario this ticket's acceptance criteria
    ask for. Registration must still succeed with no error (no
    regression from tightening anything, per Decision 4), but the
    response now carries a non-``None`` warning, and a subsequent
    ``list_tag_parameters`` call reports ``mount_z_applied=False`` for
    this registration.
    """
    frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    identity, slug, cam, _backend = _open_camera_with_frames(registry, [frame])
    calibrated_service = _service_with_camera(directories, registry, identity, slug, cam)
    calibrated_service.set_playfield(main_playfield_definition())
    calibrated_service.set_camera_config(
        CameraConfig(name=slug, playfield="main-playfield", settings={})
    )
    result = calibrated_service.calibrate(slug)
    assert result.succeeded is True

    tag = TagId(family=TagFamily.APRILTAG, number=7)
    params = MountParameters(mount_z=15.0)  # mount_x/mount_y left at their own 0.0 default

    warning = calibrated_service.register_tag(tag, params)  # must not raise

    assert warning is not None
    assert "camera locate" in warning
    assert calibrated_service.list_tag_parameters() == {
        tag: MountRegistration(params=params, mount_z_applied=False)
    }


def test_mount_z_applied_flips_to_true_once_the_observing_camera_is_located(
    directories, registry
):
    """Mirrors ``test_mount_z_is_a_noop_until_camera_position_is_pushed_then_engages_with_no_restart``'s
    existing live-mechanism-proof shape (ticket 029-001/003), at this
    ticket's own unit/headless level (ticket 030-004, SUC-004): a
    registration made while calibrated-but-unlocated reports
    ``mount_z_applied=False`` and a warning; once the observing
    camera's ``camera_position`` is pushed (no re-registration, no
    daemon restart needed -- ``_camera_geometry_for`` is read live per
    call), the same registration's ``mount_z_applied`` flips to
    ``True`` with no further action.
    """
    frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    identity, slug, cam, _backend = _open_camera_with_frames(registry, [frame])
    calibrated_service = _service_with_camera(directories, registry, identity, slug, cam)
    calibrated_service.set_playfield(main_playfield_definition())
    calibrated_service.set_camera_config(
        CameraConfig(name=slug, playfield="main-playfield", settings={})
    )
    result = calibrated_service.calibrate(slug)
    assert result.succeeded is True

    tag = TagId(family=TagFamily.APRILTAG, number=7)
    params = MountParameters(mount_z=15.0)
    calibrated_service.register_tag(tag, params)
    assert calibrated_service.list_tag_parameters()[tag].mount_z_applied is False

    calibrated_service.set_camera_config(
        CameraConfig(
            name=slug,
            playfield="main-playfield",
            settings={},
            camera_position=CameraPosition(nadir=WorldPoint(x=0.0, y=0.0), height_cm=128.25),
        )
    )

    assert calibrated_service.list_tag_parameters() == {
        tag: MountRegistration(params=params, mount_z_applied=True)
    }
    # register_tag's own warning is also gone now, for a fresh registration.
    assert calibrated_service.register_tag(tag, params) is None


def test_register_tag_warning_absent_when_a_different_unrelated_camera_is_located(
    directories, registry
):
    """The 'any camera' scope (ticket 030-004, SUC-004, Decision 4):
    the *observing* camera (``slug``) is calibrated but never
    ``locate``\\ d, yet a wholly different, never-opened camera slug
    has a ``camera_position`` pushed for it -- mirrors
    ``NotCalibrated``'s own "any camera" precedent exactly (a mobile
    tag is not bound to one specific camera), so no warning is
    returned."""
    frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    identity, slug, cam, _backend = _open_camera_with_frames(registry, [frame])
    calibrated_service = _service_with_camera(directories, registry, identity, slug, cam)
    calibrated_service.set_playfield(main_playfield_definition())
    calibrated_service.set_camera_config(
        CameraConfig(name=slug, playfield="main-playfield", settings={})
    )
    result = calibrated_service.calibrate(slug)
    assert result.succeeded is True

    # An unrelated camera slug this service has never even opened.
    calibrated_service.set_camera_config(
        CameraConfig(
            name="some-other-camera",
            playfield="main-playfield",
            settings={},
            camera_position=CameraPosition(nadir=WorldPoint(x=1.0, y=2.0), height_cm=100.0),
        )
    )

    tag = TagId(family=TagFamily.APRILTAG, number=7)
    warning = calibrated_service.register_tag(tag, MountParameters(mount_z=15.0))

    assert warning is None
    assert calibrated_service.list_tag_parameters()[tag].mount_z_applied is True


def test_register_tag_zero_mount_z_never_warns_regardless_of_located_state(service):
    """Mirrors ``register_tag``'s existing ``NotCalibrated`` scoping
    (ticket 030-004, SUC-004): a zero ``mount_z`` (the default) never
    produces a warning, even against this fixture's fully uncalibrated,
    unlocated daemon -- there is nothing for parallax to project in
    that case."""
    tag = TagId(family=TagFamily.APRILTAG, number=7)

    warning = service.register_tag(tag, MountParameters())

    assert warning is None


def test_register_tag_for_a_tag_not_currently_visible_is_accepted(service):
    """A registration for a tag never yet detected is accepted -- the
    tag may appear later (UC-12's own failure-mode table)."""
    tag = TagId(family=TagFamily.APRILTAG, number=250)  # never detected by this service

    service.register_tag(tag, MountParameters(mount_x=1.0))

    assert tag in service.list_tag_parameters()


# ---------------------------------------------------------------------------
# Mount registry persistence (ticket 036-007): register_tag/unregister_tag
# write through to <state_dir>/mounts/registry.json and DaemonService.
# __init__ reloads it -- the identical tmp+fsync+os.replace write and
# tolerant-of-corruption load stance _persist_calibration/_load_calibrations
# already establish, applied here to a whole-registry file. "Simulated
# restart" below always means constructing a second, independent
# DaemonService against the SAME directories/registry -- exactly the
# pattern test_fresh_daemon_service_reloads_calibration_json_after_a_
# simulated_restart above already uses for calibration.
# ---------------------------------------------------------------------------


def test_register_tag_persists_the_expected_on_disk_shape(service, directories):
    """One concrete on-disk record: a flat JSON object keyed by
    ``"<family>:<number>"``, mapping to ``MountParameters``' fields --
    ``size_cm`` present only when set, mirroring
    ``Calibration.to_dict``'s "omit an optional field entirely rather
    than write JSON null" convention."""
    tag = TagId(family=TagFamily.APRILTAG, number=7)
    service.register_tag(
        tag, MountParameters(mount_x=8.0, mount_y=3.0, mount_yaw_rad=0.5, size_cm=10.0)
    )

    path = service._mounts_path()  # noqa: SLF001 - internal; asserting the write itself
    assert path == directories.state_dir / "mounts" / "registry.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {
        "apriltag:7": {
            "mount_x": 8.0,
            "mount_y": 3.0,
            "mount_z": 0.0,
            "mount_yaw_rad": 0.5,
            "size_cm": 10.0,
        }
    }


def test_mount_registration_survives_a_simulated_daemon_restart(directories, registry):
    """Definition of Done: register a mount, construct a second,
    independent ``DaemonService`` against the same ``state_dir`` (no
    ``register_tag`` call on it), and confirm it reports the *corrected*
    robot pose -- not the raw tag pose -- with no re-registration.
    ``mount_z`` stays at its ``0.0`` default (the offset/yaw half is
    what's end-to-end checkable here; see
    ``test_register_tag_corrects_get_tags_get_tag_and_history_then_
    unregister_reverts`` above for why)."""
    frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    identity, slug, cam, _backend = _open_camera_with_frames(
        registry, [frame] * (CALIBRATION_FRAME_COUNT + CONFIRMATION_STREAK + 3)
    )
    service = _service_with_camera(directories, registry, identity, slug, cam)
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))
    assert service.calibrate(slug).succeeded is True

    tag = TagId(family=TagFamily.ARUCO, number=1)
    for _ in range(CONFIRMATION_STREAK - 1):
        service.get_tags(slug)
    raw_frame = service.get_tags(slug)
    raw_record = next(r for r in raw_frame.tags if r.tag == tag)
    assert raw_record.world is not None
    assert raw_record.yaw_rad is not None

    mount = MountParameters(mount_x=8.0, mount_y=3.0, mount_yaw_rad=math.radians(30.0))
    service.register_tag(tag, mount)
    expected_position, expected_yaw = apply_mount_offset(raw_record.world, raw_record.yaw_rad, mount)

    # "Restart": a second DaemonService, freshly constructed against the
    # same directories/registry, with its own freshly-opened camera
    # (rendering the identical frame) -- register_tag is never called on
    # this instance.
    identity2, slug2, cam2, _backend2 = _open_camera_with_frames(
        registry, [frame] * (CONFIRMATION_STREAK + 3)
    )
    assert slug2 == slug  # same real identity resolves to the same registry slug
    restarted = _service_with_camera(directories, registry, identity2, slug2, cam2)

    for _ in range(CONFIRMATION_STREAK - 1):
        restarted.get_tags(slug)
    tag_frame = restarted.get_tags(slug)
    record = next(r for r in tag_frame.tags if r.tag == tag)
    assert record.world is not None
    assert record.world.x == pytest.approx(expected_position.x, abs=1e-6)
    assert record.world.y == pytest.approx(expected_position.y, abs=1e-6)
    assert record.yaw_rad == pytest.approx(expected_yaw, abs=1e-6)


def test_unregistered_mount_does_not_reappear_after_a_simulated_restart(directories, registry):
    """Register, then unregister -- the entry must be gone from *disk*,
    not merely from this process's memory: a second, independent
    ``DaemonService`` against the same ``state_dir`` reports the raw tag
    pose, and the tag is absent from its own ``list_tag_parameters``."""
    frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    identity, slug, cam, _backend = _open_camera_with_frames(
        registry, [frame] * (CALIBRATION_FRAME_COUNT + CONFIRMATION_STREAK + 3)
    )
    service = _service_with_camera(directories, registry, identity, slug, cam)
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))
    assert service.calibrate(slug).succeeded is True

    tag = TagId(family=TagFamily.ARUCO, number=1)
    for _ in range(CONFIRMATION_STREAK - 1):
        service.get_tags(slug)
    raw_frame = service.get_tags(slug)
    raw_record = next(r for r in raw_frame.tags if r.tag == tag)
    assert raw_record.world is not None

    service.register_tag(tag, MountParameters(mount_x=8.0, mount_y=3.0))
    service.unregister_tag(tag)

    identity2, slug2, cam2, _backend2 = _open_camera_with_frames(
        registry, [frame] * (CONFIRMATION_STREAK + 3)
    )
    restarted = _service_with_camera(directories, registry, identity2, slug2, cam2)

    for _ in range(CONFIRMATION_STREAK - 1):
        restarted.get_tags(slug)
    tag_frame = restarted.get_tags(slug)
    record = next(r for r in tag_frame.tags if r.tag == tag)
    assert record.world is not None
    assert record.world.x == pytest.approx(raw_record.world.x, abs=1e-6)
    assert record.world.y == pytest.approx(raw_record.world.y, abs=1e-6)
    assert tag not in restarted.list_tag_parameters()


def test_missing_mounts_registry_loads_cleanly_as_empty(directories, registry):
    """First run: no ``mounts/registry.json`` on disk at all -- daemon
    construction succeeds with an empty registry, no warning-as-error."""
    discovery = CameraDiscovery(
        registry,
        active_slugs=lambda: frozenset(),
        identity_source=lambda: {},
        backend_factory=lambda index: FakeBackend(),
    )

    service = DaemonService(registry=registry, discovery=discovery, directories=directories)

    assert service.list_tag_parameters() == {}
    assert not (directories.state_dir / "mounts" / "registry.json").exists()


def test_corrupt_mounts_registry_loads_cleanly_with_a_warning(directories, registry, caplog):
    """A corrupt (non-JSON) ``mounts/registry.json`` is tolerated --
    logged as a warning, never raised, daemon construction succeeds with
    an empty registry -- mirrors ``camera.registry.CameraRegistry``'s /
    ``_load_calibrations``'s "a corrupt file must never crash the
    daemon" stance."""
    path = directories.state_dir / "mounts" / "registry.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not valid json", encoding="utf-8")
    discovery = CameraDiscovery(
        registry,
        active_slugs=lambda: frozenset(),
        identity_source=lambda: {},
        backend_factory=lambda index: FakeBackend(),
    )

    with caplog.at_level(logging.WARNING):
        service = DaemonService(registry=registry, discovery=discovery, directories=directories)

    assert service.list_tag_parameters() == {}
    assert any("corrupt mount registry" in record.message for record in caplog.records)


def test_mounts_registry_with_malformed_entries_skips_them_and_keeps_the_rest(
    directories, registry, caplog
):
    """A well-formed JSON object whose individual entries are malformed
    (bad key, non-object value) has each bad entry logged as a warning
    and skipped -- the good entry alongside them still loads, and
    daemon construction succeeds."""
    path = directories.state_dir / "mounts" / "registry.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "apriltag:7": {"mount_x": 1.0},
                "not-a-valid-key": {"mount_x": 2.0},
                "aruco:1": "not an object",
            }
        ),
        encoding="utf-8",
    )
    discovery = CameraDiscovery(
        registry,
        active_slugs=lambda: frozenset(),
        identity_source=lambda: {},
        backend_factory=lambda index: FakeBackend(),
    )

    with caplog.at_level(logging.WARNING):
        service = DaemonService(registry=registry, discovery=discovery, directories=directories)

    params = service.list_tag_parameters()
    good_tag = TagId(family=TagFamily.APRILTAG, number=7)
    assert set(params) == {good_tag}
    assert params[good_tag].params.mount_x == 1.0
    malformed_warnings = [
        r for r in caplog.records if "malformed mount registry entry" in r.message
    ]
    assert len(malformed_warnings) == 2


def test_persisted_nonzero_mount_z_reloads_without_calibration_and_reports_inert(
    directories, registry
):
    """The trickiest acceptance criterion: a persisted nonzero-``mount_z``
    registration reloaded into a ``DaemonService`` with **no**
    calibration at all must not raise ``NotCalibrated`` and must not be
    dropped -- it behaves exactly like a fresh registration into that
    same daemon state today (registration stands, height correction
    inert, ``mount_z_applied`` reports the truth). ``_load_mounts``
    populates ``self._mount_params`` directly, never via
    ``register_tag``, specifically so this scenario -- which
    ``register_tag`` itself would reject with ``NotCalibrated`` -- can
    exist on reload at all."""
    tag = TagId(family=TagFamily.APRILTAG, number=7)
    path = directories.state_dir / "mounts" / "registry.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"apriltag:7": {"mount_x": 1.0, "mount_z": 15.0}}), encoding="utf-8"
    )
    discovery = CameraDiscovery(
        registry,
        active_slugs=lambda: frozenset(),
        identity_source=lambda: {},
        backend_factory=lambda index: FakeBackend(),
    )

    # Must not raise NotCalibrated -- this daemon holds no calibration
    # for any camera at all, the exact condition register_tag() itself
    # would reject a fresh nonzero-mount_z registration for.
    service = DaemonService(registry=registry, discovery=discovery, directories=directories)

    params = service.list_tag_parameters()
    assert tag in params
    assert params[tag].params.mount_x == 1.0
    assert params[tag].params.mount_z == 15.0
    assert params[tag].mount_z_applied is False  # inert: no calibration, no located camera


def test_mount_z_applied_after_restart_reflects_the_new_processs_current_state(
    directories, registry
):
    """AC: ``mount_z_applied`` is unaffected by this ticket -- it is
    computed fresh from current calibration/location state at query
    time, never from anything persisted on the registration itself.
    Confirmed here rather than assumed: a registration that reported
    ``mount_z_applied=False`` (calibrated but unlocated) in the process
    that made it reports ``True`` after a "restart" whose fresh process
    happens to have its camera located before the first query -- proving
    the flag is recomputed per process/per query, not carried over from
    persistence."""
    frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    identity, slug, cam, _backend = _open_camera_with_frames(
        registry, [frame] * (CALIBRATION_FRAME_COUNT + 3)
    )
    service = _service_with_camera(directories, registry, identity, slug, cam)
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))
    assert service.calibrate(slug).succeeded is True

    tag = TagId(family=TagFamily.APRILTAG, number=7)
    warning = service.register_tag(tag, MountParameters(mount_z=15.0))
    assert warning is not None  # calibrated but not yet located
    assert service.list_tag_parameters()[tag].mount_z_applied is False

    identity2, slug2, cam2, _backend2 = _open_camera_with_frames(registry, [frame])
    restarted = _service_with_camera(directories, registry, identity2, slug2, cam2)
    restarted.set_camera_config(
        CameraConfig(
            name=slug2,
            playfield="main-playfield",
            settings={},
            camera_position=CameraPosition(nadir=WorldPoint(x=0.0, y=0.0), height_cm=128.25),
        )
    )

    assert restarted.list_tag_parameters()[tag].mount_z_applied is True


# ---------------------------------------------------------------------------
# Mobile-tag registry -- correction wired through the real DaemonService
# DetectionEngine (ticket 025-004, UC-12). test_detection.py covers the
# correction math itself, with an injected mount_lookup/
# camera_geometry_lookup, in isolation; these are the only two places the
# actual DaemonService wiring (mount_lookup=self._mount_lookup,
# camera_geometry_lookup=self._camera_geometry_for) is exercised end to
# end, using the real (default, no detection= override) DetectionEngine a
# production daemon process constructs.
# ---------------------------------------------------------------------------


def test_register_tag_corrects_get_tags_get_tag_and_history_then_unregister_reverts(
    directories, registry
):
    """Definition of Done: register a mount, confirm get_tags/get_tag/
    get_tag_history all report the corrected robot pose (not the raw
    tag pose), cross-checked against ``calibration.geometry.
    apply_mount_offset`` computed from the actually-reported raw pose;
    unregister and confirm reversion to raw. ``mount_z`` is left at its
    ``0.0`` default -- only the offset/yaw half is checkable end to end
    against a live (fake) camera today, since ``DaemonService``'s real
    ``_camera_geometry_for`` is an explicitly provisional stopgap that
    makes ``mount_z`` inert (see that method's own docstring;
    ``test_detection.py`` proves the parallax half of the math with an
    injected camera-geometry lookup instead)."""
    frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    identity, slug, cam, _backend = _open_camera_with_frames(
        registry, [frame] * (CALIBRATION_FRAME_COUNT + CONFIRMATION_STREAK + 3)
    )
    service = _service_with_camera(directories, registry, identity, slug, cam)
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))
    result = service.calibrate(slug)
    assert result.succeeded is True

    tag = TagId(family=TagFamily.ARUCO, number=1)
    for _ in range(CONFIRMATION_STREAK - 1):
        service.get_tags(slug)
    raw_frame = service.get_tags(slug)
    raw_record = next(r for r in raw_frame.tags if r.tag == tag)
    assert raw_record.world is not None
    assert raw_record.yaw_rad is not None

    mount = MountParameters(mount_x=8.0, mount_y=3.0, mount_yaw_rad=math.radians(30.0))
    service.register_tag(tag, mount)
    expected_position, expected_yaw = apply_mount_offset(raw_record.world, raw_record.yaw_rad, mount)

    tag_frame = service.get_tags(slug)
    record = next(r for r in tag_frame.tags if r.tag == tag)
    assert record.world is not None
    assert record.world.x == pytest.approx(expected_position.x, abs=1e-6)
    assert record.world.y == pytest.approx(expected_position.y, abs=1e-6)
    assert record.yaw_rad == pytest.approx(expected_yaw, abs=1e-6)

    single = service.get_tag(slug, tag)
    assert single is not None
    assert single.world is not None
    assert single.world.x == pytest.approx(expected_position.x, abs=1e-6)
    assert single.world.y == pytest.approx(expected_position.y, abs=1e-6)

    history = service.get_tag_history(slug, tag)
    latest_record = next(r for r in history[-1].tags if r.tag == tag)
    assert latest_record.world is not None
    assert latest_record.world.x == pytest.approx(expected_position.x, abs=1e-6)

    service.unregister_tag(tag)
    reverted_frame = service.get_tags(slug)
    reverted_record = next(r for r in reverted_frame.tags if r.tag == tag)
    assert reverted_record.world is not None
    assert reverted_record.world.x == pytest.approx(raw_record.world.x, abs=1e-6)
    assert reverted_record.world.y == pytest.approx(raw_record.world.y, abs=1e-6)
    assert reverted_record.yaw_rad == pytest.approx(raw_record.yaw_rad, abs=1e-6)


def test_register_tag_is_not_retroactive_buffered_history_keeps_raw_values(
    directories, registry
):
    """Resolved 2026-08-03 (sprint.md, ticket 003): mount-parameter
    changes are not retroactive. Every ``TagFrame`` already buffered
    before a registration is byte-identical afterward; only frames
    detected *after* the registration are corrected."""
    frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    identity, slug, cam, _backend = _open_camera_with_frames(
        registry, [frame] * (CALIBRATION_FRAME_COUNT + CONFIRMATION_STREAK + 2)
    )
    service = _service_with_camera(directories, registry, identity, slug, cam)
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))
    result = service.calibrate(slug)
    assert result.succeeded is True

    tag = TagId(family=TagFamily.ARUCO, number=1)
    for _ in range(CONFIRMATION_STREAK - 1):
        service.get_tags(slug)
    service.get_tags(slug)  # first confirmed, buffered burst -- raw (unregistered)

    history_before = service.get_tag_history(slug, tag)
    raw_record = next(r for r in history_before[-1].tags if r.tag == tag)
    assert raw_record.world is not None

    service.register_tag(tag, MountParameters(mount_x=8.0, mount_y=3.0))
    service.get_tags(slug)  # a new, corrected burst, appended after the registration

    history_after = service.get_tag_history(slug, tag)

    # Every frame buffered *before* the registration is byte-identical
    # afterward -- not corrected retroactively.
    assert history_after[: len(history_before)] == history_before
    # The newest frame (buffered after registration) really is corrected
    # -- proving the prefix-equality check above is not vacuously true
    # because nothing here ever actually changes.
    corrected_record = next(r for r in history_after[-1].tags if r.tag == tag)
    assert corrected_record.world != raw_record.world


# ---------------------------------------------------------------------------
# Mobile-tag registry -- real gRPC round trip (ticket 025-004, UC-12).
# ---------------------------------------------------------------------------


def test_rpc_register_tag_then_list_tag_parameters_round_trips(stub):
    tag_msg = aprilcam_v2_pb2.TagId(family=aprilcam_v2_pb2.APRILTAG, number=7)
    params_msg = aprilcam_v2_pb2.MountParameters(
        mount_x=8.0, mount_y=3.0, mount_z=0.0, mount_yaw_rad=0.5
    )

    register_response = stub.RegisterTag(
        aprilcam_v2_pb2.RegisterTagRequest(tag=tag_msg, params=params_msg)
    )
    response = stub.ListTagParameters(aprilcam_v2_pb2.ListTagParametersRequest())

    # mount_z == 0.0 -- no warning on RegisterTagResponse (ticket
    # 030-004, SUC-004), and mount_z_applied rides the wire additively
    # on TagMountEntry, vacuously True.
    assert not register_response.HasField("warning")
    assert len(response.entries) == 1
    entry = response.entries[0]
    assert entry.tag.family == aprilcam_v2_pb2.APRILTAG
    assert entry.tag.number == 7
    assert entry.params.mount_x == pytest.approx(8.0)
    assert entry.params.mount_y == pytest.approx(3.0)
    assert entry.params.mount_yaw_rad == pytest.approx(0.5)
    assert not entry.params.HasField("size_cm")
    assert entry.mount_z_applied is True


def test_rpc_register_tag_warning_round_trips_present_when_calibrated_but_unlocated(
    directories, registry
):
    """Wire round trip of the new additive ``RegisterTagResponse.warning``
    field (ticket 030-004, SUC-004): a calibrated-but-never-``locate``\\ d
    camera's nonzero ``mount_z`` registration comes back over gRPC with
    a present, non-empty warning naming ``camera locate``."""
    frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    identity, slug, cam, _backend = _open_camera_with_frames(registry, [frame])
    calibrated_service = _service_with_camera(directories, registry, identity, slug, cam)
    calibrated_service.set_playfield(main_playfield_definition())
    calibrated_service.set_camera_config(
        CameraConfig(name=slug, playfield="main-playfield", settings={})
    )
    assert calibrated_service.calibrate(slug).succeeded is True

    with _bind_stub(calibrated_service) as calibrated_stub:
        tag_msg = aprilcam_v2_pb2.TagId(family=aprilcam_v2_pb2.APRILTAG, number=7)
        params_msg = aprilcam_v2_pb2.MountParameters(mount_z=15.0)

        response = calibrated_stub.RegisterTag(
            aprilcam_v2_pb2.RegisterTagRequest(tag=tag_msg, params=params_msg)
        )

        assert response.HasField("warning")
        assert "camera locate" in response.warning

        entries = calibrated_stub.ListTagParameters(
            aprilcam_v2_pb2.ListTagParametersRequest()
        ).entries
        assert entries[0].mount_z_applied is False


def test_rpc_register_tag_with_size_cm_round_trips_present(stub):
    tag_msg = aprilcam_v2_pb2.TagId(family=aprilcam_v2_pb2.ARUCO, number=3)
    params_msg = aprilcam_v2_pb2.MountParameters(size_cm=6.5)

    stub.RegisterTag(aprilcam_v2_pb2.RegisterTagRequest(tag=tag_msg, params=params_msg))
    response = stub.ListTagParameters(aprilcam_v2_pb2.ListTagParametersRequest())

    entry = response.entries[0]
    assert entry.params.HasField("size_cm")
    assert entry.params.size_cm == pytest.approx(6.5)


def test_rpc_unregister_tag_round_trip(stub):
    tag_msg = aprilcam_v2_pb2.TagId(family=aprilcam_v2_pb2.APRILTAG, number=7)
    stub.RegisterTag(
        aprilcam_v2_pb2.RegisterTagRequest(
            tag=tag_msg, params=aprilcam_v2_pb2.MountParameters(mount_x=1.0)
        )
    )

    stub.UnregisterTag(aprilcam_v2_pb2.UnregisterTagRequest(tag=tag_msg))

    response = stub.ListTagParameters(aprilcam_v2_pb2.ListTagParametersRequest())
    assert list(response.entries) == []


def test_rpc_unregister_tag_absent_does_not_raise(stub):
    tag_msg = aprilcam_v2_pb2.TagId(family=aprilcam_v2_pb2.APRILTAG, number=42)

    stub.UnregisterTag(aprilcam_v2_pb2.UnregisterTagRequest(tag=tag_msg))  # must not raise


def test_rpc_list_tag_parameters_empty_when_nothing_registered(stub):
    response = stub.ListTagParameters(aprilcam_v2_pb2.ListTagParametersRequest())

    assert list(response.entries) == []


def test_rpc_register_tag_mount_z_with_no_calibration_maps_to_failed_precondition(stub):
    tag_msg = aprilcam_v2_pb2.TagId(family=aprilcam_v2_pb2.APRILTAG, number=7)
    params_msg = aprilcam_v2_pb2.MountParameters(mount_z=20.0)

    with pytest.raises(grpc.RpcError) as excinfo:
        stub.RegisterTag(aprilcam_v2_pb2.RegisterTagRequest(tag=tag_msg, params=params_msg))

    assert excinfo.value.code() == grpc.StatusCode.FAILED_PRECONDITION
    metadata = dict(excinfo.value.trailing_metadata())
    assert metadata["aprilcam-error-type"] == "NotCalibrated"


# ---------------------------------------------------------------------------
# Regression, ticket 022-007: list_cameras()/CameraState.calibrated must
# reflect the real calibration store, not a hardcoded False. Reported
# independently by tickets 005 and 006's team-lead verification --
# get_frame(deskewed=True)/get_tags/playfield view all read the real
# calibration correctly, but `camera list`/`daemon status` said
# "uncalibrated" for a camera that genuinely was calibrated.
# ---------------------------------------------------------------------------


def test_list_cameras_reports_calibrated_true_for_a_held_open_camera_after_calibrate(
    directories, registry
):
    """The 'held open' branch of ``list_cameras`` (the camera is in
    ``self._cameras`` -- exactly the state a just-calibrated camera is
    in, since :meth:`DaemonService.calibrate` opens it via
    ``_capture_calibration_frames``/``_camera_for``). Before the fix,
    this branch read ``Camera.state`` verbatim, which hardcoded
    ``calibrated=False`` unconditionally -- so a camera calibrated in
    the same process, still held open, reported uncalibrated forever."""
    frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    identity, slug, cam, _backend = _open_camera_with_frames(registry, [frame])
    service = _service_with_camera(directories, registry, identity, slug, cam)
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))

    before = next(s for s in service.list_cameras() if s.name == slug)
    assert before.calibrated is False
    assert before.calibration_stale is False

    result = service.calibrate(slug)
    assert result.succeeded is True

    after = next(s for s in service.list_cameras() if s.name == slug)
    assert after.calibrated is True
    assert after.calibration_stale is False


def test_list_cameras_reports_calibrated_true_for_a_not_held_camera_after_restart(
    directories, registry
):
    """The 'not held open' branch of ``list_cameras`` (no ``Camera`` in
    ``self._cameras`` -- exactly a freshly-restarted daemon's state for
    every camera until something opens it again). Before the fix, this
    branch built its ``CameraState`` with an explicit, separate
    ``calibrated=False`` literal -- so a calibration reloaded from
    ``calibration.json`` at startup (UC-01) was invisible to
    ``list_cameras`` even though :meth:`get_calibration` reported it
    correctly. Mirrors
    ``test_fresh_daemon_service_reloads_calibration_json_after_a_simulated_restart``'s
    restart simulation, but asserts on ``list_cameras`` instead of
    ``get_calibration`` directly -- the exact surface tickets 005/006's
    team-lead verification found wrong."""
    frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    identity, slug, cam, _backend = _open_camera_with_frames(registry, [frame])
    service = _service_with_camera(directories, registry, identity, slug, cam)
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))
    assert service.calibrate(slug).succeeded is True

    fresh_discovery = CameraDiscovery(
        registry,
        active_slugs=lambda: frozenset(),
        identity_source=lambda: {},
        backend_factory=lambda index: FakeBackend(),
    )
    restarted = DaemonService(registry=registry, discovery=fresh_discovery, directories=directories)

    state = next(s for s in restarted.list_cameras() if s.name == slug)
    assert state.calibrated is True
    assert state.calibration_stale is False


# ---------------------------------------------------------------------------
# Deskewed frames (ticket 022-005) -- Daemon.get_frame(deskewed=True), the
# real implementation replacing sprint 020's unconditional NotCalibrated.
# ---------------------------------------------------------------------------


def test_get_frame_deskewed_returns_a_real_deskewed_image_once_calibrated(directories, registry):
    # CALIBRATION_FRAME_COUNT reads for calibrate(), plus one more for
    # this test's own get_frame(deskewed=True) call.
    frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    identity, slug, cam, _backend = _open_camera_with_frames(
        registry, [frame] * (CALIBRATION_FRAME_COUNT + 1)
    )
    service = _service_with_camera(directories, registry, identity, slug, cam)
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))
    assert service.calibrate(slug).succeeded is True

    image = service.get_frame(slug, deskewed=True, encoding="png")

    assert image.deskewed is True
    assert image.width == 800
    assert image.height == 532
    assert image.px_per_cm == pytest.approx(800 / 134.3)
    assert image.world_extent == pytest.approx((134.3, 89.3))
    assert image.data[:8] == b"\x89PNG\r\n\x1a\n"  # PNG signature -- a real image, not a placeholder
    decoded = cv.imdecode(np.frombuffer(image.data, dtype=np.uint8), cv.IMREAD_COLOR)
    assert decoded.shape == (532, 800, 3)


def test_get_frame_deskewed_by_persistent_number_uses_the_calibrated_camera(directories, registry):
    """SUC-005 number-or-slug addressing, on the deskewed branch too --
    mirrors ticket 022-003's ``calibrate``/``get_calibration`` hotfix."""
    frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    identity, slug, cam, _backend = _open_camera_with_frames(
        registry, [frame] * (CALIBRATION_FRAME_COUNT + 1)
    )
    number = registry.get(slug).number
    service = _service_with_camera(directories, registry, identity, slug, cam)
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))
    assert service.calibrate(slug).succeeded is True

    image = service.get_frame(str(number), deskewed=True)

    assert image.deskewed is True
    assert image.width == 800 and image.height == 532


def test_get_frame_deskewed_embeds_the_latest_ring_buffer_entry_without_triggering_a_burst(
    directories, registry
):
    """Same 'get_frame never triggers its own detection burst, only
    embeds whatever is already buffered' contract as the raw path
    (sprint 021 Decision 4) -- carried through the deskewed branch."""
    # CALIBRATION_FRAME_COUNT reads for calibrate(), plus three more:
    # this test's own two get_frame(deskewed=True) calls and the one
    # get_tags() burst between them.
    frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    identity, slug, cam, _backend = _open_camera_with_frames(
        registry, [frame] * (CALIBRATION_FRAME_COUNT + 3)
    )
    service = _service_with_camera(directories, registry, identity, slug, cam)
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))
    assert service.calibrate(slug).succeeded is True

    before = service.get_frame(slug, deskewed=True)
    assert before.tags is None  # nothing has triggered a burst yet

    burst = service.get_tags(slug)
    after = service.get_frame(slug, deskewed=True)

    assert after.tags == burst


def test_get_frame_deskewed_uncalibrated_never_touches_the_camera_backend(directories, registry):
    """UC-06.S-3/UC-19.S-2, proven mechanically, not just documented: an
    exploding ``backend_factory`` means any attempt to open a camera
    backend fails the test loudly -- the same style
    ``test_calibrate_with_no_playfield_link_raises_before_touching_camera_backend``
    already established for ``calibrate``."""

    def exploding_backend_factory(index: int):
        raise AssertionError(
            "must not open a camera backend for an uncalibrated deskewed request"
        )

    discovery = CameraDiscovery(
        registry,
        active_slugs=lambda: frozenset(),
        identity_source=lambda: {},
        backend_factory=exploding_backend_factory,
    )
    service = DaemonService(
        registry=registry,
        discovery=discovery,
        directories=directories,
        backend_factory=exploding_backend_factory,
    )

    with pytest.raises(NotCalibrated):
        service.get_frame("never-registered-camera", deskewed=True)


def test_get_frame_deskewed_missing_playfield_raises_not_calibrated(directories, registry):
    """A calibration whose playfield the daemon no longer holds (the
    calibration reloaded from ``calibration.json`` at restart, but the
    playfield -- ephemeral, UC-02 -- was never re-pushed) cannot be
    deskewed either: there is no width_cm/height_cm to warp against.
    Reported as :class:`NotCalibrated`, not a new error type -- from a
    caller's point of view "this camera cannot currently be deskewed"
    is exactly what both cases mean. Proven camera-backend-untouched the
    same mechanical way as the fully-uncalibrated case above."""
    frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    identity, slug, cam, _backend = _open_camera_with_frames(registry, [frame])
    service = _service_with_camera(directories, registry, identity, slug, cam)
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))
    assert service.calibrate(slug).succeeded is True

    def exploding_backend_factory(index: int):
        raise AssertionError(
            "must not open a camera backend when the calibrated playfield is gone"
        )

    fresh_discovery = CameraDiscovery(
        registry,
        active_slugs=lambda: frozenset(),
        identity_source=lambda: {},
        backend_factory=exploding_backend_factory,
    )
    restarted = DaemonService(
        registry=registry,
        discovery=fresh_discovery,
        directories=directories,
        backend_factory=exploding_backend_factory,
    )
    assert restarted.get_calibration(slug) is not None  # reloaded from calibration.json
    assert restarted.list_playfields() == ()  # UC-02: never re-pushed this process

    with pytest.raises(NotCalibrated):
        restarted.get_frame(slug, deskewed=True)


# ---------------------------------------------------------------------------
# Calibration -- RPC round trips (ticket 022-003)
# ---------------------------------------------------------------------------


def test_rpc_calibrate_and_get_calibration_round_trip(directories, registry):
    frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    identity, slug, cam, _backend = _open_camera_with_frames(registry, [frame])
    service = _service_with_camera(directories, registry, identity, slug, cam)
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))

    with _bind_stub(service) as stub:
        calibrate_response = stub.Calibrate(aprilcam_v2_pb2.CalibrateRequest(camera=slug))
        assert calibrate_response.result.succeeded is True
        assert list(calibrate_response.result.matched_ids) == list(range(1, 11))
        assert calibrate_response.result.HasField("reprojection_error_cm")

        get_response = stub.GetCalibration(aprilcam_v2_pb2.GetCalibrationRequest(camera=slug))

    assert get_response.HasField("calibration")
    assert get_response.calibration.playfield == "main-playfield"
    assert get_response.calibration.stale is False
    assert len(get_response.calibration.homography) == 9
    assert list(get_response.calibration.matched_ids) == list(range(1, 11))


def test_rpc_calibrate_by_persistent_number_round_trip(directories, registry):
    """The exact live reproduction (team-lead verification): ``camera
    calibrate 3`` over the real wire, against a camera ``camera list``
    already shows linked, must succeed identically to addressing it by
    slug -- end to end through ``daemon/rpc.py``, not just
    ``DaemonService`` in-process."""
    frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    identity, slug, cam, _backend = _open_camera_with_frames(registry, [frame])
    number = registry.get(slug).number
    service = _service_with_camera(directories, registry, identity, slug, cam)
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))

    with _bind_stub(service) as stub:
        response = stub.Calibrate(aprilcam_v2_pb2.CalibrateRequest(camera=str(number)))

    assert response.result.succeeded is True
    assert list(response.result.matched_ids) == list(range(1, 11))


def test_rpc_calibrate_failure_names_missing_ids_exactly(directories, registry):
    positions = dict(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    del positions[6]
    frame = render_playfield_frame(positions)
    identity, slug, cam, _backend = _open_camera_with_frames(registry, [frame])
    service = _service_with_camera(directories, registry, identity, slug, cam)
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))

    with _bind_stub(service) as stub:
        response = stub.Calibrate(aprilcam_v2_pb2.CalibrateRequest(camera=slug))

    assert response.result.succeeded is False
    assert list(response.result.missing_ids) == [6]


def test_rpc_get_calibration_absent_when_never_calibrated(stub, held_camera):
    _, slug, _ = held_camera

    response = stub.GetCalibration(aprilcam_v2_pb2.GetCalibrationRequest(camera=slug))

    assert response.HasField("calibration") is False


def test_rpc_calibrate_no_playfield_linked_maps_to_failed_precondition(stub, held_camera):
    _, slug, _ = held_camera  # never linked to a playfield

    with pytest.raises(grpc.RpcError) as excinfo:
        stub.Calibrate(aprilcam_v2_pb2.CalibrateRequest(camera=slug))

    assert excinfo.value.code() == grpc.StatusCode.FAILED_PRECONDITION
    metadata = dict(excinfo.value.trailing_metadata())
    assert metadata["aprilcam-error-type"] == "NoPlayfieldLinked"
    assert metadata["aprilcam-camera"] == slug


# ---------------------------------------------------------------------------
# Opt-in intrinsics estimation (sprint 030 ticket 002) -- estimate_intrinsics
# plumbing end to end over the real RPC/proto wire, height_cm playfield
# round trip, and the confirmed carry-forward-after-restart bug fix.
# ---------------------------------------------------------------------------


def test_rpc_calibrate_estimate_intrinsics_true_stores_and_reports_intrinsics(directories, registry):
    frame = render_projective_frame(
        {i: (x, y, 0.0) for i, (x, y) in MAIN_PLAYFIELD_ARUCO_POSITIONS.items()}, tilt_deg=8.0
    )
    identity, slug, cam, _backend = _open_camera_with_frames(registry, [frame])
    service = _service_with_camera(directories, registry, identity, slug, cam)
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))

    with _bind_stub(service) as stub:
        response = stub.Calibrate(
            aprilcam_v2_pb2.CalibrateRequest(camera=slug, estimate_intrinsics=True)
        )

    assert response.result.succeeded is True, response.result.reason
    assert response.result.HasField("intrinsics_estimated")
    assert response.result.intrinsics_estimated is True
    assert response.result.intrinsics_mode == "coplanar-constrained"
    assert response.result.elevated_features_used == 0

    calibration = service.get_calibration(slug)
    assert calibration.camera_matrix is not None
    assert calibration.dist_coeffs is not None

    path = _calibration_path(directories, slug)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "camera_matrix" in data
    assert "dist_coeffs" in data


def test_rpc_calibrate_without_the_flag_reports_intrinsics_estimated_false(directories, registry):
    frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    identity, slug, cam, _backend = _open_camera_with_frames(registry, [frame])
    service = _service_with_camera(directories, registry, identity, slug, cam)
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))

    with _bind_stub(service) as stub:
        response = stub.Calibrate(aprilcam_v2_pb2.CalibrateRequest(camera=slug))

    assert response.result.succeeded is True
    assert response.result.HasField("intrinsics_estimated")
    assert response.result.intrinsics_estimated is False
    assert response.result.HasField("intrinsics_mode") is False
    assert response.result.HasField("elevated_features_used") is False


def test_rpc_calibrate_request_without_estimate_intrinsics_field_defaults_to_false(
    directories, registry
):
    """Proto3's own default -- an old client that has never heard of
    this field sends a request with it simply absent, which must behave
    identically to ``estimate_intrinsics=False`` explicitly."""
    frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    identity, slug, cam, _backend = _open_camera_with_frames(registry, [frame])
    service = _service_with_camera(directories, registry, identity, slug, cam)
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))

    with _bind_stub(service) as stub:
        request = aprilcam_v2_pb2.CalibrateRequest(camera=slug)
        response = stub.Calibrate(request)

    assert response.result.succeeded is True
    assert response.result.intrinsics_estimated is False


def test_rpc_estimate_intrinsics_flag_genuinely_reaches_the_daemon(directories, registry):
    """A structural proof the field actually crosses the wire, not just
    that the response happens to look right: a playfield with only 4
    surveyed ArUco points (below the n_pts >= 6 floor) succeeds under a
    plain ``Calibrate`` but fails, naming the floor, only when
    ``estimate_intrinsics=True`` is genuinely threaded through --
    something a silently-dropped field could never produce."""
    positions = {1: (-40.0, 30.0), 2: (40.0, 30.0), 3: (40.0, -30.0), 4: (-40.0, -30.0)}
    small_definition = PlayfieldDefinition(
        name="tiny-field",
        width_cm=120.0,
        height_cm=100.0,
        aruco_tags=tuple(
            PlayfieldFeatureEntry(slug=f"a{i}", x=x, y=y, tag_id=i) for i, (x, y) in positions.items()
        ),
    )
    frame = render_playfield_frame(positions, canvas_size=(700, 700))
    identity, slug, cam, _backend = _open_camera_with_frames(registry, [frame])
    service = _service_with_camera(directories, registry, identity, slug, cam)
    service.set_playfield(small_definition)
    service.set_camera_config(CameraConfig(name=slug, playfield="tiny-field", settings={}))

    with _bind_stub(service) as stub:
        plain = stub.Calibrate(aprilcam_v2_pb2.CalibrateRequest(camera=slug))
        assert plain.result.succeeded is True, plain.result.reason

        with_intrinsics = stub.Calibrate(
            aprilcam_v2_pb2.CalibrateRequest(camera=slug, estimate_intrinsics=True)
        )
    assert with_intrinsics.result.succeeded is False
    assert "6" in with_intrinsics.result.reason
    assert "4" in with_intrinsics.result.reason


def test_rpc_playfield_feature_height_cm_round_trips(directories, registry):
    """Sprint 030 ticket 002's other additive wire field: an elevated
    feature's ``height_cm`` survives a real ``SetPlayfield``/
    ``GetPlayfield`` round trip over the wire, and a coplanar one
    (absent/0.0) round-trips as exactly 0.0, never a fabricated value."""
    definition = PlayfieldDefinition(
        name="elevated-field",
        width_cm=200.0,
        height_cm=140.0,
        aruco_tags=(
            PlayfieldFeatureEntry(slug="a1", x=-40.0, y=30.0, tag_id=1, height_cm=12.5),
            PlayfieldFeatureEntry(slug="a2", x=40.0, y=30.0, tag_id=2),
        ),
    )
    identity, slug, cam, _backend = _open_camera_with_frames(registry, [_frame()])
    service = _service_with_camera(directories, registry, identity, slug, cam)

    with _bind_stub(service) as stub:
        stub.SetPlayfield(
            aprilcam_v2_pb2.SetPlayfieldRequest(
                definition=aprilcam_v2_pb2.PlayfieldDefinition(
                    name=definition.name,
                    width_cm=definition.width_cm,
                    height_cm=definition.height_cm,
                    aruco_tags=[
                        aprilcam_v2_pb2.PlayfieldFeatureEntry(
                            slug=e.slug, x=e.x, y=e.y, id=e.tag_id, height_cm=e.height_cm
                        )
                        for e in definition.aruco_tags
                    ],
                )
            )
        )
        response = stub.GetPlayfield(aprilcam_v2_pb2.GetPlayfieldRequest(name="elevated-field"))

    entries = {e.id: e for e in response.playfield.definition.aruco_tags}
    assert entries[1].height_cm == 12.5
    assert entries[2].height_cm == 0.0


def test_recalibration_after_a_simulated_restart_carries_forward_previously_estimated_intrinsics(
    directories, registry
):
    """The confirmed bug ticket 001 left open for this ticket to close:
    ``Calibrator``'s in-process cache (populated only by a successful
    ``calibrate()`` call *in this process*) is not the same store as
    ``DaemonService``'s own disk-loaded ``self._calibrations``. Before
    ``Calibrator.__init__``'s ``get_prior_calibration`` was wired to
    ``DaemonService.get_calibration``, an ordinary recalibration
    immediately after a daemon restart read the former (empty) instead
    of the latter (disk-backed), silently wiping a previously-estimated
    lens model back to ``None`` with no error. Exercised here with the
    calibration living *only* on disk when the second ``DaemonService``
    is constructed -- this process's first ``calibrate()`` call for that
    instance is the plain, no-flag recalibration itself, so a
    same-process-only test would pass with the bug still present."""
    frame = render_projective_frame(
        {i: (x, y, 0.0) for i, (x, y) in MAIN_PLAYFIELD_ARUCO_POSITIONS.items()}, tilt_deg=8.0
    )
    identity, slug, cam, _backend = _open_camera_with_frames(registry, [frame])
    service = _service_with_camera(directories, registry, identity, slug, cam)
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))

    first_result = service.calibrate(slug, estimate_intrinsics=True)
    assert first_result.succeeded is True, first_result.reason
    original = service.get_calibration(slug)
    assert original is not None
    assert original.camera_matrix is not None
    assert original.dist_coeffs is not None

    # Simulate a restart: a brand-new DaemonService instance (its own
    # fresh Calibrator, whose in-process cache has never seen a
    # calibrate() call), reading the SAME state_dir -- calibration.json
    # is the only thing that survives a restart (UC-01); everything
    # else (playfields, camera config) does not, which is why they are
    # re-pushed below exactly like a real operator/agent would after a
    # restart.
    frame2 = render_projective_frame(
        {i: (x, y, 0.0) for i, (x, y) in MAIN_PLAYFIELD_ARUCO_POSITIONS.items()}, tilt_deg=8.0
    )
    identity2, slug2, cam2, _backend2 = _open_camera_with_frames(registry, [frame2])
    assert slug2 == slug  # same physical camera identity -> same slug
    restarted = _service_with_camera(directories, registry, identity2, slug2, cam2)
    restarted.set_playfield(main_playfield_definition())
    restarted.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))

    # Sanity: the fresh instance really did reload it from disk, before
    # this process ever calls calibrate() on it.
    reloaded = restarted.get_calibration(slug)
    assert reloaded is not None
    assert reloaded.camera_matrix == original.camera_matrix

    second_result = restarted.calibrate(slug)  # ordinary, no estimate_intrinsics

    assert second_result.succeeded is True, second_result.reason
    assert second_result.intrinsics_estimated is False
    after_recalibration = restarted.get_calibration(slug)
    assert after_recalibration is not None
    assert after_recalibration.camera_matrix is not None, (
        "the bug this test guards: intrinsics silently wiped on a post-restart recalibration"
    )
    assert after_recalibration.camera_matrix == original.camera_matrix
    assert after_recalibration.dist_coeffs == original.dist_coeffs


# ---------------------------------------------------------------------------
# Annotation store (ticket 024-001, specification.md section 9 decision
# 8, UC-28) -- DaemonService direct-method tests first (no gRPC needed,
# mirroring every other section's construction pattern), then RPC round
# trips over a real gRPC channel.
# ---------------------------------------------------------------------------


def _marker(id: str, layer: str, x: float = 1.0, y: float = 2.0) -> Annotation:
    return Annotation(id=id, layer=layer, shape=Marker(at=WorldPoint(x=x, y=y)))


def test_put_annotation_unknown_playfield_raises_unknown_playfield_naming_held_playfields(
    service,
):
    service.set_playfield(_playfield_definition("main-playfield"))

    with pytest.raises(UnknownPlayfield) as excinfo:
        service.put_annotation("practice-field", _marker("goal", "planner"))

    assert excinfo.value.requested == "practice-field"
    assert excinfo.value.available == ("main-playfield",)


def test_remove_annotation_unknown_playfield_raises_unknown_playfield(service):
    with pytest.raises(UnknownPlayfield):
        service.remove_annotation("no-such-field", "planner", "goal")


def test_replace_layer_unknown_playfield_raises_unknown_playfield(service):
    with pytest.raises(UnknownPlayfield):
        service.replace_layer("no-such-field", "planner", ())


def test_clear_annotations_unknown_playfield_raises_unknown_playfield(service):
    with pytest.raises(UnknownPlayfield):
        service.clear_annotations("no-such-field")


def test_get_annotations_unknown_playfield_raises_unknown_playfield(service):
    with pytest.raises(UnknownPlayfield):
        service.get_annotations("no-such-field")


def test_put_annotation_then_get_annotations_round_trips_and_bumps_revision(service):
    service.set_playfield(_playfield_definition("main-playfield"))

    before = service.get_annotations("main-playfield")
    assert before.annotations == ()
    assert before.revision == 0

    service.put_annotation("main-playfield", _marker("goal", "planner", x=50.0, y=30.0))

    after = service.get_annotations("main-playfield")
    assert len(after.annotations) == 1
    assert after.annotations[0].id == "goal"
    assert after.annotations[0].layer == "planner"
    assert after.annotations[0].shape == Marker(at=WorldPoint(x=50.0, y=30.0))
    assert after.revision == 1


def test_two_independent_reads_see_the_same_put_annotation(service):
    """UC-28.S-1: a marker put by one client is visible to a second
    caller's ``list_annotations`` with no action required on its part.
    At the service level (single shared store, RPC concerns already
    covered separately below) this is exactly two independent
    ``get_annotations`` calls agreeing."""
    service.set_playfield(_playfield_definition("main-playfield"))
    service.put_annotation("main-playfield", _marker("goal", "planner", x=50.0, y=30.0))

    client_a_view = service.get_annotations("main-playfield")
    client_b_view = service.get_annotations("main-playfield")

    assert client_a_view.annotations == client_b_view.annotations
    assert client_a_view.revision == client_b_view.revision == 1


def test_put_annotation_accepts_every_shape_in_the_annotation_shape_union(service):
    service.set_playfield(_playfield_definition("main-playfield"))
    shapes = {
        "marker": Marker(at=WorldPoint(x=1.0, y=1.0), label="m"),
        "path": PathShape(points=(WorldPoint(x=0.0, y=0.0), WorldPoint(x=1.0, y=1.0))),
        "polygon": Polygon(
            points=(
                WorldPoint(x=0.0, y=0.0),
                WorldPoint(x=1.0, y=0.0),
                WorldPoint(x=0.0, y=1.0),
            )
        ),
        "circle": Circle(center=WorldPoint(x=0.0, y=0.0), radius_cm=5.0),
        "text": Text(at=WorldPoint(x=0.0, y=0.0), text="hi"),
    }
    for name, shape in shapes.items():
        service.put_annotation(
            "main-playfield",
            Annotation(id=name, layer="planner", shape=shape, style=Style(color="blue")),
        )

    after = service.get_annotations("main-playfield")

    by_id = {a.id: a for a in after.annotations}
    assert set(by_id) == set(shapes)
    for name, shape in shapes.items():
        assert by_id[name].shape == shape
        assert by_id[name].style == Style(color="blue")


def test_remove_annotation_removes_and_bumps_revision(service):
    service.set_playfield(_playfield_definition("main-playfield"))
    service.put_annotation("main-playfield", _marker("goal", "planner"))

    service.remove_annotation("main-playfield", "planner", "goal")

    after = service.get_annotations("main-playfield")
    assert after.annotations == ()
    assert after.revision == 2  # put -> 1, remove -> 2


def test_remove_annotation_absent_id_on_known_playfield_is_a_no_op(service):
    service.set_playfield(_playfield_definition("main-playfield"))

    service.remove_annotation("main-playfield", "planner", "no-such-id")  # must not raise

    after = service.get_annotations("main-playfield")
    assert after.annotations == ()
    assert after.revision == 0  # no mutation happened -- no-op, not an error


def test_remove_annotation_absent_layer_on_known_playfield_is_a_no_op(service):
    service.set_playfield(_playfield_definition("main-playfield"))
    service.put_annotation("main-playfield", _marker("goal", "planner"))

    service.remove_annotation("main-playfield", "no-such-layer", "goal")

    after = service.get_annotations("main-playfield")
    assert len(after.annotations) == 1
    assert after.revision == 1  # unchanged by the no-op remove


def test_replace_layer_atomically_swaps_contents_and_bumps_revision(service):
    service.set_playfield(_playfield_definition("main-playfield"))
    service.put_annotation("main-playfield", _marker("old", "planner"))

    service.replace_layer("main-playfield", "planner", (_marker("new", "planner", x=10.0, y=20.0),))

    after = service.get_annotations("main-playfield", layer="planner")
    assert [a.id for a in after.annotations] == ["new"]
    assert after.revision == 2  # put -> 1, replace -> 2


def test_replace_layer_with_an_empty_tuple_clears_the_layer_and_still_bumps_revision(service):
    service.set_playfield(_playfield_definition("main-playfield"))
    service.put_annotation("main-playfield", _marker("goal", "planner"))

    service.replace_layer("main-playfield", "planner", ())

    after = service.get_annotations("main-playfield")
    assert after.annotations == ()
    assert after.revision == 2


def test_replace_layer_never_exposes_a_torn_state_to_a_concurrent_reader(service):
    """UC-28.S-2: no consumer ever observes a half-updated layer --
    every read sees either the old set or the new one, never a mix.
    Hammers ``replace_layer`` from a writer thread while a reader
    thread polls ``get_annotations`` continuously (mirrors
    ``test_mark_stale_is_thread_safe_under_concurrent_calls``'s
    pattern), recording any observed id set that is neither the
    pre-replace nor the post-replace set."""
    service.set_playfield(_playfield_definition("main-playfield"))
    old_set = tuple(_marker(f"old-{i}", "planner", x=float(i)) for i in range(5))
    new_set = tuple(_marker(f"new-{i}", "planner", x=float(i)) for i in range(5))
    old_ids = frozenset(a.id for a in old_set)
    new_ids = frozenset(a.id for a in new_set)
    service.replace_layer("main-playfield", "planner", old_set)

    errors: List[BaseException] = []
    observed_bad: List[frozenset] = []

    def reader() -> None:
        try:
            for _ in range(500):
                ids = frozenset(a.id for a in service.get_annotations("main-playfield").annotations)
                if ids not in (old_ids, new_ids):
                    observed_bad.append(ids)
        except BaseException as exc:  # pragma: no cover - would fail the test either way
            errors.append(exc)

    def writer() -> None:
        try:
            for i in range(100):
                service.replace_layer(
                    "main-playfield", "planner", new_set if i % 2 == 0 else old_set
                )
        except BaseException as exc:  # pragma: no cover - would fail the test either way
            errors.append(exc)

    reader_thread = threading.Thread(target=reader)
    writer_thread = threading.Thread(target=writer)
    reader_thread.start()
    writer_thread.start()
    reader_thread.join()
    writer_thread.join()

    assert errors == []
    assert observed_bad == []


def test_clear_annotations_named_layer_clears_only_that_layer_and_bumps_revision(service):
    service.set_playfield(_playfield_definition("main-playfield"))
    service.put_annotation("main-playfield", _marker("goal", "planner"))
    service.put_annotation("main-playfield", _marker("robot", "robot-3"))

    service.clear_annotations("main-playfield", layer="planner")

    after = service.get_annotations("main-playfield")
    assert [a.id for a in after.annotations] == ["robot"]
    assert after.revision == 3  # two puts -> 2, clear -> 3


def test_clear_annotations_with_no_layer_clears_everything_and_bumps_revision(service):
    service.set_playfield(_playfield_definition("main-playfield"))
    service.put_annotation("main-playfield", _marker("goal", "planner"))
    service.put_annotation("main-playfield", _marker("robot", "robot-3"))

    service.clear_annotations("main-playfield")

    after = service.get_annotations("main-playfield")
    assert after.annotations == ()
    assert after.revision == 3


def test_clear_annotations_nonexistent_layer_on_known_playfield_is_a_no_op(service):
    """Architecture resolution 2 / acceptance criterion: clearing a
    layer that does not exist on a *known* playfield is a no-op, not an
    error, and does not bump the revision."""
    service.set_playfield(_playfield_definition("main-playfield"))
    service.put_annotation("main-playfield", _marker("goal", "planner"))
    revision_before = service.get_annotations("main-playfield").revision

    service.clear_annotations("main-playfield", layer="no-such-layer")  # must not raise

    after = service.get_annotations("main-playfield")
    assert len(after.annotations) == 1
    assert after.revision == revision_before


def test_clear_annotations_on_a_playfield_with_nothing_stored_is_a_no_op(service):
    service.set_playfield(_playfield_definition("main-playfield"))

    service.clear_annotations("main-playfield")  # must not raise

    assert service.get_annotations("main-playfield").revision == 0


def test_clear_annotations_with_no_layer_when_every_layer_is_already_empty_is_a_no_op(service):
    service.set_playfield(_playfield_definition("main-playfield"))
    service.put_annotation("main-playfield", _marker("goal", "planner"))
    service.remove_annotation("main-playfield", "planner", "goal")
    revision_after_remove = service.get_annotations("main-playfield").revision

    service.clear_annotations("main-playfield")  # every layer already empty -- no-op

    assert service.get_annotations("main-playfield").revision == revision_after_remove


def test_two_writers_on_one_key_last_write_wins_no_merge_no_error(service):
    """UC-28.S-6: two services both put layer 'shared', id 'target' --
    the later position wins outright, no merge, no error."""
    service.set_playfield(_playfield_definition("main-playfield"))

    service.put_annotation("main-playfield", _marker("target", "shared", x=10.0, y=5.0))
    service.put_annotation("main-playfield", _marker("target", "shared", x=12.0, y=6.0))

    after = service.get_annotations("main-playfield", layer="shared")
    assert len(after.annotations) == 1
    assert after.annotations[0].shape == Marker(at=WorldPoint(x=12.0, y=6.0))


# ---------------------------------------------------------------------------
# Annotation count/point limits (ticket 031-010, UC-28/SUC-010). Sprint
# 024 shipped this store with no bound at all -- these prove the two
# fixed bounds this ticket adds, and that a rejection never mutates the
# store (matching replace_layer's pre-existing atomicity guarantee,
# extended to this new failure path).
# ---------------------------------------------------------------------------


def test_put_annotation_at_the_count_bound_succeeds_one_new_id_over_it_rejects_with_no_mutation(
    service,
):
    service.set_playfield(_playfield_definition("main-playfield"))
    for i in range(_MAX_ANNOTATIONS_PER_LAYER):
        service.put_annotation("main-playfield", _marker(f"a{i}", "planner", x=float(i)))
    at_bound = service.get_annotations("main-playfield", layer="planner")
    assert len(at_bound.annotations) == _MAX_ANNOTATIONS_PER_LAYER
    revision_before = at_bound.revision

    with pytest.raises(AnnotationLimitExceeded) as excinfo:
        service.put_annotation("main-playfield", _marker("one-too-many", "planner"))

    assert excinfo.value.kind == "annotations"
    assert excinfo.value.bound == _MAX_ANNOTATIONS_PER_LAYER
    assert excinfo.value.actual == _MAX_ANNOTATIONS_PER_LAYER + 1
    after = service.get_annotations("main-playfield", layer="planner")
    assert len(after.annotations) == _MAX_ANNOTATIONS_PER_LAYER  # unchanged -- no partial mutation
    assert after.revision == revision_before  # rejection never bumps the revision


def test_put_annotation_upsert_of_an_existing_id_at_the_count_bound_is_not_rejected(service):
    """An upsert of an id already in the layer does not grow it, so it
    must never be rejected on count grounds even when the layer is
    already sitting exactly at the bound."""
    service.set_playfield(_playfield_definition("main-playfield"))
    for i in range(_MAX_ANNOTATIONS_PER_LAYER):
        service.put_annotation("main-playfield", _marker(f"a{i}", "planner", x=float(i)))

    service.put_annotation("main-playfield", _marker("a0", "planner", x=999.0))  # must not raise

    after = service.get_annotations("main-playfield", layer="planner")
    assert len(after.annotations) == _MAX_ANNOTATIONS_PER_LAYER
    by_id = {a.id: a for a in after.annotations}
    assert by_id["a0"].shape == Marker(at=WorldPoint(x=999.0, y=2.0))


def test_replace_layer_at_the_count_bound_succeeds(service):
    service.set_playfield(_playfield_definition("main-playfield"))
    at_bound = tuple(
        _marker(f"b{i}", "planner", x=float(i)) for i in range(_MAX_ANNOTATIONS_PER_LAYER)
    )

    service.replace_layer("main-playfield", "planner", at_bound)  # must not raise

    after = service.get_annotations("main-playfield", layer="planner")
    assert len(after.annotations) == _MAX_ANNOTATIONS_PER_LAYER


def test_replace_layer_over_the_count_bound_rejects_leaving_previous_contents_intact(service):
    service.set_playfield(_playfield_definition("main-playfield"))
    service.put_annotation("main-playfield", _marker("keep", "planner"))
    revision_before = service.get_annotations("main-playfield").revision

    too_many = tuple(
        _marker(f"c{i}", "planner", x=float(i)) for i in range(_MAX_ANNOTATIONS_PER_LAYER + 1)
    )
    with pytest.raises(AnnotationLimitExceeded) as excinfo:
        service.replace_layer("main-playfield", "planner", too_many)

    assert excinfo.value.kind == "annotations"
    assert excinfo.value.bound == _MAX_ANNOTATIONS_PER_LAYER
    assert excinfo.value.actual == _MAX_ANNOTATIONS_PER_LAYER + 1
    after = service.get_annotations("main-playfield")
    assert [a.id for a in after.annotations] == ["keep"]  # previous layer contents untouched
    assert after.revision == revision_before


def test_put_annotation_path_shape_at_the_point_bound_succeeds_one_over_rejects_with_no_mutation(
    service,
):
    service.set_playfield(_playfield_definition("main-playfield"))
    at_bound_points = tuple(WorldPoint(x=float(i), y=0.0) for i in range(_MAX_POINTS_PER_SHAPE))
    service.put_annotation(
        "main-playfield",
        Annotation(id="route", layer="planner", shape=PathShape(points=at_bound_points)),
    )
    revision_before = service.get_annotations("main-playfield").revision

    over_bound_points = at_bound_points + (WorldPoint(x=99999.0, y=0.0),)
    with pytest.raises(AnnotationLimitExceeded) as excinfo:
        service.put_annotation(
            "main-playfield",
            Annotation(id="too-long", layer="planner", shape=PathShape(points=over_bound_points)),
        )

    assert excinfo.value.kind == "points"
    assert excinfo.value.bound == _MAX_POINTS_PER_SHAPE
    assert excinfo.value.actual == _MAX_POINTS_PER_SHAPE + 1
    after = service.get_annotations("main-playfield")
    assert [a.id for a in after.annotations] == ["route"]  # "too-long" was never stored
    assert after.revision == revision_before


def test_replace_layer_rejects_a_polygon_over_the_point_bound_leaving_previous_contents_intact(
    service,
):
    service.set_playfield(_playfield_definition("main-playfield"))
    service.put_annotation("main-playfield", _marker("keep", "planner"))
    revision_before = service.get_annotations("main-playfield").revision

    over_bound_points = tuple(WorldPoint(x=float(i), y=0.0) for i in range(_MAX_POINTS_PER_SHAPE + 1))
    with pytest.raises(AnnotationLimitExceeded) as excinfo:
        service.replace_layer(
            "main-playfield",
            "planner",
            (Annotation(id="zone", layer="planner", shape=Polygon(points=over_bound_points)),),
        )

    assert excinfo.value.kind == "points"
    assert excinfo.value.bound == _MAX_POINTS_PER_SHAPE
    assert excinfo.value.actual == _MAX_POINTS_PER_SHAPE + 1
    after = service.get_annotations("main-playfield")
    assert [a.id for a in after.annotations] == ["keep"]
    assert after.revision == revision_before


def test_put_annotation_and_replace_layer_well_under_both_bounds_are_unaffected(service):
    """AC: a request well under both bounds behaves exactly as it did
    before this ticket -- no new error, no behaviour change."""
    service.set_playfield(_playfield_definition("main-playfield"))

    service.put_annotation(
        "main-playfield",
        Annotation(
            id="route",
            layer="planner",
            shape=PathShape(points=tuple(WorldPoint(x=float(i), y=0.0) for i in range(50))),
        ),
    )
    service.replace_layer(
        "main-playfield",
        "planner",
        tuple(_marker(f"d{i}", "planner", x=float(i)) for i in range(10)),
    )

    after = service.get_annotations("main-playfield", layer="planner")
    assert len(after.annotations) == 10


def test_get_annotations_filtered_by_layer_still_reports_the_whole_playfield_revision(service):
    service.set_playfield(_playfield_definition("main-playfield"))
    service.put_annotation("main-playfield", _marker("goal", "planner"))
    service.put_annotation("main-playfield", _marker("robot", "robot-3"))

    filtered = service.get_annotations("main-playfield", layer="planner")

    assert [a.id for a in filtered.annotations] == ["goal"]
    assert filtered.revision == 2  # the whole-playfield revision, not a per-layer one


def test_ttl_seconds_round_trips_through_put_and_get_unchanged(service):
    """AC: ``ttl_seconds`` round-trips through put -> get unchanged; no
    pruning logic exists anywhere (sprint.md's Resolved UC-28 open
    questions, item 5 -- a deliberate deferral)."""
    service.set_playfield(_playfield_definition("main-playfield"))
    annotation = Annotation(
        id="goal", layer="planner", shape=Marker(at=WorldPoint(x=1.0, y=2.0)), ttl_seconds=30.0
    )

    service.put_annotation("main-playfield", annotation)

    fetched = service.get_annotations("main-playfield").annotations[0]
    assert fetched.ttl_seconds == 30.0


def test_set_playfield_of_an_already_held_name_leaves_its_annotations_untouched(service):
    """Resolution 3: pushing a ``PlayfieldDefinition`` of an
    already-held name does not clear that playfield's annotations."""
    service.set_playfield(_playfield_definition("main-playfield"))
    service.put_annotation("main-playfield", _marker("goal", "planner"))

    service.set_playfield(_playfield_definition("main-playfield", aruco_ids=(9, 10)))  # replace

    after = service.get_annotations("main-playfield")
    assert [a.id for a in after.annotations] == ["goal"]


def test_a_fresh_daemon_service_reports_an_empty_annotation_set_at_revision_zero(
    directories, registry
):
    """UC-28.S-4: a daemon restart wipes all annotations; ``list_annotations``
    on the playfield returns an empty set at revision 0. Modeled the same
    way every other restart test in this file is -- a fresh
    ``DaemonService`` sharing the same directories/registry -- since
    annotations are in-memory only, exactly like the store they live in;
    a fresh instance simply never saw the earlier puts. The playfield is
    re-pushed on the fresh instance (the same reconnect ritual UC-28.S-4
    itself describes for annotations) so ``get_annotations`` can be
    called at all -- an unknown playfield would raise instead of
    answering."""
    frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    identity, slug, cam, _backend = _open_camera_with_frames(registry, [frame])
    original = _service_with_camera(directories, registry, identity, slug, cam)
    original.set_playfield(main_playfield_definition())
    original.put_annotation("main-playfield", _marker("goal", "planner"))
    assert original.get_annotations("main-playfield").revision == 1

    fresh_discovery = CameraDiscovery(
        registry,
        active_slugs=lambda: frozenset(),
        identity_source=lambda: {},
        backend_factory=lambda index: FakeBackend(),
    )
    restarted = DaemonService(registry=registry, discovery=fresh_discovery, directories=directories)
    restarted.set_playfield(main_playfield_definition())

    after = restarted.get_annotations("main-playfield")
    assert after.annotations == ()
    assert after.revision == 0


def test_get_frame_deskewed_embeds_an_empty_annotation_set_when_nothing_has_been_put(
    directories, registry
):
    frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    identity, slug, cam, _backend = _open_camera_with_frames(
        registry, [frame] * (CALIBRATION_FRAME_COUNT + 1)
    )
    service = _service_with_camera(directories, registry, identity, slug, cam)
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))
    assert service.calibrate(slug).succeeded is True

    image = service.get_frame(slug, deskewed=True)

    assert image.annotations is not None
    assert image.annotations.annotations == ()
    assert image.annotations.revision == 0


def test_get_frame_deskewed_embeds_the_current_annotation_set(directories, registry):
    frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    identity, slug, cam, _backend = _open_camera_with_frames(
        registry, [frame] * (CALIBRATION_FRAME_COUNT + 1)
    )
    service = _service_with_camera(directories, registry, identity, slug, cam)
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))
    assert service.calibrate(slug).succeeded is True
    service.put_annotation("main-playfield", _marker("goal", "planner", x=50.0, y=30.0))

    image = service.get_frame(slug, deskewed=True)

    assert image.annotations is not None
    assert len(image.annotations.annotations) == 1
    assert image.annotations.annotations[0].id == "goal"
    assert image.annotations.revision == 1


def test_get_frame_raw_never_embeds_annotations_even_after_a_put(directories, registry):
    """AC: a raw ``GetFrame`` never sets ``annotations`` (stays
    ``None``), unconditionally -- verified with an annotation actually
    put first, on the same camera's linked playfield, so the ``None``
    is a real contract, not merely "nothing was ever put"."""
    frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    identity, slug, cam, _backend = _open_camera_with_frames(registry, [frame])
    service = _service_with_camera(directories, registry, identity, slug, cam)
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))
    service.put_annotation("main-playfield", _marker("goal", "planner"))

    raw = service.get_frame(slug)

    assert raw.deskewed is False
    assert raw.annotations is None


# ---------------------------------------------------------------------------
# Annotations -- RPC round trips (ticket 024-001)
# ---------------------------------------------------------------------------


def _annotation_msg(
    id: str,
    layer: str,
    *,
    x: float = 1.0,
    y: float = 1.0,
) -> aprilcam_v2_pb2.Annotation:
    return aprilcam_v2_pb2.Annotation(
        id=id,
        layer=layer,
        marker=aprilcam_v2_pb2.Marker(at=aprilcam_v2_pb2.WorldPoint(x=x, y=y)),
    )


def test_rpc_put_and_get_annotations_round_trip_every_shape(stub):
    stub.SetPlayfield(aprilcam_v2_pb2.SetPlayfieldRequest(definition=_playfield_definition_msg()))

    marker = aprilcam_v2_pb2.Annotation(
        id="goal",
        layer="planner",
        marker=aprilcam_v2_pb2.Marker(at=aprilcam_v2_pb2.WorldPoint(x=50.0, y=30.0), label="Goal"),
        style=aprilcam_v2_pb2.Style(color="red", line_width=2.0),
        ttl_seconds=30.0,
    )
    path = aprilcam_v2_pb2.Annotation(
        id="route",
        layer="planner",
        path=aprilcam_v2_pb2.PathShape(
            points=[
                aprilcam_v2_pb2.WorldPoint(x=0.0, y=0.0),
                aprilcam_v2_pb2.WorldPoint(x=25.0, y=10.0),
                aprilcam_v2_pb2.WorldPoint(x=50.0, y=30.0),
            ]
        ),
    )
    zone = aprilcam_v2_pb2.Annotation(
        id="zone",
        layer="planner",
        polygon=aprilcam_v2_pb2.Polygon(
            points=[
                aprilcam_v2_pb2.WorldPoint(x=-10.0, y=-10.0),
                aprilcam_v2_pb2.WorldPoint(x=10.0, y=-10.0),
                aprilcam_v2_pb2.WorldPoint(x=0.0, y=10.0),
            ]
        ),
    )
    ring = aprilcam_v2_pb2.Annotation(
        id="ring",
        layer="planner",
        circle=aprilcam_v2_pb2.Circle(
            center=aprilcam_v2_pb2.WorldPoint(x=5.0, y=5.0), radius_cm=15.0
        ),
    )
    label = aprilcam_v2_pb2.Annotation(
        id="label",
        layer="planner",
        text=aprilcam_v2_pb2.Text(at=aprilcam_v2_pb2.WorldPoint(x=0.0, y=0.0), text="Base"),
    )

    for annotation in (marker, path, zone, ring, label):
        stub.PutAnnotation(
            aprilcam_v2_pb2.PutAnnotationRequest(playfield="main-playfield", annotation=annotation)
        )

    response = stub.GetAnnotations(
        aprilcam_v2_pb2.GetAnnotationsRequest(playfield="main-playfield")
    )

    by_id = {a.id: a for a in response.annotations.annotations}
    assert set(by_id) == {"goal", "route", "zone", "ring", "label"}
    assert response.annotations.revision == 5

    fetched_marker = by_id["goal"]
    assert fetched_marker.WhichOneof("shape") == "marker"
    assert fetched_marker.marker.at.x == 50.0 and fetched_marker.marker.at.y == 30.0
    assert fetched_marker.marker.label == "Goal"
    assert fetched_marker.style.color == "red"
    assert fetched_marker.style.line_width == 2.0
    assert fetched_marker.HasField("ttl_seconds")
    assert fetched_marker.ttl_seconds == 30.0

    fetched_path = by_id["route"]
    assert fetched_path.WhichOneof("shape") == "path"
    assert [(p.x, p.y) for p in fetched_path.path.points] == [
        (0.0, 0.0),
        (25.0, 10.0),
        (50.0, 30.0),
    ]

    fetched_zone = by_id["zone"]
    assert fetched_zone.WhichOneof("shape") == "polygon"
    assert len(fetched_zone.polygon.points) == 3

    fetched_ring = by_id["ring"]
    assert fetched_ring.WhichOneof("shape") == "circle"
    assert fetched_ring.circle.radius_cm == 15.0

    fetched_label = by_id["label"]
    assert fetched_label.WhichOneof("shape") == "text"
    assert fetched_label.text.text == "Base"


def test_rpc_remove_annotation_round_trip(stub):
    stub.SetPlayfield(aprilcam_v2_pb2.SetPlayfieldRequest(definition=_playfield_definition_msg()))
    stub.PutAnnotation(
        aprilcam_v2_pb2.PutAnnotationRequest(
            playfield="main-playfield", annotation=_annotation_msg("goal", "planner")
        )
    )

    stub.RemoveAnnotation(
        aprilcam_v2_pb2.RemoveAnnotationRequest(
            playfield="main-playfield", layer="planner", id="goal"
        )
    )

    response = stub.GetAnnotations(
        aprilcam_v2_pb2.GetAnnotationsRequest(playfield="main-playfield")
    )
    assert list(response.annotations.annotations) == []


def test_rpc_remove_annotation_absent_key_on_known_playfield_does_not_raise(stub):
    stub.SetPlayfield(aprilcam_v2_pb2.SetPlayfieldRequest(definition=_playfield_definition_msg()))

    stub.RemoveAnnotation(
        aprilcam_v2_pb2.RemoveAnnotationRequest(
            playfield="main-playfield", layer="planner", id="no-such-id"
        )
    )  # must not raise


def test_rpc_replace_layer_round_trip_swaps_a_layers_contents(stub):
    stub.SetPlayfield(aprilcam_v2_pb2.SetPlayfieldRequest(definition=_playfield_definition_msg()))
    stub.PutAnnotation(
        aprilcam_v2_pb2.PutAnnotationRequest(
            playfield="main-playfield", annotation=_annotation_msg("old", "planner")
        )
    )

    stub.ReplaceLayer(
        aprilcam_v2_pb2.ReplaceLayerRequest(
            playfield="main-playfield",
            layer="planner",
            annotations=[_annotation_msg("new", "planner", x=2.0, y=2.0)],
        )
    )

    response = stub.GetAnnotations(
        aprilcam_v2_pb2.GetAnnotationsRequest(playfield="main-playfield")
    )
    assert [a.id for a in response.annotations.annotations] == ["new"]


def test_rpc_clear_annotations_round_trip_named_layer_and_everything(stub):
    stub.SetPlayfield(aprilcam_v2_pb2.SetPlayfieldRequest(definition=_playfield_definition_msg()))
    stub.PutAnnotation(
        aprilcam_v2_pb2.PutAnnotationRequest(
            playfield="main-playfield", annotation=_annotation_msg("goal", "planner")
        )
    )
    stub.PutAnnotation(
        aprilcam_v2_pb2.PutAnnotationRequest(
            playfield="main-playfield", annotation=_annotation_msg("robot", "robot-3")
        )
    )

    stub.ClearAnnotations(
        aprilcam_v2_pb2.ClearAnnotationsRequest(playfield="main-playfield", layer="planner")
    )
    after_one = stub.GetAnnotations(
        aprilcam_v2_pb2.GetAnnotationsRequest(playfield="main-playfield")
    )
    assert [a.id for a in after_one.annotations.annotations] == ["robot"]

    stub.ClearAnnotations(aprilcam_v2_pb2.ClearAnnotationsRequest(playfield="main-playfield"))
    after_all = stub.GetAnnotations(
        aprilcam_v2_pb2.GetAnnotationsRequest(playfield="main-playfield")
    )
    assert list(after_all.annotations.annotations) == []


def test_rpc_clear_annotations_nonexistent_layer_on_known_playfield_does_not_raise(stub):
    stub.SetPlayfield(aprilcam_v2_pb2.SetPlayfieldRequest(definition=_playfield_definition_msg()))

    stub.ClearAnnotations(
        aprilcam_v2_pb2.ClearAnnotationsRequest(playfield="main-playfield", layer="no-such-layer")
    )  # must not raise


def test_rpc_put_annotation_unknown_playfield_maps_to_not_found_naming_held_playfields(stub):
    stub.SetPlayfield(aprilcam_v2_pb2.SetPlayfieldRequest(definition=_playfield_definition_msg()))

    with pytest.raises(grpc.RpcError) as excinfo:
        stub.PutAnnotation(
            aprilcam_v2_pb2.PutAnnotationRequest(
                playfield="practice-field", annotation=_annotation_msg("goal", "planner")
            )
        )

    assert excinfo.value.code() == grpc.StatusCode.NOT_FOUND
    metadata = dict(excinfo.value.trailing_metadata())
    assert metadata["aprilcam-error-type"] == "UnknownPlayfield"
    assert metadata["aprilcam-requested"] == "practice-field"
    assert metadata["aprilcam-available"] == "main-playfield"


def test_rpc_put_annotation_over_point_bound_maps_to_resource_exhausted(stub):
    """ticket 031-010: over the wire, over-bound is RESOURCE_EXHAUSTED
    with ``aprilcam-kind``/``aprilcam-bound``/``aprilcam-actual``
    trailing metadata, and the rejected annotation is never stored."""
    stub.SetPlayfield(aprilcam_v2_pb2.SetPlayfieldRequest(definition=_playfield_definition_msg()))
    over_bound = aprilcam_v2_pb2.Annotation(
        id="route",
        layer="planner",
        path=aprilcam_v2_pb2.PathShape(
            points=[
                aprilcam_v2_pb2.WorldPoint(x=float(i), y=0.0)
                for i in range(_MAX_POINTS_PER_SHAPE + 1)
            ]
        ),
    )

    with pytest.raises(grpc.RpcError) as excinfo:
        stub.PutAnnotation(
            aprilcam_v2_pb2.PutAnnotationRequest(playfield="main-playfield", annotation=over_bound)
        )

    assert excinfo.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED
    metadata = dict(excinfo.value.trailing_metadata())
    assert metadata["aprilcam-error-type"] == "AnnotationLimitExceeded"
    assert metadata["aprilcam-kind"] == "points"
    assert metadata["aprilcam-bound"] == str(_MAX_POINTS_PER_SHAPE)
    assert metadata["aprilcam-actual"] == str(_MAX_POINTS_PER_SHAPE + 1)

    after = stub.GetAnnotations(aprilcam_v2_pb2.GetAnnotationsRequest(playfield="main-playfield"))
    assert list(after.annotations.annotations) == []


def test_rpc_replace_layer_over_count_bound_maps_to_resource_exhausted_leaving_previous_layer_intact(
    stub,
):
    stub.SetPlayfield(aprilcam_v2_pb2.SetPlayfieldRequest(definition=_playfield_definition_msg()))
    stub.PutAnnotation(
        aprilcam_v2_pb2.PutAnnotationRequest(
            playfield="main-playfield", annotation=_annotation_msg("keep", "planner")
        )
    )
    too_many = [
        aprilcam_v2_pb2.Annotation(
            id=f"e{i}",
            layer="planner",
            marker=aprilcam_v2_pb2.Marker(at=aprilcam_v2_pb2.WorldPoint(x=float(i), y=0.0)),
        )
        for i in range(_MAX_ANNOTATIONS_PER_LAYER + 1)
    ]

    with pytest.raises(grpc.RpcError) as excinfo:
        stub.ReplaceLayer(
            aprilcam_v2_pb2.ReplaceLayerRequest(
                playfield="main-playfield", layer="planner", annotations=too_many
            )
        )

    assert excinfo.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED
    metadata = dict(excinfo.value.trailing_metadata())
    assert metadata["aprilcam-error-type"] == "AnnotationLimitExceeded"
    assert metadata["aprilcam-kind"] == "annotations"
    assert metadata["aprilcam-bound"] == str(_MAX_ANNOTATIONS_PER_LAYER)
    assert metadata["aprilcam-actual"] == str(_MAX_ANNOTATIONS_PER_LAYER + 1)

    after = stub.GetAnnotations(aprilcam_v2_pb2.GetAnnotationsRequest(playfield="main-playfield"))
    assert [a.id for a in after.annotations.annotations] == ["keep"]


def test_rpc_remove_annotation_unknown_playfield_maps_to_not_found(stub):
    with pytest.raises(grpc.RpcError) as excinfo:
        stub.RemoveAnnotation(
            aprilcam_v2_pb2.RemoveAnnotationRequest(
                playfield="no-such-field", layer="planner", id="goal"
            )
        )
    assert excinfo.value.code() == grpc.StatusCode.NOT_FOUND


def test_rpc_replace_layer_unknown_playfield_maps_to_not_found(stub):
    with pytest.raises(grpc.RpcError) as excinfo:
        stub.ReplaceLayer(
            aprilcam_v2_pb2.ReplaceLayerRequest(
                playfield="no-such-field", layer="planner", annotations=[]
            )
        )
    assert excinfo.value.code() == grpc.StatusCode.NOT_FOUND


def test_rpc_clear_annotations_unknown_playfield_maps_to_not_found(stub):
    with pytest.raises(grpc.RpcError) as excinfo:
        stub.ClearAnnotations(aprilcam_v2_pb2.ClearAnnotationsRequest(playfield="no-such-field"))
    assert excinfo.value.code() == grpc.StatusCode.NOT_FOUND


def test_rpc_get_annotations_unknown_playfield_maps_to_not_found(stub):
    with pytest.raises(grpc.RpcError) as excinfo:
        stub.GetAnnotations(aprilcam_v2_pb2.GetAnnotationsRequest(playfield="no-such-field"))

    assert excinfo.value.code() == grpc.StatusCode.NOT_FOUND
    metadata = dict(excinfo.value.trailing_metadata())
    assert metadata["aprilcam-error-type"] == "UnknownPlayfield"


def test_rpc_get_frame_raw_never_carries_annotations_even_after_a_put(stub, held_camera):
    stub.SetPlayfield(aprilcam_v2_pb2.SetPlayfieldRequest(definition=_playfield_definition_msg()))
    stub.PutAnnotation(
        aprilcam_v2_pb2.PutAnnotationRequest(
            playfield="main-playfield", annotation=_annotation_msg("goal", "planner")
        )
    )
    _, slug, _ = held_camera

    response = stub.GetFrame(aprilcam_v2_pb2.GetFrameRequest(camera=slug))

    assert response.HasField("annotations") is False


def test_rpc_get_frame_deskewed_embeds_annotations_round_trip(directories, registry):
    frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    identity, slug, cam, _backend = _open_camera_with_frames(
        registry, [frame] * (CALIBRATION_FRAME_COUNT + 2)
    )
    service = _service_with_camera(directories, registry, identity, slug, cam)
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))
    assert service.calibrate(slug).succeeded is True

    with _bind_stub(service) as stub:
        empty_response = stub.GetFrame(
            aprilcam_v2_pb2.GetFrameRequest(camera=slug, deskewed=True)
        )
        assert empty_response.HasField("annotations") is True
        assert list(empty_response.annotations.annotations) == []
        assert empty_response.annotations.revision == 0

        stub.PutAnnotation(
            aprilcam_v2_pb2.PutAnnotationRequest(
                playfield="main-playfield",
                annotation=_annotation_msg("goal", "planner", x=50.0, y=30.0),
            )
        )
        filled_response = stub.GetFrame(
            aprilcam_v2_pb2.GetFrameRequest(camera=slug, deskewed=True)
        )

    assert filled_response.HasField("annotations") is True
    assert len(filled_response.annotations.annotations) == 1
    assert filled_response.annotations.annotations[0].id == "goal"
    assert filled_response.annotations.revision == 1


# ---------------------------------------------------------------------------
# get_guide (ticket 025-007, UC-27) -- DaemonService.get_guide, direct
# ---------------------------------------------------------------------------


def test_get_guide_no_name_defaults_to_agent(service):
    assert service.get_guide() == service.get_guide("agent")


def test_get_guide_matches_client_guides_byte_identical_for_every_name(service):
    """The single-source-of-truth guarantee this ticket exists to prove
    (sprint 025's Design Rationale): ``DaemonService.get_guide`` and
    ``client.guides.Guides.get`` both read ``aprilcam.guides.read_guide``
    directly, so for every guide name -- not just the default -- the two
    accessors return the exact same text, not merely similar text."""
    client_guides = Guides()
    for name in AVAILABLE_GUIDES:
        assert service.get_guide(name) == client_guides.get(name)


def test_get_guide_unknown_name_raises_unknown_guide_naming_available(service):
    with pytest.raises(UnknownGuide) as excinfo:
        service.get_guide("nonexistent")

    assert excinfo.value.requested == "nonexistent"
    assert excinfo.value.available == ("agent", "robot")


# ---------------------------------------------------------------------------
# GetGuide -- RPC round trips (ticket 025-007, UC-27, this sprint's last)
# ---------------------------------------------------------------------------


def test_rpc_get_guide_no_name_round_trips_agent_guide_text(stub):
    response = stub.GetGuide(aprilcam_v2_pb2.GetGuideRequest())

    assert response.text == Guides().get("agent")


def test_rpc_get_guide_agent_name_round_trips_agent_guide_text(stub):
    response = stub.GetGuide(aprilcam_v2_pb2.GetGuideRequest(name="agent"))

    assert response.text == Guides().get("agent")


def test_rpc_get_guide_robot_name_round_trips_robot_guide_text(stub):
    response = stub.GetGuide(aprilcam_v2_pb2.GetGuideRequest(name="robot"))

    assert response.text == Guides().get("robot")


def test_rpc_get_guide_unknown_name_maps_to_not_found(stub):
    with pytest.raises(grpc.RpcError) as excinfo:
        stub.GetGuide(aprilcam_v2_pb2.GetGuideRequest(name="nonexistent"))

    assert excinfo.value.code() == grpc.StatusCode.NOT_FOUND
    metadata = dict(excinfo.value.trailing_metadata())
    assert metadata["aprilcam-error-type"] == "UnknownGuide"
    assert metadata["aprilcam-requested"] == "nonexistent"
    assert metadata["aprilcam-available"] == "agent,robot"


# ---------------------------------------------------------------------------
# @pytest.mark.camera -- real hardware, end to end through DaemonService
# (ticket 023-001, UC-03.S-2/S-3/S-4). Requires the real `aprilcam daemon`
# process to be stopped first: this builds a genuine DaemonService that
# opens real OpenCVBackend/UvcUtilBackend handles, and an AVFoundation
# capture session is exclusive per process (the same precondition
# test_camera.py's own real-hardware section already carries).
#
# Camera 3 (arducam-ov9782-usb-camera) is the real, currently-calibrated
# main-playfield camera -- every test below that touches it restores its
# exact pre-test hardware settings in a ``finally`` block, so a real run
# of this module never leaves it altered for whoever uses it next.
# ---------------------------------------------------------------------------

#: Camera 3's baseline settings as installed on this project's real rig
#: (queried live via ``uvc-util`` before ticket 023-001's changes were
#: made) -- restored after every real-hardware test that touches it.
_CAMERA_3_BASELINE_SETTINGS: Dict[str, float] = {
    "auto_exposure": 1,
    "exposure": 6,
    "gain": 2,
    "auto_white_balance": 0,
    "white_balance_temperature": 4600,
}

#: ``hd-usb-camera``'s factory-default auto-exposure mode (aperture
#: priority) -- restored after the independence test below leaves it in
#: manual mode.
_HD_USB_CAMERA_AUTO_EXPOSURE_DEFAULT: Dict[str, float] = {"auto_exposure": 8}

#: Seconds to wait after a settings push before capturing a frame meant
#: to show the effect -- an exposure/gain change takes a frame or two to
#: reach the sensor's actual output; capturing immediately after a
#: successful push (`UvcUtilBackend.set_property` has already verified
#: the *control* took, this is a separate settling delay on the *pixels*)
#: was found live to sometimes still return the previous frame.
_SETTLE_S = 0.5


def _real_identity(name_substring: str) -> Optional[CameraIdentity]:
    """The real ``CameraIdentity`` for the first attached camera whose
    device name contains *name_substring* (case-insensitive), or
    ``None`` if not attached -- the ``DaemonService``-level counterpart
    to ``test_camera.py``'s ``_real_camera_index`` (that one resolves a
    bare OpenCV index; this resolves a full identity, since
    ``DaemonService``/``CameraRegistry`` address cameras by slug)."""
    for identity in identity_module.resolve_all().values():
        if name_substring.lower() in identity.device_name.lower():
            return identity
    return None


@pytest.fixture
def real_daemon_service(tmp_path: Path):
    """A genuine ``DaemonService`` against real, currently-attached
    hardware -- registry/state live under *tmp_path* (never the real
    ``~/.local/share/aprilcam``), but every camera open is a real
    ``OpenCVBackend``/``UvcUtilBackend`` pair against whatever is
    physically attached, via ``DaemonService``'s own real defaults
    (``identity_source=resolve_all``, ``backend_factory=
    _default_backend_factory``, ``control_backend_factory=
    _default_control_backend_factory``) -- no override needed, this is
    exactly what a real daemon process builds."""
    directories = Directories(config_dir=tmp_path / "config", state_dir=tmp_path / "state")
    registry = CameraRegistry(tmp_path / "state" / "cameras")
    for identity in identity_module.resolve_all().values():
        registry.resolve(identity)
    discovery = CameraDiscovery(registry, active_slugs=lambda: frozenset())
    service = DaemonService(registry=registry, discovery=discovery, directories=directories)
    try:
        yield service
    finally:
        service.close()


def _open_or_skip(service: DaemonService, slug: str):
    """``_real_identity`` (above) only proves a camera is enumerated by
    name -- never that this process can actually open it right now
    (tests/aprilcam/hardware_camera.py's own "enumeration is not
    availability" contract; docs/knowledge/daemon-camera-grant-and-
    detection-priming.md). Every real-hardware test below opens its
    camera by calling ``DaemonService.get_frame`` directly (not through
    gRPC), so a camera-open failure surfaces here as a bare
    ``RuntimeError`` (``opencv_backend.OpenCVBackend.open``), not an
    ``AprilCamError``. Only the specific, known "failed to open camera"
    signature is treated as environmental; anything else is re-raised so
    a genuine bug still fails loudly. Returns the fetched frame so
    callers that need it (most do, as a warm-up) get it in one call."""
    try:
        return service.get_frame(slug)
    except RuntimeError as exc:
        if "failed to open camera" not in str(exc):
            raise
        pytest.skip(
            f"{slug!r} is attached but could not be opened just now -- "
            f"{exc}. Expected when this process has no camera TCC grant "
            f"(e.g. an agent session) or the device is genuinely busy "
            f"elsewhere; re-run from an operator Terminal session to "
            f"exercise this test for real."
        )


def _mean_brightness(jpeg_bytes: bytes) -> float:
    array = np.frombuffer(jpeg_bytes, dtype=np.uint8)
    image = cv.imdecode(array, cv.IMREAD_GRAYSCALE)
    assert image is not None, "failed to decode captured JPEG frame"
    return float(image.mean())


_UVC_UTIL_CURRENT_VALUE_RE = re.compile(r"current-value:\s*(\S+)")


def _uvc_util_current_value(binary: str, device_name: str, control: str) -> Optional[float]:
    """Query *control*'s live value straight off the hardware via
    ``uvc-util``, independent of anything ``UvcUtilBackend`` or
    ``DaemonService`` remembers -- the strongest possible check that a
    setting really reached the physical device."""
    result = subprocess.run(
        [binary, "-N", device_name, "-S", control],
        shell=False,
        capture_output=True,
        text=True,
        timeout=5.0,
    )
    if result.returncode != 0:
        return None
    match = _UVC_UTIL_CURRENT_VALUE_RE.search(result.stdout)
    if match is None:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


@pytest.mark.camera
def test_set_camera_config_manual_low_exposure_darkens_a_real_frame(real_daemon_service):
    """UC-03.S-2, live: pushing a manual low-exposure config to camera 3
    through ``DaemonService.set_camera_config`` -> ``Camera.
    apply_config`` -> ``UvcUtilBackend`` produces a visibly, measurably
    darker frame than a brighter manual-exposure push -- the real-hardware
    finding this ticket exists to prove (``cv2.VideoCapture.set()`` does
    not do this on this hardware; ``uvc-util`` does)."""
    identity = _real_identity("Arducam OV9782")
    if identity is None:
        pytest.skip("no attached camera matching 'Arducam OV9782'")
    slug = identity.slug

    try:
        _open_or_skip(real_daemon_service, slug)  # open + hold -- apply_config needs this

        real_daemon_service.set_camera_config(
            CameraConfig(
                name=slug,
                playfield=None,
                settings={"auto_exposure": 1, "exposure": 5000, "gain": 50},
            )
        )
        time.sleep(_SETTLE_S)  # the sensor needs a frame or two to apply a new exposure
        bright = real_daemon_service.get_frame(slug)

        real_daemon_service.set_camera_config(
            CameraConfig(
                name=slug, playfield=None, settings={"auto_exposure": 1, "exposure": 1, "gain": 0}
            )
        )
        time.sleep(_SETTLE_S)
        dark = real_daemon_service.get_frame(slug)

        assert _mean_brightness(dark.data) < _mean_brightness(bright.data)
    finally:
        real_daemon_service.set_camera_config(
            CameraConfig(name=slug, playfield=None, settings=_CAMERA_3_BASELINE_SETTINGS)
        )


@pytest.mark.camera
def test_set_camera_config_unsupported_key_fails_whole_push_camera_unchanged_live(
    real_daemon_service,
):
    """UC-03.S-3, live: an unsupported key fails the **whole** push and
    leaves the previously-pushed document in place -- re-fetching the
    config after the failed push shows the pre-push values, not the
    rejected ones."""
    identity = _real_identity("Arducam OV9782")
    if identity is None:
        pytest.skip("no attached camera matching 'Arducam OV9782'")
    slug = identity.slug

    try:
        _open_or_skip(real_daemon_service, slug)  # open + hold -- apply_config needs this

        real_daemon_service.set_camera_config(
            CameraConfig(name=slug, playfield=None, settings={"auto_exposure": 1, "exposure": 6})
        )
        before = real_daemon_service.get_camera_config(slug)

        with pytest.raises(UnsupportedSetting) as exc_info:
            real_daemon_service.set_camera_config(
                CameraConfig(
                    name=slug, playfield=None, settings={"not_a_real_control": 1}
                )
            )
        assert exc_info.value.key == "not_a_real_control"

        after = real_daemon_service.get_camera_config(slug)
        assert after == before
    finally:
        real_daemon_service.set_camera_config(
            CameraConfig(name=slug, playfield=None, settings=_CAMERA_3_BASELINE_SETTINGS)
        )


@pytest.mark.camera
def test_set_camera_config_out_of_range_value_fails_whole_push_camera_unchanged_live(
    real_daemon_service,
):
    """UC-03.S-3's other half, live: an out-of-range *value* (not an
    unknown key) also fails the whole push. Real-hardware finding
    (ticket 023-001): ``uvc-util`` can report success while silently
    clamping an out-of-range request instead of rejecting it --
    ``UvcUtilBackend.set_property``'s readback verification is what
    catches that and turns it into this same hard error."""
    identity = _real_identity("Arducam OV9782")
    if identity is None:
        pytest.skip("no attached camera matching 'Arducam OV9782'")
    slug = identity.slug

    try:
        _open_or_skip(real_daemon_service, slug)  # open + hold -- apply_config needs this

        real_daemon_service.set_camera_config(
            CameraConfig(name=slug, playfield=None, settings={"auto_exposure": 1, "exposure": 6})
        )
        before = real_daemon_service.get_camera_config(slug)

        with pytest.raises(UnsupportedSetting) as exc_info:
            real_daemon_service.set_camera_config(
                CameraConfig(name=slug, playfield=None, settings={"exposure": 999999})
            )
        assert exc_info.value.key == "exposure"

        after = real_daemon_service.get_camera_config(slug)
        assert after == before
    finally:
        real_daemon_service.set_camera_config(
            CameraConfig(name=slug, playfield=None, settings=_CAMERA_3_BASELINE_SETTINGS)
        )


@pytest.mark.camera
def test_set_camera_config_two_real_cameras_configured_independently(real_daemon_service):
    """UC-03.S-4, live: camera 3 and ``hd-usb-camera`` configured in the
    same session -- pushing to one does not change the other."""
    id3 = _real_identity("Arducam OV9782")
    id2 = _real_identity("HD USB")
    if id3 is None or id2 is None:
        pytest.skip("requires both arducam-ov9782-usb-camera and hd-usb-camera attached")
    slug3, slug2 = id3.slug, id2.slug

    try:
        _open_or_skip(real_daemon_service, slug3)  # open + hold both -- apply_config needs this
        real_daemon_service.get_frame(slug2)

        real_daemon_service.set_camera_config(
            CameraConfig(
                name=slug3, playfield=None, settings={"auto_exposure": 1, "exposure": 6, "gain": 2}
            )
        )
        real_daemon_service.set_camera_config(
            CameraConfig(
                name=slug2,
                playfield=None,
                settings={"auto_exposure": 1, "exposure": 300, "gain": 10},
            )
        )

        cfg3 = real_daemon_service.get_camera_config(slug3)
        cfg2 = real_daemon_service.get_camera_config(slug2)
        assert cfg3.settings["exposure"] == 6
        assert cfg2.settings["exposure"] == 300

        # Stronger than the config-store check above: read each
        # control straight off the hardware via uvc-util, independent
        # of anything DaemonService remembers, to prove the *physical*
        # controls -- not just two in-memory dict entries -- are
        # independent.
        binary = shutil.which("uvc-util")
        if binary is not None:
            assert _uvc_util_current_value(binary, id3.device_name, "exposure-time-abs") == 6.0
            assert _uvc_util_current_value(binary, id2.device_name, "exposure-time-abs") == 300.0
    finally:
        real_daemon_service.set_camera_config(
            CameraConfig(name=slug3, playfield=None, settings=_CAMERA_3_BASELINE_SETTINGS)
        )
        real_daemon_service.set_camera_config(
            CameraConfig(name=slug2, playfield=None, settings=_HD_USB_CAMERA_AUTO_EXPOSURE_DEFAULT)
        )


@pytest.mark.camera
def test_reset_camera_config_reapplies_the_real_on_disk_document_live(
    real_daemon_service, directories
):
    """UC-01's ``ResetCameraConfig`` flow, verified live on camera 3
    (ticket 023-004's AC): install camera 3's baseline as its on-disk
    ``config_dir`` document, push a live override different from it,
    ``reset_camera_config``, and confirm hardware reflects the on-disk
    value again -- verified by ``uvc-util`` readback straight off the
    hardware, independent of anything ``DaemonService``/``Camera``
    remember, with no daemon restart anywhere in the flow."""
    identity = _real_identity("Arducam OV9782")
    if identity is None:
        pytest.skip("no attached camera matching 'Arducam OV9782'")
    slug = identity.slug
    binary = shutil.which("uvc-util")
    if binary is None:
        pytest.skip("uvc-util not found on PATH -- cannot verify by hardware readback")

    config_path = directories.config_dir / "cameras" / slug / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {"name": slug, "playfield": None, "settings": dict(_CAMERA_3_BASELINE_SETTINGS)}
        )
    )

    try:
        _open_or_skip(real_daemon_service, slug)  # open + hold -- wires config_loader/config_path

        real_daemon_service.set_camera_config(
            CameraConfig(
                name=slug,
                playfield=None,
                settings={"auto_exposure": 1, "exposure": 2000, "gain": 40},
            )
        )
        time.sleep(_SETTLE_S)
        assert _uvc_util_current_value(binary, identity.device_name, "exposure-time-abs") == 2000.0

        real_daemon_service.reset_camera_config(slug)
        time.sleep(_SETTLE_S)

        assert _uvc_util_current_value(binary, identity.device_name, "exposure-time-abs") == 6.0
        assert _uvc_util_current_value(binary, identity.device_name, "gain") == 2.0
        assert _uvc_util_current_value(binary, identity.device_name, "auto-exposure-mode") == 1.0
    finally:
        real_daemon_service.set_camera_config(
            CameraConfig(name=slug, playfield=None, settings=_CAMERA_3_BASELINE_SETTINGS)
        )


@pytest.mark.camera
def test_dump_camera_config_round_trips_through_install_and_restart_live(
    real_daemon_service, directories
):
    """UC-24.S-1, live, on camera 3 (ticket 023-005's AC): dump the live
    config, install it under ``config_dir``, simulate a daemon restart
    (a fresh ``DaemonService`` boot-loaded from the same on-disk
    ``config_dir``/registry exactly the way ``daemon.__main__.build()``
    loads a real one -- ticket 023-003's ``_load_config_dir``), and dump
    again. The two documents compare byte-for-byte identical as JSON --
    the round trip ticket 023-005 depends on ticket 023-002 to make
    accurate closes for real."""
    identity = _real_identity("Arducam OV9782")
    if identity is None:
        pytest.skip("no attached camera matching 'Arducam OV9782'")
    slug = identity.slug

    try:
        _open_or_skip(real_daemon_service, slug)  # open + hold -- apply_config needs this
        real_daemon_service.set_camera_config(
            CameraConfig(name=slug, playfield=None, settings=dict(_CAMERA_3_BASELINE_SETTINGS))
        )

        dumped_once = real_daemon_service.get_camera_config(slug).to_dict()

        config_path = directories.config_dir / "cameras" / slug / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(dumped_once, indent=2, sort_keys=True))

        registry = CameraRegistry(directories.state_dir / "cameras")
        restart_discovery = CameraDiscovery(registry, active_slugs=lambda: frozenset())
        restarted = DaemonService(registry=registry, discovery=restart_discovery, directories=directories)
        try:
            daemon_main._load_config_dir(restarted, directories)
            dumped_twice = restarted.get_camera_config(slug).to_dict()
        finally:
            restarted.close()

        assert dumped_twice == dumped_once
    finally:
        real_daemon_service.set_camera_config(
            CameraConfig(name=slug, playfield=None, settings=_CAMERA_3_BASELINE_SETTINGS)
        )


def test_flatfield_path_for_is_state_dir_cameras_slug_flatfield_npy(service, directories):
    """Out-of-process change, 2026-09-02: the per-camera flat-field gain
    map lives next to ``calibration.json`` under ``state_dir``, and is
    what every ``Camera`` built by ``_camera_for`` is wired to."""
    from aprilcam.camera.camera import FLATFIELD_FILENAME

    assert FLATFIELD_FILENAME == "flatfield.npy"
    assert service._flatfield_path_for("cam-x") == (
        directories.state_dir / "cameras" / "cam-x" / "flatfield.npy"
    )


# ---------------------------------------------------------------------------
# capture_flat_field / get_flatfield / clear_flat_field (sprint 037,
# ticket 002) -- DaemonService orchestration around ticket 001's
# FlatFieldCapturer: mask derivation, exposure push/restore, persistence,
# staleness. The gain-map math itself (quality gates, spread) is
# calibration/flatfield.py's own test scope (test_flatfield.py); these
# tests mostly pass force=True so a deterministic quality-gate pass/fail
# never depends on this ticket's own frame content, except where a test's
# whole point is the refusal path.
# ---------------------------------------------------------------------------


def _calibration_for_main_playfield(slug: str = "test-camera") -> Calibration:
    """A real ``Calibration`` solved against ``main_playfield_definition()``
    via ``Calibrator`` directly -- avoids plumbing a second, unrelated
    frame queue through a full ``DaemonService.calibrate()`` RPC just to
    get a stored calibration for a flat-field mask-derivation test."""
    definition = main_playfield_definition()
    frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    calibrator = Calibrator(
        get_camera_config=lambda name: CameraConfig(
            name=name, playfield="main-playfield", settings={}
        ),
        get_playfield=lambda name: definition if name == "main-playfield" else None,
        capture_frames=lambda name: [frame],
    )
    result = calibrator.calibrate(slug)
    assert result.succeeded is True
    calibration = calibrator.get_calibration(slug)
    assert calibration is not None
    return calibration


def _uniform_frame(value: int = 128) -> np.ndarray:
    """A flat, constant-colour frame the same canvas size
    ``render_playfield_frame``'s default produces -- content-neutral for
    every test below that only cares about mask *derivation*, not the
    gain-map math itself."""
    h, w = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS).shape[:2]
    return np.full((h, w, 3), value, dtype=np.uint8)


def _flatfield_paths(directories: Directories, slug: str) -> tuple:
    cam_dir = directories.state_dir / "cameras" / slug
    return cam_dir / "flatfield.npy", cam_dir / "flatfield.json"


def _open_camera_with_frames_and_flatfield_path(
    registry: CameraRegistry,
    directories: Directories,
    frames: List[np.ndarray],
    *,
    hardware_key: str = "hw:ff-live",
    device_name: str = "FlatField Live Camera",
):
    """Like ``_open_camera_with_frames``, but wires ``flatfield_path`` the
    way :meth:`DaemonService._camera_for` always does in production --
    needed only by the handful of tests below that peek at ``Camera.
    _flatfield`` directly to prove a reload actually took effect live.
    Every other flat-field test above uses the plain
    ``_open_camera_with_frames``/``_service_with_camera`` pair, since
    those never read this attribute back."""
    identity = _identity(hardware_key, device_name)
    slug = registry.resolve(identity)
    number = registry.get(slug).number
    backend = FakeBackend(read_queue=list(frames))
    npy_path, _json_path = _flatfield_paths(directories, slug)
    cam = Camera(slug, backend, number=number, flatfield_path=npy_path)
    cam.open()
    return identity, slug, cam, backend


def test_capture_flat_field_uses_stored_calibration_for_the_mask_and_persists(
    directories, registry
):
    identity, slug, cam, backend = _open_camera_with_frames(
        registry, [_uniform_frame() for _ in range(FLATFIELD_FRAME_COUNT + 2)]
    )
    service = _service_with_camera(directories, registry, identity, slug, cam)
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))
    with service._lock:  # noqa: SLF001 - test-only direct calibration injection
        service._calibrations[slug] = _calibration_for_main_playfield(slug)

    result = service.capture_flat_field(slug, force=True)

    assert result.succeeded is True
    assert result.gain is not None

    npy_path, json_path = _flatfield_paths(directories, slug)
    assert npy_path.exists()
    assert json_path.exists()

    info = service.get_flatfield(slug)
    assert info is not None
    assert info.present is True
    assert info.captured_at is not None
    assert info.stale is False
    assert info.shape == tuple(result.gain.shape)


def test_capture_flat_field_falls_back_to_detected_aruco_corners_when_uncalibrated(
    directories, registry
):
    marker_frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    burst_frames = [_uniform_frame() for _ in range(FLATFIELD_FRAME_COUNT + 2)]
    identity, slug, cam, backend = _open_camera_with_frames(
        registry, [marker_frame] + burst_frames
    )
    service = _service_with_camera(directories, registry, identity, slug, cam)
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))
    # Deliberately no stored calibration -- forces the ArUco-corner
    # detection fallback to actually run.
    assert service.get_calibration(slug) is None

    result = service.capture_flat_field(slug, force=True)

    assert result.succeeded is True
    assert service.get_flatfield(slug) is not None


def test_capture_flat_field_raises_no_field_mask_with_neither_calibration_nor_playfield(
    service, held_camera
):
    _, slug, _ = held_camera

    with pytest.raises(NoFieldMask):
        service.capture_flat_field(slug)

    assert service.get_flatfield(slug) is None


def test_capture_flat_field_raises_no_field_mask_when_playfield_linked_but_no_corners_detected(
    directories, registry
):
    identity, slug, cam, backend = _open_camera_with_frames(registry, [_frame()])
    service = _service_with_camera(directories, registry, identity, slug, cam)
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))

    with pytest.raises(NoFieldMask):
        service.capture_flat_field(slug)


def test_capture_flat_field_pushes_and_restores_exposure_on_a_forced_success(
    directories, registry
):
    identity, slug, cam, backend = _open_camera_with_frames(
        registry, [_uniform_frame() for _ in range(FLATFIELD_FRAME_COUNT + 2)]
    )
    service = _service_with_camera(directories, registry, identity, slug, cam)
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(
        CameraConfig(name=slug, playfield="main-playfield", settings={"exposure": 50.0})
    )
    with service._lock:  # noqa: SLF001
        service._calibrations[slug] = _calibration_for_main_playfield(slug)

    pushed = []
    original_set = backend.set_property

    def _spy_set(name, value):
        pushed.append((name, value))
        return original_set(name, value)

    backend.set_property = _spy_set  # type: ignore[method-assign]

    result = service.capture_flat_field(slug, exposure=5.0, force=True)

    assert result.succeeded is True
    assert backend.get_property("exposure") == 50.0  # restored
    exposures_pushed = [value for name, value in pushed if name == "exposure"]
    assert exposures_pushed == [5.0, 50.0]


def test_capture_flat_field_refusal_still_restores_exposure_and_writes_nothing(
    directories, registry
):
    """A frame saturated at the sensor ceiling inside the mask trips the
    in-field-clipping quality gate deterministically (``calibration.
    flatfield.SATURATION_VALUE``) -- exercised here unforced, so the real
    refusal path (not ``force=True``) restores the exposure override and
    leaves no filesystem write."""
    saturated = np.full(_uniform_frame().shape, 255, dtype=np.uint8)
    identity, slug, cam, backend = _open_camera_with_frames(
        registry, [saturated for _ in range(FLATFIELD_FRAME_COUNT + 2)]
    )
    service = _service_with_camera(directories, registry, identity, slug, cam)
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(
        CameraConfig(name=slug, playfield="main-playfield", settings={"exposure": 50.0})
    )
    with service._lock:  # noqa: SLF001
        service._calibrations[slug] = _calibration_for_main_playfield(slug)

    result = service.capture_flat_field(slug, exposure=5.0)

    assert result.succeeded is False
    assert backend.get_property("exposure") == 50.0

    npy_path, json_path = _flatfield_paths(directories, slug)
    assert not npy_path.exists()
    assert not json_path.exists()
    assert service.get_flatfield(slug) is None


def test_capture_flat_field_restores_exposure_even_when_the_capturer_raises(
    directories, registry
):
    identity, slug, cam, backend = _open_camera_with_frames(
        registry, [_uniform_frame() for _ in range(FLATFIELD_FRAME_COUNT + 2)]
    )
    service = _service_with_camera(directories, registry, identity, slug, cam)
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(
        CameraConfig(name=slug, playfield="main-playfield", settings={"exposure": 50.0})
    )
    with service._lock:  # noqa: SLF001
        service._calibrations[slug] = _calibration_for_main_playfield(slug)

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated flat-field build failure")

    service._flatfield_capturer.build = _boom  # type: ignore[method-assign]  # noqa: SLF001

    with pytest.raises(RuntimeError, match="simulated flat-field build failure"):
        service.capture_flat_field(slug, exposure=5.0)

    assert backend.get_property("exposure") == 50.0


def test_capture_flat_field_dry_run_writes_nothing_and_does_not_change_the_live_map(
    directories, registry
):
    identity, slug, cam, backend = _open_camera_with_frames(
        registry, [_uniform_frame() for _ in range(FLATFIELD_FRAME_COUNT + 2)]
    )
    service = _service_with_camera(directories, registry, identity, slug, cam)
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))
    with service._lock:  # noqa: SLF001
        service._calibrations[slug] = _calibration_for_main_playfield(slug)

    result = service.capture_flat_field(slug, force=True, dry_run=True)

    assert result.succeeded is True
    assert result.gain is not None

    npy_path, json_path = _flatfield_paths(directories, slug)
    assert not npy_path.exists()
    assert not json_path.exists()
    assert service.get_flatfield(slug) is None
    assert cam._flatfield is None  # noqa: SLF001 - never reloaded


def _gradient_frame() -> np.ndarray:
    """A left-to-right brightness ramp, same canvas size as
    ``render_playfield_frame``'s default -- unlike :func:`_uniform_frame`,
    this produces a genuinely spatially-varying (non-1.0-everywhere) gain
    map, needed by the one test below that proves a subsequent raw read
    actually comes back pixel-corrected, not merely multiplied by a
    uniform no-op gain of 1.0."""
    h, w = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS).shape[:2]
    ramp = np.linspace(60.0, 200.0, w, dtype=np.float32)
    frame = np.repeat(np.tile(ramp, (h, 1))[:, :, None], 3, axis=2)
    return frame.astype(np.uint8)


def test_capture_flat_field_persists_and_a_freshly_constructed_service_reloads_it(
    directories, registry
):
    identity, slug, cam, backend = _open_camera_with_frames_and_flatfield_path(
        registry, directories, [_gradient_frame() for _ in range(FLATFIELD_FRAME_COUNT + 2)]
    )
    service = _service_with_camera(directories, registry, identity, slug, cam)
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))
    with service._lock:  # noqa: SLF001
        service._calibrations[slug] = _calibration_for_main_playfield(slug)
    assert cam._flatfield is None  # noqa: SLF001 - nothing loaded yet

    result = service.capture_flat_field(slug, force=True)
    assert result.succeeded is True
    info = service.get_flatfield(slug)
    assert info is not None

    # Proves the reload actually took effect live, with no daemon
    # restart and no explicit second call -- capture_flat_field's own
    # persistence step already reloaded it.
    assert cam._flatfield is not None  # noqa: SLF001

    # AC: "verified via a subsequent get_frame/read on the same
    # held-open camera" -- a fresh, differently-valued raw frame read
    # right after capture comes back gain-corrected (not bit-identical
    # to what the backend actually handed out), proving the reload is
    # live, not merely an in-memory flag.
    raw_next = np.full_like(_uniform_frame(), 200)
    backend._queue = [raw_next]  # noqa: SLF001 - append one more frame to read
    corrected = cam.read()
    assert corrected is not None
    assert not np.array_equal(corrected, raw_next)

    discovery2 = CameraDiscovery(
        registry,
        active_slugs=lambda: frozenset(),
        identity_source=lambda: {},
        backend_factory=lambda index: FakeBackend(),
    )
    service2 = DaemonService(
        registry=registry, discovery=discovery2, directories=directories, identity_source=lambda: {}
    )
    reloaded = service2.get_flatfield(slug)
    assert reloaded == info


def test_load_flatfields_reports_present_true_captured_at_none_for_an_npy_with_no_sidecar(
    directories, registry
):
    """The manual-recipe migration case (``docs/knowledge/flat-field-gain-map.md``):
    a ``flatfield.npy`` installed by hand, with no ``flatfield.json``
    sidecar, must never report a fabricated ``captured_at``."""
    identity = _identity("hw:manual-ff", "Manual FlatField Camera")
    slug = registry.resolve(identity)
    npy_path = directories.state_dir / "cameras" / slug / "flatfield.npy"
    npy_path.parent.mkdir(parents=True, exist_ok=True)
    np.save(npy_path, np.ones((4, 4), dtype=np.float32))

    discovery = CameraDiscovery(
        registry,
        active_slugs=lambda: frozenset(),
        identity_source=lambda: {},
        backend_factory=lambda index: FakeBackend(),
    )
    service = DaemonService(
        registry=registry, discovery=discovery, directories=directories, identity_source=lambda: {}
    )

    info = service.get_flatfield(slug)

    assert info is not None
    assert info.present is True
    assert info.captured_at is None


def test_get_flatfield_returns_none_for_a_camera_never_captured(service, held_camera):
    _, slug, _ = held_camera

    assert service.get_flatfield(slug) is None


def test_clear_flat_field_deletes_files_and_drops_the_live_map(directories, registry):
    identity, slug, cam, backend = _open_camera_with_frames_and_flatfield_path(
        registry, directories, [_uniform_frame() for _ in range(FLATFIELD_FRAME_COUNT + 2)]
    )
    service = _service_with_camera(directories, registry, identity, slug, cam)
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))
    with service._lock:  # noqa: SLF001
        service._calibrations[slug] = _calibration_for_main_playfield(slug)
    assert service.capture_flat_field(slug, force=True).succeeded is True
    assert cam._flatfield is not None  # noqa: SLF001

    service.clear_flat_field(slug)

    npy_path, json_path = _flatfield_paths(directories, slug)
    assert not npy_path.exists()
    assert not json_path.exists()
    assert service.get_flatfield(slug) is None
    assert cam._flatfield is None  # noqa: SLF001 - live map dropped, no restart


def test_clear_flat_field_on_an_absent_flatfield_is_a_no_op(service, held_camera):
    _, slug, _ = held_camera

    service.clear_flat_field(slug)  # must not raise

    assert service.get_flatfield(slug) is None


#: Sprint 037's own measured no-clip exposure for camera 3
#: (``docs/knowledge/flat-field-gain-map.md``, measured 2026-09-02): both
#: real cameras saturate a large fraction of the field at their normal
#: working/swept exposure (camera 3: 86% of the field at 255 at
#: exposure=6), so this test -- the one place a real capture burst is
#: actually built -- must push the same kind of low override
#: ``aprilcam camera flatfield --exposure`` documents, or the quality
#: gate's in-field-clipping check would legitimately refuse the attempt
#: regardless of camera aim or field content.
_FLATFIELD_TEST_EXPOSURE = 1.0


@pytest.mark.camera
def test_capture_flat_field_installs_and_applies_a_real_gain_map(real_daemon_service, directories):
    """Ticket 037-006's own real-hardware AC: one real ``CaptureFlatField``
    (through ``DaemonService`` directly, mirroring every other test in
    this module's real-hardware section) against camera 3, verifying the
    built map installs and is actually applied on a subsequent real frame
    read -- then ``ClearFlatField`` removes it again.

    Unlike the manual-recipe measurements in
    ``docs/knowledge/flat-field-gain-map.md`` (captured against a plain
    white sheet laid over the field), this test runs unattended against
    whatever the real field currently shows -- there is no actuator here
    to lay a sheet down. So it does not assert the sheet-only numbers
    from that doc verbatim; instead it asserts what genuinely holds for
    *any* real capture at a deliberately low, non-clipping exposure:

    * the chosen exposure is not itself refused for in-field clipping
      (the specific problem a low override exists to avoid -- both real
      cameras clip most of the field at their normal working exposure);
    * the correction generally reduces, not worsens, the field's overall
      percentile brightness spread (flattening low-frequency lighting
      unevenness pulls extremes toward the mean even when local
      high-contrast content, like tag ink, is untouched by a
      low-frequency gain map);
    * the map is genuinely installed (``get_flatfield`` reports
      ``present``, with the persisted shape matching the built gain
      array) and live-applied on the held-open ``Camera`` with no daemon
      restart (mirrors ``test_capture_flat_field_..._reloads_the_live_map``'s
      own ``cam._flatfield is not None`` check, plus one real subsequent
      frame read to prove the shape-compatible correction path actually
      runs against real pixels, not just a loaded array);
    * ``clear_flat_field`` removes both the live map and the persisted
      files.

    This fixture's ``directories``/``registry`` (see ``real_daemon_service``)
    live entirely under ``tmp_path``, never the real operator's
    ``~/.local/share/aprilcam`` -- so there is no on-disk flat-field state
    from any other process for this test to preserve or restore; the only
    real, cross-process state it touches is the physical camera's UVC
    exposure/gain, restored in ``finally`` exactly like every other real
    hardware test in this section.

    Skips cleanly -- never fails -- when no camera matching 'Arducam
    OV9782' (camera 3, the currently-calibrated main-playfield camera) is
    attached, or when it is attached but this process cannot open it
    right now (:func:`_open_or_skip`)."""
    identity = _real_identity("Arducam OV9782")
    if identity is None:
        pytest.skip("no attached camera matching 'Arducam OV9782'")
    slug = identity.slug

    try:
        _open_or_skip(real_daemon_service, slug)  # open + hold -- capture_flat_field needs this
        cam = real_daemon_service._cameras[slug]  # noqa: SLF001 - peek at the held-open Camera

        real_daemon_service.set_playfield(main_playfield_definition())
        # A real, freshly-swept detection-friendly exposure -- this
        # becomes capture_flat_field's "prior config", restored
        # automatically after the burst's own low-exposure override
        # (see the RPC's own docstring). Also links the playfield so
        # _field_mask_for's live-ArUco fallback (no stored Calibration
        # here) can resolve it.
        real_daemon_service.set_camera_config(
            hardware_test_camera_config(slug, playfield="main-playfield", camera=cam)
        )
        assert cam._flatfield is None  # noqa: SLF001 - clean start, fresh tmp_path state

        result = real_daemon_service.capture_flat_field(
            slug, exposure=_FLATFIELD_TEST_EXPOSURE, force=True
        )

        assert result.gain is not None
        assert result.succeeded is True  # force=True always persists
        # The specific problem a low override exists to avoid -- assert
        # the *clipping* check by name never fired, whatever else this
        # unattended field's real content did or did not trip.
        assert "clipping" not in result.forced_checks
        assert max(result.spread_after) < max(result.spread_before)

        info = real_daemon_service.get_flatfield(slug)
        assert info is not None
        assert info.present is True
        assert info.shape == tuple(result.gain.shape)

        # Live-applied with no daemon restart -- the same "reload
        # actually took effect" check the offline tests above make.
        assert cam._flatfield is not None  # noqa: SLF001

        # A real subsequent frame read must succeed cleanly through the
        # now-corrected path (proves the persisted map's shape matches
        # this camera's real frame shape -- a mismatch is skipped with a
        # warning, not an exception, so this is the strongest "applied"
        # check available without a second, uncorrected camera handle).
        applied_frame = real_daemon_service.get_frame(slug)
        assert applied_frame is not None
        assert len(applied_frame.data) > 0

        real_daemon_service.clear_flat_field(slug)

        assert real_daemon_service.get_flatfield(slug) is None
        assert cam._flatfield is None  # noqa: SLF001
        npy_path, json_path = _flatfield_paths(directories, slug)
        assert not npy_path.exists()
        assert not json_path.exists()
    finally:
        real_daemon_service.set_camera_config(
            CameraConfig(name=slug, playfield=None, settings=_CAMERA_3_BASELINE_SETTINGS)
        )


# ---------------------------------------------------------------------------
# Flat-field staleness (sprint 037): the same two triggers ticket 023-007
# already established for calibration -- a successful self-heal/explicit
# reopen, and a resolution-changing set_camera_config push.
# ---------------------------------------------------------------------------


def test_camera_reopen_via_self_heal_marks_the_flatfield_stale_through_real_wiring(
    directories, registry
):
    """Mirrors ``test_camera_reopen_via_self_heal_marks_the_calibration_stale_through_real_wiring``
    exactly, through the same real ``_camera_for``-constructed ``Camera``
    wiring -- a directly-injected ``Camera`` (``_open_camera_with_frames``)
    would bypass the ``on_reopen`` hook entirely."""
    frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    identity = _identity("hw:ff-reopen", "FlatField Reopen Camera")
    slug = registry.resolve(identity)
    backend = FakeBackend(read_queue=[frame] * CALIBRATION_FRAME_COUNT)
    discovery = CameraDiscovery(
        registry,
        active_slugs=lambda: frozenset(),
        identity_source=lambda: {0: identity},
        backend_factory=lambda index: backend,
    )
    service = DaemonService(
        registry=registry,
        discovery=discovery,
        directories=directories,
        identity_source=lambda: {0: identity},
        backend_factory=lambda index: backend,
    )
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))
    assert service.calibrate(slug).succeeded is True

    # Seed a not-yet-stale flat-field entry directly -- capture_flat_field's
    # own orchestration is covered by its own tests above; this test only
    # needs an entry for the reopen trigger to flip.
    with service._lock:  # noqa: SLF001
        service._flatfields[slug] = FlatFieldInfo(present=True, captured_at=time.time())

    cam = service._camera_for(slug)  # noqa: SLF001 - same Camera calibrate() opened
    opens_before = backend.open_calls
    for _ in range(FAILURE_THRESHOLD):
        cam.read()  # queue is now empty -- every read fails; the 5th triggers self-heal

    assert backend.open_calls == opens_before + 1  # the reopen genuinely happened
    info = service.get_flatfield(slug)
    assert info is not None
    assert info.stale is True


def test_a_resolution_changing_set_camera_config_push_marks_the_flatfield_stale(
    directories, registry
):
    identity, slug, cam, backend = _open_camera_with_frames(registry, [_frame()])
    service = _service_with_camera(directories, registry, identity, slug, cam)
    service.set_camera_config(
        CameraConfig(
            name=slug, playfield=None, settings={"frame_width": 640.0, "frame_height": 480.0}
        )
    )
    with service._lock:  # noqa: SLF001
        service._flatfields[slug] = FlatFieldInfo(present=True, captured_at=time.time())

    service.set_camera_config(
        CameraConfig(
            name=slug, playfield=None, settings={"frame_width": 1280.0, "frame_height": 480.0}
        )
    )

    info = service.get_flatfield(slug)
    assert info is not None
    assert info.stale is True


def test_a_non_resolution_set_camera_config_push_does_not_mark_the_flatfield_stale(
    directories, registry
):
    identity, slug, cam, backend = _open_camera_with_frames(registry, [_frame()])
    service = _service_with_camera(directories, registry, identity, slug, cam)
    service.set_camera_config(
        CameraConfig(
            name=slug, playfield=None, settings={"frame_width": 640.0, "frame_height": 480.0}
        )
    )
    with service._lock:  # noqa: SLF001
        service._flatfields[slug] = FlatFieldInfo(present=True, captured_at=time.time())

    service.set_camera_config(
        CameraConfig(name=slug, playfield=None, settings={"exposure": 12.0})
    )

    info = service.get_flatfield(slug)
    assert info is not None
    assert info.stale is False


def test_list_cameras_overlays_flatfield_present_and_stale_for_a_held_camera(
    directories, registry
):
    identity, slug, cam, backend = _open_camera_with_frames(
        registry, [_uniform_frame() for _ in range(FLATFIELD_FRAME_COUNT + 2)]
    )
    service = _service_with_camera(directories, registry, identity, slug, cam)
    service.set_playfield(main_playfield_definition())
    service.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))
    with service._lock:  # noqa: SLF001
        service._calibrations[slug] = _calibration_for_main_playfield(slug)
    assert service.capture_flat_field(slug, force=True).succeeded is True

    states = {s.name: s for s in service.list_cameras()}

    assert states[slug].flatfield_present is True
    assert states[slug].flatfield_stale is False


def test_list_cameras_reports_flatfield_absent_for_a_not_held_registered_camera(
    directories, registry
):
    identity = _identity("hw:ff-absent", "FlatField Absent Camera")
    slug = registry.resolve(identity)
    discovery = CameraDiscovery(
        registry,
        active_slugs=lambda: frozenset(),
        identity_source=lambda: {},
        backend_factory=lambda index: FakeBackend(),
    )
    service = DaemonService(
        registry=registry, discovery=discovery, directories=directories, identity_source=lambda: {}
    )

    states = {s.name: s for s in service.list_cameras()}

    assert states[slug].flatfield_present is False
    assert states[slug].flatfield_stale is False
