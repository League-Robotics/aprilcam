"""Tests for 014-007: TCP streaming — daemon binds 0.0.0.0, host-aware
stream consumers, tags CLI via daemon RPC, view CLI host-aware.
"""

from __future__ import annotations

import socket
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# 1. daemon/stream.py _bind_tcp_socket binds 0.0.0.0
# ---------------------------------------------------------------------------


def test_bind_tcp_socket_binds_all_interfaces():
    """_bind_tcp_socket binds to 0.0.0.0 so remote clients can connect."""
    pytest.importorskip("aprilcam.daemon.stream", reason="requires aprilcam[daemon]")
    from aprilcam.daemon.stream import _bind_tcp_socket

    sock = _bind_tcp_socket()
    assert sock is not None, "_bind_tcp_socket() returned None"
    try:
        host, port = sock.getsockname()
        assert host == "0.0.0.0", (
            f"Stream socket should bind to 0.0.0.0, got {host!r}"
        )
        assert port > 0, f"Expected non-zero ephemeral port, got {port}"
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# 2. client/stream.py ImageStreamConsumer accepts host param
# ---------------------------------------------------------------------------


class TestImageStreamConsumerHostParam:
    """ImageStreamConsumer respects the host parameter on TCP connect."""

    def test_default_host_is_localhost(self):
        """host defaults to 'localhost' for backward compatibility."""
        from aprilcam.client.models import StreamEndpoint
        from aprilcam.client.stream import ImageStreamConsumer

        endpoint = StreamEndpoint(tcp_port=9999)
        consumer = ImageStreamConsumer(endpoint)
        assert consumer._host == "localhost"

    def test_custom_host_stored(self):
        """Custom host is stored on the instance."""
        from aprilcam.client.models import StreamEndpoint
        from aprilcam.client.stream import ImageStreamConsumer

        endpoint = StreamEndpoint(tcp_port=9999)
        consumer = ImageStreamConsumer(endpoint, host="192.168.1.50")
        assert consumer._host == "192.168.1.50"

    def test_unix_socket_ignores_host(self, tmp_path):
        """When socket_path is set, Unix socket is preferred regardless of host."""
        import tempfile

        from aprilcam.client.models import StreamEndpoint
        from aprilcam.client.stream import ImageStreamConsumer

        sock_path = tempfile.mktemp(prefix="ac_img_", suffix=".sock", dir="/tmp")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(sock_path)
        server.listen(1)

        endpoint = StreamEndpoint(socket_path=sock_path, tcp_port=9999)
        consumer = ImageStreamConsumer(endpoint, host="192.168.1.50")
        consumer.connect()
        conn, _ = server.accept()

        # It connected via Unix socket despite a custom host
        assert consumer._sock is not None
        assert consumer._sock.family == socket.AF_UNIX
        consumer.close()
        conn.close()
        server.close()

    def test_tcp_connect_uses_custom_host(self, monkeypatch):
        """When only tcp_port is set, connect() uses self._host."""
        from aprilcam.client.models import StreamEndpoint
        from aprilcam.client.stream import ImageStreamConsumer

        connected_to: list[tuple] = []

        original_connect = socket.socket.connect

        def _fake_connect(self_sock, addr):
            connected_to.append(addr)
            # Prevent actual network call — raise immediately
            raise ConnectionRefusedError("test: no server")

        monkeypatch.setattr(socket.socket, "connect", _fake_connect)

        endpoint = StreamEndpoint(tcp_port=5555)
        consumer = ImageStreamConsumer(endpoint, host="pi.local")
        with pytest.raises(ConnectionRefusedError):
            consumer.connect()

        assert len(connected_to) == 1
        assert connected_to[0] == ("pi.local", 5555)


# ---------------------------------------------------------------------------
# 3. client/stream.py TagStreamConsumer accepts host param
# ---------------------------------------------------------------------------


