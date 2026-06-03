"""Tests for aprilcam.daemon.stream — ImageStreamProducer and TagStreamProducer."""

from __future__ import annotations

import socket
import struct
import time
from pathlib import Path

import pytest

pytest.importorskip("aprilcam.daemon.grpc_server", reason="requires aprilcam[daemon]")

from aprilcam.config import Config
from aprilcam.daemon.stream import ImageStreamProducer, TagStreamProducer
from aprilcam.proto import aprilcam_pb2


# ── Helpers ───────────────────────────────────────────────────────────────────

_LENGTH_FMT = ">I"
_LENGTH_SIZE = struct.calcsize(_LENGTH_FMT)


def _recv_exactly(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError(
                f"Socket closed after {len(buf)} bytes (expected {n})"
            )
        buf += chunk
    return bytes(buf)


def _read_proto_frame(sock: socket.socket) -> bytes:
    """Read one length-prefixed protobuf payload from *sock*."""
    prefix = _recv_exactly(sock, _LENGTH_SIZE)
    (length,) = struct.unpack(_LENGTH_FMT, prefix)
    return _recv_exactly(sock, length)


def _read_tag_frame(sock: socket.socket) -> aprilcam_pb2.TagFrame:
    """Read one StreamMessage from *sock* and return its tag_frame payload."""
    payload = _read_proto_frame(sock)
    wrapper = aprilcam_pb2.StreamMessage()
    wrapper.ParseFromString(payload)
    return wrapper.tag_frame


def _make_config(tmp_path: Path) -> Config:
    """Return a Config that uses a short path under /tmp to satisfy AF_UNIX limits."""
    import tempfile

    base = Path(tempfile.mkdtemp(prefix="acs_", dir="/tmp"))
    sock_dir = base / "s"
    data_dir = base / "d"
    sock_dir.mkdir()
    data_dir.mkdir()
    return Config(
        data_dir=data_dir,
        socket_dir=sock_dir,
        calibration_dir=data_dir / "calibration",
        log_level="DEBUG",
        daemon_pidfile=sock_dir / "aprilcamd.pid",
    )


def _make_tag_frame(
    frame_id: int = 0,
    tags: list | None = None,
    ts_mono_ns: int = 0,
    ts_wall_ms: int = 0,
    fps: float = 30.0,
) -> aprilcam_pb2.TagFrame:
    """Build a minimal TagFrame protobuf for testing."""
    tag_msgs = []
    for tag in (tags or []):
        tag_msgs.append(
            aprilcam_pb2.TagMsg(
                id=tag["id"],
                cx_px=float(tag.get("cx_px", 100.0)),
                cy_px=float(tag.get("cy_px", 100.0)),
            )
        )
    return aprilcam_pb2.TagFrame(
        frame_id=frame_id,
        ts_mono_ns=ts_mono_ns,
        ts_wall_ms=ts_wall_ms,
        tags=tag_msgs,
        fps=fps,
    )


# ── ImageStreamProducer tests ─────────────────────────────────────────────────


class TestImageStreamProducer:
    def test_start_creates_unix_socket(self, tmp_path):
        """start() returns a StreamEndpoint with a non-None socket_path."""
        config = _make_config(tmp_path)
        producer = ImageStreamProducer("cam0", config, enable_unix=True, enable_tcp=False)
        try:
            endpoint = producer.start()
            assert endpoint.socket_path is not None
            assert Path(endpoint.socket_path).exists()
        finally:
            producer.stop()

    def test_start_creates_tcp_socket(self, tmp_path):
        """start() with enable_tcp=True returns a non-zero tcp_port."""
        config = _make_config(tmp_path)
        producer = ImageStreamProducer("cam0", config, enable_unix=False, enable_tcp=True)
        try:
            endpoint = producer.start()
            assert endpoint.tcp_port is not None
            assert endpoint.tcp_port > 0
        finally:
            producer.stop()

    def test_publish_delivers_image_frame_via_unix(self, tmp_path):
        """A client connected via Unix socket receives a valid ImageFrame."""
        config = _make_config(tmp_path)
        producer = ImageStreamProducer("cam0", config, enable_unix=True, enable_tcp=False)
        try:
            endpoint = producer.start()
            assert endpoint.socket_path is not None

            # Connect a raw client
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(5.0)
            client.connect(endpoint.socket_path)

            # Give the accept thread time to register the connection
            time.sleep(0.1)

            fake_jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 100 + b"\xff\xd9"
            producer.publish(
                frame_id=42,
                ts_mono_ns=1_000_000_000,
                ts_wall_ms=1_700_000_000_000,
                jpeg=fake_jpeg,
                width=640,
                height=480,
            )

            payload = _read_proto_frame(client)
            msg = aprilcam_pb2.ImageFrame()
            msg.ParseFromString(payload)

            assert msg.frame_id == 42
            assert msg.width == 640
            assert msg.height == 480
            assert msg.jpeg == fake_jpeg
        finally:
            try:
                client.close()
            except Exception:
                pass
            producer.stop()

    def test_publish_delivers_image_frame_via_tcp(self, tmp_path):
        """A client connected via TCP receives a valid ImageFrame."""
        config = _make_config(tmp_path)
        producer = ImageStreamProducer("cam0", config, enable_unix=False, enable_tcp=True)
        try:
            endpoint = producer.start()
            assert endpoint.tcp_port is not None

            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            client.settimeout(5.0)
            client.connect(("127.0.0.1", endpoint.tcp_port))

            time.sleep(0.1)

            fake_jpeg = b"\xff\xd8" + b"\xaa" * 50 + b"\xff\xd9"
            producer.publish(1, 0, 0, fake_jpeg, 320, 240)

            payload = _read_proto_frame(client)
            msg = aprilcam_pb2.ImageFrame()
            msg.ParseFromString(payload)

            assert msg.frame_id == 1
            assert msg.jpeg == fake_jpeg
        finally:
            try:
                client.close()
            except Exception:
                pass
            producer.stop()

    def test_multiple_clients_all_receive(self, tmp_path):
        """Two clients connected simultaneously both receive the published frame."""
        config = _make_config(tmp_path)
        producer = ImageStreamProducer("cam0", config, enable_unix=True, enable_tcp=False)
        clients = []
        try:
            endpoint = producer.start()
            for _ in range(2):
                c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                c.settimeout(5.0)
                c.connect(endpoint.socket_path)
                clients.append(c)

            time.sleep(0.15)

            fake_jpeg = b"\xff\xd8\xff\xd9"
            producer.publish(99, 0, 0, fake_jpeg, 100, 100)

            for c in clients:
                payload = _read_proto_frame(c)
                msg = aprilcam_pb2.ImageFrame()
                msg.ParseFromString(payload)
                assert msg.frame_id == 99
        finally:
            for c in clients:
                try:
                    c.close()
                except Exception:
                    pass
            producer.stop()

    def test_stop_cleans_up_socket_file(self, tmp_path):
        """stop() removes the Unix socket file."""
        config = _make_config(tmp_path)
        producer = ImageStreamProducer("cam0", config, enable_unix=True, enable_tcp=False)
        endpoint = producer.start()
        sock_path = Path(endpoint.socket_path)
        assert sock_path.exists()
        producer.stop()
        assert not sock_path.exists()


# ── TagStreamProducer tests ───────────────────────────────────────────────────


class TestTagStreamProducer:
    def test_start_returns_endpoint(self, tmp_path):
        config = _make_config(tmp_path)
        producer = TagStreamProducer("cam0", config, enable_unix=True, enable_tcp=False)
        try:
            endpoint = producer.start()
            assert endpoint.socket_path is not None
        finally:
            producer.stop()

    def test_change_triggers_publish(self, tmp_path):
        """Moving a tag beyond the threshold causes an immediate publish."""
        config = _make_config(tmp_path)
        producer = TagStreamProducer(
            "cam0", config,
            max_hz=100.0,
            change_threshold_px=8.0,
            enable_unix=True,
            enable_tcp=False,
        )
        try:
            endpoint = producer.start()

            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(3.0)
            client.connect(endpoint.socket_path)
            time.sleep(0.1)

            # First publish (establishes baseline positions)
            frame1 = _make_tag_frame(frame_id=1, tags=[{"id": 5, "cx_px": 100.0, "cy_px": 100.0}])
            producer.publish_if_changed(frame1)
            msg1 = _read_tag_frame(client)
            assert msg1.frame_id == 1

            # Move tag more than 8 px — should trigger publish
            frame2 = _make_tag_frame(frame_id=2, tags=[{"id": 5, "cx_px": 110.0, "cy_px": 100.0}])
            producer.publish_if_changed(frame2)
            msg2 = _read_tag_frame(client)
            assert msg2.frame_id == 2
        finally:
            try:
                client.close()
            except Exception:
                pass
            producer.stop()

    def test_no_change_suppresses_publish(self, tmp_path):
        """A stationary tag set does not trigger a publish (within the heartbeat window)."""
        config = _make_config(tmp_path)
        producer = TagStreamProducer(
            "cam0", config,
            max_hz=100.0,
            change_threshold_px=8.0,
            enable_unix=True,
            enable_tcp=False,
        )
        try:
            endpoint = producer.start()
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(0.3)
            client.connect(endpoint.socket_path)
            time.sleep(0.1)

            # Publish first frame to establish baseline
            frame1 = _make_tag_frame(frame_id=1, tags=[{"id": 5, "cx_px": 100.0, "cy_px": 100.0}])
            producer.publish_if_changed(frame1)
            # Drain the first publish
            _read_proto_frame(client)

            # Try to publish again with the same position — should be suppressed
            frame2 = _make_tag_frame(frame_id=2, tags=[{"id": 5, "cx_px": 100.0, "cy_px": 100.0}])
            producer.publish_if_changed(frame2)

            # No message should arrive within the timeout
            with pytest.raises((socket.timeout, TimeoutError)):
                _read_proto_frame(client)
        finally:
            try:
                client.close()
            except Exception:
                pass
            producer.stop()

    def test_tag_entry_triggers_publish(self, tmp_path):
        """A new tag appearing in the scene triggers an immediate publish."""
        config = _make_config(tmp_path)
        producer = TagStreamProducer(
            "cam0", config,
            max_hz=100.0,
            change_threshold_px=8.0,
            enable_unix=True,
            enable_tcp=False,
        )
        try:
            endpoint = producer.start()
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(3.0)
            client.connect(endpoint.socket_path)
            time.sleep(0.1)

            # Frame with no tags (establishes empty baseline)
            frame1 = _make_tag_frame(frame_id=1, tags=[])
            producer.publish_if_changed(frame1)
            _read_proto_frame(client)  # drain

            # New tag appears
            frame2 = _make_tag_frame(frame_id=2, tags=[{"id": 7, "cx_px": 200.0, "cy_px": 200.0}])
            producer.publish_if_changed(frame2)
            msg = _read_tag_frame(client)
            assert msg.frame_id == 2
            assert len(msg.tags) == 1
            assert msg.tags[0].id == 7
        finally:
            try:
                client.close()
            except Exception:
                pass
            producer.stop()

    def test_heartbeat_fires_after_one_second(self, tmp_path):
        """The heartbeat publishes a frame ~1 second after the last change publish."""
        config = _make_config(tmp_path)
        producer = TagStreamProducer(
            "cam0", config,
            max_hz=100.0,
            change_threshold_px=8.0,
            enable_unix=True,
            enable_tcp=False,
        )
        try:
            endpoint = producer.start()
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(3.0)
            client.connect(endpoint.socket_path)
            time.sleep(0.1)

            # Set a stable tag so the heartbeat has something to publish
            frame = _make_tag_frame(frame_id=10, tags=[{"id": 1, "cx_px": 50.0, "cy_px": 50.0}])
            producer.publish_if_changed(frame)
            _read_proto_frame(client)  # drain initial publish

            # With stationary tags, the next publish should be the heartbeat
            # (after ~1 second).  We wait up to 2 seconds.
            t0 = time.monotonic()
            payload = _read_proto_frame(client)  # blocks until heartbeat
            elapsed = time.monotonic() - t0

            wrapper = aprilcam_pb2.StreamMessage()
            wrapper.ParseFromString(payload)
            msg = wrapper.tag_frame

            # Heartbeat should fire within ~1.5s (generous for CI)
            assert elapsed < 2.0, f"Heartbeat took too long: {elapsed:.2f}s"
            # tag_id 1 still visible in the heartbeat
            assert len(msg.tags) == 1
            assert msg.tags[0].id == 1
        finally:
            try:
                client.close()
            except Exception:
                pass
            producer.stop()

    def test_force_publish_always_publishes(self, tmp_path):
        """force_publish() sends the frame unconditionally."""
        config = _make_config(tmp_path)
        producer = TagStreamProducer(
            "cam0", config,
            max_hz=100.0,
            change_threshold_px=8.0,
            enable_unix=True,
            enable_tcp=False,
        )
        try:
            endpoint = producer.start()
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(3.0)
            client.connect(endpoint.socket_path)
            time.sleep(0.1)

            frame = _make_tag_frame(frame_id=5, tags=[])
            producer.force_publish(frame)

            msg = _read_tag_frame(client)
            assert msg.frame_id == 5
        finally:
            try:
                client.close()
            except Exception:
                pass
            producer.stop()

    def test_rate_limit_respected(self, tmp_path):
        """Changes faster than max_hz are suppressed after the first publish."""
        config = _make_config(tmp_path)
        producer = TagStreamProducer(
            "cam0", config,
            max_hz=2.0,           # at most 1 publish per 0.5s
            change_threshold_px=8.0,
            enable_unix=True,
            enable_tcp=False,
        )
        try:
            endpoint = producer.start()
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(0.3)
            client.connect(endpoint.socket_path)
            time.sleep(0.1)

            # Publish first frame (always goes through as a change)
            frame1 = _make_tag_frame(frame_id=1, tags=[{"id": 1, "cx_px": 0.0, "cy_px": 0.0}])
            producer.publish_if_changed(frame1)
            _read_proto_frame(client)  # drain

            # Rapid subsequent changes — each moves 20px so it's a "change",
            # but the rate limiter should suppress most of them
            for i in range(5):
                move_frame = _make_tag_frame(
                    frame_id=i + 2,
                    tags=[{"id": 1, "cx_px": float((i + 1) * 20), "cy_px": 0.0}],
                )
                producer.publish_if_changed(move_frame)

            # Should have suppressed the rapid fire; at most 1 more frame
            # arrives in <0.3s (which is less than the 0.5s min interval)
            received = 0
            while True:
                try:
                    _read_proto_frame(client)
                    received += 1
                except (socket.timeout, TimeoutError):
                    break

            assert received <= 1, f"Rate limiter failed: got {received} publishes"
        finally:
            try:
                client.close()
            except Exception:
                pass
            producer.stop()
