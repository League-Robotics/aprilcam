"""Tests for ImageStreamConsumer and TagStreamConsumer host selection (014-008).

Verifies that when a consumer is constructed with an explicit *host* argument
and the endpoint has only a tcp_port (no Unix socket path), the consumer
connects to the provided *host* — not unconditionally to "localhost".

This is the invariant that allows remote Mac/Pi clients to reach the correct
machine when DaemonControl is TCP-connected (i.e. _unix_path is None).
"""

from __future__ import annotations

import socket
from unittest.mock import MagicMock, patch, call

import pytest

from aprilcam.client.models import StreamEndpoint
from aprilcam.client.stream import ImageStreamConsumer, TagStreamConsumer


# ---------------------------------------------------------------------------
# ImageStreamConsumer host selection
# ---------------------------------------------------------------------------


class TestImageStreamConsumerHost:
    """ImageStreamConsumer.connect() connects to self._host when using TCP."""

    def test_default_host_is_localhost(self) -> None:
        """Without an explicit host, ImageStreamConsumer defaults to 'localhost'."""
        endpoint = StreamEndpoint(tcp_port=9876)
        consumer = ImageStreamConsumer(endpoint)
        assert consumer._host == "localhost"

    def test_explicit_host_stored(self) -> None:
        """Constructor stores the provided host."""
        endpoint = StreamEndpoint(tcp_port=9876)
        consumer = ImageStreamConsumer(endpoint, host="pi.local")
        assert consumer._host == "pi.local"

    def test_connect_uses_provided_host(self) -> None:
        """connect() calls socket.connect((host, port)) with the provided host."""
        endpoint = StreamEndpoint(tcp_port=9876)
        consumer = ImageStreamConsumer(endpoint, host="pi.local")

        fake_sock = MagicMock()

        with patch("socket.socket") as mock_socket_cls:
            mock_socket_cls.return_value = fake_sock
            consumer.connect()

        # Verify the socket connected to the correct host.
        fake_sock.connect.assert_called_once_with(("pi.local", 9876))

    def test_connect_defaults_to_localhost(self) -> None:
        """connect() defaults to 'localhost' when no explicit host is given."""
        endpoint = StreamEndpoint(tcp_port=5555)
        consumer = ImageStreamConsumer(endpoint)  # host defaults to "localhost"

        fake_sock = MagicMock()

        with patch("socket.socket") as mock_socket_cls:
            mock_socket_cls.return_value = fake_sock
            consumer.connect()

        fake_sock.connect.assert_called_once_with(("localhost", 5555))

    def test_connect_ignores_host_for_unix_socket(self) -> None:
        """connect() uses the Unix socket when the daemon host is local."""
        import tempfile
        import os

        sock_path = tempfile.mktemp(prefix="ac_isc_", suffix=".sock", dir="/tmp")

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(sock_path)
        server.listen(1)

        endpoint = StreamEndpoint(socket_path=sock_path, tcp_port=9999)
        consumer = ImageStreamConsumer(endpoint, host="localhost")
        consumer.connect()
        conn, _ = server.accept()

        # Local host + socket_path → Unix socket (AF_UNIX), not TCP
        assert consumer._sock is not None
        assert consumer._sock.family == socket.AF_UNIX

        consumer.close()
        conn.close()
        server.close()
        try:
            os.unlink(sock_path)
        except OSError:
            pass

    def test_host_forwarded_by_daemon_control_stream_host(self) -> None:
        """DaemonControl._stream_host() returns self._host when TCP-connected."""
        from aprilcam.client.control import DaemonControl

        # TCP-connected DaemonControl (no unix_path).
        dc = DaemonControl(unix_path=None, host="pi.local", port=5280)
        assert dc._stream_host() == "pi.local"

    def test_stream_host_is_localhost_when_unix_connected(self) -> None:
        """DaemonControl._stream_host() returns 'localhost' when Unix-connected."""
        from aprilcam.client.control import DaemonControl

        dc = DaemonControl(unix_path="/tmp/fake.sock", host="ignored-host")
        assert dc._stream_host() == "localhost"


# ---------------------------------------------------------------------------
# TagStreamConsumer host selection
# ---------------------------------------------------------------------------


