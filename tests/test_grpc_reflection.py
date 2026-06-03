"""Verifies that gRPC Server Reflection is active and exposes the AprilCam service.

Starts a real in-process gRPC server (same pattern as test_daemon_control.py),
then uses the gRPC reflection client to enumerate services.  The test asserts
that "aprilcam.AprilCam" appears in the returned service list.
"""

from __future__ import annotations

import threading
from pathlib import Path

import grpc
import pytest

pytest.importorskip("aprilcam.daemon.grpc_server", reason="requires aprilcam[daemon]")

from grpc_reflection.v1alpha.reflection_pb2_grpc import ServerReflectionStub
from grpc_reflection.v1alpha import reflection_pb2

from aprilcam.config import Config
from aprilcam.daemon.grpc_server import AprilCamServicer, make_grpc_server


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _start_server(tmp_path: Path) -> tuple[grpc.Server, str]:
    """Start an in-process gRPC server on a random TCP port; return (server, target)."""
    sock_dir = tmp_path / "s"
    data_dir = tmp_path / "d"
    sock_dir.mkdir()
    data_dir.mkdir()

    config = Config(
        data_dir=data_dir,
        socket_dir=sock_dir,
        daemon_pidfile=sock_dir / "aprilcamd.pid",
    )

    servicer = AprilCamServicer(
        cameras={},
        cam_lock=threading.Lock(),
        config=config,
        shutdown_event=threading.Event(),
    )
    server = make_grpc_server([], servicer)
    port = server.add_insecure_port("localhost:0")
    server.start()
    return server, f"localhost:{port}"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_reflection_service_lists_aprilcam(tmp_path: Path) -> None:
    """gRPC reflection returns 'aprilcam.AprilCam' in the service list."""
    server, target = _start_server(tmp_path)
    try:
        channel = grpc.insecure_channel(target)
        stub = ServerReflectionStub(channel)

        # The reflection API is a bidirectional streaming RPC.
        # Send one ListServices request and collect the response.
        request = reflection_pb2.ServerReflectionRequest(list_services="")
        responses = list(stub.ServerReflectionInfo(iter([request])))

        service_names: list[str] = []
        for resp in responses:
            for svc in resp.list_services_response.service:
                service_names.append(svc.name)

        assert "aprilcam.AprilCam" in service_names, (
            f"Expected 'aprilcam.AprilCam' in reflection service list; got: {service_names}"
        )

        channel.close()
    finally:
        server.stop(grace=0)
