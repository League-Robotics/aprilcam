"""Stream error frames (sprint 043 ticket 003, SUC-004): a failed
subscribe reaches the client as a typed error, not a clean end of stream.

Hardware-free: a socketpair with a fake sender for the client decode, and
a real ``StreamTransportManager`` with a service double whose
``stream_images``/``stream_tags`` raise for the daemon-to-client path.
"""

from __future__ import annotations

import shutil
import socket
import tempfile
from pathlib import Path
from typing import Iterator

import pytest

from aprilcam import framing
from aprilcam.client.streams import SocketImageStream, SocketTagStream
from aprilcam.daemon.interfaces import Daemon
from aprilcam.daemon.rpc import _stream_error_bytes, build_stream_transport
from aprilcam.errors import (
    AprilCamError,
    CameraNotPresent,
    NotCalibrated,
    UnknownCamera,
)
from aprilcam.proto import aprilcam_v2_pb2


def _error_frame(kind: str, message: str = "boom", subject: str = "", available=()) -> bytes:
    body = aprilcam_v2_pb2.StreamError(
        kind=kind, message=message, subject=subject, available=list(available)
    ).SerializeToString()
    return framing.pack_frame(framing.ERROR_FRAME_MARKER + body)


def _stream_over_pair(cls, wire: bytes):
    client, server = socket.socketpair()
    server.sendall(wire)
    server.close()
    return cls(client)


@pytest.mark.parametrize("cls", [SocketImageStream, SocketTagStream])
def test_not_calibrated_error_frame_raises(cls):
    stream = _stream_over_pair(cls, _error_frame("NotCalibrated", "'cam' is not calibrated"))
    with pytest.raises(NotCalibrated, match="not calibrated"):
        list(stream)


@pytest.mark.parametrize("cls", [SocketImageStream, SocketTagStream])
def test_unknown_camera_error_frame_raises_with_alternatives(cls):
    stream = _stream_over_pair(cls, _error_frame("UnknownCamera", subject="nope", available=("a", "b")))
    with pytest.raises(UnknownCamera) as info:
        list(stream)
    assert info.value.requested == "nope"
    assert info.value.available == ("a", "b")


@pytest.mark.parametrize("cls", [SocketImageStream, SocketTagStream])
def test_camera_not_present_error_frame_raises(cls):
    stream = _stream_over_pair(cls, _error_frame("CameraNotPresent", subject="usb-1"))
    with pytest.raises(CameraNotPresent) as info:
        list(stream)
    assert info.value.slug == "usb-1"


@pytest.mark.parametrize("cls", [SocketImageStream, SocketTagStream])
def test_unknown_kind_raises_generic_aprilcam_error(cls):
    stream = _stream_over_pair(cls, _error_frame("ValueError", "weird"))
    with pytest.raises(AprilCamError, match="weird") as info:
        list(stream)
    assert type(info.value) is AprilCamError


def test_clean_close_without_error_frame_is_still_a_clean_end():
    assert list(_stream_over_pair(SocketTagStream, b"")) == []


def test_empty_tag_frame_is_not_mistaken_for_an_error():
    # An all-defaults TagFrame serializes to zero bytes.
    wire = framing.pack_frame(aprilcam_v2_pb2.TagFrame().SerializeToString())
    frames = list(_stream_over_pair(SocketTagStream, wire))
    assert len(frames) == 1


class _FailingDaemon(Daemon):
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def stream_images(self, camera, *, deskewed=False):
        raise self._exc

    def stream_tags(self, camera):
        raise self._exc


@pytest.fixture
def socket_dir() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="apr-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _endpoint_proto(endpoint) -> aprilcam_v2_pb2.StreamEndpoint:
    return aprilcam_v2_pb2.StreamEndpoint(socket_path=endpoint.socket_path)


@pytest.mark.parametrize(
    "exc, expected",
    [
        (NotCalibrated("'cam' is not calibrated"), NotCalibrated),
        (UnknownCamera("x", ("cam",)), UnknownCamera),
        (CameraNotPresent("cam"), CameraNotPresent),
        (RuntimeError("unexpected"), AprilCamError),
    ],
)
def test_failing_subscribe_sends_error_frame_then_closes(socket_dir, exc, expected):
    transport = build_stream_transport(_FailingDaemon(exc), socket_dir, enable_tcp=False)
    try:
        endpoint = transport.get_or_create_image_endpoint("cam", deskewed=True)
        stream = SocketImageStream.connect(_endpoint_proto(endpoint), host="localhost")
        stream._sock.settimeout(5.0)
        with pytest.raises(expected):
            list(stream)

        tag_endpoint = transport.get_or_create_tag_endpoint("cam")
        tag_stream = SocketTagStream.connect(_endpoint_proto(tag_endpoint), host="localhost")
        tag_stream._sock.settimeout(5.0)
        with pytest.raises(expected):
            list(tag_stream)
    finally:
        transport.stop()


def test_stream_error_bytes_round_trip():
    msg = aprilcam_v2_pb2.StreamError.FromString(_stream_error_bytes(UnknownCamera("x", ("a",))))
    assert (msg.kind, msg.subject, list(msg.available)) == ("UnknownCamera", "x", ["a"])
