"""Tests for ``aprilcam.client.daemon_client`` and
``aprilcam.client.models`` (ticket 020-005).

Fully headless: reuses ticket 004's fake-camera daemon fixture
construction (``tests/aprilcam/daemon/test_service.py``'s
``held_camera``/``service`` pattern -- a registered camera held open
via :class:`FakeBackend`, no real hardware) and binds a real
``grpc.Server`` to a ``tmp_path`` unix socket, exactly like
``test_service.py``'s own ``stub`` fixture. The difference is what
drives the RPCs: here it is ``client.daemon_client.GrpcDaemon`` (the
client-side wrapper this ticket adds), not the bare generated stub --
so these tests exercise the full client-side proto boundary
(``client/models.py``'s ``from_proto`` conversions returning genuine
``aprilcam.types`` dataclasses) and typed-error reconstruction from
gRPC trailing metadata, on top of the wire format ticket 004 already
covers.
"""

from __future__ import annotations

import shutil
import tempfile
from concurrent import futures
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator

import grpc
import numpy as np
import pytest

from aprilcam.camera import identity as identity_module
from aprilcam.camera.camera import Camera
from aprilcam.camera.discovery import CameraDiscovery
from aprilcam.camera.identity import CameraIdentity
from aprilcam.camera.registry import CameraRegistry
from aprilcam.client.daemon_client import GrpcDaemon
from aprilcam.client.guides import Guides
from aprilcam.config.directories import Directories
from aprilcam.config.interfaces import CameraConfig, CameraPosition, Feature
from aprilcam.config.loader import PlayfieldDefinition, PlayfieldFeatureEntry
from aprilcam.daemon.interfaces import MountRegistration
from aprilcam.daemon.rpc import PROTOCOL_VERSION, build_server
from aprilcam.daemon.service import (
    CALIBRATION_FRAME_COUNT,
    _MAX_ANNOTATIONS_PER_LAYER,
    _MAX_POINTS_PER_SHAPE,
    DaemonService,
)
from aprilcam.errors import (
    AnnotationLimitExceeded,
    CameraNotPresent,
    ConnectionFailed,
    DaemonVersionMismatch,
    NotCalibrated,
    UnknownCamera,
    UnknownGuide,
    UnknownPlayfield,
    UnsupportedSetting,
)
from aprilcam.proto import aprilcam_v2_pb2, aprilcam_v2_pb2_grpc
from aprilcam.types import (
    Annotation,
    AnnotationSet,
    CameraState,
    Circle,
    ImageFrame,
    Marker,
    MountParameters,
    PathShape,
    Polygon,
    ProbeReport,
    Style,
    TagFamily,
    TagFrame,
    TagId,
    TagRecord,
    Text,
    WorldPoint,
)
from tests.aprilcam.calibration.test_calibration import (
    MAIN_PLAYFIELD_ARUCO_POSITIONS,
    main_playfield_definition,
    render_playfield_frame,
)
from tests.aprilcam.camera.test_camera import FakeBackend
from tests.aprilcam.daemon.test_service import (
    _build_tag_service,
    _CountingDetector,
    _detection,
    _open_camera_with_frames,
    _playfield_definition,
    _ScriptedDetector,
    _service_with_camera,
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
        active_camera_state=lambda slug: cameras[slug].state if slug in cameras else None,
        identity_source=lambda: {0: identity},
        backend_factory=lambda index: FakeBackend(),
    )
    return DaemonService(
        registry=registry,
        discovery=discovery,
        directories=directories,
        cameras=cameras,
        # Headless-safe: see test_service.py's identical note -- without
        # this, _camera_for's _index_for falls through to real hardware
        # enumeration for any registered-but-uncached camera.
        identity_source=lambda: {0: identity},
    )


@pytest.fixture
def absent_camera(registry: CameraRegistry):
    """A registered-but-not-currently-enumerated camera -- see
    test_service.py's identical fixture for the full rationale."""
    identity = _identity("hw:absent", "Absent Camera")
    slug = registry.resolve(identity)
    number = registry.get(slug).number
    return slug, number


@contextmanager
def _bind_client(service) -> Iterator[GrpcDaemon]:
    # A short, top-level /tmp dir -- not pytest's tmp_path, whose deeply
    # nested per-test path routinely exceeds unix domain sockets'
    # ~103-character path limit on macOS/Linux (same workaround as
    # test_service.py's ``stub`` fixture). Factored out of the
    # ``client`` fixture below (ticket 021-005) so the tag-detection
    # round-trip tests can bind a *different* service per test (each
    # needs its own scripted detector).
    socket_dir = Path(tempfile.mkdtemp(prefix="ac-dc-", dir="/tmp"))
    socket_path = socket_dir / "control.sock"
    try:
        listener = build_server(service, socket_path)
        listener.start()
        channel = grpc.insecure_channel(f"unix:{socket_path}")
        try:
            grpc.channel_ready_future(channel).result(timeout=5)
            stub = aprilcam_v2_pb2_grpc.AprilCamV2Stub(channel)
            yield GrpcDaemon(stub, target="localhost")
        finally:
            channel.close()
            listener.stop(1.0)
    finally:
        shutil.rmtree(socket_dir, ignore_errors=True)


@pytest.fixture
def client(service) -> Iterator[GrpcDaemon]:
    with _bind_client(service) as c:
        yield c


@pytest.fixture
def tag_client_factory(directories, registry):
    """Factory fixture: build a ``GrpcDaemon`` connected to a fresh
    ``DaemonService`` wired to a caller-supplied scripted/counting
    detector (reusing ``test_service.py``'s ``_build_tag_service`` --
    the same cross-module-fixture convention ``test_service.py``
    itself follows for ``FakeBackend``), yielding ``(client, slug)``.
    """

    @contextmanager
    def _make(detector, *, frame_count: int = 10):
        service, slug = _build_tag_service(
            directories, registry, detector=detector, frame_count=frame_count
        )
        with _bind_client(service) as client:
            yield client, slug

    return _make