class TestTagStreamConsumerHostParam:
    """TagStreamConsumer respects the host parameter on TCP connect."""

    def test_default_host_is_localhost(self):
        """host defaults to 'localhost'."""
        from aprilcam.client.models import StreamEndpoint
        from aprilcam.client.stream import TagStreamConsumer

        endpoint = StreamEndpoint(tcp_port=9999)
        consumer = TagStreamConsumer(endpoint)
        assert consumer._host == "localhost"

    def test_custom_host_stored(self):
        """Custom host is stored on the instance."""
        from aprilcam.client.models import StreamEndpoint
        from aprilcam.client.stream import TagStreamConsumer

        endpoint = StreamEndpoint(tcp_port=9999)
        consumer = TagStreamConsumer(endpoint, host="10.0.0.5")
        assert consumer._host == "10.0.0.5"

    def test_tcp_connect_uses_custom_host(self, monkeypatch):
        """When only tcp_port is set, connect() uses self._host."""
        from aprilcam.client.models import StreamEndpoint
        from aprilcam.client.stream import TagStreamConsumer

        connected_to: list[tuple] = []

        def _fake_connect(self_sock, addr):
            connected_to.append(addr)
            raise ConnectionRefusedError("test: no server")

        monkeypatch.setattr(socket.socket, "connect", _fake_connect)

        endpoint = StreamEndpoint(tcp_port=6666)
        consumer = TagStreamConsumer(endpoint, host="vidar.local")
        with pytest.raises(ConnectionRefusedError):
            consumer.connect()

        assert connected_to[0] == ("vidar.local", 6666)


# ---------------------------------------------------------------------------
# 4. DaemonControl._stream_host() returns correct host per transport
# ---------------------------------------------------------------------------


class TestDaemonControlStreamHost:
    """DaemonControl._stream_host() returns localhost for Unix, self._host for TCP."""

    def test_unix_connection_uses_localhost(self):
        """Unix-connected DaemonControl returns 'localhost' for stream host."""
        from aprilcam.client.control import DaemonControl

        dc = DaemonControl(unix_path="/tmp/aprilcam/control.sock")
        assert dc._stream_host() == "localhost"

    def test_tcp_connection_uses_host(self):
        """TCP-connected DaemonControl returns self._host for stream host."""
        from aprilcam.client.control import DaemonControl

        dc = DaemonControl(host="192.168.1.100", port=5280)
        assert dc._stream_host() == "192.168.1.100"

    def test_default_tcp_host_is_localhost(self):
        """Default TCP host is 'localhost'."""
        from aprilcam.client.control import DaemonControl

        dc = DaemonControl()  # no unix_path, host defaults to localhost
        assert dc._stream_host() == "localhost"


# ---------------------------------------------------------------------------
# 5. cli/tags_cli.py — no VideoCapture; uses GetTags RPC
# ---------------------------------------------------------------------------


class TestTagsCliUsesDaemon:
    """tags_cli uses GetTags RPC; no cv.VideoCapture opened."""

    def _make_mock_dc(self):
        """Build a mock DaemonControl returning synthetic tag data.

        Uses MagicMock for the TagFrame/TagRecord to avoid constructing Pydantic
        models with all required fields — the tags_cli only iterates over
        tag_frame.tags and reads a few attributes.
        """
        # Build a lightweight mock tag record
        tr = MagicMock()
        tr.id = 5
        tr.center_px = (100.0, 200.0)
        tr.world_xy = (10.0, 20.0)
        tr.yaw = 0.5
        tr.family = "36h11"

        # Build a lightweight mock tag frame
        tf = MagicMock()
        tf.tags = [tr]

        dc = MagicMock()
        dc.open_camera.return_value = ("test-cam", "/data/cameras/test-cam")
        dc.get_tags.return_value = tf
        return dc

    def test_no_videocapture_called(self, monkeypatch):
        """tags_cli does not call cv.VideoCapture at all."""
        from aprilcam.cli import tags_cli
        from aprilcam.camera.registry import CameraRegistry, resolve_enum_to_index

        dc = self._make_mock_dc()

        monkeypatch.setattr(tags_cli, "connect_from_args" if hasattr(tags_cli, "connect_from_args") else "_placeholder", dc, raising=False)

        # Patch the cli._daemon module as imported inside tags_cli
        with patch("aprilcam.cli._daemon.connect_from_args", return_value=dc):
            with patch("cv2.VideoCapture") as mock_vc:
                with patch(
                    "aprilcam.camera.registry.resolve_enum_to_index",
                    return_value=0,
                ):
                    with patch(
                        "aprilcam.camera.identity.resolve_all",
                        return_value=[],
                    ):
                        with patch(
                            "aprilcam.camera.registry.CameraRegistry",
                        ):
                            ret = tags_cli.main(["1"])

        mock_vc.assert_not_called(), "cv2.VideoCapture must not be called in tags_cli"

    def test_get_tags_rpc_called(self, monkeypatch):
        """tags_cli calls dc.open_camera and dc.get_tags."""
        from aprilcam.cli import tags_cli

        dc = self._make_mock_dc()

        with patch("aprilcam.cli._daemon.connect_from_args", return_value=dc):
            with patch("cv2.VideoCapture"):
                with patch(
                    "aprilcam.camera.registry.resolve_enum_to_index",
                    return_value=0,
                ):
                    with patch("aprilcam.camera.identity.resolve_all", return_value=[]):
                        with patch("aprilcam.camera.registry.CameraRegistry"):
                            ret = tags_cli.main(["1"])

        dc.open_camera.assert_called_once_with(0)
        dc.get_tags.assert_called_once_with("test-cam")

    def test_returns_zero_on_success(self, monkeypatch):
        """tags_cli exits 0 when daemon responds successfully."""
        from aprilcam.cli import tags_cli

        dc = self._make_mock_dc()

        with patch("aprilcam.cli._daemon.connect_from_args", return_value=dc):
            with patch(
                "aprilcam.camera.registry.resolve_enum_to_index",
                return_value=0,
            ):
                with patch("aprilcam.camera.identity.resolve_all", return_value=[]):
                    with patch("aprilcam.camera.registry.CameraRegistry"):
                        ret = tags_cli.main(["1"])

        assert ret == 0


