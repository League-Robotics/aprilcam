"""Tests for ``aprilcam.daemon.stream_transport`` and ``daemon/rpc.py``'s
``GetImageStream``/``GetTagStream`` handlers (ticket 027-005).

Fully headless, real sockets on localhost throughout -- the ticket's own
Definition of Done: "a real socket client (localhost, real ``socket``
module, not mocked)". Reuses ticket 003/004's looping-fake-camera
pattern (``tests.aprilcam.daemon.test_service``'s ``_identity``/
``_open_camera_looping``/``_service_with_camera``/``_frame``) so both
the tag stream (needs frames to detect against) and the image stream
(needs frames to encode) have continuous input for as long as a real
subscriber stays connected -- exactly ``development-plan.md`` Stage 8's
"fake-camera daemon (fixture replay backend)" this sprint's testing
infrastructure section calls for.

Three layers of coverage, matching the ticket's Testing section
line-for-line:

* :class:`~aprilcam.daemon.stream_transport.StreamTransportManager`
  directly (no gRPC) -- endpoint allocation starts no producer, real
  socket framing, second-subscriber reuse, disconnect idle-gating, and
  the "thread count returns to baseline after stop()" shutdown proof.
* The real gRPC ``GetImageStream``/``GetTagStream`` round trip
  (``daemon/rpc.py``'s ``_Servicer``, via ``build_server(...,
  transport=...)``) -- the same real-socket-client pattern, but
  reaching the transport through the actual RPC surface a remote client
  uses, mirroring ``test_service.py``'s own ``_bind_stub`` convention.
* ``daemon/__main__.py``'s wiring -- a monkeypatched
  ``build_stream_transport`` proves ``DaemonLifecycle.stop()`` really
  calls ``transport.stop()`` during shutdown, without needing a real
  camera (``daemon_main.build()`` has no camera-injection hook, unlike
  this file's own ``DaemonService``-direct fixtures).
"""

from __future__ import annotations

import shutil
import socket
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Tuple

import grpc
import pytest

from aprilcam import framing
from aprilcam.camera.registry import CameraRegistry
from aprilcam.config.directories import Directories
from aprilcam.daemon import __main__ as daemon_main
from aprilcam.daemon.rpc import build_server, build_stream_transport
from aprilcam.daemon.service import DaemonService
from aprilcam.daemon.stream_transport import StreamTransportManager
from aprilcam.proto import aprilcam_v2_pb2, aprilcam_v2_pb2_grpc
from tests.aprilcam.daemon.test_service import (
    _frame,
    _open_camera_looping,
    _service_with_camera,
)

# ---------------------------------------------------------------------------
# Shared fixtures/helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def directories(tmp_path: Path) -> Directories:
    return Directories(config_dir=tmp_path / "config", state_dir=tmp_path / "state")


@pytest.fixture
def registry(tmp_path: Path) -> CameraRegistry:
    return CameraRegistry(tmp_path / "state" / "cameras")


def _looping_service(directories: Directories, registry: CameraRegistry) -> Tuple[DaemonService, str]:
    """A ``DaemonService`` with one camera, backed by a *looping* fake
    backend (ticket 003/004's fixture-replay pattern), so a background
    stream producer -- tags or raw images -- never runs dry for as long
    as a test keeps a real subscriber connected."""
    identity, slug, cam, _backend = _open_camera_looping(registry, _frame())
    service = _service_with_camera(directories, registry, identity, slug, cam)
    return service, slug