# ---------------------------------------------------------------------------
# Round trips -- proto boundary: real aprilcam.types dataclasses out
# ---------------------------------------------------------------------------


def test_list_cameras_round_trip_returns_camera_state_dataclasses(client, held_camera):
    _, slug, _ = held_camera

    states = client.list_cameras()

    assert len(states) == 1
    assert isinstance(states[0], CameraState)
    assert states[0].name == slug
    assert states[0].present is True
    assert states[0].usable is True
    assert states[0].number is not None


def test_probe_cameras_round_trip_returns_probe_report(client, held_camera):
    _, slug, _ = held_camera

    report = client.probe_cameras()

    assert isinstance(report, ProbeReport)
    assert len(report.skipped) == 1
    assert report.skipped[0].backend == slug


def test_get_frame_jpeg_round_trip(client, held_camera):
    _, slug, _ = held_camera

    frame = client.get_frame(slug)

    assert isinstance(frame, ImageFrame)
    assert frame.encoding == "jpeg"
    assert frame.width == 4 and frame.height == 4
    assert frame.data[:2] == b"\xff\xd8"  # JPEG SOI marker
    assert frame.deskewed is False


def test_get_frame_png_round_trip(client, held_camera):
    _, slug, _ = held_camera

    frame = client.get_frame(slug, encoding="png")

    assert frame.encoding == "png"
    assert frame.data[:8] == b"\x89PNG\r\n\x1a\n"  # PNG signature


def test_set_camera_config_and_get_camera_config_round_trip_camera_position(client, held_camera):
    """Ticket 029-001, SUC-002: the full client stack (``GrpcDaemon.
    set_camera_config``/``get_camera_config``, not just the raw gRPC
    stub -- ``test_service.py`` covers that layer) carries
    ``camera_position`` both directions as a real ``CameraPosition``
    dataclass, not proto fields."""
    _, slug, _ = held_camera
    pushed = CameraConfig(
        name=slug,
        playfield="main-playfield",
        settings={},
        camera_position=CameraPosition(nadir=WorldPoint(x=3.05, y=-2.75), height_cm=128.25),
    )

    client.set_camera_config(pushed)
    fetched = client.get_camera_config(slug)

    assert fetched.camera_position == CameraPosition(
        nadir=WorldPoint(x=3.05, y=-2.75), height_cm=128.25
    )


def test_get_camera_config_camera_position_none_when_unset(client, held_camera):
    _, slug, _ = held_camera
    client.set_camera_config(CameraConfig(name=slug, playfield="main-playfield", settings={}))

    fetched = client.get_camera_config(slug)

    assert fetched.camera_position is None


def test_set_camera_config_with_non_numeric_setting_raises_unsupported_setting_with_fields_intact(
    client, held_camera
):
    """2026-08-22 hotfix: camera 3's real on-disk config held
    ``"exposure": "100"`` (a JSON string). Before this fix, a
    non-numeric settings value crashed ``Camera.apply_config`` with a
    bare, untyped ``TypeError`` that ``daemon/rpc.py``'s ``_abort``
    could only report as opaque ``INTERNAL`` text -- and even a clean
    ``UnsupportedSetting`` had no trailing-metadata mapping at all
    (``UnsupportedSetting`` was entirely absent from ``_STATUS_CODES``
    and from ``daemon_client.py``'s ``_reconstruct_error``), so it
    reconstructed client-side as a generic
    ``ConnectionFailed("did not answer: ...")`` instead of the typed
    exception a caller could actually catch. This confirms the full
    round trip -- daemon-side raise, RPC trailing metadata, client-side
    reconstruction -- now produces the real ``UnsupportedSetting``,
    naming the key, over the real gRPC wire (not just a direct
    in-process ``Camera.apply_config`` call, which
    ``test_camera.py`` already covers)."""
    _, slug, _ = held_camera

    with pytest.raises(UnsupportedSetting) as excinfo:
        client.set_camera_config(
            CameraConfig(name=slug, playfield=None, settings={"exposure": "not-a-number"})
        )

    assert excinfo.value.key == "exposure"
    assert excinfo.value.reason is not None
    assert "not-a-number" in excinfo.value.reason


def test_status_round_trip(client, directories, held_camera):
    _, slug, _ = held_camera

    status = client.status()

    assert status.running is True
    assert [c.name for c in status.cameras] == [slug]
    assert status.config_dir == str(directories.config_dir)
    assert status.state_dir == str(directories.state_dir)


def test_get_status_response_carries_protocol_version_on_the_wire(service):
    """Ticket 031-001, SUC-001 main flow step 2: a compatible v2 daemon's
    ``GetStatus`` response always carries ``protocol_version`` set to
    ``daemon.rpc.PROTOCOL_VERSION`` -- a plain protobuf serialization
    round trip through the real server (``daemon/rpc.py``'s
    ``_status_to_proto``), not just a Python-level dataclass check.
    Deliberately drives the raw generated stub, not ``GrpcDaemon`` --
    ``daemon.interfaces.DaemonStatus`` does not carry this field (it is
    a protocol-level fact about the daemon build, not part of the
    business-level status this ticket's plan scopes it to), so the field
    is only observable on the wire message itself."""
    socket_dir = Path(tempfile.mkdtemp(prefix="ac-dc-pv-", dir="/tmp"))
    socket_path = socket_dir / "control.sock"
    try:
        listener = build_server(service, socket_path)
        listener.start()
        channel = grpc.insecure_channel(f"unix:{socket_path}")
        try:
            grpc.channel_ready_future(channel).result(timeout=5)
            stub = aprilcam_v2_pb2_grpc.AprilCamV2Stub(channel)

            response = stub.GetStatus(aprilcam_v2_pb2.GetStatusRequest())

            assert response.protocol_version == PROTOCOL_VERSION
        finally:
            channel.close()
            listener.stop(1.0)
    finally:
        shutil.rmtree(socket_dir, ignore_errors=True)


