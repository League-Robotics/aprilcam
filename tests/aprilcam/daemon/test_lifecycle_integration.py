"""Full daemon lifecycle against real hardware on this Mac (ticket
020-004's ``@pytest.mark.camera`` acceptance pass).

Exercises exactly what ``python -m aprilcam.daemon`` would do --
``daemon.__main__.build()`` assembles the identical stack a real daemon
process wires together -- against a ``tmp_path``/short-``/tmp`` state
and control-plane root instead of the real system directories, so this
never collides with (or requires) a genuinely running daemon on the
machine executing the suite.

Skips cleanly (whole module, or the individual test below that needs a
specific playfield-linked camera) if no usable camera is attached at all,
matching every other ``@pytest.mark.camera`` module's convention
(``tests/aprilcam/camera/test_discovery.py``, ``test_camera.py``).
"""

from __future__ import annotations

import json
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
from tests.aprilcam.hardware_camera import resolve_hardware_camera_slug

pytestmark = pytest.mark.camera

#: Resolved lazily, once per test, by :func:`_skip_if_arducam_not_attached`
#: against this machine's real, persisted system state (see
#: tests/aprilcam/hardware_camera.py) -- never hardcoded.
ARDUCAM_SLUG: str = ""

#: The real, checked-in playfield definition (same file
#: ``test_calibration_hardware.py``/``test_detection_hardware.py`` name
#: as ground truth).
MAIN_PLAYFIELD_JSON = (
    Path(__file__).resolve().parents[3] / "config" / "aprilcam-old" / "playfields" / "main-playfield.json"
)


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


def _skip_if_camera_open_rpc_error(exc: grpc.RpcError) -> None:
    """``_skip_if_no_cameras_attached``/``_skip_if_arducam_not_attached``
    (below) only prove a camera is enumerated or configured/calibrated
    on this machine's real, persisted state -- never that this process
    can actually open it right now (tests/aprilcam/hardware_camera.py's
    own "enumeration is not availability" contract; docs/knowledge/
    daemon-camera-grant-and-detection-priming.md). These tests drive the
    daemon through a raw gRPC stub (not the typed ``GrpcDaemon`` client),
    so a camera-open failure on the daemon side surfaces here as a bare
    ``grpc.RpcError`` rather than ``aprilcam.errors.AprilCamError``. Only
    the specific, known "failed to open camera" signature is treated as
    environmental and re-raised as a skip; anything else is re-raised
    unchanged so a genuine bug still fails loudly."""
    details = exc.details() or ""
    if "failed to open camera" not in details:
        raise exc
    pytest.skip(
        f"a real camera qualifies on this machine but could not be opened "
        f"just now -- {details}. Expected when this process has no camera "
        f"TCC grant (e.g. an agent session) or the device is genuinely "
        f"busy elsewhere; re-run from an operator Terminal session to "
        f"exercise this test for real."
    )


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
        try:
            frame_response = stub.GetFrame(aprilcam_v2_pb2.GetFrameRequest(camera=some_camera))
        except grpc.RpcError as exc:
            _skip_if_camera_open_rpc_error(exc)
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


# ---------------------------------------------------------------------------
# UC-01.S-4 end to end (ticket 023-003): a playfield + camera config
# installed directly into config_dir -- never pushed over RPC -- comes
# back linked and ready on every fresh start, closing the restart trap
# sprint 022 hit live.
# ---------------------------------------------------------------------------


def _skip_if_arducam_not_attached() -> None:
    global ARDUCAM_SLUG
    ARDUCAM_SLUG = resolve_hardware_camera_slug()


def test_boot_loaded_playfield_and_camera_config_need_no_manual_push(tmp_path, short_tmp_dir):
    """UC-01.S-4, closed for real: ``main-playfield.json`` plus a camera
    config linking a real, discovered camera (see
    ``tests/aprilcam/hardware_camera.py``) to it are installed
    directly into ``config_dir`` -- never pushed over RPC anywhere in
    this test -- before the daemon ever starts. Calibrating with zero
    ``SetPlayfield``/``SetCameraConfig`` calls proves the playfield link
    came from disk alone (``Calibrator.calibrate`` raises
    ``NoPlayfieldLinked`` before ever touching the camera backend if it
    did not -- see ``calibration.calibration``'s module docstring), and a
    restart preserves both the calibration (file-backed, unchanged from
    before this ticket) and the playfield link (boot-loaded from
    ``config_dir``, this ticket's fix) -- a deskewed frame request
    succeeds immediately after the restart with no push at all."""
    _skip_if_arducam_not_attached()

    config_dir = tmp_path / "config"
    (config_dir / "playfields").mkdir(parents=True)
    (config_dir / "cameras" / ARDUCAM_SLUG).mkdir(parents=True)
    shutil.copyfile(MAIN_PLAYFIELD_JSON, config_dir / "playfields" / "main-playfield.json")
    (config_dir / "cameras" / ARDUCAM_SLUG / "config.json").write_text(
        json.dumps({"name": ARDUCAM_SLUG, "playfield": "main-playfield", "settings": {}}),
        encoding="utf-8",
    )

    state_dir = tmp_path / "state"
    lock_path = short_tmp_dir / "daemon.lock"
    socket_path = short_tmp_dir / "control.sock"
    directories = Directories(config_dir=config_dir, state_dir=state_dir)

    lifecycle = daemon_main.build(directories, lock_path=lock_path, socket_path=socket_path)
    lifecycle.start()
    try:
        stub = _stub_for(socket_path)

        # No SetPlayfield/SetCameraConfig RPC anywhere above or here --
        # if the boot-time load did not run, this raises NoPlayfieldLinked.
        try:
            calibrate_response = stub.Calibrate(aprilcam_v2_pb2.CalibrateRequest(camera=ARDUCAM_SLUG))
        except grpc.RpcError as exc:
            _skip_if_camera_open_rpc_error(exc)
        assert calibrate_response.result.succeeded is True, (
            "calibrate failed with config loaded from disk only, no "
            f"manual push -- matched {list(calibrate_response.result.matched_ids)}, "
            f"missing {list(calibrate_response.result.missing_ids)}, "
            f"reason={calibrate_response.result.reason!r}. Check the arducam is "
            "physically aimed at main-playfield."
        )
    finally:
        lifecycle.stop()

    # -- restart: fresh process-equivalent stack, still nothing pushed --
    restarted = daemon_main.build(directories, lock_path=lock_path, socket_path=socket_path)
    restarted.start()
    try:
        stub = _stub_for(socket_path)
        frame_response = stub.GetFrame(
            aprilcam_v2_pb2.GetFrameRequest(camera=ARDUCAM_SLUG, deskewed=True)
        )
        assert frame_response.deskewed is True
        assert len(frame_response.data) > 0
    finally:
        restarted.stop()
