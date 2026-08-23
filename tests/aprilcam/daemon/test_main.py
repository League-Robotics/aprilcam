"""Tests for ``aprilcam.daemon.__main__`` (ticket 023-003): boot-time
config load from disk, wired through the real assembly ``build()``
performs.

Fully headless -- ``start_watch=False`` skips both halves of camera
enumeration entirely (``daemon.__main__.build``'s own docstring), so
these tests never touch real camera hardware or gRPC-server anything
beyond the real unix-socket control plane itself. A short, top-level
``/tmp`` directory stands in for the lock/socket pair, matching every
other module's identically-named workaround for unix domain sockets'
~103-character path limit (``test_lifecycle_integration.py``'s
``short_tmp_dir`` fixture, duplicated here rather than shared, per this
project's established per-module fixture convention -- see
``test_calibration_hardware.py``'s ``cli_env`` docstring for the same
stated precedent).
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
from pathlib import Path

import grpc
import pytest

from aprilcam.config.directories import Directories
from aprilcam.daemon import __main__ as daemon_main
from aprilcam.proto import aprilcam_v2_pb2, aprilcam_v2_pb2_grpc


@pytest.fixture
def short_tmp_dir():
    d = Path(tempfile.mkdtemp(prefix="ac-main-", dir="/tmp"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _stub_for(socket_path: Path) -> aprilcam_v2_pb2_grpc.AprilCamV2Stub:
    channel = grpc.insecure_channel(f"unix:{socket_path}")
    grpc.channel_ready_future(channel).result(timeout=5)
    return aprilcam_v2_pb2_grpc.AprilCamV2Stub(channel)


def _write_playfield(config_dir: Path, name: str) -> None:
    (config_dir / "playfields").mkdir(parents=True, exist_ok=True)
    (config_dir / "playfields" / f"{name}.json").write_text(
        json.dumps(
            {
                "name": name,
                "playfield": {"width_cm": 100.0, "height_cm": 80.0},
                "aruco_tags": [{"slug": "u1", "id": 1, "x": 0, "y": 0}],
            }
        ),
        encoding="utf-8",
    )


def _write_camera_config(config_dir: Path, slug: str, *, playfield: str, settings: dict) -> None:
    (config_dir / "cameras" / slug).mkdir(parents=True, exist_ok=True)
    (config_dir / "cameras" / slug / "config.json").write_text(
        json.dumps({"name": slug, "playfield": playfield, "settings": settings}),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# AC2/UC-01.S-4: build() loads config_dir into the service, no RPC push
# ---------------------------------------------------------------------------


def test_build_loads_config_dir_into_service_with_zero_rpc_pushes(tmp_path, short_tmp_dir):
    """SUC-001's defining claim, at the ``build()`` level: fetching a
    playfield/camera config immediately after ``start()`` returns
    exactly what was installed on disk -- this test never calls
    ``SetPlayfield``/``SetCameraConfig`` at all."""
    config_dir = tmp_path / "config"
    _write_playfield(config_dir, "main-playfield")
    _write_camera_config(
        config_dir,
        "arducam-ov9782-usb-camera",
        playfield="main-playfield",
        settings={"gain": 3},
    )

    directories = Directories(config_dir=config_dir, state_dir=tmp_path / "state")
    lifecycle = daemon_main.build(
        directories,
        lock_path=short_tmp_dir / "daemon.lock",
        socket_path=short_tmp_dir / "control.sock",
        start_watch=False,
    )
    lifecycle.start()
    try:
        stub = _stub_for(lifecycle.socket_path)

        playfield_response = stub.GetPlayfield(
            aprilcam_v2_pb2.GetPlayfieldRequest(name="main-playfield")
        )
        assert playfield_response.playfield.name == "main-playfield"
        assert playfield_response.playfield.definition.width_cm == 100.0

        config_response = stub.GetCameraConfig(
            aprilcam_v2_pb2.GetCameraConfigRequest(camera="arducam-ov9782-usb-camera")
        )
        assert config_response.config.playfield == "main-playfield"
        assert json.loads(config_response.config.settings_json) == {"gain": 3}
    finally:
        lifecycle.stop()


def test_build_with_no_config_dir_starts_with_nothing_loaded(tmp_path, short_tmp_dir):
    """UC-01.S-2's empty-start case still holds with the boot-load step
    wired in: no ``config_dir`` contents at all is not an error, and
    fetching an unconfigured playfield still reports ``UnknownPlayfield``
    exactly as before this ticket."""
    directories = Directories(config_dir=tmp_path / "config", state_dir=tmp_path / "state")
    lifecycle = daemon_main.build(
        directories,
        lock_path=short_tmp_dir / "daemon.lock",
        socket_path=short_tmp_dir / "control.sock",
        start_watch=False,
    )
    lifecycle.start()
    try:
        stub = _stub_for(lifecycle.socket_path)
        with pytest.raises(grpc.RpcError) as excinfo:
            stub.GetPlayfield(aprilcam_v2_pb2.GetPlayfieldRequest(name="main-playfield"))
        assert excinfo.value.code() == grpc.StatusCode.NOT_FOUND
    finally:
        lifecycle.stop()


# ---------------------------------------------------------------------------
# AC3: a malformed file among valid ones does not prevent the daemon
# from starting, and every other file still loads.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# ticket 028-003: tcp_port threaded through to build_server, and a real
# MdnsAdvertiser wired into DaemonLifecycle's advertise/stop_advertise
# hooks. A fake stands in for MdnsAdvertiser here (monkeypatched at the
# module level daemon_main.build() actually calls it from) so this
# test never opens a real multicast socket -- test_mdns.py covers
# MdnsAdvertiser's own zeroconf-facing behavior with its own injected
# fakes, and this test is only about build()'s wiring.
# ---------------------------------------------------------------------------


class _FakeAdvertiser:
    """Records ``start``/``stop`` calls; constructed with no arguments,
    matching ``daemon.mdns.MdnsAdvertiser``'s real constructor."""

    instances: list = []

    def __init__(self) -> None:
        self.calls: list = []
        _FakeAdvertiser.instances.append(self)

    def start(self, tcp_port, *, hostname=None):
        self.calls.append(("start", tcp_port, hostname))

    def stop(self):
        self.calls.append(("stop",))


