"""CLI-level acceptance tests for ticket 020-006, exercising the real
``aprilcam`` entry point (not fakes) against a real local daemon.

Two tiers:

* ``test_daemon_and_camera_round_trip_against_real_hardware`` --
  ``@pytest.mark.camera``. Spawns the real ``aprilcam`` console script
  (the installed ``[project.scripts]`` entry point, not
  ``python -m aprilcam.cli``) via ``subprocess`` against a real local
  daemon with this project's two fixture cameras attached
  (``arducam-ov9782-usb-camera``, ``hd-usb-camera``), covering
  UC-01.S-1, UC-13.S-3/S-4, UC-14.S-1, UC-25.S-1/S-2, UC-17.S-1/S-2/S-3
  end to end. Skips cleanly if neither fixture camera is attached,
  matching every other ``@pytest.mark.camera`` module's convention.

* ``test_camera_list_all_fans_out_across_two_real_daemons_and_one_dead_target``
  -- ``@pytest.mark.integration``, no camera hardware needed. Covers
  UC-14.S-3/S-4's fan-out contract against two genuinely running (in
  -process) daemon gRPC servers plus one deliberately unreachable
  target. See that test's docstring for why this cannot be a
  *subprocess*-level ``camera list --all`` test this sprint: Discovery
  .find_all() is stubbed to a single local socket (sprint architecture
  Decision 2) and no daemon opens a TCP listener this sprint
  (config/directories.py's Open Question 2 resolution), so a second
  real daemon has no address the standard ``Discovery`` can dial at
  all -- only a test-local ``Discovery``-shaped object that knows both
  sockets directly, as this test supplies, can fan out across two real
  daemons before that lands.

Startup-race note (read before touching the round-trip test's
ordering): the client-side readiness signal
(``client.discovery.probe_socket_reachable`` -- bare gRPC channel
connectivity) can and does return before the daemon's *own* initial
synchronous camera probe (``daemon/__main__.py``'s
``enumerate_then_watch``) finishes enumerating real hardware, which
this Mac's AVFoundation backend measurably takes more than an instant
for. A ``camera list`` run immediately after ``daemon start`` can
therefore legitimately observe an empty or partial registry -- this is
an existing gap in the already-implemented readiness signal (tickets
020-004/020-005), not something ticket 020-006 owns fixing. This test
sidesteps it exactly the way the product itself recommends (UC-25):
run ``camera probe`` -- whose RPC handler blocks until enumeration is
actually done -- before trusting anything ``camera list``/``daemon
status`` report.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, Optional, Tuple

import grpc
import pytest

from aprilcam.camera.identity import resolve_all


def _aprilcam_bin() -> Path:
    """The real console-script entry point for *this* interpreter's
    editable install -- ``Path(sys.executable).parent / "aprilcam"`` --
    rather than whatever ``aprilcam`` happens to resolve to on $PATH
    (which may be an unrelated pipx install)."""
    candidate = Path(sys.executable).parent / "aprilcam"
    if not candidate.exists():  # pragma: no cover - environment-dependent
        pytest.skip(f"no aprilcam console script next to {sys.executable}")
    return candidate


def _run(bin_path: Path, *args: str, env: Dict[str, str], timeout: float = 30.0):
    return subprocess.run(
        [str(bin_path), *args], capture_output=True, env=env, timeout=timeout
    )


# ---------------------------------------------------------------------------
# Real subprocess round trip -- @pytest.mark.camera
# ---------------------------------------------------------------------------


@pytest.fixture
def short_runtime_dir():
    """A short, top-level ``/tmp`` directory -- unix domain sockets have
    a ~103-character path limit on macOS/Linux, and pytest's
    ``tmp_path`` nests too deeply (see ``test_service.py``'s ``stub``
    fixture for the same workaround)."""
    d = Path(tempfile.mkdtemp(prefix="ac-cli-int-", dir="/tmp"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def isolated_env(tmp_path: Path, short_runtime_dir: Path, monkeypatch):
    """Environment that relocates ``config_dir``/``state_dir``/
    ``socket_dir`` away from the real system roots, inherited by every
    subprocess spawned below (``subprocess.run``'s default ``env``
    behaviour mirrors ``client/lifecycle.py``'s own). Skips (rather
    than silently talking to a real running daemon) if this sandbox's
    ``/run/aprilcam`` is itself available and writable, exactly like
    ``tests/aprilcam/client/test_lifecycle.py``'s real-subprocess round
    trip test.
    """
    from aprilcam.config.directories import Directories

    monkeypatch.setenv("APRILCAM_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("APRILCAM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(short_runtime_dir))

    directories = Directories()
    if directories.socket_dir != short_runtime_dir / "aprilcam":
        pytest.skip(
            "this sandbox's /run/aprilcam is available and writable, so "
            "socket_dir is not configurable down to a private tmp root -- "
            "cannot safely isolate a real-subprocess CLI test from a real "
            "daemon here"
        )
    return dict(os.environ)


def _skip_if_fixture_cameras_absent() -> None:
    slugs = {identity.slug for identity in resolve_all().values()}
    missing = {"arducam-ov9782-usb-camera", "hd-usb-camera"} - slugs
    if missing:
        pytest.skip(f"fixture camera(s) not attached to this machine: {sorted(missing)}")


@pytest.mark.camera
def test_daemon_and_camera_round_trip_against_real_hardware(tmp_path, isolated_env):
    _skip_if_fixture_cameras_absent()
    bin_path = _aprilcam_bin()
    env = isolated_env

    # -- UC-13.S-3: status before any daemon exists --------------------
    result = _run(bin_path, "daemon", "status", env=env)
    assert result.returncode == 0, result.stderr
    assert b"not running" in result.stdout

    # -- UC-13.S-4: a remote start refuses and starts nothing at all,
    #    checked *before* any local daemon exists so "nothing is
    #    started anywhere" is unambiguous -----------------------------
    result = _run(bin_path, "daemon", "start", "-d", "pi-field.local", env=env)
    assert result.returncode == 1
    assert b"SSH" in result.stderr
    assert b"local-only" in result.stderr
    result = _run(bin_path, "daemon", "status", env=env)
    assert b"not running" in result.stdout

    # -- UC-13.S-3 / UC-01.S-2: start succeeds with zero configuration -
    result = _run(bin_path, "daemon", "start", env=env)
    assert result.returncode == 0, result.stderr
    assert b"daemon started" in result.stdout

    try:
        # Force the registry to settle before trusting list/status --
        # see the module docstring's startup-race note. This is also
        # UC-25.S-1/S-2 in its own right: each fixture camera reported
        # independently, by its derived name.
        result = _run(bin_path, "camera", "probe", env=env)
        assert result.returncode == 0, result.stderr
        probe_out = result.stdout.decode()
        assert "arducam-ov9782-usb-camera" in probe_out
        assert "hd-usb-camera" in probe_out

        # -- UC-14.S-1: both fixture cameras, with enumeration numbers -
        result = _run(bin_path, "camera", "list", env=env)
        assert result.returncode == 0, result.stderr
        list_out = result.stdout.decode()
        assert "arducam-ov9782-usb-camera" in list_out
        assert "hd-usb-camera" in list_out
        assert "[1]" in list_out or "[2]" in list_out or "[3]" in list_out or "[4]" in list_out

        # -- UC-13.S-3: status now reports running with the camera list
        result = _run(bin_path, "daemon", "status", env=env)
        assert result.returncode == 0, result.stderr
        status_out = result.stdout.decode()
        assert "local: running" in status_out
        assert "arducam-ov9782-usb-camera" in status_out

        # -- UC-17.S-1: lossless PNG fixture file ----------------------
        png_path = tmp_path / "frame.png"
        result = _run(
            bin_path,
            "camera",
            "image",
            "arducam-ov9782-usb-camera",
            "-o",
            str(png_path),
            "--encoding",
            "png",
            env=env,
        )
        assert result.returncode == 0, result.stderr
        data = png_path.read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n"

        # -- UC-17.S-2: raw bytes to stdout, pipeable -------------------
        result = _run(bin_path, "camera", "image", "hd-usb-camera", env=env)
        assert result.returncode == 0, result.stderr
        assert result.stdout[:2] == b"\xff\xd8"  # JPEG SOI marker

        # -- UC-17.S-3: base64 over a text-only channel -----------------
        result = _run(bin_path, "camera", "image", "hd-usb-camera", "--base64", env=env)
        assert result.returncode == 0, result.stderr
        decoded = base64.b64decode(result.stdout.strip())
        assert decoded[:2] == b"\xff\xd8"

        # -- unknown camera surfaces the typed error, not a bare crash --
        result = _run(bin_path, "camera", "image", "no-such-camera", "--base64", env=env)
        assert result.returncode == 1
        assert b"no-such-camera" in result.stderr
        assert b"arducam-ov9782-usb-camera" in result.stderr
    finally:
        result = _run(bin_path, "daemon", "stop", env=env)
        assert result.returncode == 0, result.stderr
        assert b"daemon stopped" in result.stdout

    # -- UC-13.S-3: stop leaves the daemon cleanly not-running ----------
    result = _run(bin_path, "daemon", "status", env=env)
    assert result.returncode == 0, result.stderr
    assert b"not running" in result.stdout


# ---------------------------------------------------------------------------
# --all fan-out against two real (in-process) daemons -- @pytest.mark.integration
# ---------------------------------------------------------------------------


class _TwoRealDaemonDiscovery:
    """A ``client.interfaces.Discovery``-shaped stand-in backed by two
    genuinely running gRPC daemon servers plus one deliberately
    unreachable host -- see the module docstring for why this cannot
    be the *real* ``Discovery`` this sprint (Architecture Decision 2's
    local-only ``find_all()`` stub, plus no TCP listener existing at
    all yet). ``fan_out()`` (``cli/_common.py``) only needs
    ``find_all()``/``connect(host)`` structurally, so this is a drop-in
    ``discovery_factory`` for ``camera_cli.cmd_list`` exactly like the
    fakes in ``test_camera_cli.py`` -- the difference here is that
    every ``connect()`` call opens a real gRPC channel to a real
    server.
    """

    def __init__(self, infos: Tuple[object, ...], sockets: Dict[Optional[str], Optional[Path]]):
        self._infos = infos
        self._sockets = sockets

    def find_all(self) -> Tuple[object, ...]:
        return self._infos

    def connect(self, host: Optional[str] = None):
        from aprilcam.client.daemon_client import GrpcDaemon
        from aprilcam.errors import ConnectionFailed
        from aprilcam.proto import aprilcam_v2_pb2_grpc

        socket_path = self._sockets.get(host)
        if socket_path is None:
            raise ConnectionFailed(host or "localhost", reason="no daemon answered")
        channel = grpc.insecure_channel(f"unix:{socket_path}")
        try:
            grpc.channel_ready_future(channel).result(timeout=2.0)
        except grpc.FutureTimeoutError as exc:
            channel.close()
            raise ConnectionFailed(host or "localhost", reason="no daemon answered") from exc
        return GrpcDaemon(aprilcam_v2_pb2_grpc.AprilCamV2Stub(channel), target=host or "localhost")


class _Info:
    def __init__(self, host: str, is_local: bool) -> None:
        self.host = host
        self.is_local = is_local


def _bind_zero_camera_daemon(socket_dir: Path):
    """A real ``DaemonService`` over a real ``grpc.Server``, zero
    cameras (a valid, successful daemon per UC-01.S-2 -- no hardware
    needed for a fan-out test)."""
    from aprilcam.camera.discovery import CameraDiscovery
    from aprilcam.camera.registry import CameraRegistry
    from aprilcam.config.directories import Directories
    from aprilcam.daemon.rpc import build_server
    from aprilcam.daemon.service import DaemonService

    directories = Directories(config_dir=socket_dir / "config", state_dir=socket_dir / "state")
    registry = CameraRegistry(directories.state_dir / "cameras")
    discovery = CameraDiscovery(registry, identity_source=lambda: {})
    service = DaemonService(registry=registry, discovery=discovery, directories=directories, cameras={})
    socket_path = socket_dir / "control.sock"
    listener = build_server(service, socket_path)
    listener.start()
    return listener, socket_path


@pytest.mark.integration
def test_camera_list_all_fans_out_across_two_real_daemons_and_one_dead_target(capsys):
    from aprilcam.cli import camera as camera_cli

    dir_a = Path(tempfile.mkdtemp(prefix="ac-fanout-a-", dir="/tmp"))
    dir_b = Path(tempfile.mkdtemp(prefix="ac-fanout-b-", dir="/tmp"))
    listener_a = listener_b = None
    try:
        listener_a, socket_a = _bind_zero_camera_daemon(dir_a)
        listener_b, socket_b = _bind_zero_camera_daemon(dir_b)

        discovery = _TwoRealDaemonDiscovery(
            infos=(
                _Info(host="localhost", is_local=True),
                _Info(host="daemon-b", is_local=False),
                _Info(host="daemon-c-unreachable", is_local=False),
            ),
            sockets={None: socket_a, "daemon-b": socket_b, "daemon-c-unreachable": None},
        )

        import argparse

        args = argparse.Namespace(daemon=None, all=True)
        code = camera_cli.cmd_list(args, discovery_factory=lambda: discovery)

        assert code == 1  # one target unreachable -> visible in the exit code
        out, err = capsys.readouterr()
        assert "localhost (local):" in out
        assert "daemon-b:" in out
        assert "(no cameras)" in out  # both real daemons genuinely have zero cameras
        assert "daemon-c-unreachable: unreachable" in err
        assert "no daemon answered" in err
    finally:
        if listener_a is not None:
            listener_a.stop(1.0)
        if listener_b is not None:
            listener_b.stop(1.0)
        shutil.rmtree(dir_a, ignore_errors=True)
        shutil.rmtree(dir_b, ignore_errors=True)
