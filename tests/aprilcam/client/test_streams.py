"""Tests for ``aprilcam.client.streams`` and ``GrpcDaemon.stream_images``/
``stream_tags`` (ticket 027-006).

Fully headless, real sockets throughout -- the ticket's own Definition
of Done: "a real socket client (localhost, real ``socket`` module, not
mocked)". Two layers of coverage:

* :class:`~aprilcam.client.streams.SocketImageStream`/
  :class:`~aprilcam.client.streams.SocketTagStream` directly, against a
  real ``socket.socketpair()`` -- decode correctness, clean-EOF-as-
  StopIteration, and a bounded-wait timeout when nothing arrives. No
  daemon needed for any of this: these are pure wire-decode/iteration
  properties of the class itself.
* The full real round trip -- a real ``DaemonService`` +
  ``daemon/stream_transport.py`` + ``daemon/rpc.py``'s gRPC server
  (ticket 005's own pattern, extended with the *transport* parameter)
  + this ticket's real ``GrpcDaemon.stream_images``/``stream_tags`` --
  reusing ticket 003's looping fake-camera backend
  (``tests.aprilcam.daemon.test_service``'s ``_open_camera_looping``/
  ``_service_with_camera``) so a background producer never runs dry for
  as long as a test keeps a real subscriber connected. ``.close()``'s
  daemon-side effect is verified against the same internal producer
  state ``tests/aprilcam/daemon/test_stream_transport.py`` already
  proved for a bare socket client, now reached through this ticket's
  actual client classes instead.
"""

from __future__ import annotations

import shutil
import socket
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Tuple

import grpc
import pytest

from aprilcam import framing
from aprilcam.camera.registry import CameraRegistry
from aprilcam.client.daemon_client import GrpcDaemon
from aprilcam.client.streams import SocketImageStream, SocketTagStream
from aprilcam.config.directories import Directories
from aprilcam.daemon.interfaces import ImageStream, TagStream
from aprilcam.daemon.rpc import build_server, build_stream_transport
from aprilcam.daemon.service import DaemonService
from aprilcam.daemon.stream_transport import StreamTransportManager
from aprilcam.proto import aprilcam_v2_pb2, aprilcam_v2_pb2_grpc
from aprilcam.types import ImageFrame, TagFrame
from tests.aprilcam.daemon.test_service import _frame, _open_camera_looping, _service_with_camera

# ---------------------------------------------------------------------------
# Shared fixtures/helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def directories(tmp_path: Path) -> Directories:
    return Directories(config_dir=tmp_path / "config", state_dir=tmp_path / "state")


@pytest.fixture
def registry(tmp_path: Path) -> CameraRegistry:
    return CameraRegistry(tmp_path / "state" / "cameras")