# ---------------------------------------------------------------------------
# 6. server/mcp_server.py start_live_view passes daemon host/port for TCP
# ---------------------------------------------------------------------------


class TestStartLiveViewDaemonArgs:
    """_handle_start_live_view passes --daemon-host/--daemon-port when TCP."""

    def _setup_registry(self, cam_id: str):
        """Register a camera so _handle_start_live_view doesn't error early."""
        from aprilcam.server import mcp_server

        mcp_server.registry.open(None, handle=cam_id)
        mcp_server._cam_info[cam_id] = {"cam_name": cam_id}

    def _teardown_registry(self, cam_id: str):
        from aprilcam.server import mcp_server

        mcp_server.live_view_registry.pop(f"live_{cam_id}", None)
        try:
            mcp_server.registry.close(cam_id)
        except Exception:
            pass
        mcp_server._cam_info.pop(cam_id, None)

    def test_tcp_connection_adds_daemon_args(self, monkeypatch):
        """When DaemonControl is TCP-connected, subprocess gets --daemon-host/--daemon-port."""
        from aprilcam.server import mcp_server

        cam_id = "view-tcp-cam-001"
        self._setup_registry(cam_id)

        # TCP-connected DaemonControl (unix_path is None)
        dc = MagicMock()
        dc._unix_path = None
        dc._host = "192.168.1.10"
        dc._port = 5280
        monkeypatch.setattr(mcp_server, "_ensure_daemon_client", lambda: dc)

        captured_cmds: list[list[str]] = []

        def _fake_popen(cmd, **kwargs):
            captured_cmds.append(cmd)
            return MagicMock()

        monkeypatch.setattr(mcp_server.subprocess, "Popen", _fake_popen)

        result = mcp_server._handle_start_live_view(cam_id)
        assert "error" not in result, result

        assert captured_cmds, "Popen was not called"
        cmd = captured_cmds[0]
        assert "--daemon-host" in cmd
        idx = cmd.index("--daemon-host")
        assert cmd[idx + 1] == "192.168.1.10"
        assert "--daemon-port" in cmd
        idx2 = cmd.index("--daemon-port")
        assert cmd[idx2 + 1] == "5280"

        self._teardown_registry(cam_id)

    def test_unix_connection_no_daemon_args(self, monkeypatch):
        """When DaemonControl is Unix-connected, subprocess does NOT get --daemon-host."""
        from aprilcam.server import mcp_server

        cam_id = "view-unix-cam-002"
        self._setup_registry(cam_id)

        # Unix-connected DaemonControl
        dc = MagicMock()
        dc._unix_path = "/tmp/aprilcam/control.sock"
        dc._host = "localhost"
        dc._port = 5280
        monkeypatch.setattr(mcp_server, "_ensure_daemon_client", lambda: dc)

        captured_cmds: list[list[str]] = []

        def _fake_popen(cmd, **kwargs):
            captured_cmds.append(cmd)
            return MagicMock()

        monkeypatch.setattr(mcp_server.subprocess, "Popen", _fake_popen)

        result = mcp_server._handle_start_live_view(cam_id)
        assert "error" not in result, result

        assert captured_cmds, "Popen was not called"
        cmd = captured_cmds[0]
        assert "--daemon-host" not in cmd, (
            f"Unix connection should not pass --daemon-host; cmd={cmd}"
        )

        self._teardown_registry(cam_id)
