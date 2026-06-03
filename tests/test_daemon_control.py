"""Integration tests for DaemonControl.

These tests start a real gRPC server in-process and exercise DaemonControl
against it without requiring camera hardware or a running daemon process.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import grpc
import pytest

pytest.importorskip("aprilcam.daemon.grpc_server", reason="requires aprilcam[daemon]")

from aprilcam.client.control import DaemonControl
from aprilcam.daemon.grpc_server import AprilCamServicer, make_grpc_server
from aprilcam.proto import aprilcam_pb2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_server(tmp_path: Path) -> tuple[grpc.Server, str]:
    """Start an in-process gRPC server on a random TCP port; return (server, target)."""
    from aprilcam.config import Config

    sock_dir = tmp_path / "s"
    data_dir = tmp_path / "d"
    sock_dir.mkdir()
    data_dir.mkdir()

    config = Config(
        data_dir=data_dir,
        socket_dir=sock_dir,
        daemon_pidfile=sock_dir / "aprilcamd.pid",
    )

    cameras: dict = {}
    cam_lock = threading.Lock()
    shutdown = threading.Event()

    servicer = AprilCamServicer(
        cameras=cameras,
        cam_lock=cam_lock,
        config=config,
        shutdown_event=shutdown,
    )
    server = make_grpc_server([], servicer)
    port = server.add_insecure_port("localhost:0")
    server.start()
    return server, f"localhost:{port}"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDaemonControlConnect:
    """DaemonControl connection and context-manager behaviour."""

    def test_context_manager_cleans_up(self, tmp_path):
        server, target = _make_server(tmp_path)
        try:
            host, port_str = target.rsplit(":", 1)
            with DaemonControl(host=host, port=int(port_str)) as dc:
                assert dc._stub is not None
            # After __exit__, channel should be closed
            assert dc._channel is None
        finally:
            server.stop(grace=0)

    def test_connect_idempotent(self, tmp_path):
        server, target = _make_server(tmp_path)
        try:
            host, port_str = target.rsplit(":", 1)
            dc = DaemonControl(host=host, port=int(port_str))
            dc.connect()
            stub1 = dc._stub
            dc.connect()
            assert dc._stub is stub1  # same stub — no reconnect
            dc.close()
        finally:
            server.stop(grace=0)

    def test_stub_or_raise_before_connect(self):
        dc = DaemonControl()
        with pytest.raises(RuntimeError, match="not connected"):
            dc._stub_or_raise()


class TestDaemonControlListCameras:
    """list_cameras() returns an empty list when no cameras are open."""

    def test_list_cameras_empty(self, tmp_path):
        server, target = _make_server(tmp_path)
        try:
            host, port_str = target.rsplit(":", 1)
            with DaemonControl(host=host, port=int(port_str)) as dc:
                cameras = dc.list_cameras()
            assert cameras == []
        finally:
            server.stop(grace=0)

    def test_list_cameras_returns_list_of_str(self, tmp_path):
        server, target = _make_server(tmp_path)
        try:
            host, port_str = target.rsplit(":", 1)
            with DaemonControl(host=host, port=int(port_str)) as dc:
                result = dc.list_cameras()
            assert isinstance(result, list)
            for item in result:
                assert isinstance(item, str)
        finally:
            server.stop(grace=0)


class TestDaemonControlConnectDefault:
    """connect_default() connects to an already-running daemon without spawning."""

    @pytest.fixture()
    def short_tmp(self, tmp_path):
        """Return a path short enough for Unix domain sockets (≤ 103 chars)."""
        import tempfile
        import os
        # Use /tmp directly with a short prefix to stay under the 104-char limit
        d = tempfile.mkdtemp(prefix="ac_", dir="/tmp")
        yield Path(d)
        # Cleanup
        import shutil
        shutil.rmtree(d, ignore_errors=True)

    def test_connect_default_reaches_running_daemon_via_unix(self, short_tmp):
        """connect_default() succeeds when the daemon is already running on a Unix socket."""
        from aprilcam.config import Config

        sock_dir = short_tmp / "s"
        data_dir = short_tmp / "d"
        sock_dir.mkdir()
        data_dir.mkdir()

        unix_sock_path = sock_dir / "ctrl.sock"

        config = Config(
            data_dir=data_dir,
            socket_dir=sock_dir,
            daemon_pidfile=sock_dir / "aprilcamd.pid",
        )

        cameras: dict = {}
        cam_lock = threading.Lock()
        shutdown = threading.Event()

        from aprilcam.daemon.grpc_server import AprilCamServicer, make_grpc_server

        servicer = AprilCamServicer(
            cameras=cameras,
            cam_lock=cam_lock,
            config=config,
            shutdown_event=shutdown,
        )
        server = make_grpc_server([], servicer)
        server.add_insecure_port(f"unix:{unix_sock_path}")
        server.start()

        # Give the server a moment to bind
        time.sleep(0.05)

        try:
            dc = DaemonControl.connect_default(
                config, unix_path=str(unix_sock_path)
            )
            cameras_list = dc.list_cameras()
            assert cameras_list == []
            dc.close()
        finally:
            server.stop(grace=0)

    def test_connect_default_returns_daemon_control_instance(self, short_tmp):
        """connect_default() returns a DaemonControl instance."""
        from aprilcam.config import Config

        sock_dir = short_tmp / "s"
        data_dir = short_tmp / "d"
        sock_dir.mkdir()
        data_dir.mkdir()

        unix_sock_path = sock_dir / "ctrl.sock"

        config = Config(
            data_dir=data_dir,
            socket_dir=sock_dir,
            daemon_pidfile=sock_dir / "aprilcamd.pid",
        )

        cameras: dict = {}
        cam_lock = threading.Lock()
        shutdown = threading.Event()

        from aprilcam.daemon.grpc_server import AprilCamServicer, make_grpc_server

        servicer = AprilCamServicer(
            cameras=cameras,
            cam_lock=cam_lock,
            config=config,
            shutdown_event=shutdown,
        )
        server = make_grpc_server([], servicer)
        server.add_insecure_port(f"unix:{unix_sock_path}")
        server.start()
        time.sleep(0.05)

        try:
            dc = DaemonControl.connect_default(
                config, unix_path=str(unix_sock_path)
            )
            assert isinstance(dc, DaemonControl)
            assert dc._channel is not None
            dc.close()
        finally:
            server.stop(grace=0)


class TestDaemonControlGetTag:
    """get_tag() selects a single tag by id from the latest frame."""

    def _dc_with_tag_ids(self, ids):
        from aprilcam.proto import aprilcam_pb2

        dc = DaemonControl(host="localhost", port=1)
        resp = aprilcam_pb2.TagFrameResponse(
            frame_id=7,
            tags=[aprilcam_pb2.TagMsg(id=i) for i in ids],
        )
        dc._stub = MagicMock()
        dc._stub.GetTags.return_value = resp
        return dc

    def test_get_tag_found(self):
        dc = self._dc_with_tag_ids([1, 2, 3])
        tag = dc.get_tag("cam-0", 2)
        assert tag is not None and tag.id == 2

    def test_get_tag_missing_returns_none(self):
        dc = self._dc_with_tag_ids([1, 2])
        assert dc.get_tag("cam-0", 99) is None

    def test_get_tag_empty_frame_returns_none(self):
        dc = self._dc_with_tag_ids([])
        assert dc.get_tag("cam-0", 1) is None