def test_build_wires_a_bound_tcp_port_into_the_mdns_advertise_hook(
    tmp_path, short_tmp_dir, monkeypatch
):
    """AC: build()'s tcp_port parameter flows through to build_server
    (observable via a real TCP client connecting), and
    DaemonLifecycle's advertise/stop_advertise hooks are wired to a
    real MdnsAdvertiser instance's start/stop methods."""
    _FakeAdvertiser.instances = []
    monkeypatch.setattr(daemon_main, "MdnsAdvertiser", _FakeAdvertiser)

    directories = Directories(config_dir=tmp_path / "config", state_dir=tmp_path / "state")
    lifecycle = daemon_main.build(
        directories,
        lock_path=short_tmp_dir / "daemon.lock",
        socket_path=short_tmp_dir / "control.sock",
        start_watch=False,
        tcp_port=0,
    )
    lifecycle.start()
    try:
        assert len(_FakeAdvertiser.instances) == 1
        advertiser = _FakeAdvertiser.instances[0]
        assert len(advertiser.calls) == 1
        assert advertiser.calls[0][0] == "start"
        bound_port = advertiser.calls[0][1]
        assert isinstance(bound_port, int) and bound_port != 0

        channel = grpc.insecure_channel(f"127.0.0.1:{bound_port}")
        try:
            grpc.channel_ready_future(channel).result(timeout=5)
        finally:
            channel.close()
    finally:
        lifecycle.stop()

    assert advertiser.calls[-1] == ("stop",)


def test_build_with_tcp_port_none_never_calls_advertise(tmp_path, short_tmp_dir, monkeypatch):
    """AC: passing tcp_port=None disables the TCP bind entirely, and
    DaemonLifecycle.start() -- which only calls advertise when a real
    port was bound -- never invokes the (fake) MdnsAdvertiser at all."""
    _FakeAdvertiser.instances = []
    monkeypatch.setattr(daemon_main, "MdnsAdvertiser", _FakeAdvertiser)

    directories = Directories(config_dir=tmp_path / "config", state_dir=tmp_path / "state")
    lifecycle = daemon_main.build(
        directories,
        lock_path=short_tmp_dir / "daemon.lock",
        socket_path=short_tmp_dir / "control.sock",
        start_watch=False,
        tcp_port=None,
    )
    lifecycle.start()
    try:
        advertiser = _FakeAdvertiser.instances[0]
        assert advertiser.calls == []
    finally:
        lifecycle.stop()


def test_build_with_a_malformed_file_among_valid_ones_still_starts_and_loads_the_rest(
    tmp_path, short_tmp_dir, caplog
):
    config_dir = tmp_path / "config"
    _write_playfield(config_dir, "good-field")
    (config_dir / "playfields" / "broken.json").write_text("{not valid json", encoding="utf-8")
    _write_camera_config(config_dir, "hd-usb-camera", playfield="good-field", settings={})

    directories = Directories(config_dir=config_dir, state_dir=tmp_path / "state")

    with caplog.at_level(logging.WARNING, logger="aprilcam.daemon.__main__"):
        lifecycle = daemon_main.build(
            directories,
            lock_path=short_tmp_dir / "daemon.lock",
            socket_path=short_tmp_dir / "control.sock",
            start_watch=False,
        )
        lifecycle.start()
    try:
        assert lifecycle.status().running is True

        stub = _stub_for(lifecycle.socket_path)
        good_response = stub.GetPlayfield(aprilcam_v2_pb2.GetPlayfieldRequest(name="good-field"))
        assert good_response.playfield.name == "good-field"

        config_response = stub.GetCameraConfig(
            aprilcam_v2_pb2.GetCameraConfigRequest(camera="hd-usb-camera")
        )
        assert config_response.config.playfield == "good-field"
    finally:
        lifecycle.stop()

    assert any("broken.json" in record.message for record in caplog.records)
