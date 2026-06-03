"""Unit tests for ImageStreamConsumer and TagStreamConsumer.

Uses mock sockets to feed pre-built protobuf messages without a live daemon.
"""

from __future__ import annotations

import struct
import socket
import threading
from unittest.mock import MagicMock, patch

import pytest

from aprilcam.client.models import StreamEndpoint, TagFrame
from aprilcam.client.stream import ImageStreamConsumer, TagStreamConsumer
from aprilcam.proto import aprilcam_pb2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _length_prefix(data: bytes) -> bytes:
    """Prepend a 4-byte big-endian uint32 length prefix to *data*."""
    return struct.pack(">I", len(data)) + data


def _build_image_frame_bytes(frame_id: int, jpeg: bytes) -> bytes:
    msg = aprilcam_pb2.ImageFrame(
        frame_id=frame_id,
        ts_mono_ns=1_000_000,
        jpeg=jpeg,
        width=320,
        height=240,
    )
    return _length_prefix(msg.SerializeToString())


def _build_tag_frame_bytes(frame_id: int, fps: float = 30.0) -> bytes:
    """Build length-prefixed StreamMessage wrapping a TagFrame."""
    tag = aprilcam_pb2.TagMsg(
        id=7,
        cx_px=100.0,
        cy_px=200.0,
        corners_px=[90.0, 190.0, 110.0, 190.0, 110.0, 210.0, 90.0, 210.0],
        yaw=1.5,
    )
    tag_frame = aprilcam_pb2.TagFrame(
        frame_id=frame_id,
        ts_mono_ns=2_000_000,
        ts_wall_ms=3_000,
        tags=[tag],
        fps=fps,
    )
    stream_msg = aprilcam_pb2.StreamMessage(tag_frame=tag_frame)
    return _length_prefix(stream_msg.SerializeToString())


def _build_overlay_frame_bytes(camera_id: str = "cam0") -> bytes:
    """Build length-prefixed StreamMessage wrapping an OverlayFrame."""
    overlay = aprilcam_pb2.OverlayFrame(
        timestamp=1.0,
        ttl=5.0,
        camera_id=camera_id,
        elements=[
            aprilcam_pb2.OverlayElement(
                type="circle",
                params=[50.0, 50.0, 10.0],
                color=[255, 0, 0],
                thickness=2,
            )
        ],
    )
    stream_msg = aprilcam_pb2.StreamMessage(overlay=overlay)
    return _length_prefix(stream_msg.SerializeToString())


def _loopback_pair() -> tuple[socket.socket, socket.socket]:
    """Create a connected loopback socket pair."""
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.bind(("127.0.0.1", 0))
    server_sock.listen(1)
    port = server_sock.getsockname()[1]

    client_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client_sock.connect(("127.0.0.1", port))

    conn, _ = server_sock.accept()
    server_sock.close()
    return conn, client_sock  # (writer side, reader side)


# ---------------------------------------------------------------------------
# ImageStreamConsumer tests
# ---------------------------------------------------------------------------


class TestImageStreamConsumer:

    def test_read_raw_returns_frame_id_and_jpeg(self):
        """read_raw() correctly parses frame_id and jpeg bytes."""
        fake_jpeg = b"\xff\xd8\xff\xe0fake"
        writer, reader = _loopback_pair()

        data = _build_image_frame_bytes(frame_id=42, jpeg=fake_jpeg)
        writer.sendall(data)
        writer.close()

        endpoint = StreamEndpoint(tcp_port=None, socket_path=None)
        consumer = ImageStreamConsumer(endpoint)
        consumer._sock = reader  # inject reader socket directly

        frame_id, jpeg = consumer.read_raw()

        assert frame_id == 42
        assert jpeg == fake_jpeg
        consumer.close()

    @pytest.mark.needs_cv2
    def test_read_returns_ndarray(self):
        """read() decodes JPEG and returns a numpy array."""
        cv2 = pytest.importorskip("cv2", reason="requires aprilcam[imaging]")
        import numpy as np

        # Build a tiny valid JPEG
        img = np.zeros((8, 8, 3), dtype=np.uint8)
        _, jpeg_buf = cv2.imencode(".jpg", img)
        jpeg = jpeg_buf.tobytes()

        writer, reader = _loopback_pair()
        writer.sendall(_build_image_frame_bytes(frame_id=1, jpeg=jpeg))
        writer.close()

        endpoint = StreamEndpoint(tcp_port=None, socket_path=None)
        consumer = ImageStreamConsumer(endpoint)
        consumer._sock = reader

        frame = consumer.read()
        assert isinstance(frame, np.ndarray)
        consumer.close()

    @pytest.mark.needs_cv2
    def test_iter_stops_on_eof(self):
        """__iter__ stops cleanly when the connection closes."""
        cv2 = pytest.importorskip("cv2", reason="requires aprilcam[imaging]")
        import numpy as np

        img = np.zeros((8, 8, 3), dtype=np.uint8)
        _, jpeg_buf = cv2.imencode(".jpg", img)
        jpeg = jpeg_buf.tobytes()

        writer, reader = _loopback_pair()
        # Send two frames then close
        for fid in (1, 2):
            writer.sendall(_build_image_frame_bytes(frame_id=fid, jpeg=jpeg))
        writer.close()

        endpoint = StreamEndpoint(tcp_port=None, socket_path=None)
        consumer = ImageStreamConsumer(endpoint)
        consumer._sock = reader

        frames = list(consumer)
        assert len(frames) == 2

    def test_connect_unix_socket(self, tmp_path):
        """connect() uses Unix socket when socket_path is provided."""
        import tempfile
        sock_path = tempfile.mktemp(prefix="ac_t_", suffix=".sock", dir="/tmp")

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(sock_path)
        server.listen(1)

        endpoint = StreamEndpoint(socket_path=sock_path)
        consumer = ImageStreamConsumer(endpoint)
        consumer.connect()
        conn, _ = server.accept()

        assert consumer._sock is not None
        consumer.close()
        conn.close()
        server.close()

    def test_connect_tcp_socket(self):
        """connect() falls back to TCP when only tcp_port is provided."""
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]

        endpoint = StreamEndpoint(tcp_port=port)
        consumer = ImageStreamConsumer(endpoint)
        consumer.connect()
        conn, _ = server.accept()

        assert consumer._sock is not None
        consumer.close()
        conn.close()
        server.close()

    def test_connect_no_endpoint_raises(self):
        endpoint = StreamEndpoint()
        consumer = ImageStreamConsumer(endpoint)
        with pytest.raises(ValueError, match="neither socket_path nor tcp_port"):
            consumer.connect()

    def test_context_manager(self):
        """Context manager connects and closes cleanly."""
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]

        endpoint = StreamEndpoint(tcp_port=port)

        def accept():
            conn, _ = server.accept()
            conn.close()

        t = threading.Thread(target=accept, daemon=True)
        t.start()

        with ImageStreamConsumer(endpoint) as consumer:
            assert consumer._sock is not None

        assert consumer._sock is None
        t.join(timeout=1)
        server.close()