class TestTagStreamConsumerHost:
    """TagStreamConsumer.connect() connects to self._host when using TCP."""

    def test_default_host_is_localhost(self) -> None:
        """Without an explicit host, TagStreamConsumer defaults to 'localhost'."""
        endpoint = StreamEndpoint(tcp_port=9876)
        consumer = TagStreamConsumer(endpoint)
        assert consumer._host == "localhost"

    def test_explicit_host_stored(self) -> None:
        """Constructor stores the provided host."""
        endpoint = StreamEndpoint(tcp_port=9876)
        consumer = TagStreamConsumer(endpoint, host="robot-pi.local")
        assert consumer._host == "robot-pi.local"

    def test_connect_uses_provided_host(self) -> None:
        """connect() calls socket.connect((host, port)) with the provided host."""
        endpoint = StreamEndpoint(tcp_port=7777)
        consumer = TagStreamConsumer(endpoint, host="robot-pi.local")

        fake_sock = MagicMock()

        with patch("socket.socket") as mock_socket_cls:
            mock_socket_cls.return_value = fake_sock
            consumer.connect()

        fake_sock.connect.assert_called_once_with(("robot-pi.local", 7777))

    def test_connect_defaults_to_localhost(self) -> None:
        """connect() defaults to 'localhost' when no explicit host is given."""
        endpoint = StreamEndpoint(tcp_port=6666)
        consumer = TagStreamConsumer(endpoint)

        fake_sock = MagicMock()

        with patch("socket.socket") as mock_socket_cls:
            mock_socket_cls.return_value = fake_sock
            consumer.connect()

        fake_sock.connect.assert_called_once_with(("localhost", 6666))

    def test_connect_ignores_host_for_unix_socket(self) -> None:
        """connect() uses the Unix socket when the daemon host is local."""
        import tempfile
        import os

        sock_path = tempfile.mktemp(prefix="ac_tsc_", suffix=".sock", dir="/tmp")

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(sock_path)
        server.listen(1)

        endpoint = StreamEndpoint(socket_path=sock_path, tcp_port=9999)
        consumer = TagStreamConsumer(endpoint, host="localhost")
        consumer.connect()
        conn, _ = server.accept()

        # Local host + socket_path → Unix socket (AF_UNIX), not TCP.
        assert consumer._sock is not None
        assert consumer._sock.family == socket.AF_UNIX

        consumer.close()
        conn.close()
        server.close()
        try:
            os.unlink(sock_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# EnumerateCameras used by clients (no local probe)
# ---------------------------------------------------------------------------


class TestListCamerasUsesEnumerateCameras:
    """_handle_list_cameras() uses EnumerateCameras RPC, never a local probe."""

    def test_list_cameras_calls_enumerate_cameras_rpc(self, monkeypatch) -> None:
        """_handle_list_cameras() calls client.enumerate_cameras(), not local probe."""
        from aprilcam.server import mcp_server
        from aprilcam.client.models import CameraDevice

        fake_devices = [
            CameraDevice(index=0, name="FaceTime HD Camera", slug="facetime-hd-camera"),
            CameraDevice(index=1, name="OV9782 1", slug="ov9782-1"),
        ]

        fake_dc = MagicMock()
        fake_dc.enumerate_cameras.return_value = fake_devices
        monkeypatch.setattr(mcp_server, "_ensure_daemon_client", lambda: fake_dc)

        result = mcp_server._handle_list_cameras()

        fake_dc.enumerate_cameras.assert_called_once()
        assert len(result) == 2
        assert result[0]["name"] == "FaceTime HD Camera"
        assert result[1]["slug"] == "ov9782-1"

    def test_list_cameras_never_calls_local_camutil(self, monkeypatch) -> None:
        """_handle_list_cameras() never calls camutil.list_cameras (local probe)."""
        from aprilcam.server import mcp_server

        fake_dc = MagicMock()
        fake_dc.enumerate_cameras.return_value = []
        monkeypatch.setattr(mcp_server, "_ensure_daemon_client", lambda: fake_dc)

        # If camutil.list_cameras were called, it would probe hardware and block.
        # Patch it to raise so the test fails if it's called.
        with patch("aprilcam.camera.camutil.list_cameras",
                   side_effect=AssertionError("camutil.list_cameras must not be called")):
            result = mcp_server._handle_list_cameras()

        # Reached here without AssertionError — camutil was not called.
        assert isinstance(result, list)
