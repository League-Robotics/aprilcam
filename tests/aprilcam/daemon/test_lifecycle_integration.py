"""Full daemon lifecycle against real hardware on this Mac (ticket
020-004's ``@pytest.mark.camera`` acceptance pass).

Exercises exactly what ``python -m aprilcam.daemon`` would do --
``daemon.__main__.build()`` assembles the identical stack a real daemon
process wires together -- against a ``tmp_path``/short-``/tmp`` state
and control-plane root instead of the real system directories, so this
never collides with (or requires) a genuinely running daemon on the
machine executing the suite.

Skips cleanly (whole module) if neither of this project's two fixture
cameras (``arducam-ov9782-usb-camera``, ``hd-usb-camera``) is attached,
matching every other ``@pytest.mark.camera`` module's convention
(``tests/aprilcam/camera/test_discovery.py``, ``test_camera.py``).
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import grpc
import pytest

from aprilcam.camera.identity import resolve_all
from aprilcam.config.directories import Directories
from aprilcam.daemon import __main__ as daemon_main
from aprilcam.errors import AlreadyRunning
from aprilcam.proto import aprilcam_v2_pb2, aprilcam_v2_pb2_grpc

pytestmark = pytest.mark.camera


@pytest.fixture
def short_tmp_dir():
    """A short, top-level ``/tmp`` directory for the lock/socket pair --
    pytest's ``tmp_path`` nests deeply enough (test name and all) to
    routinely exceed unix domain sockets' ~103-character path limit on
    macOS/Linux (see ``test_service.py``'s ``stub`` fixture for the
    same workaround)."""
    d = Path(tempfile.mkdtemp(prefix="ac-int-", dir="/tmp"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _skip_if_no_cameras_attached():
    if not resolve_all():
        pytest.skip("no cameras enumerated on this machine")


def _stub_for(socket_path: Path) -> aprilcam_v2_pb2_grpc.AprilCamV2Stub:
    channel = grpc.insecure_channel(f"unix:{socket_path}")
    grpc.channel_ready_future(channel).result(timeout=5)
    return aprilcam_v2_pb2_grpc.AprilCamV2Stub(channel)


def test_start_stop_and_restart_stability_against_real_cameras(tmp_path, short_tmp_dir):
    _skip_if_no_cameras_attached()

    state_dir = tmp_path / "state"
    lock_path = short_tmp_dir / "daemon.lock"
    socket_path = short_tmp_dir / "control.sock"
    directories = Directories(config_dir=tmp_path / "config", state_dir=state_dir)

    # -- first start: enumerates whatever is attached -----------------
    lifecycle = daemon_main.build(
        directories, lock_path=lock_path, socket_path=socket_path
    )
    lifecycle.start()
    try:
        status = lifecycle.status()
        assert status.running is True
        assert len(status.cameras) >= 1  # at least one Mac fixture camera
        first_numbers = {c.name: c.number for c in status.cameras}
        assert all(n is not None for n in first_numbers.values())

        # -- a second instance is refused, naming the holder, and does
        #    not disturb the first (UC-01.S-3) --------------------------
        contender = daemon_main.build(
            Directories(config_dir=tmp_path / "config2", state_dir=tmp_path / "state2"),
            lock_path=lock_path,
            socket_path=short_tmp_dir / "control2.sock",
        )
        with pytest.raises(AlreadyRunning):
            contender.start()
        assert lifecycle.status().running is True

        # -- the RPC surface answers for real hardware (UC-14, UC-17) --
        stub = _stub_for(socket_path)
        list_response = stub.ListCameras(aprilcam_v2_pb2.ListCamerasRequest())
        assert {c.name for c in list_response.cameras} == set(first_numbers.keys())

        probe_response = stub.ProbeCameras(aprilcam_v2_pb2.ProbeCamerasRequest())
        assert {c.name for c in probe_response.cameras} == set(first_numbers.keys())

        some_camera = next(iter(first_numbers.keys()))
        frame_response = stub.GetFrame(aprilcam_v2_pb2.GetFrameRequest(camera=some_camera))
        assert len(frame_response.data) > 0
        assert frame_response.width > 0 and frame_response.height > 0
    finally:
        lifecycle.stop()

    # -- restart: a fresh process-equivalent stack reads the same
    #    registry.json and must report the same enumeration numbers
    #    (UC-01.S-1's restart-stability check, through the full
    #    lifecycle rather than ticket 002's unit-level check) ----------
    restarted = daemon_main.build(
        Directories(config_dir=tmp_path / "config", state_dir=state_dir),
        lock_path=lock_path,
        socket_path=socket_path,
    )
    restarted.start()
    try:
        second_numbers = {c.name: c.number for c in restarted.status().cameras}
        assert second_numbers == first_numbers
    finally:
        restarted.stop()