@pytest.fixture
def short_socket_dir() -> Iterator[Path]:
    """A short top-level ``/tmp`` directory for stream unix sockets --
    unix domain socket paths have a ~103-character OS limit on
    macOS/Linux, routinely exceeded by pytest's deeply nested
    ``tmp_path``, exactly the reasoning ``test_service.py``'s own
    ``_bind_stub`` already documents for the control-plane socket."""
    d = Path(tempfile.mkdtemp(prefix="ac-strm-", dir="/tmp"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _connect_unix(socket_path: str, *, timeout: float = 5.0) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(socket_path)
    return sock


def _read_one(sock: socket.socket) -> bytes:
    """Read exactly one length-prefixed payload via
    ``aprilcam.framing.read_frame`` -- the same reader ticket 006's
    client will use, exercised here against this ticket's real sender."""
    return framing.read_frame(sock)


def _wait_until(predicate, *, timeout: float = 3.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# ---------------------------------------------------------------------------
# StreamTransportManager -- direct (no gRPC)
# ---------------------------------------------------------------------------


def test_get_or_create_image_endpoint_starts_no_producer(directories, registry, short_socket_dir):
    service, slug = _looping_service(directories, registry)
    transport = build_stream_transport(service, short_socket_dir)
    try:
        endpoint = transport.get_or_create_image_endpoint(slug, deskewed=False)
        assert endpoint.socket_path is not None or endpoint.tcp_port is not None
        # No client ever connected -- the daemon stays fully idle for
        # this product (ticket's AC1).
        assert service._image_stream_producers == {}  # noqa: SLF001
    finally:
        transport.stop()


def test_get_or_create_tag_endpoint_starts_no_producer(directories, registry, short_socket_dir):
    service, slug = _looping_service(directories, registry)
    transport = build_stream_transport(service, short_socket_dir)
    try:
        endpoint = transport.get_or_create_tag_endpoint(slug)
        assert endpoint.socket_path is not None or endpoint.tcp_port is not None
        assert service._tag_stream_producers == {}  # noqa: SLF001
    finally:
        transport.stop()


def test_real_socket_client_receives_framed_image_frames(directories, registry, short_socket_dir):
    service, slug = _looping_service(directories, registry)
    transport = build_stream_transport(service, short_socket_dir)
    try:
        endpoint = transport.get_or_create_image_endpoint(slug, deskewed=False)
        assert endpoint.socket_path is not None
        sock = _connect_unix(endpoint.socket_path)
        try:
            payload = _read_one(sock)
            msg = aprilcam_v2_pb2.GetFrameResponse.FromString(payload)
            assert msg.width == 4 and msg.height == 4
            assert msg.encoding == "jpeg"
            assert msg.deskewed is False
            assert msg.data[:2] == b"\xff\xd8"  # JPEG SOI marker

            # Exactly one real DaemonService.stream_images subscription
            # was created for this one connection.
            producer = service._image_stream_producers[(slug, False)]  # noqa: SLF001
            assert producer.broadcaster.subscriber_count == 1
        finally:
            sock.close()
    finally:
        transport.stop()


def test_real_socket_client_receives_framed_tag_frames(directories, registry, short_socket_dir):
    service, slug = _looping_service(directories, registry)
    transport = build_stream_transport(service, short_socket_dir)
    try:
        endpoint = transport.get_or_create_tag_endpoint(slug)
        assert endpoint.socket_path is not None
        sock = _connect_unix(endpoint.socket_path)
        try:
            payload = _read_one(sock)
            msg = aprilcam_v2_pb2.TagFrame.FromString(payload)
            # The synthetic all-black fixture frame has no real tags --
            # a real, decodable (if empty) TagFrame still arrives
            # (heartbeat), proving the wire shape, not detection itself.
            assert list(msg.tags) == []

            producer = service._tag_stream_producers[slug]  # noqa: SLF001
            assert producer.broadcaster.subscriber_count == 1
        finally:
            sock.close()
    finally:
        transport.stop()


def test_second_client_to_same_endpoint_shares_one_producer(directories, registry, short_socket_dir):
    service, slug = _looping_service(directories, registry)
    transport = build_stream_transport(service, short_socket_dir)
    try:
        endpoint1 = transport.get_or_create_image_endpoint(slug, deskewed=False)
        endpoint2 = transport.get_or_create_image_endpoint(slug, deskewed=False)
        # Reuse, not a second allocation -- byte-identical endpoint.
        assert endpoint1 == endpoint2
        assert endpoint1.socket_path is not None

        sock1 = _connect_unix(endpoint1.socket_path)
        sock2 = _connect_unix(endpoint1.socket_path)
        try:
            frame1 = aprilcam_v2_pb2.GetFrameResponse.FromString(_read_one(sock1))
            frame2 = aprilcam_v2_pb2.GetFrameResponse.FromString(_read_one(sock2))
            assert frame1.width == frame2.width and frame1.height == frame2.height

            assert len(service._image_stream_producers) == 1  # noqa: SLF001
            producer = service._image_stream_producers[(slug, False)]  # noqa: SLF001
            assert _wait_until(lambda: producer.broadcaster.subscriber_count == 2)
        finally:
            sock1.close()
            sock2.close()
    finally:
        transport.stop()


def test_second_client_to_same_tag_endpoint_shares_one_producer(directories, registry, short_socket_dir):
    service, slug = _looping_service(directories, registry)
    transport = build_stream_transport(service, short_socket_dir)
    try:
        endpoint1 = transport.get_or_create_tag_endpoint(slug)
        endpoint2 = transport.get_or_create_tag_endpoint(slug)
        assert endpoint1 == endpoint2
        assert endpoint1.socket_path is not None

        sock1 = _connect_unix(endpoint1.socket_path)
        sock2 = _connect_unix(endpoint1.socket_path)
        try:
            _read_one(sock1)
            _read_one(sock2)
            assert len(service._tag_stream_producers) == 1  # noqa: SLF001
            producer = service._tag_stream_producers[slug]  # noqa: SLF001
            assert _wait_until(lambda: producer.broadcaster.subscriber_count == 2)
        finally:
            sock1.close()
            sock2.close()
    finally:
        transport.stop()


def test_closing_client_socket_unsubscribes_and_idle_gates_last_subscriber(
    directories, registry, short_socket_dir
):
    service, slug = _looping_service(directories, registry)
    transport = build_stream_transport(service, short_socket_dir)
    try:
        endpoint = transport.get_or_create_tag_endpoint(slug)
        assert endpoint.socket_path is not None
        sock = _connect_unix(endpoint.socket_path)
        _read_one(sock)  # prove the subscription is really flowing first

        producer = service._tag_stream_producers[slug]  # noqa: SLF001
        assert _wait_until(lambda: producer.running is True)
        assert producer.broadcaster.subscriber_count == 1

        sock.close()

        # Bounded time: the tag stream heartbeats at 1 Hz, so a closed
        # socket is noticed on the very next publish attempt at worst.
        assert _wait_until(lambda: producer.broadcaster.subscriber_count == 0, timeout=5.0)
        # It was the last subscriber -- ticket 003's idle-gating, now
        # verified end-to-end through a real socket (not just
        # Broadcaster.unsubscribe called directly, as test_service.py's
        # in-process tests do). The producer object itself stays
        # tracked (DaemonService reuses it on a future resubscribe,
        # per stream_tags's own docstring) -- only its background
        # thread goes away.
        assert _wait_until(lambda: producer.running is False, timeout=5.0)
        assert slug in service._tag_stream_producers  # noqa: SLF001
    finally:
        transport.stop()


def test_closing_one_of_two_clients_does_not_idle_gate(directories, registry, short_socket_dir):
    service, slug = _looping_service(directories, registry)
    transport = build_stream_transport(service, short_socket_dir)
    try:
        endpoint = transport.get_or_create_tag_endpoint(slug)
        assert endpoint.socket_path is not None
        sock1 = _connect_unix(endpoint.socket_path)
        sock2 = _connect_unix(endpoint.socket_path)
        _read_one(sock1)
        _read_one(sock2)

        producer = service._tag_stream_producers[slug]  # noqa: SLF001
        assert _wait_until(lambda: producer.broadcaster.subscriber_count == 2)

        sock1.close()

        assert _wait_until(lambda: producer.broadcaster.subscriber_count == 1, timeout=5.0)
        # The still-connected subscriber keeps the producer alive.
        assert producer.running is True

        sock2.close()
        assert _wait_until(lambda: producer.broadcaster.subscriber_count == 0, timeout=5.0)
    finally:
        transport.stop()


def test_transport_stop_leaves_no_orphaned_threads(directories, registry, short_socket_dir):
    """Definition of Done: "daemon shutdown cleanly tears down the
    accept loop with no leaked threads (a test asserting thread count
    returns to baseline after shutdown)". Isolated to exactly what
    ``StreamTransportManager`` owns -- no gRPC control plane involved --
    so the assertion is not coupled to gRPC's own thread pool lifecycle.
    """
    service, slug = _looping_service(directories, registry)
    transport = build_stream_transport(service, short_socket_dir)

    baseline = threading.active_count()

    image_endpoint = transport.get_or_create_image_endpoint(slug, deskewed=False)
    tag_endpoint = transport.get_or_create_tag_endpoint(slug)
    assert image_endpoint.socket_path is not None
    assert tag_endpoint.socket_path is not None

    image_sock = _connect_unix(image_endpoint.socket_path)
    tag_sock = _connect_unix(tag_endpoint.socket_path)
    try:
        _read_one(image_sock)
        _read_one(tag_sock)
        # Real accept-loop + sender threads now exist (>= 2 accept
        # threads x 2 listeners + 2 sender threads, at minimum).
        assert threading.active_count() > baseline
    finally:
        image_sock.close()
        tag_sock.close()

    transport.stop()

    assert _wait_until(lambda: threading.active_count() == baseline, timeout=5.0)


def test_transport_stop_is_idempotent(directories, registry, short_socket_dir):
    service, slug = _looping_service(directories, registry)
    transport = build_stream_transport(service, short_socket_dir)
    transport.get_or_create_tag_endpoint(slug)

    transport.stop()
    transport.stop()  # must not raise


# ---------------------------------------------------------------------------
# The real GetImageStream/GetTagStream gRPC round trip
# ---------------------------------------------------------------------------


@contextmanager
def _bind_stream_stub(service: DaemonService, socket_dir: Path):
    """Bind a real ``grpc.Server`` (control plane) wired to a real
    :class:`StreamTransportManager` (bulk-stream sockets), both under
    short top-level ``/tmp`` directories, and yield a connected, real
    generated stub -- ``test_service.py``'s own ``_bind_stub``, extended
    with the *transport* parameter this ticket adds to
    :func:`~aprilcam.daemon.rpc.build_server`.
    """
    control_dir = Path(tempfile.mkdtemp(prefix="ac-ctrl-", dir="/tmp"))
    transport = build_stream_transport(service, socket_dir)
    try:
        listener = build_server(service, control_dir / "control.sock", transport=transport)
        listener.start()
        channel = grpc.insecure_channel(f"unix:{control_dir / 'control.sock'}")
        try:
            grpc.channel_ready_future(channel).result(timeout=5)
            yield aprilcam_v2_pb2_grpc.AprilCamV2Stub(channel), transport
        finally:
            channel.close()
            listener.stop(1.0)
    finally:
        transport.stop()
        shutil.rmtree(control_dir, ignore_errors=True)


def test_rpc_get_image_stream_returns_endpoint_without_starting_a_producer(
    directories, registry, short_socket_dir
):
    service, slug = _looping_service(directories, registry)
    with _bind_stream_stub(service, short_socket_dir) as (stub, _transport):
        response = stub.GetImageStream(
            aprilcam_v2_pb2.GetImageStreamRequest(camera=slug, deskewed=False)
        )
        assert response.endpoint.HasField("socket_path") or response.endpoint.HasField("tcp_port")
        assert service._image_stream_producers == {}  # noqa: SLF001


def test_rpc_get_tag_stream_returns_endpoint_without_starting_a_producer(
    directories, registry, short_socket_dir
):
    service, slug = _looping_service(directories, registry)
    with _bind_stream_stub(service, short_socket_dir) as (stub, _transport):
        response = stub.GetTagStream(aprilcam_v2_pb2.GetTagStreamRequest(camera=slug))
        assert response.endpoint.HasField("socket_path") or response.endpoint.HasField("tcp_port")
        assert service._tag_stream_producers == {}  # noqa: SLF001


def test_rpc_get_image_stream_socket_delivers_real_frames(directories, registry, short_socket_dir):
    service, slug = _looping_service(directories, registry)
    with _bind_stream_stub(service, short_socket_dir) as (stub, _transport):
        response = stub.GetImageStream(
            aprilcam_v2_pb2.GetImageStreamRequest(camera=slug, deskewed=False)
        )
        assert response.endpoint.HasField("socket_path")
        sock = _connect_unix(response.endpoint.socket_path)
        try:
            msg = aprilcam_v2_pb2.GetFrameResponse.FromString(_read_one(sock))
            assert msg.width == 4 and msg.height == 4
        finally:
            sock.close()


def test_rpc_get_tag_stream_socket_delivers_real_frames(directories, registry, short_socket_dir):
    service, slug = _looping_service(directories, registry)
    with _bind_stream_stub(service, short_socket_dir) as (stub, _transport):
        response = stub.GetTagStream(aprilcam_v2_pb2.GetTagStreamRequest(camera=slug))
        assert response.endpoint.HasField("socket_path")
        sock = _connect_unix(response.endpoint.socket_path)
        try:
            msg = aprilcam_v2_pb2.TagFrame.FromString(_read_one(sock))
            assert list(msg.tags) == []
        finally:
            sock.close()


def test_rpc_second_get_image_stream_call_reuses_the_same_endpoint(
    directories, registry, short_socket_dir
):
    service, slug = _looping_service(directories, registry)
    with _bind_stream_stub(service, short_socket_dir) as (stub, _transport):
        first = stub.GetImageStream(
            aprilcam_v2_pb2.GetImageStreamRequest(camera=slug, deskewed=False)
        ).endpoint
        second = stub.GetImageStream(
            aprilcam_v2_pb2.GetImageStreamRequest(camera=slug, deskewed=False)
        ).endpoint
        assert first.socket_path == second.socket_path
        assert first.tcp_port == second.tcp_port

        sock1 = _connect_unix(first.socket_path)
        sock2 = _connect_unix(second.socket_path)
        try:
            _read_one(sock1)
            _read_one(sock2)
            assert len(service._image_stream_producers) == 1  # noqa: SLF001
            producer = service._image_stream_producers[(slug, False)]  # noqa: SLF001
            assert _wait_until(lambda: producer.broadcaster.subscriber_count == 2)
        finally:
            sock1.close()
            sock2.close()


def test_rpc_get_image_stream_and_get_tag_stream_use_independent_endpoints(
    directories, registry, short_socket_dir
):
    """A camera's image and tag streams never collide on one socket --
    each ``get_or_create_*`` key space is independent."""
    service, slug = _looping_service(directories, registry)
    with _bind_stream_stub(service, short_socket_dir) as (stub, _transport):
        image_endpoint = stub.GetImageStream(
            aprilcam_v2_pb2.GetImageStreamRequest(camera=slug, deskewed=False)
        ).endpoint
        tag_endpoint = stub.GetTagStream(
            aprilcam_v2_pb2.GetTagStreamRequest(camera=slug)
        ).endpoint
        assert image_endpoint.socket_path != tag_endpoint.socket_path


def test_get_image_stream_without_a_transport_configured_is_unimplemented(directories, registry):
    """``build_server`` with no *transport* (every pre-027-005 caller)
    still builds a fully working server for every other RPC --
    ``GetImageStream``/``GetTagStream`` answer ``UNIMPLEMENTED`` rather
    than raising an ``AttributeError``."""
    service, slug = _looping_service(directories, registry)
    control_dir = Path(tempfile.mkdtemp(prefix="ac-ctrl-", dir="/tmp"))
    try:
        listener = build_server(service, control_dir / "control.sock")
        listener.start()
        channel = grpc.insecure_channel(f"unix:{control_dir / 'control.sock'}")
        try:
            grpc.channel_ready_future(channel).result(timeout=5)
            stub = aprilcam_v2_pb2_grpc.AprilCamV2Stub(channel)
            with pytest.raises(grpc.RpcError) as excinfo:
                stub.GetImageStream(
                    aprilcam_v2_pb2.GetImageStreamRequest(camera=slug, deskewed=False)
                )
            assert excinfo.value.code() == grpc.StatusCode.UNIMPLEMENTED
        finally:
            channel.close()
            listener.stop(1.0)
    finally:
        shutil.rmtree(control_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# daemon/__main__.py wiring -- on_stop calls transport.stop()
# ---------------------------------------------------------------------------


class _RecordingTransport:
    """A minimal stand-in for ``StreamTransportManager`` -- records
    whether ``stop()`` was called, without needing a real camera or any
    real socket. ``daemon_main.build()`` has no camera-injection hook
    (unlike this file's own ``DaemonService``-direct fixtures above),
    so this wiring-only test cannot drive a real stream end to end; it
    exists purely to pin that ``DaemonLifecycle.stop()`` really reaches
    ``transport.stop()`` during shutdown, independent of camera
    availability."""

    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


def test_daemon_main_on_stop_calls_transport_stop(tmp_path, short_socket_dir, monkeypatch):
    recording = _RecordingTransport()
    monkeypatch.setattr(
        daemon_main, "build_stream_transport", lambda *args, **kwargs: recording
    )

    directories = Directories(config_dir=tmp_path / "config", state_dir=tmp_path / "state")
    # Explicit short lock/socket paths -- pytest's tmp_path is routinely
    # too deep for unix domain sockets' ~103-character limit, the same
    # workaround test_main.py's own short_tmp_dir fixture documents.
    lifecycle = daemon_main.build(
        directories,
        lock_path=short_socket_dir / "daemon.lock",
        socket_path=short_socket_dir / "control.sock",
        start_watch=False,
    )
    lifecycle.start()
    try:
        assert recording.stopped is False
    finally:
        lifecycle.stop()
    assert recording.stopped is True


def test_unix_socket_path_is_shortened_to_fit_sun_path_and_is_stable(tmp_path):
    """2026-09-04 (out of process): a tag stream named after a full camera
    slug produced a 105-byte path under macOS's $TMPDIR and failed to bind
    ("AF_UNIX path too long"). Long names must be shortened deterministically."""
    from pathlib import Path

    from aprilcam.daemon.stream_transport import MAX_UNIX_SOCKET_PATH_BYTES, unix_socket_path

    short = unix_socket_path(Path("/tmp/x"), "images-raw-2")
    assert short == Path("/tmp/x/stream-images-raw-2.sock")
    long_dir = Path("/var/folders/f_/xrglkf752lxg31tz3b57_fl00000gn/T/aprilcam-501")
    name = "tags-arducam-ov9782-usb-camera"
    assert len(str(long_dir / f"stream-{name}.sock").encode()) > MAX_UNIX_SOCKET_PATH_BYTES
    p1 = unix_socket_path(long_dir, name)
    assert len(str(p1).encode()) <= MAX_UNIX_SOCKET_PATH_BYTES
    assert p1.name.startswith("stream-") and p1.suffix == ".sock"
    assert p1 == unix_socket_path(long_dir, name)  # stable
    assert p1 != unix_socket_path(long_dir, "tags-arducam-ov9281-usb-camera")  # distinct per name