@pytest.fixture
def short_socket_dir() -> Iterator[Path]:
    """A short top-level ``/tmp`` directory for stream unix sockets --
    unix domain socket paths have a ~103-character OS limit, routinely
    exceeded by pytest's deeply nested ``tmp_path`` (same reasoning
    ``test_stream_transport.py``'s identical fixture documents)."""
    d = Path(tempfile.mkdtemp(prefix="ac-strc-", dir="/tmp"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _looping_service(directories: Directories, registry: CameraRegistry) -> Tuple[DaemonService, str]:
    """A ``DaemonService`` with one camera, backed by a *looping* fake
    backend (ticket 003/004's fixture-replay pattern), so a background
    stream producer never runs dry for as long as a test keeps a real
    subscriber connected."""
    identity, slug, cam, _backend = _open_camera_looping(registry, _frame())
    service = _service_with_camera(directories, registry, identity, slug, cam)
    return service, slug


def _wait_until(predicate, *, timeout: float = 3.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@contextmanager
def _bind_stream_client(
    service: DaemonService, socket_dir: Path
) -> Iterator[Tuple[GrpcDaemon, StreamTransportManager]]:
    """Bind a real ``grpc.Server`` (control plane) wired to a real
    :class:`StreamTransportManager` (bulk-stream sockets), both under
    short top-level ``/tmp`` directories, and yield a connected, real
    ``GrpcDaemon`` -- ``test_daemon_client.py``'s own ``_bind_client``,
    extended with the *transport* parameter ticket 005 added to
    :func:`~aprilcam.daemon.rpc.build_server`, exactly like
    ``test_stream_transport.py``'s ``_bind_stream_stub`` but handing
    back this ticket's ``GrpcDaemon`` instead of the bare generated
    stub.
    """
    control_dir = Path(tempfile.mkdtemp(prefix="ac-strc-ctrl-", dir="/tmp"))
    transport = build_stream_transport(service, socket_dir)
    try:
        listener = build_server(service, control_dir / "control.sock", transport=transport)
        listener.start()
        channel = grpc.insecure_channel(f"unix:{control_dir / 'control.sock'}")
        try:
            grpc.channel_ready_future(channel).result(timeout=5)
            stub = aprilcam_v2_pb2_grpc.AprilCamV2Stub(channel)
            yield GrpcDaemon(stub, target="localhost"), transport
        finally:
            channel.close()
            listener.stop(1.0)
    finally:
        transport.stop()
        shutil.rmtree(control_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# SocketImageStream / SocketTagStream -- direct, no daemon
# ---------------------------------------------------------------------------


def test_socket_image_stream_derives_from_image_stream_base():
    left, right = socket.socketpair()
    stream = SocketImageStream(left)
    try:
        assert isinstance(stream, ImageStream)
    finally:
        stream.close()
        right.close()


def test_socket_tag_stream_derives_from_tag_stream_base():
    left, right = socket.socketpair()
    stream = SocketTagStream(left)
    try:
        assert isinstance(stream, TagStream)
    finally:
        stream.close()
        right.close()


def test_socket_image_stream_decodes_a_real_get_frame_response():
    """Payload shape: a serialized ``GetFrameResponse`` -- the identical
    message ``GetFrame``'s one-shot response uses (ticket's own
    Definition of Done), decoded via ``client/models.py``'s
    ``image_frame_from_proto``, not a second hand-rolled path."""
    left, right = socket.socketpair()
    stream = SocketImageStream(left)
    try:
        msg = aprilcam_v2_pb2.GetFrameResponse(
            data=b"\xff\xd8fake-jpeg-bytes",
            width=8,
            height=6,
            encoding="jpeg",
            timestamp=123.5,
            deskewed=False,
            calibration_stale=False,
            frame_dark=False,
        )
        right.sendall(framing.pack_frame(msg.SerializeToString()))

        frame = next(iter(stream))

        assert isinstance(frame, ImageFrame)
        assert frame.data == b"\xff\xd8fake-jpeg-bytes"
        assert frame.width == 8
        assert frame.height == 6
        assert frame.encoding == "jpeg"
        assert frame.deskewed is False
    finally:
        stream.close()
        right.close()


def test_socket_tag_stream_decodes_a_real_tag_frame():
    """Payload shape: a serialized ``TagFrame`` -- the identical message
    ``GetTagsResponse.tags`` uses."""
    left, right = socket.socketpair()
    stream = SocketTagStream(left)
    try:
        msg = aprilcam_v2_pb2.TagFrame(timestamp=42.0, frame_index=3, calibration_stale=True)
        right.sendall(framing.pack_frame(msg.SerializeToString()))

        frame = next(iter(stream))

        assert isinstance(frame, TagFrame)
        assert frame.tags == ()
        assert frame.timestamp == 42.0
        assert frame.frame_index == 3
        assert frame.calibration_stale is True
    finally:
        stream.close()
        right.close()


def test_socket_image_stream_clean_close_ends_iteration_without_raising():
    """``EOFError`` from ``framing.read_frame`` on a clean close --
    ``__iter__`` must surface this as a plain end of iteration
    (``StopIteration``), never a crash."""
    left, right = socket.socketpair()
    right.close()
    stream = SocketImageStream(left)

    frames = list(stream)

    assert frames == []


def test_socket_tag_stream_clean_close_ends_iteration_without_raising():
    left, right = socket.socketpair()
    right.close()
    stream = SocketTagStream(left)

    frames = list(stream)

    assert frames == []


def test_socket_image_stream_read_times_out_when_no_frame_arrives():
    """A bounded wait, mirroring v1's ``set_timeout`` -- a caller-set
    socket timeout surfaces as a real ``socket.timeout``/``OSError``,
    never swallowed into a clean ``StopIteration`` the way a genuine
    ``EOFError`` is."""
    left, right = socket.socketpair()
    try:
        left.settimeout(0.2)
        stream = SocketImageStream(left)

        with pytest.raises(OSError):
            next(iter(stream))
    finally:
        right.close()


def test_socket_tag_stream_read_times_out_when_no_frame_arrives():
    left, right = socket.socketpair()
    try:
        left.settimeout(0.2)
        stream = SocketTagStream(left)

        with pytest.raises(OSError):
            next(iter(stream))
    finally:
        right.close()


def test_socket_image_stream_close_is_idempotent():
    left, right = socket.socketpair()
    stream = SocketImageStream(left)
    stream.close()
    stream.close()  # must not raise
    right.close()


# ---------------------------------------------------------------------------
# GrpcDaemon.stream_images / stream_tags -- full real round trip
# ---------------------------------------------------------------------------


def test_grpc_daemon_stream_images_round_trip_receives_real_frames(
    directories, registry, short_socket_dir
):
    service, slug = _looping_service(directories, registry)
    with _bind_stream_client(service, short_socket_dir) as (client, _transport):
        stream = client.stream_images(slug)
        try:
            assert isinstance(stream, ImageStream)
            frame = next(iter(stream))
            assert isinstance(frame, ImageFrame)
            assert frame.width == 4 and frame.height == 4
            assert frame.deskewed is False
        finally:
            stream.close()


def test_grpc_daemon_stream_tags_round_trip_receives_real_frames(
    directories, registry, short_socket_dir
):
    service, slug = _looping_service(directories, registry)
    with _bind_stream_client(service, short_socket_dir) as (client, _transport):
        stream = client.stream_tags(slug)
        try:
            assert isinstance(stream, TagStream)
            frame = next(iter(stream))
            assert isinstance(frame, TagFrame)
        finally:
            stream.close()


def test_grpc_daemon_stream_images_close_unsubscribes_and_idle_gates_daemon_side(
    directories, registry, short_socket_dir
):
    service, slug = _looping_service(directories, registry)
    with _bind_stream_client(service, short_socket_dir) as (client, _transport):
        stream = client.stream_images(slug)
        # Keep the iterator itself referenced, not just its first
        # result: SocketImageStream.__iter__'s docstring notes that a
        # throwaway `next(iter(stream))` -- discarding the intermediate
        # generator -- closes the socket as soon as that generator is
        # garbage collected. Holding `iterator` keeps it alive so the
        # "prove flowing" assertions below observe a genuinely open
        # subscription, not one already torn down by the interpreter.
        iterator = iter(stream)
        next(iterator)  # prove the subscription is really flowing first

        producer = service._image_stream_producers[(slug, False)]  # noqa: SLF001
        assert _wait_until(lambda: producer.running is True)
        assert producer.broadcaster.subscriber_count == 1

        stream.close()

        # Bounded time: the daemon notices a closed socket on its next
        # publish attempt at worst -- ticket 005's own idle-gating,
        # verified end-to-end through this ticket's real client class
        # rather than a raw socket.
        assert _wait_until(lambda: producer.broadcaster.subscriber_count == 0, timeout=5.0)
        assert _wait_until(lambda: producer.running is False, timeout=5.0)
        assert (slug, False) in service._image_stream_producers  # noqa: SLF001
        del iterator


def test_grpc_daemon_stream_tags_close_unsubscribes_and_idle_gates_daemon_side(
    directories, registry, short_socket_dir
):
    service, slug = _looping_service(directories, registry)
    with _bind_stream_client(service, short_socket_dir) as (client, _transport):
        stream = client.stream_tags(slug)
        iterator = iter(stream)  # see the image-stream test above for why
        next(iterator)

        producer = service._tag_stream_producers[slug]  # noqa: SLF001
        assert _wait_until(lambda: producer.running is True)
        assert producer.broadcaster.subscriber_count == 1

        stream.close()

        assert _wait_until(lambda: producer.broadcaster.subscriber_count == 0, timeout=5.0)
        assert _wait_until(lambda: producer.running is False, timeout=5.0)
        assert slug in service._tag_stream_producers  # noqa: SLF001
        del iterator


def test_grpc_daemon_stream_images_second_call_shares_one_daemon_side_producer(
    directories, registry, short_socket_dir
):
    """Endpoint reuse (ticket 005): two calls for the same (camera,
    deskewed) resolve to the same daemon-side producer -- the client
    needs no connection pooling of its own to avoid a duplicate."""
    service, slug = _looping_service(directories, registry)
    with _bind_stream_client(service, short_socket_dir) as (client, _transport):
        stream1 = client.stream_images(slug)
        stream2 = client.stream_images(slug)
        iterator1 = iter(stream1)  # kept alive -- see the idle-gating test above
        iterator2 = iter(stream2)
        try:
            next(iterator1)
            next(iterator2)
            assert len(service._image_stream_producers) == 1  # noqa: SLF001
            producer = service._image_stream_producers[(slug, False)]  # noqa: SLF001
            assert _wait_until(lambda: producer.broadcaster.subscriber_count == 2)
        finally:
            stream1.close()
            stream2.close()


def test_grpc_daemon_stream_tags_unknown_camera_connects_then_raises_unknown_camera(
    directories, registry, short_socket_dir
):
    """Endpoint allocation never validates the camera (ticket 005's own
    contract) -- an unknown camera's socket still connects, and the
    sender thread's ``subscribe()`` raises ``UnknownCamera``. Sprint 043
    ticket 003: that failure now reaches the client as a typed error
    frame, so iteration raises ``UnknownCamera`` rather than ending
    cleanly with no frames."""
    from aprilcam.errors import UnknownCamera

    service, _slug = _looping_service(directories, registry)
    with _bind_stream_client(service, short_socket_dir) as (client, _transport):
        stream = client.stream_tags("no-such-camera")
        try:
            with pytest.raises(UnknownCamera):
                list(stream)
        finally:
            stream.close()