def test_status_round_trip_unaffected_by_protocol_version_field(client, directories, held_camera):
    """SUC-001's third acceptance bullet: a v2 client talking to a
    compatible v2 daemon is unaffected by the new field -- no new
    failure mode, ``client.status()`` still returns the same
    ``DaemonStatus`` shape it always did, in one round trip. (The
    positive-path counterpart to
    ``test_get_status_response_carries_protocol_version_on_the_wire``
    above, driven through the full client stack instead of the raw
    stub.)"""
    _, slug, _ = held_camera

    status = client.status()

    assert status.running is True
    assert [c.name for c in status.cameras] == [slug]


# ---------------------------------------------------------------------------
# Typed errors survive the round trip as the right exception classes
# ---------------------------------------------------------------------------


def test_get_frame_unknown_camera_raises_unknown_camera_with_fields_intact(
    client, held_camera, registry
):
    _, slug, _ = held_camera
    number = registry.get(slug).number

    with pytest.raises(UnknownCamera) as excinfo:
        client.get_frame("no-such-camera")

    # The typed fields survive the round trip intact -- not just the
    # message text: the registry's known cameras as "{number} {slug}"
    # pairs (ticket 021-002), not an empty tuple and not bare slugs.
    assert excinfo.value.requested == "no-such-camera"
    assert excinfo.value.available == (f"{number} {slug}",)


def test_get_frame_deskewed_raises_not_calibrated(client, held_camera):
    _, slug, _ = held_camera

    with pytest.raises(NotCalibrated):
        client.get_frame(slug, deskewed=True)


def test_get_frame_by_number_round_trip(client, held_camera, registry):
    _, slug, _ = held_camera
    number = registry.get(slug).number

    frame = client.get_frame(str(number))

    assert isinstance(frame, ImageFrame)
    assert frame.data[:2] == b"\xff\xd8"


def test_get_frame_camera_not_present_raises_camera_not_present_with_slug_intact(
    client, absent_camera
):
    slug, _number = absent_camera

    with pytest.raises(CameraNotPresent) as excinfo:
        client.get_frame(slug)

    assert excinfo.value.slug == slug


def test_rpc_failure_after_server_shutdown_raises_connection_failed(service):
    """An unclassified RPC failure (no ``aprilcam-error-type`` metadata
    -- here, the server going away mid-session) is reported as
    ConnectionFailed naming the target, not a bare grpc.RpcError."""
    socket_dir = Path(tempfile.mkdtemp(prefix="ac-dc-down-", dir="/tmp"))
    socket_path = socket_dir / "control.sock"
    try:
        listener = build_server(service, socket_path)
        listener.start()
        channel = grpc.insecure_channel(f"unix:{socket_path}")
        grpc.channel_ready_future(channel).result(timeout=5)
        stub = aprilcam_v2_pb2_grpc.AprilCamV2Stub(channel)
        client = GrpcDaemon(stub, target="localhost")

        listener.stop(0)

        with pytest.raises(ConnectionFailed) as excinfo:
            client.status()
        assert excinfo.value.host == "localhost"

        channel.close()
    finally:
        shutil.rmtree(socket_dir, ignore_errors=True)


