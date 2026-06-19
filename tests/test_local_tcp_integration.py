"""Local TCP golden-path integration test — no camera hardware required.

This test starts the AprilCam daemon in-process with TCP-only transport on a
free localhost port, connects a DaemonControl client over TCP, and exercises
the golden path for camera-less RPCs:

    EnumerateCameras → ListCameras → ListPlayfields

All three return empty lists when no camera hardware is present — the test
asserts the RPCs complete without exception and return the expected types.

Coverage:
  - TCP transport bind and gRPC server startup.
  - DaemonControl connection over TCP (no Unix socket).
  - EnumerateCameras, ListCameras, ListPlayfields RPCs.
  - Graceful daemon shutdown.

Out of scope (requires hardware):
  - OpenCamera, CaptureFrame, GetTags, streaming.

Marker: ``pytest.mark.integration`` — run with ``uv run pytest -m integration``.
"""

from __future__ import annotations

import socket
import threading
from pathlib import Path

import pytest

pytest.importorskip("aprilcam.daemon.grpc_server", reason="requires aprilcam[daemon]")


def _free_tcp_port() -> int:
    """Return a free TCP port on 127.0.0.1 (immediately released after probing)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.integration
def test_local_tcp_daemon_golden_path(tmp_path: Path) -> None:
    """Connect DaemonControl over TCP; call camera-less RPCs; assert no exception.

    The daemon runs in-process on a free TCP port (no Unix socket) so the
    test works in any environment, including headless CI.  No camera hardware
    is required — EnumerateCameras probes the host and returns whatever is
    present (possibly an empty list); the test only asserts the return types.

    Teardown is deterministic: the shutdown event is set before joining the
    daemon thread, and the gRPC server is stopped with a short grace period.
    """
    from aprilcam.config import Config
    from aprilcam.daemon.server import DaemonServer
    from aprilcam.client.control import DaemonControl

    # Build a config that uses tmp directories so the test is isolated.
    sock_dir = tmp_path / "s"
    data_dir = tmp_path / "d"
    sock_dir.mkdir()
    data_dir.mkdir()

    cfg = Config(
        data_dir=data_dir,
        socket_dir=sock_dir,
        daemon_pidfile=sock_dir / "aprilcamd.pid",
        log_level="WARNING",
    )

    # Find a free port and start the daemon TCP-only on that port.
    port = _free_tcp_port()

    daemon = DaemonServer(
        cfg,
        unix_enabled=False,
        tcp_enabled=True,
        tcp_port=port,
    )

    daemon_thread = threading.Thread(target=daemon.run, daemon=True, name="test-daemon")
    daemon_thread.start()

    # Wait for the gRPC server to report readiness (up to 10 s).
    assert daemon.started_event.wait(timeout=10.0), (
        "DaemonServer did not set started_event within 10 s"
    )

    try:
        # Connect a DaemonControl client over TCP.
        with DaemonControl(host="127.0.0.1", port=port) as dc:
            # EnumerateCameras probes host hardware — returns a list (possibly empty).
            cameras = dc.enumerate_cameras()
            assert isinstance(cameras, list), (
                f"enumerate_cameras() should return list, got {type(cameras)}"
            )

            # ListCameras returns names of open cameras — none open, so empty list.
            open_cams = dc.list_cameras()
            assert isinstance(open_cams, list), (
                f"list_cameras() should return list, got {type(open_cams)}"
            )
            assert open_cams == [], (
                f"No cameras opened, expected empty list, got {open_cams!r}"
            )

            # ListPlayfields returns playfield entries from the data dir.
            pf_response = dc.list_playfields()
            # list_playfields() returns a proto ListPlayfieldsResponse.
            # The repeated field 'playfields' should be iterable.
            playfields = list(pf_response.playfields)
            assert isinstance(playfields, list), (
                f"list_playfields().playfields should be list, got {type(playfields)}"
            )
            # No playfields seeded → empty.
            assert playfields == [], (
                f"No playfields seeded, expected empty list, got {playfields!r}"
            )

    finally:
        # Trigger graceful shutdown and wait for the thread to exit.
        daemon._shutdown_event.set()
        daemon_thread.join(timeout=10.0)
        assert not daemon_thread.is_alive(), (
            "Daemon thread did not exit within 10 s after shutdown"
        )