# ---------------------------------------------------------------------------
# TagStreamConsumer tests
# ---------------------------------------------------------------------------


class TestTagStreamConsumer:

    def test_read_returns_tag_frame(self):
        """read() returns a TagFrame Pydantic model with expected fields."""
        writer, reader = _loopback_pair()
        writer.sendall(_build_tag_frame_bytes(frame_id=99, fps=25.0))
        writer.close()

        endpoint = StreamEndpoint(tcp_port=None, socket_path=None)
        consumer = TagStreamConsumer(endpoint)
        consumer._sock = reader

        tag_frame = consumer.read()

        assert isinstance(tag_frame, TagFrame)
        assert tag_frame.frame_id == 99
        assert tag_frame.fps == pytest.approx(25.0)
        assert len(tag_frame.tags) == 1
        assert tag_frame.tags[0].id == 7
        consumer.close()

    def test_iter_yields_tag_frames(self):
        """__iter__ yields TagFrame objects until connection closes."""
        writer, reader = _loopback_pair()
        for fid in (1, 2, 3):
            writer.sendall(_build_tag_frame_bytes(frame_id=fid, fps=30.0))
        writer.close()

        endpoint = StreamEndpoint(tcp_port=None, socket_path=None)
        consumer = TagStreamConsumer(endpoint)
        consumer._sock = reader

        frames = list(consumer)
        assert len(frames) == 3
        assert all(isinstance(f, TagFrame) for f in frames)
        assert [f.frame_id for f in frames] == [1, 2, 3]

    def test_connect_prefers_unix_over_tcp(self, tmp_path):
        """connect() chooses Unix socket when socket_path is set."""
        import tempfile
        sock_path = tempfile.mktemp(prefix="ac_tags_", suffix=".sock", dir="/tmp")

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(sock_path)
        server.listen(1)

        endpoint = StreamEndpoint(socket_path=sock_path, tcp_port=9999)
        consumer = TagStreamConsumer(endpoint)
        consumer.connect()
        conn, _ = server.accept()

        # Verify it's a Unix socket (AF_UNIX family)
        assert consumer._sock.family == socket.AF_UNIX
        consumer.close()
        conn.close()
        server.close()

    def test_read_returns_overlay_frame(self):
        """read() returns an OverlayFrame proto when the StreamMessage carries an overlay."""
        writer, reader = _loopback_pair()
        writer.sendall(_build_overlay_frame_bytes(camera_id="cam0"))
        writer.close()

        endpoint = StreamEndpoint(tcp_port=None, socket_path=None)
        consumer = TagStreamConsumer(endpoint)
        consumer._sock = reader

        result = consumer.read()

        assert isinstance(result, aprilcam_pb2.OverlayFrame)
        assert result.camera_id == "cam0"
        assert len(result.elements) == 1
        consumer.close()

    def test_read_before_connect_raises(self):
        endpoint = StreamEndpoint(tcp_port=None, socket_path=None)
        consumer = TagStreamConsumer(endpoint)
        with pytest.raises(RuntimeError, match="not connected"):
            consumer.read()

    def test_context_manager(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]

        endpoint = StreamEndpoint(tcp_port=port)

        def accept():
            conn, _ = server.accept()
            conn.close()

        t = threading.Thread(target=accept, daemon=True)
        t.start()

        with TagStreamConsumer(endpoint) as consumer:
            assert consumer._sock is not None

        assert consumer._sock is None
        t.join(timeout=1)
        server.close()