def test_status_against_daemon_with_no_v2_service_raises_daemon_version_mismatch():
    """Ticket 031-001, SUC-001 main flow steps 3-4: a daemon that
    implements none of ``aprilcam_v2.proto``'s RPCs -- exactly what a v1
    daemon (from a sibling checkout) sharing this project's conventional
    control-plane socket path looks like -- fails ``GetStatus``, the
    first RPC any fresh connection makes, with ``UNIMPLEMENTED`` and no
    ``aprilcam-error-type`` trailing metadata. This is deliberately *not*
    the ``service``/``build_server`` fixture: binding a bare ``grpc.
    server()`` with no servicer registered at all is the most faithful
    simulation of "the responder does not speak this protocol," since
    grpc-python's own UNIMPLEMENTED-for-unregistered-method behavior is
    exactly what produces v1's real-world ``Method not found!``. The
    client raises the new, distinctly-named ``DaemonVersionMismatch`` --
    not the generic ``ConnectionFailed`` a "nothing is listening" failure
    would produce (see ``test_rpc_failure_after_server_shutdown_raises_
    connection_failed`` above for that contrasting case, which must stay
    ``ConnectionFailed`` after this ticket, not be swept into this new
    branch)."""
    socket_dir = Path(tempfile.mkdtemp(prefix="ac-dc-v1sim-", dir="/tmp"))
    socket_path = socket_dir / "control.sock"
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    server.add_insecure_port(f"unix:{socket_path}")
    server.start()
    try:
        channel = grpc.insecure_channel(f"unix:{socket_path}")
        try:
            grpc.channel_ready_future(channel).result(timeout=5)
            stub = aprilcam_v2_pb2_grpc.AprilCamV2Stub(channel)
            client = GrpcDaemon(stub, target="localhost")

            with pytest.raises(DaemonVersionMismatch) as excinfo:
                client.status()

            assert excinfo.value.target == "localhost"
            assert "localhost" in str(excinfo.value)
            assert "incompatible protocol" in str(excinfo.value)
        finally:
            channel.close()
    finally:
        server.stop(0)
        shutil.rmtree(socket_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# get_tags / get_tag / get_tag_history -- client-side round trips
# (ticket 021-005). Exercises the full proto boundary: real
# aprilcam.types dataclasses out, with the None-not-0.0 discipline
# intact after crossing client/models.py's from_proto conversions, not
# just HasField() at the raw protobuf level (test_service.py covers
# that layer).
# ---------------------------------------------------------------------------


def test_get_tags_round_trip_returns_a_tag_frame_of_honestly_uncalibrated_records(
    tag_client_factory,
):
    detector = _ScriptedDetector([[_detection(7)]])
    with tag_client_factory(detector, frame_count=1) as (client, slug):
        tag_frame = client.get_tags(slug)

    assert isinstance(tag_frame, TagFrame)
    assert len(tag_frame.tags) == 1
    record = tag_frame.tags[0]
    assert isinstance(record, TagRecord)
    assert record.tag == TagId(family=TagFamily.APRILTAG, number=7)
    assert record.calibrated is False
    # None, never 0.0/empty -- the client-side dataclass must not lose
    # this distinction after crossing client/models.py.
    assert record.world is None
    assert record.heading_rad is None
    assert record.world_velocity is None
    assert record.speed is None
    assert record.yaw_rad is None
    assert record.world_corners is None
    assert record.pixel is not None
    assert record.pixel_corners is not None
    assert len(record.pixel_corners) == 4
    # Ticket 039-002: image_yaw_deg is a real end-to-end RPC round-trip
    # concern, not just a dataclass field -- daemon/rpc.py's
    # _tag_record_to_proto and client/models.py's tag_record_from_proto
    # both need an explicit case for it (aprilcam_v2.proto field 12),
    # unlike the MCP/pydantic boundary where the field rides along for
    # free. _detection(7)'s square corners give a known, non-degenerate
    # front edge (front_left=(0,0), front_right=(10,0) -> straight
    # "right" on screen -> 90.0 degrees per _image_space_yaw's
    # convention), so this is not just "not None" but the exact value.
    assert record.image_yaw_deg == pytest.approx(90.0)


def test_get_tags_round_trip_empty_field_returns_empty_tag_frame(tag_client_factory):
    detector = _ScriptedDetector([[]])
    with tag_client_factory(detector, frame_count=1) as (client, slug):
        tag_frame = client.get_tags(slug)

    assert tag_frame.tags == ()
    assert tag_frame.calibration_stale is False


def test_get_tags_unknown_camera_raises_unknown_camera(client):
    with pytest.raises(UnknownCamera) as excinfo:
        client.get_tags("no-such-camera")
    assert excinfo.value.requested == "no-such-camera"


def test_get_tag_round_trip_found_returns_a_tag_record(tag_client_factory):
    detector = _ScriptedDetector([[_detection(7)]])
    with tag_client_factory(detector, frame_count=1) as (client, slug):
        record = client.get_tag(slug, TagId(family=TagFamily.APRILTAG, number=7))

    assert record is not None
    assert isinstance(record, TagRecord)
    assert record.tag == TagId(family=TagFamily.APRILTAG, number=7)


def test_get_tag_round_trip_not_found_returns_none_never_a_zero_valued_record(
    tag_client_factory,
):
    detector = _ScriptedDetector([[]])
    with tag_client_factory(detector, frame_count=1) as (client, slug):
        record = client.get_tag(slug, TagId(family=TagFamily.APRILTAG, number=99))

    assert record is None


def test_get_tag_unknown_camera_raises_unknown_camera(client):
    with pytest.raises(UnknownCamera):
        client.get_tag("no-such-camera", TagId(family=TagFamily.APRILTAG, number=1))


def test_get_tag_history_round_trip_preserves_explicit_gap(tag_client_factory):
    target = TagId(family=TagFamily.APRILTAG, number=7)
    script = [[_detection(7)], [_detection(9)], [_detection(7)]]
    detector = _ScriptedDetector(script)
    with tag_client_factory(detector, frame_count=3) as (client, slug):
        client.get_tags(slug)
        client.get_tags(slug)
        client.get_tags(slug)

        history = client.get_tag_history(slug, tag=target)

    assert isinstance(history, tuple)
    assert len(history) == 3
    assert all(isinstance(f, TagFrame) for f in history)
    assert len(history[0].tags) == 1 and history[0].tags[0].tag == target
    assert history[1].tags == ()  # explicit gap -- retained, empty
    assert len(history[2].tags) == 1 and history[2].tags[0].tag == target


def test_get_tag_history_round_trip_with_no_prior_queries_returns_empty_tuple(
    tag_client_factory,
):
    detector = _CountingDetector()
    with tag_client_factory(detector, frame_count=1) as (client, slug):
        history = client.get_tag_history(slug)

    assert history == ()


def test_get_tag_history_unknown_camera_raises_unknown_camera(client):
    with pytest.raises(UnknownCamera):
        client.get_tag_history("no-such-camera")


def test_get_frame_embeds_tags_after_a_prior_get_tags_call(tag_client_factory):
    detector = _ScriptedDetector([[_detection(7)]])
    with tag_client_factory(detector, frame_count=2) as (client, slug):
        tag_frame = client.get_tags(slug)
        frame = client.get_frame(slug)

    assert frame.tags == tag_frame


def test_get_frame_tags_is_none_when_no_detection_has_run(tag_client_factory):
    detector = _CountingDetector()
    with tag_client_factory(detector, frame_count=1) as (client, slug):
        frame = client.get_frame(slug)

    assert frame.tags is None


# ---------------------------------------------------------------------------
# find_feature -- client wire round trips (ticket 025-002, UC-08).
# Exercises the full proto boundary both directions: GrpcDaemon builds the
# FindFeatureRequest, client/models.py's feature_from_proto turns the
# response back into real config.interfaces.Feature dataclasses.
# ---------------------------------------------------------------------------


def _playfield_definition_with_dots(name: str = "main-playfield") -> PlayfieldDefinition:
    """A small real definition with dots -- ``find_feature``'s round trip
    needs findable coloured features, which ``test_service.py``'s
    ``_playfield_definition()`` (ArUco ids only, reused above) does not
    carry."""
    return PlayfieldDefinition(
        name=name,
        width_cm=134.3,
        height_cm=89.3,
        dots=(
            PlayfieldFeatureEntry(slug="dot-west-blue", x=-50.0, y=0.0, colour="blue"),
            PlayfieldFeatureEntry(slug="dot-northwest-orange", x=-50.0, y=30.0, colour="orange"),
            PlayfieldFeatureEntry(slug="dot-northeast-orange", x=50.0, y=30.0, colour="orange"),
            PlayfieldFeatureEntry(slug="dot-southeast-orange", x=50.0, y=-30.0, colour="orange"),
            PlayfieldFeatureEntry(slug="dot-southwest-orange", x=-50.0, y=-30.0, colour="orange"),
        ),
    )


def test_find_feature_round_trip_returns_feature_dataclasses(client):
    client.set_playfield(_playfield_definition_with_dots())

    matches = client.find_feature("main-playfield", "the blue dot")

    assert len(matches) == 1
    match = matches[0]
    assert isinstance(match, Feature)
    assert match.slug == "dot-west-blue"
    assert match.kind == "dot"
    assert match.colour == "blue"
    assert match.position == WorldPoint(x=-50.0, y=0.0)


def test_find_feature_ambiguous_query_round_trip_returns_all_four_orange_dots(client):
    client.set_playfield(_playfield_definition_with_dots())

    matches = client.find_feature("main-playfield", "the orange dot")

    assert {m.slug for m in matches} == {
        "dot-northwest-orange",
        "dot-northeast-orange",
        "dot-southeast-orange",
        "dot-southwest-orange",
    }


def test_find_feature_no_match_round_trip_returns_empty_tuple(client):
    client.set_playfield(_playfield_definition_with_dots())

    matches = client.find_feature("main-playfield", "the orange triangle")

    assert matches == ()


def test_find_feature_unknown_playfield_raises_unknown_playfield_naming_available(client):
    with pytest.raises(UnknownPlayfield) as excinfo:
        client.find_feature("no-such-field", "the blue dot")

    assert excinfo.value.requested == "no-such-field"
    assert excinfo.value.available == ()


# ---------------------------------------------------------------------------
# Mobile-tag registry -- client wire round trips (ticket 025-004, UC-12).
# Exercises the full proto boundary both directions: GrpcDaemon builds the
# request (client/daemon_client.py's own _mount_parameters_to_proto),
# client/models.py's mount_parameters_from_proto/tag_parameters_from_proto
# turn the response back into real aprilcam.types.MountParameters
# dataclasses.
# ---------------------------------------------------------------------------


def test_register_tag_then_list_tag_parameters_round_trip_returns_dataclasses(client):
    tag = TagId(family=TagFamily.APRILTAG, number=7)
    params = MountParameters(mount_x=8.0, mount_y=3.0, mount_yaw_rad=0.5)

    client.register_tag(tag, params)
    registrations = client.list_tag_parameters()

    # mount_z is 0.0 (default/unset) here, so mount_z_applied is
    # vacuously True (ticket 030-004, SUC-004) -- nothing for parallax
    # to withhold with no elevation registered.
    assert registrations == {tag: MountRegistration(params=params, mount_z_applied=True)}
    assert isinstance(next(iter(registrations)), TagId)
    entry = next(iter(registrations.values()))
    assert isinstance(entry, MountRegistration)
    assert isinstance(entry.params, MountParameters)


def test_register_tag_with_size_cm_round_trips_present(client):
    tag = TagId(family=TagFamily.ARUCO, number=3)
    params = MountParameters(size_cm=6.5)

    client.register_tag(tag, params)

    assert client.list_tag_parameters() == {
        tag: MountRegistration(params=params, mount_z_applied=True)
    }


def test_list_tag_parameters_empty_when_nothing_registered(client):
    assert client.list_tag_parameters() == {}


def test_unregister_tag_round_trip(client):
    tag = TagId(family=TagFamily.APRILTAG, number=7)
    client.register_tag(tag, MountParameters(mount_x=1.0))

    client.unregister_tag(tag)

    assert client.list_tag_parameters() == {}


def test_unregister_tag_absent_does_not_raise(client):
    client.unregister_tag(TagId(family=TagFamily.APRILTAG, number=42))  # must not raise


def test_register_tag_mount_z_with_no_calibration_raises_not_calibrated(client):
    with pytest.raises(NotCalibrated):
        client.register_tag(TagId(family=TagFamily.APRILTAG, number=7), MountParameters(mount_z=20.0))

    assert client.list_tag_parameters() == {}


# ---------------------------------------------------------------------------
# Annotation store -- client wire round trips (ticket 024-002, UC-28).
# Exercises the full proto boundary both directions: GrpcDaemon builds the
# request (client/daemon_client.py's own _annotation_to_proto and friends),
# client/models.py's from_proto converters turn the response back into real
# aprilcam.types dataclasses -- mirroring test_service.py's DaemonService
# annotation-store test section, one layer further out over a real gRPC
# channel.
# ---------------------------------------------------------------------------


def _marker_annotation(id: str, layer: str, x: float = 1.0, y: float = 2.0) -> Annotation:
    return Annotation(id=id, layer=layer, shape=Marker(at=WorldPoint(x=x, y=y)))


@pytest.fixture
def playfield_client(client: GrpcDaemon) -> GrpcDaemon:
    """``client`` with ``main-playfield`` already pushed -- every
    annotation RPC below requires a known playfield (UC-28.S-5), and
    this fixture pushes it the same way a real caller would: over the
    wire via ``set_playfield``, not by reaching into the service."""
    client.set_playfield(_playfield_definition())
    return client


def test_put_annotation_then_get_annotations_round_trip_returns_annotation_dataclasses(
    playfield_client,
):
    playfield_client.put_annotation(
        "main-playfield", _marker_annotation("goal", "planner", x=50.0, y=30.0)
    )

    result = playfield_client.get_annotations("main-playfield")

    assert isinstance(result, AnnotationSet)
    assert len(result.annotations) == 1
    annotation = result.annotations[0]
    assert isinstance(annotation, Annotation)
    assert annotation.id == "goal"
    assert annotation.layer == "planner"
    assert annotation.shape == Marker(at=WorldPoint(x=50.0, y=30.0))
    # revision/timestamp survive the round trip, not just the shape.
    assert result.revision == 1
    assert result.timestamp > 0.0


def test_put_annotation_every_shape_in_the_annotation_shape_union_round_trips(playfield_client):
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
        playfield_client.put_annotation(
            "main-playfield",
            Annotation(id=name, layer="planner", shape=shape, style=Style(color="blue")),
        )

    after = playfield_client.get_annotations("main-playfield")

    by_id = {a.id: a for a in after.annotations}
    assert set(by_id) == set(shapes)
    for name, shape in shapes.items():
        assert by_id[name].shape == shape
        assert by_id[name].style == Style(color="blue")


def test_put_annotation_style_with_unset_fields_stays_none_not_defaulted(playfield_client):
    playfield_client.put_annotation(
        "main-playfield",
        Annotation(id="goal", layer="planner", shape=Marker(at=WorldPoint(x=1.0, y=1.0))),
    )

    after = playfield_client.get_annotations("main-playfield")

    style = after.annotations[0].style
    assert style.color is None
    assert style.line_width is None


def test_put_annotation_ttl_seconds_round_trips(playfield_client):
    playfield_client.put_annotation(
        "main-playfield",
        Annotation(
            id="goal",
            layer="planner",
            shape=Marker(at=WorldPoint(x=1.0, y=1.0)),
            ttl_seconds=30.0,
        ),
    )

    after = playfield_client.get_annotations("main-playfield")

    assert after.annotations[0].ttl_seconds == 30.0


def test_remove_annotation_round_trip(playfield_client):
    playfield_client.put_annotation("main-playfield", _marker_annotation("goal", "planner"))

    playfield_client.remove_annotation("main-playfield", "planner", "goal")

    after = playfield_client.get_annotations("main-playfield")
    assert after.annotations == ()


def test_remove_annotation_absent_key_on_known_playfield_is_a_no_op(playfield_client):
    playfield_client.remove_annotation(
        "main-playfield", "planner", "no-such-id"
    )  # must not raise

    after = playfield_client.get_annotations("main-playfield")
    assert after.annotations == ()


def test_replace_layer_round_trip_swaps_a_layers_contents(playfield_client):
    playfield_client.put_annotation("main-playfield", _marker_annotation("old", "planner"))

    playfield_client.replace_layer(
        "main-playfield", "planner", (_marker_annotation("new", "planner", x=2.0, y=2.0),)
    )

    after = playfield_client.get_annotations("main-playfield")
    assert [a.id for a in after.annotations] == ["new"]


def test_clear_annotations_named_layer_and_everything_round_trip(playfield_client):
    playfield_client.put_annotation("main-playfield", _marker_annotation("goal", "planner"))
    playfield_client.put_annotation("main-playfield", _marker_annotation("robot", "robot-3"))

    playfield_client.clear_annotations("main-playfield", layer="planner")
    after_one = playfield_client.get_annotations("main-playfield")
    assert [a.id for a in after_one.annotations] == ["robot"]

    playfield_client.clear_annotations("main-playfield")
    after_all = playfield_client.get_annotations("main-playfield")
    assert after_all.annotations == ()


def test_get_annotations_filtered_by_layer_round_trip(playfield_client):
    playfield_client.put_annotation("main-playfield", _marker_annotation("goal", "planner"))
    playfield_client.put_annotation("main-playfield", _marker_annotation("robot", "robot-3"))

    filtered = playfield_client.get_annotations("main-playfield", layer="planner")

    assert [a.id for a in filtered.annotations] == ["goal"]


def test_get_annotations_on_a_fresh_playfield_returns_empty_set_at_revision_zero(
    playfield_client,
):
    result = playfield_client.get_annotations("main-playfield")

    assert result.annotations == ()
    assert result.revision == 0


def test_get_frame_raw_never_carries_annotations_even_after_a_put(playfield_client, held_camera):
    _, slug, _ = held_camera
    playfield_client.put_annotation("main-playfield", _marker_annotation("goal", "planner"))

    raw = playfield_client.get_frame(slug)

    assert raw.deskewed is False
    assert raw.annotations is None


# ---------------------------------------------------------------------------
# UnknownPlayfield reconstructs client-side for the new RPCs too -- the
# mapping already exists (established by get_playfield); these confirm it
# fires here as well, not just there (ticket 024-002 AC).
# ---------------------------------------------------------------------------


def test_put_annotation_unknown_playfield_raises_unknown_playfield_with_fields_intact(client):
    client.set_playfield(_playfield_definition("main-playfield"))

    with pytest.raises(UnknownPlayfield) as excinfo:
        client.put_annotation("practice-field", _marker_annotation("goal", "planner"))

    assert excinfo.value.requested == "practice-field"
    assert excinfo.value.available == ("main-playfield",)


def test_get_annotations_unknown_playfield_raises_unknown_playfield_with_fields_intact(client):
    with pytest.raises(UnknownPlayfield) as excinfo:
        client.get_annotations("no-such-field")

    assert excinfo.value.requested == "no-such-field"
    assert excinfo.value.available == ()


def test_remove_annotation_unknown_playfield_raises_unknown_playfield(client):
    with pytest.raises(UnknownPlayfield):
        client.remove_annotation("no-such-field", "planner", "goal")


def test_replace_layer_unknown_playfield_raises_unknown_playfield(client):
    with pytest.raises(UnknownPlayfield):
        client.replace_layer("no-such-field", "planner", ())


def test_clear_annotations_unknown_playfield_raises_unknown_playfield(client):
    with pytest.raises(UnknownPlayfield):
        client.clear_annotations("no-such-field")


# ---------------------------------------------------------------------------
# AnnotationLimitExceeded reconstructs client-side too (ticket 031-010) --
# the daemon-side bound tests live in test_service.py; these confirm the
# RPC trailing-metadata contract (daemon/rpc.py's _abort) round-trips back
# into the exact typed exception on this side of the wire, not a generic
# ConnectionFailed.
# ---------------------------------------------------------------------------


def test_put_annotation_over_point_bound_raises_annotation_limit_exceeded_with_fields_intact(
    playfield_client,
):
    over_bound_points = tuple(WorldPoint(x=float(i), y=0.0) for i in range(_MAX_POINTS_PER_SHAPE + 1))

    with pytest.raises(AnnotationLimitExceeded) as excinfo:
        playfield_client.put_annotation(
            "main-playfield",
            Annotation(id="route", layer="planner", shape=PathShape(points=over_bound_points)),
        )

    assert excinfo.value.kind == "points"
    assert excinfo.value.bound == _MAX_POINTS_PER_SHAPE
    assert excinfo.value.actual == _MAX_POINTS_PER_SHAPE + 1


def test_replace_layer_over_count_bound_raises_annotation_limit_exceeded_with_fields_intact(
    playfield_client,
):
    too_many = tuple(
        _marker_annotation(f"f{i}", "planner", x=float(i)) for i in range(_MAX_ANNOTATIONS_PER_LAYER + 1)
    )

    with pytest.raises(AnnotationLimitExceeded) as excinfo:
        playfield_client.replace_layer("main-playfield", "planner", too_many)

    assert excinfo.value.kind == "annotations"
    assert excinfo.value.bound == _MAX_ANNOTATIONS_PER_LAYER
    assert excinfo.value.actual == _MAX_ANNOTATIONS_PER_LAYER + 1


# ---------------------------------------------------------------------------
# Multi-client: a put on one GrpcDaemon instance is visible via
# get_annotations on a second, independently-connected GrpcDaemon instance
# -- UC-28.S-1/S-3, the scenario ticket 005's integration ticket leans on
# being proven here at the wire level first.
# ---------------------------------------------------------------------------


def test_put_by_one_client_is_visible_to_a_second_independently_connected_client(service):
    service.set_playfield(_playfield_definition())

    with _bind_client(service) as client_a, _bind_client(service) as client_b:
        client_a.put_annotation(
            "main-playfield", _marker_annotation("goal", "planner", x=50.0, y=30.0)
        )

        seen_by_b = client_b.get_annotations("main-playfield")

    assert len(seen_by_b.annotations) == 1
    assert seen_by_b.annotations[0].id == "goal"
    assert seen_by_b.annotations[0].shape == Marker(at=WorldPoint(x=50.0, y=30.0))


# ---------------------------------------------------------------------------
# GetFrame's annotations embed, over the real client wire (ticket 024-002):
# a deskewed frame always carries a real AnnotationSet (empty-but-present
# survives, not coerced to None); a raw frame never does, unconditionally
# -- test_get_frame_raw_never_carries_annotations_even_after_a_put above
# covers the raw side without needing calibration. These two need a real
# calibrated camera (ArUco-rendered frames), mirroring
# test_service.py's test_rpc_get_frame_deskewed_embeds_annotations_round_trip
# one layer further out, through GrpcDaemon/image_frame_from_proto instead
# of the bare stub.
# ---------------------------------------------------------------------------


def test_get_frame_deskewed_embeds_the_current_annotation_set_via_client(directories, registry):
    frame = render_playfield_frame(MAIN_PLAYFIELD_ARUCO_POSITIONS)
    identity, slug, cam, _backend = _open_camera_with_frames(
        registry, [frame] * (CALIBRATION_FRAME_COUNT + 2)
    )
    calibrated_service = _service_with_camera(directories, registry, identity, slug, cam)
    calibrated_service.set_playfield(main_playfield_definition())
    calibrated_service.set_camera_config(
        CameraConfig(name=slug, playfield="main-playfield", settings={})
    )
    assert calibrated_service.calibrate(slug).succeeded is True

    with _bind_client(calibrated_service) as client:
        empty = client.get_frame(slug, deskewed=True)
        assert empty.annotations is not None
        assert empty.annotations.annotations == ()
        assert empty.annotations.revision == 0

        client.put_annotation(
            "main-playfield", _marker_annotation("goal", "planner", x=50.0, y=30.0)
        )
        filled = client.get_frame(slug, deskewed=True)

    assert filled.annotations is not None
    assert len(filled.annotations.annotations) == 1
    assert filled.annotations.annotations[0].id == "goal"
    assert filled.annotations.revision == 1


# ---------------------------------------------------------------------------
# get_guide -- client wire round trips (ticket 025-007, UC-27, this
# sprint's last). Exercises the full proto boundary: GrpcDaemon.get_guide
# builds the GetGuideRequest, reads GetGuideResponse.text back directly
# (no client/models.py converter needed -- a bare string has nothing to
# convert).
# ---------------------------------------------------------------------------


def test_get_guide_no_name_round_trip_returns_agent_guide_text(client):
    assert client.get_guide() == Guides().get("agent")


def test_get_guide_agent_name_round_trip_returns_agent_guide_text(client):
    assert client.get_guide("agent") == Guides().get("agent")


def test_get_guide_robot_name_round_trip_returns_robot_guide_text(client):
    assert client.get_guide("robot") == Guides().get("robot")


def test_get_guide_unknown_name_raises_unknown_guide_naming_available(client):
    with pytest.raises(UnknownGuide) as excinfo:
        client.get_guide("nonexistent")

    assert excinfo.value.requested == "nonexistent"
    assert excinfo.value.available == ("agent", "robot")


# ---------------------------------------------------------------------------
# The single-source-of-truth proof (ticket 025-007's Definition of Done):
# the RPC path (``client`` below, a real gRPC connection to a live daemon)
# and the CLI's local, no-daemon path (``aprilcam guide``, which reads
# ``client.guides.Guides`` directly -- see ``cli/guide.py``) both
# ultimately read the same ``aprilcam/guides.py`` leaf, so they must serve
# byte-identical text, not merely similar text. This is the one test in
# the whole ticket that exercises *both* paths together in the same
# assertion.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", [None, "agent", "robot"])
def test_cli_no_daemon_path_and_live_daemon_rpc_serve_byte_identical_guide_text(
    client, capsys, name
):
    from aprilcam.cli import main as cli_main

    rpc_text = client.get_guide(name)

    argv = ["guide"] if name is None else ["guide", name]
    exit_code = cli_main(argv)
    cli_text = capsys.readouterr().out

    assert exit_code == 0
    assert cli_text == rpc_text


# ---------------------------------------------------------------------------
# Marker size / policy on the wire (sprint 045 ticket 002).
# ---------------------------------------------------------------------------


def _sized_definition() -> PlayfieldDefinition:
    return PlayfieldDefinition(
        name="sized-playfield",
        width_cm=100.0,
        height_cm=60.0,
        marker_size_mm_default=40.0,
        aruco_tags=(
            PlayfieldFeatureEntry(slug="a", x=0.0, y=0.0, tag_id=1, size_mm=25.5, policy="helpful"),
            PlayfieldFeatureEntry(slug="b", x=10.0, y=0.0, tag_id=2),
        ),
    )


def test_marker_size_and_policy_round_trip_through_set_and_get_playfield(client):
    client.set_playfield(_sized_definition())

    got = client.get_playfield("sized-playfield").definition

    assert got.marker_size_mm_default == 40.0
    a, b = got.aruco_tags
    assert (a.size_mm, a.policy) == (25.5, "helpful")
    assert (b.size_mm, b.policy) == (None, None)
    assert got.marker_size_mm(1) == 25.5
    assert got.marker_size_mm(2) == 40.0
    assert got.helpful_marker_ids() == (1,)
    assert got == _sized_definition()


def test_definition_without_new_keys_leaves_wire_fields_unset():
    from aprilcam.client.daemon_client import _playfield_definition_to_proto
    from aprilcam.client.models import playfield_definition_from_proto
    from aprilcam.daemon.rpc import _playfield_definition_from_proto
    from aprilcam.daemon.rpc import _playfield_definition_to_proto as server_to_proto

    plain = _playfield_definition_with_dots()
    for msg in (_playfield_definition_to_proto(plain), server_to_proto(plain)):
        assert not msg.HasField("marker_size_mm")
        assert all(
            not e.HasField("size_mm") and not e.HasField("policy") for e in msg.dots
        )
        assert playfield_definition_from_proto(msg) == plain
        assert _playfield_definition_from_proto(msg) == plain

    sized = _sized_definition()
    msg = server_to_proto(sized)
    assert msg.HasField("marker_size_mm") and msg.marker_size_mm == 40.0
    assert playfield_definition_from_proto(msg) == sized


# ---------------------------------------------------------------------------
# Calibration target on the wire (sprint 045 ticket 010).
# ---------------------------------------------------------------------------


def _targeted_definition() -> PlayfieldDefinition:
    from aprilcam.config.interfaces import CalibrationTarget

    return PlayfieldDefinition(
        name="targeted-playfield",
        width_cm=100.0,
        height_cm=60.0,
        calibration_target=CalibrationTarget(
            kind="charuco", board="6x8:28:22:DICT_4X4_50",
            centre_x_cm=5.0, centre_y_cm=-2.5, rotation_deg=90.0,
        ),
    )


def test_calibration_target_round_trips_through_set_and_get_playfield(client):
    client.set_playfield(_targeted_definition())

    got = client.get_playfield("targeted-playfield").definition

    assert got.calibration_target == _targeted_definition().calibration_target
    assert got == _targeted_definition()


def test_calibration_target_unset_on_the_wire_for_old_definitions():
    from aprilcam.client.daemon_client import _playfield_definition_to_proto
    from aprilcam.client.models import playfield_definition_from_proto
    from aprilcam.daemon.rpc import _playfield_definition_from_proto
    from aprilcam.daemon.rpc import _playfield_definition_to_proto as server_to_proto

    plain = _playfield_definition_with_dots()
    for msg in (_playfield_definition_to_proto(plain), server_to_proto(plain)):
        assert not msg.HasField("calibration_target")
        assert playfield_definition_from_proto(msg).calibration_target is None

    targeted = _targeted_definition()
    for msg in (_playfield_definition_to_proto(targeted), server_to_proto(targeted)):
        assert msg.HasField("calibration_target")
        assert playfield_definition_from_proto(msg) == targeted
        assert _playfield_definition_from_proto(msg) == targeted
