"""CLI-level acceptance tests for ticket 020-006, exercising the real
``aprilcam`` entry point (not fakes) against a real local daemon.

Two tiers:

* ``test_daemon_and_camera_round_trip_against_real_hardware`` --
  ``@pytest.mark.camera``. Spawns the real ``aprilcam`` console script
  (the installed ``[project.scripts]`` entry point, not
  ``python -m aprilcam.cli``) via ``subprocess`` against a real local
  daemon, covering UC-01.S-1, UC-13.S-3/S-4, UC-14.S-1, UC-25.S-1/S-2,
  UC-17.S-1/S-2/S-3 end to end against whichever camera(s) are actually
  attached to this machine -- discovered at run time via
  ``camera.identity.resolve_all()``, never a hardcoded device name
  (camera hardware varies by machine and must not be baked into the
  test). Skips cleanly if no camera is attached, matching every other
  ``@pytest.mark.camera`` module's convention.

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

* ``test_camera_list_all_against_a_real_multi_daemon_find_all_reports_partial_failure``
  -- ``@pytest.mark.integration``, no camera hardware needed (ticket
  028-005, SUC-007). Unlike the test above -- which predates ticket
  004, when ``Discovery.find_all()`` was still stubbed local-only, and
  so had to fake ``find_all()``/``connect()`` entirely -- this one
  drives the **real** ``client.discovery.Discovery`` (no fakes, no DI
  overrides beyond a shrunk ``connect_timeout``) against a real local
  daemon plus a genuine mDNS advertisement with nothing listening
  behind its advertised port. It is proof that ticket 004's now-real
  ``find_all()`` composes correctly with every pre-existing ``--all``
  command, not only the new ``daemon list`` one (ticket 005) -- and
  changes no production code in ``fan_out()``/``camera_cli.cmd_list``
  to get there.

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
import numpy as np
import pytest

from aprilcam.camera.camera import Camera
from aprilcam.camera.discovery import CameraDiscovery
from aprilcam.camera.identity import CameraIdentity, resolve_all
from aprilcam.camera.registry import CameraRegistry
from aprilcam.config.directories import Directories
from aprilcam.daemon.rpc import build_server
from aprilcam.daemon.service import DaemonService
from tests.aprilcam.camera.test_camera import FakeBackend


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


def _attached_camera_slugs_or_skip() -> list[str]:
    """The sorted slugs of every camera actually attached to this
    machine right now, per ``camera.identity.resolve_all()`` -- the
    same discovery source this module's fixture-presence check has
    always used, just returned instead of compared against a
    hardcoded pair. Skips (rather than hardcoding specific device
    names) if nothing is attached, matching every other
    ``@pytest.mark.camera`` module's convention.
    """
    slugs = sorted({identity.slug for identity in resolve_all().values()})
    if not slugs:
        pytest.skip("no camera attached to this machine")
    return slugs


def _skip_if_camera_open_failed(result: subprocess.CompletedProcess, slug: str) -> None:
    """``resolve_all()``/``camera probe``/``camera list`` (above) only
    prove a camera is *enumerated* -- not that this process can actually
    open it right now (e.g. an agent session has no macOS camera TCC
    grant; docs/knowledge/daemon-camera-grant-and-detection-priming.md).
    Only the specific, known "failed to open camera" signature is
    treated as environmental; any other non-zero exit still fails the
    calling assertion for real."""
    if result.returncode != 0 and b"failed to open camera" in result.stderr:
        pytest.skip(
            f"{slug!r} is attached but the isolated daemon could not open "
            f"it just now -- {result.stderr.decode(errors='replace').strip()}. "
            f"Expected when this process has no camera TCC grant (e.g. an "
            f"agent session) or the device is genuinely busy elsewhere; "
            f"re-run from an operator Terminal session to exercise this "
            f"test for real."
        )


@pytest.mark.camera
def test_daemon_and_camera_round_trip_against_real_hardware(tmp_path, isolated_env):
    slugs = _attached_camera_slugs_or_skip()
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
        # UC-25.S-1/S-2 in its own right: each attached camera reported
        # independently, by its derived name.
        result = _run(bin_path, "camera", "probe", env=env)
        assert result.returncode == 0, result.stderr
        probe_out = result.stdout.decode()
        for slug in slugs:
            assert slug in probe_out

        # -- UC-14.S-1: every attached camera, with enumeration numbers -
        result = _run(bin_path, "camera", "list", env=env)
        assert result.returncode == 0, result.stderr
        list_out = result.stdout.decode()
        for slug in slugs:
            assert slug in list_out
        assert "[1]" in list_out or "[2]" in list_out or "[3]" in list_out or "[4]" in list_out

        # -- UC-13.S-3: status now reports running with the camera list
        result = _run(bin_path, "daemon", "status", env=env)
        assert result.returncode == 0, result.stderr
        status_out = result.stdout.decode()
        assert "local: running" in status_out
        assert slugs[0] in status_out

        # Two distinct slugs cover the two capture-encoding checks below
        # when two or more cameras are attached; with only one attached,
        # reuse it for both -- encoding-format coverage doesn't require
        # a second physical camera, that pairing was incidental to the
        # old hardcoded pair, not a real requirement.
        png_slug = slugs[0]
        jpeg_slug = slugs[1] if len(slugs) > 1 else slugs[0]

        # -- UC-17.S-1: lossless PNG fixture file ----------------------
        png_path = tmp_path / "frame.png"
        result = _run(
            bin_path,
            "camera",
            "image",
            png_slug,
            "-o",
            str(png_path),
            "--encoding",
            "png",
            env=env,
        )
        _skip_if_camera_open_failed(result, png_slug)
        assert result.returncode == 0, result.stderr
        data = png_path.read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n"

        # -- UC-17.S-2: raw bytes to stdout, pipeable -------------------
        result = _run(bin_path, "camera", "image", jpeg_slug, env=env)
        assert result.returncode == 0, result.stderr
        assert result.stdout[:2] == b"\xff\xd8"  # JPEG SOI marker

        # -- UC-17.S-3: base64 over a text-only channel -----------------
        result = _run(bin_path, "camera", "image", jpeg_slug, "--base64", env=env)
        assert result.returncode == 0, result.stderr
        decoded = base64.b64decode(result.stdout.strip())
        assert decoded[:2] == b"\xff\xd8"

        # -- unknown camera surfaces the typed error, not a bare crash --
        result = _run(bin_path, "camera", "image", "no-such-camera", "--base64", env=env)
        assert result.returncode == 1
        assert b"no-such-camera" in result.stderr
        assert any(slug.encode() in result.stderr for slug in slugs)
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


@pytest.mark.integration
def test_camera_list_all_against_a_real_multi_daemon_find_all_reports_partial_failure(
    capsys, tmp_path: Path, monkeypatch
):
    """SUC-007, proven against the REAL, now-multi-daemon
    ``Discovery.find_all()`` (ticket 028-004) -- see the module
    docstring for why this is distinct from the fake-``Discovery``
    fan-out test above. One real local daemon (found via the
    lock-probe) is reachable; a genuine mDNS advertisement with
    nothing actually listening behind its advertised port (found via
    the real default browse) is not -- "the process died between
    advertise and connect", per SUC-007's own acceptance-criterion
    wording. ``start_watch=False`` means the real local daemon never
    runs an actual camera probe, so it reports zero cameras regardless
    of what hardware, if any, is attached to the machine running this
    suite -- no ``@pytest.mark.camera`` needed. This test changes no
    production code: ``fan_out()`` and ``camera_cli.cmd_list`` run
    completely unmodified.
    """
    import argparse
    import socket

    from aprilcam.cli import camera as camera_cli
    from aprilcam.client.discovery import Discovery
    from aprilcam.config.directories import Directories
    from aprilcam.daemon import __main__ as daemon_main
    from aprilcam.daemon.mdns import MdnsAdvertiser

    short_dir = Path(tempfile.mkdtemp(prefix="ac-fanout-real-", dir="/tmp"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(short_dir))
    directories = Directories(config_dir=tmp_path / "config", state_dir=tmp_path / "state")
    assert directories.socket_dir == short_dir / "aprilcam"

    lifecycle = daemon_main.build(directories, start_watch=False, tcp_port=0)
    lifecycle.start()

    # A phantom second daemon: a genuine mDNS advertisement for an
    # OS-assigned ephemeral TCP port that is grabbed and released
    # immediately, with nothing ever bound to it afterward -- so the
    # real browse finds a real, resolvable record whose connect must
    # genuinely fail, rather than a fake standing in for that failure.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("0.0.0.0", 0))
    dead_port = probe.getsockname()[1]
    probe.close()

    advertiser = MdnsAdvertiser()
    advertiser.start(dead_port, hostname="test-second-host")
    try:
        # No browse= override, no lock_holder/socket_reachable override
        # -- the real Discovery, real default browse. connect_timeout
        # is shrunk only so this test does not wait out the production
        # 5s default against a port nothing answers on.
        discovery = Discovery(directories=directories, connect_timeout=1.5)

        code = camera_cli.cmd_list(
            argparse.Namespace(daemon=None, all=True), discovery_factory=lambda: discovery
        )

        assert code == 1  # partial failure is visible in the exit code
        out, err = capsys.readouterr()
        assert "localhost (local):" in out
        assert "(no cameras)" in out
        assert "unreachable" in err
    finally:
        advertiser.stop()
        lifecycle.stop()
        shutil.rmtree(short_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# ``playfield annotations`` subprocess round trip -- @pytest.mark.integration,
# no camera hardware needed (ticket 024-005, UC-28, SUC-004): annotations
# are playfield-addressed, never camera-addressed, so this needs nothing
# this module's zero-camera fan-out test above does not already prove is
# safe to run headless (a real daemon subprocess, real local socket).
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_playfield_annotations_subprocess_round_trips_against_a_python_client_put(isolated_env):
    """``aprilcam playfield annotations`` (the real, installed console
    script, via subprocess) reads back exactly what a ``GrpcDaemon``
    Python client put on the *same* running daemon -- ``cmd_annotations``
    "invents no separate truth" (``cli/playfield.py``'s own docstring)
    from what ``get_annotations``/``clear_annotations`` actually report.
    ``--clear`` is confirmed by a follow-up ``get_annotations`` Python
    call against the same daemon, not just by the CLI's own printed
    claim.
    """
    from aprilcam.client.discovery import Discovery
    from aprilcam.config.loader import PlayfieldDefinition
    from aprilcam.types import Annotation, Marker, WorldPoint

    bin_path = _aprilcam_bin()
    env = isolated_env

    result = _run(bin_path, "daemon", "start", env=env)
    assert result.returncode == 0, result.stderr

    try:
        client = Discovery().connect()
        client.set_playfield(
            PlayfieldDefinition(name="main-playfield", width_cm=100.0, height_cm=80.0)
        )
        client.put_annotation(
            "main-playfield",
            Annotation(id="goal", layer="planner", shape=Marker(at=WorldPoint(x=1.0, y=2.0))),
        )

        result = _run(bin_path, "playfield", "annotations", "main-playfield", env=env)
        assert result.returncode == 0, result.stderr
        list_out = result.stdout.decode()
        assert "revision 1" in list_out
        assert "planner" in list_out
        assert "goal" in list_out

        result = _run(
            bin_path, "playfield", "annotations", "main-playfield", "--clear", env=env
        )
        assert result.returncode == 0, result.stderr
        assert b"cleared" in result.stdout

        after = client.get_annotations("main-playfield")
        assert after.annotations == ()
    finally:
        result = _run(bin_path, "daemon", "stop", env=env)
        assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# BrokenPipeError / SIGPIPE handling -- ticket 031-003, SUC-003. Reproduces
# the issue's own scenario end to end: the real ``aprilcam`` console script,
# piping ``camera image --base64`` into a truncating reader, against a fake
# (in-process gRPC) daemon -- no camera hardware needed.
# ---------------------------------------------------------------------------


def _identity(hardware_key: str = "hw:fake-broken-pipe", device_name: str = "Fake Camera") -> CameraIdentity:
    from aprilcam.camera import identity as identity_module

    return CameraIdentity(
        slug=identity_module.device_name_slug(device_name),
        device_name=device_name,
        hardware_key=hardware_key,
        source="vid_pid_location",
        is_fallback=False,
    )


def _bind_daemon_with_one_large_frame_camera(directories: Directories):
    """A real ``DaemonService``/``grpc.Server`` (matching this module's
    own ``_bind_zero_camera_daemon`` pattern above), holding one
    ``FakeBackend``-backed camera whose single queued frame is large,
    high-entropy noise -- large enough that its base64 encoding
    (``cli/camera.py``'s ``cmd_image`` ``--base64`` branch) exceeds any
    platform's pipe buffer capacity. That size is the whole point: it
    forces the CLI's single ``print()`` call to block partway through
    an in-flight OS-level write, so closing the reader early reliably
    produces a genuine ``BrokenPipeError`` inside that write rather than
    racing against read timing (a small payload could complete a full
    write before the reader ever closes, never exercising the bug at
    all -- this is why the issue's own repro needed a real image, not a
    trivial fixture frame).
    """
    registry = CameraRegistry(directories.state_dir / "cameras")
    identity = _identity()
    slug = registry.resolve(identity)
    number = registry.get(slug).number
    noisy_frame = np.random.default_rng(0).integers(0, 256, size=(480, 640, 3), dtype=np.uint8)
    camera = Camera(slug, FakeBackend(read_queue=[noisy_frame]), number=number)
    camera.open()
    cameras: Dict[str, Camera] = {slug: camera}
    discovery = CameraDiscovery(
        registry,
        active_camera_state=lambda slug: cameras[slug].state if slug in cameras else None,
        identity_source=lambda: {0: identity},
        backend_factory=lambda index: FakeBackend(),
    )
    service = DaemonService(
        registry=registry,
        discovery=discovery,
        directories=directories,
        cameras=cameras,
        identity_source=lambda: {0: identity},
    )
    socket_path = directories.socket_dir / "control.sock"
    listener = build_server(service, socket_path)
    listener.start()
    return listener, slug


@pytest.mark.integration
def test_camera_image_base64_piped_into_a_truncating_reader_exits_without_a_traceback(
    isolated_env, short_runtime_dir: Path
):
    """The exact scenario named in the linked issue (section 2,
    ``sprint-020-followups-socket-collision-and-cli-polish.md``):
    ``aprilcam camera image <camera> --base64 | head -c 80``. Run here
    against a fake daemon (real gRPC server, fake camera backend) so no
    hardware is required -- ``isolated_env``/``short_runtime_dir``
    isolate ``socket_dir`` down to a private tmp root exactly as the
    real-hardware round-trip test above does, so
    :func:`_bind_daemon_with_one_large_frame_camera`'s server and the
    subprocess spawned below resolve the *same* control socket.

    This drives the real, installed ``aprilcam`` console script (not
    ``main()`` in-process) so the assertion covers the whole boundary:
    argparse dispatch, the real ``BrokenPipeError``, and the real
    ``SIGPIPE`` disposition CPython/this ticket's fix establish for a
    genuine subprocess. Rather than shelling out through an actual
    ``head`` (which would hand back only the *pipeline's* exit code,
    not ``aprilcam``'s own), this reads exactly 80 bytes from the
    subprocess's stdout pipe and then closes our end -- precisely what
    ``head -c 80`` does to the pipe from the writer's point of view --
    which lets the assertions below inspect ``aprilcam``'s own
    returncode and stderr directly.
    """
    env = isolated_env
    # ``isolated_env`` already set APRILCAM_CONFIG_DIR/APRILCAM_STATE_DIR/
    # XDG_RUNTIME_DIR to point at tmp_path/short_runtime_dir for this
    # process's environment -- a bare ``Directories()`` here resolves
    # identically to what the subprocess spawned below resolves.
    directories = Directories()
    assert directories.socket_dir == short_runtime_dir / "aprilcam"
    listener, slug = _bind_daemon_with_one_large_frame_camera(directories)
    try:
        bin_path = _aprilcam_bin()
        proc = subprocess.Popen(
            [str(bin_path), "camera", "image", slug, "--base64"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        stdout = proc.stdout
        assert stdout is not None
        try:
            head = stdout.read(80)
            assert len(head) == 80  # confirms the payload was actually large
        finally:
            stdout.close()  # closes our read end early, like `head -c 80` exiting

        stderr_data = b""
        stderr = proc.stderr
        if stderr is not None:
            stderr_data = stderr.read()
            stderr.close()
        returncode = proc.wait(timeout=30)

        assert b"Traceback" not in stderr_data
        assert b"BrokenPipeError" not in stderr_data
        assert returncode != 0
    finally:
        listener.stop(1.0)
