"""System test for the sanctioned robot-program surface: ``aprilcam.client``
driven directly, the way an outside program would (sprint 035, ticket
012) -- ``Discovery().connect()`` to a typed ``Daemon``, never through
the MCP stdio wrapper (``tests/aprilcam/mcp/test_stdio_hardware.py``,
ticket 011, is the sibling system test that drives the same isolated
daemon *through* MCP). ``src/aprilcam/client/interfaces.py``'s module
docstring names this surface directly: "the CLI, the MCP server, the
ROS node, and robot programs all use it" -- this module is the first
above-unit-test-level exercise of that claim for the client library on
its own, most notably ``put_annotation``/``replace_layer``
(``client/DESIGN.md`` section 1 calls those two out by name as
otherwise-untested).

**Two independent, non-overlapping sessions, deliberately split by what
actually needs a camera** -- the central design decision this ticket's
own text calls out. A private daemon started from an agent inherits
Claude Code's macOS TCC camera attribution (none), so ``get_frame``
fails with "failed to open camera at index N" even though enumeration
keeps reporting the rig present (see
``docs/knowledge/daemon-camera-grant-and-detection-priming.md``).
Rather than let that failure blanket-skip everything in this module:

* :func:`daemon_only_session` starts a private, isolated daemon and
  never attempts to open a camera at all -- ``put_annotation``,
  ``replace_layer``, ``get_annotations``, ``clear_annotations``,
  ``get_guide``, and playfield push/get only ever need a playfield
  pushed into a running daemon, never a camera. These genuinely execute
  and pass in an agent session.
* :func:`camera_session` (parameterized over every camera
  :func:`tests.aprilcam.hardware_camera.resolve_all_hardware_camera_identities`
  finds configured+calibrated against ``main-playfield``) gates on a
  real ``get_frame`` before yielding, exactly mirroring
  ``tests/aprilcam/calibration/test_full_calibrate_hardware.py``'s own
  ``daemon`` fixture (ticket 010's dependency) -- a failure there skips
  *that one camera's* parameterized cases individually, with
  remediation text naming the TCC gap, never a hard failure and never a
  blanket module-wide skip.

**Isolation**: every fixture below relocates ``config_dir``/
``state_dir`` to ``tmp_path`` (flag tier -- ``Directories(config_dir=...,
state_dir=...)``, never an env override) and ``socket_dir`` to a short
``/tmp`` root via ``XDG_RUNTIME_DIR`` (the only tier ``socket_dir`` has
at all -- ``config/directories.py``'s "sockets are not configurable"),
mirroring ``tests/aprilcam/cli/test_cli_integration.py``'s
``short_runtime_dir``/``isolated_env`` pattern. This (rather than
``test_full_calibrate_hardware.py``'s own explicit ``lock_path``/
``socket_path`` override) is deliberate: this module's whole point is
exercising ``Discovery().connect()`` (AC1), which resolves the local
socket from ``Directories().socket_dir`` alone -- an explicit
``socket_path`` override handed only to ``daemon.__main__.build()``
would leave ``Discovery`` looking in the wrong place. Every session also
snapshots the real, un-overridden ``~/.config/aprilcam``/
``~/.local/share/aprilcam`` (or wherever this machine's own
:class:`~aprilcam.config.directories.Directories` resolves them,
captured once at import time -- same reasoning as
``tests/aprilcam/hardware_camera.py``'s own
``_REAL_SYSTEM_DIRECTORIES``) before starting and asserts an identical
snapshot after stopping, the concrete proof behind "every write lands
under the isolated roots."

Never writes ``camera_position`` anywhere -- ``hardware_test_camera_config``
(``tests/aprilcam/daemon/conftest.py``) pushes only ``name``/
``playfield``/a swept ``exposure`` setting, and nothing in this module
constructs a ``CameraConfig`` with ``camera_position`` set at all.

**Out-of-process cleanup (2026-08-23): three of the four ``camera_session``
tests were converted to a third session, against the operator's
already-running daemon, because they never needed a private one.** A
private daemon started from this process cannot open a camera at all
(``docs/knowledge/daemon-camera-grant-and-detection-priming.md`` --
no macOS camera TCC grant), so ``camera_session`` always skips every
case in an agent run. But the operator's own daemon is already up,
already linked+calibrated (camera 3, ``arducam-ov9782-usb-camera``,
against ``main-playfield``), and already serving frames/tags to any
client on this machine -- ``get_frame``/``get_tags``/``stream_images``
are pure reads against that state, so nothing stops them running for
real against it instead:

* :func:`running_daemon_session` -- ``Discovery().connect()`` with **no**
  isolation env vars and **no** spawned daemon, targeting
  :data:`_RELIABLE_CAMERA_SLUG` specifically rather than the full
  multi-camera parameterization ``camera_session`` uses. Deliberately a
  single fixed camera, not another ``resolve_all_hardware_camera_
  identities`` parameterization: camera 5 (``arducam-ov9281-usb-camera``)
  also qualifies (linked+calibrated to ``main-playfield``) but only sees
  ArUco 1 ~7/12 of the time on this rig, and the whole point of these
  three tests is a real, non-flaky read, not an availability sweep.
  Pushes nothing -- no ``set_playfield``, no ``set_camera_config``, no
  ``calibrate`` -- since the target camera is already configured and
  calibrated on this machine's real, persisted state (confirmed on disk
  before writing this fixture); a push here would be a live override
  visible to the operator and to anyone else watching this daemon, which
  is exactly what this conversion exists to avoid.
  :func:`test_get_frame_succeeds_raw_and_deskewed` and
  :func:`test_get_tags_get_tag_and_get_tag_history_return_well_typed_results`
  use it unchanged from their ``camera_session`` bodies otherwise (same
  assertions, same ``_prime_tags`` helper).
  :func:`test_stream_images_subscription_receives_a_frame_and_unsubscribes_cleanly_on_close`
  drops the private-daemon variant's ``lifecycle._status_source.__self__``/
  ``_image_stream_producers`` reach-in -- that inspects the daemon
  process's own internals, which for this session live in a separate,
  operator-owned process this test does not control -- and instead
  proves "unsubscribes cleanly" the black-box way: ``close()`` raises
  nothing, and a plain ``get_frame`` right after still succeeds (the
  daemon's stream-handling state was not left corrupted by the close).
* :func:`test_register_tag_and_unregister_tag_round_trip` **stays on the
  private, isolated ``camera_session`` path.** ``register_tag`` is not a
  read -- ``types.MountParameters``' own docstring: "runtime
  registrations... applies whatever mount_x/mount_y/mount_z/mount_yaw_rad
  it is given" -- and this test's ``mount_x=3.0``/``mount_y=-1.5`` would
  offset the reported world position of *any* real ARUCO tag 2 the
  operator's daemon sees, on any camera, for as long as the registration
  is live, exactly the same "live override the operator would see" risk
  ruled out for camera config. A ``finally``-guarded ``unregister_tag``
  narrows the exposure window but does not remove it (a crash between
  register and the ``finally`` would leave a real tag's reported
  position wrong for the rest of the daemon's uptime, and this project's
  own rule is never to restart it to recover). Judged unsafe against a
  shared daemon; left exactly as ticket 012 wrote it.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path
from typing import Iterator, Tuple

import pytest

from aprilcam.client.discovery import Discovery
from aprilcam.client.guides import Guides
from aprilcam.config.directories import Directories
from aprilcam.daemon import __main__ as daemon_main
from aprilcam.daemon.interfaces import Daemon
from aprilcam.errors import AprilCamError
from aprilcam.types import (
    Annotation,
    AnnotationSet,
    ImageFrame,
    Marker,
    MountParameters,
    PathShape,
    TagFamily,
    TagFrame,
    TagId,
    TagRecord,
    WorldPoint,
)

from tests.aprilcam.calibration.test_calibration import main_playfield_definition
from tests.aprilcam.daemon.conftest import calibration_diagnosis, hardware_test_camera_config
from tests.aprilcam.hardware_camera import resolve_all_hardware_camera_identities

#: The only playfield this module has ground truth for -- matches every
#: other sprint 035 hardware module's own ``_TARGET_PLAYFIELD``.
_TARGET_PLAYFIELD = "main-playfield"

#: This machine's real, un-overridden system directories -- resolved
#: **once, at module import time**, before any fixture below has a
#: chance to run (pytest collects this module, evaluating every
#: module-level statement, before any fixture's ``monkeypatch`` takes
#: effect). See ``tests/aprilcam/hardware_camera.py``'s own
#: ``_REAL_SYSTEM_DIRECTORIES`` for the identical reasoning -- this
#: module never overrides ``APRILCAM_CONFIG_DIR``/``APRILCAM_STATE_DIR``
#: (only ``XDG_RUNTIME_DIR``, which does not affect these two paths), so
#: a lazy per-call ``Directories()`` would almost always agree anyway;
#: captured eagerly regardless, to make that true by construction.
_REAL_DIRECTORIES = Directories()


def _dir_listing(root: Path) -> Tuple[str, ...]:
    """A cheap identity snapshot of *root* -- relative paths only, never
    contents -- sufficient to prove "nothing was written here" for the
    isolation spot-check every session fixture below performs."""
    if not root.exists():
        return ()
    return tuple(sorted(str(p.relative_to(root)) for p in root.rglob("*")))


def _real_dirs_snapshot() -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    return _dir_listing(_REAL_DIRECTORIES.config_dir), _dir_listing(_REAL_DIRECTORIES.state_dir)


def _prime_tags(daemon: Daemon, slug: str, *, attempts: int = 20, delay: float = 0.5) -> TagFrame:
    """Poll ``get_tags`` until at least one tag is seen or *attempts* are
    exhausted. The first poll after a camera opens (or a daemon starts)
    returns zero tags -- detections must survive
    ``CONFIRMATION_GRACE`` (sprint 034) before they are reported -- so a
    single untried poll proves nothing (``docs/knowledge/
    daemon-camera-grant-and-detection-priming.md``)."""
    frame = daemon.get_tags(slug)
    for _ in range(attempts):
        if frame.tags:
            return frame
        time.sleep(delay)
        frame = daemon.get_tags(slug)
    return frame


def _link_and_calibrate(daemon: Daemon, slug: str):
    """Push the real main-playfield definition, link *slug*'s camera to
    it with a freshly-swept exposure, and calibrate -- the setup every
    camera-dependent test below shares, mirroring
    ``test_full_calibrate_hardware.py``'s identically-named helper."""
    daemon.set_playfield(main_playfield_definition())
    daemon.set_camera_config(hardware_test_camera_config(slug, playfield=_TARGET_PLAYFIELD))
    return daemon.calibrate(slug)


def _short_tmp_dir(prefix: str) -> Path:
    """A short, top-level ``/tmp`` directory -- unix domain sockets have
    a ~103-character path limit, and pytest's ``tmp_path`` nests too
    deeply (matches every other hardware/integration test module's
    identically-named helper in this suite)."""
    return Path(tempfile.mkdtemp(prefix=prefix, dir="/tmp"))


# ---------------------------------------------------------------------------
# Isolation plumbing shared by both sessions below.
# ---------------------------------------------------------------------------


@pytest.fixture
def short_runtime_dir() -> Iterator[Path]:
    d = _short_tmp_dir("ac-012-rt-")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def isolated_directories(tmp_path: Path, short_runtime_dir: Path, monkeypatch) -> Directories:
    """``config_dir``/``state_dir`` isolated via the flag tier directly
    (no env/monkeypatch needed for those two); ``socket_dir`` isolated
    via ``XDG_RUNTIME_DIR`` since it has no flag tier at all
    (``config/directories.py``: "sockets are not configurable"). Skips
    (rather than silently talking to a real daemon) if this sandbox's
    ``/run/aprilcam`` is itself available and writable -- identical
    precondition check to ``tests/aprilcam/cli/test_cli_integration.py``'s
    ``isolated_env`` and ``tests/aprilcam/client/test_lifecycle.py``'s
    real-subprocess round trip.
    """
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(short_runtime_dir))
    directories = Directories(config_dir=tmp_path / "config", state_dir=tmp_path / "state")
    if directories.socket_dir != short_runtime_dir / "aprilcam":
        pytest.skip(
            "this sandbox's /run/aprilcam is available and writable, so "
            "socket_dir cannot be isolated down to a private tmp root -- "
            "cannot safely isolate this test from a real daemon here"
        )
    return directories


# ---------------------------------------------------------------------------
# Daemon-only, camera-free session -- annotations, get_guide, playfield
# push/get. No camera is opened anywhere in this fixture, so these
# genuinely execute (no macOS TCC camera grant required) in an agent run.
# ---------------------------------------------------------------------------


@pytest.fixture
def daemon_only_session(isolated_directories: Directories) -> Iterator[Daemon]:
    before = _real_dirs_snapshot()
    lifecycle = daemon_main.build(isolated_directories)
    lifecycle.start()
    try:
        daemon = Discovery(directories=isolated_directories).connect()
        daemon.set_playfield(main_playfield_definition())
        yield daemon
    finally:
        lifecycle.stop()
    after = _real_dirs_snapshot()
    assert after == before, (
        "the real ~/.config/aprilcam / ~/.local/share/aprilcam changed "
        "during a daemon-only test run -- isolation leaked"
    )


@pytest.mark.integration
def test_discovery_connect_returns_a_working_daemon_against_the_isolated_daemon(daemon_only_session):
    """AC1: ``Discovery().connect()`` succeeds against the isolated
    daemon and returns a working ``Daemon`` -- proven by exercising it
    (``status``, ``list_playfields``, ``get_playfield``), not merely by
    the connect call not raising."""
    daemon = daemon_only_session

    status = daemon.status()
    assert status.running is True

    playfields = daemon.list_playfields()
    assert any(p.name == _TARGET_PLAYFIELD for p in playfields)

    playfield = daemon.get_playfield(_TARGET_PLAYFIELD)
    assert playfield.name == _TARGET_PLAYFIELD


@pytest.mark.integration
def test_put_annotation_and_replace_layer_round_trip_through_get_annotations_then_clear(daemon_only_session):
    """The one place in the whole test suite exercising ``put_annotation``/
    ``replace_layer`` above unit level (ticket description). Presence is
    verified via ``get_annotations`` -- the "list_annotations-equivalent
    client call" branch of the acceptance criterion's either/or, chosen
    deliberately over the ``get_frame``-embedded-set branch so this test
    stays camera-free (a deskewed frame's embedded annotation set
    requires a calibrated camera -- see ``types.ImageFrame``'s own
    docstring)."""
    daemon = daemon_only_session

    marker = Annotation(
        id="robot-1", layer="robots", shape=Marker(at=WorldPoint(x=1.0, y=2.0), label="r1")
    )
    daemon.put_annotation(_TARGET_PLAYFIELD, marker)

    after_put = daemon.get_annotations(_TARGET_PLAYFIELD)
    assert isinstance(after_put, AnnotationSet)
    assert any(a.id == "robot-1" and a.layer == "robots" for a in after_put.annotations)
    revision_after_put = after_put.revision

    route_a = Annotation(
        id="route-a",
        layer="routes",
        shape=PathShape(points=(WorldPoint(x=0.0, y=0.0), WorldPoint(x=10.0, y=10.0))),
    )
    daemon.replace_layer(_TARGET_PLAYFIELD, "routes", (route_a,))

    after_replace = daemon.get_annotations(_TARGET_PLAYFIELD, layer="routes")
    assert {a.id for a in after_replace.annotations} == {"route-a"}
    assert after_replace.revision > revision_after_put  # AnnotationSet.revision is monotonic

    # replace_layer is an atomic swap, not an upsert (UC-28.S-2): a second
    # call with a different id set must remove the first entirely.
    route_b = Annotation(
        id="route-b",
        layer="routes",
        shape=PathShape(points=(WorldPoint(x=1.0, y=1.0), WorldPoint(x=2.0, y=2.0))),
    )
    daemon.replace_layer(_TARGET_PLAYFIELD, "routes", (route_b,))
    swapped = daemon.get_annotations(_TARGET_PLAYFIELD, layer="routes")
    assert {a.id for a in swapped.annotations} == {"route-b"}

    # A replace on "routes" must not disturb the "robots" layer.
    still_there = daemon.get_annotations(_TARGET_PLAYFIELD, layer="robots")
    assert any(a.id == "robot-1" for a in still_there.annotations)

    daemon.clear_annotations(_TARGET_PLAYFIELD)
    cleared = daemon.get_annotations(_TARGET_PLAYFIELD)
    assert cleared.annotations == ()


@pytest.mark.integration
def test_daemon_get_guide_rpc_serves_guide_text_against_the_isolated_daemon(daemon_only_session):
    """The daemon-only, camera-free half of ``get_guide`` coverage: the
    wire RPC (``Daemon.get_guide``) served by a real, running (if
    camera-less) daemon -- distinct from
    :func:`test_get_guide_succeeds_with_no_daemon_dependency` below,
    which proves the client-side accessor needs no daemon at all."""
    daemon = daemon_only_session
    text = daemon.get_guide()
    assert isinstance(text, str)
    assert text.strip() != ""


@pytest.mark.integration
def test_get_guide_succeeds_with_no_daemon_dependency():
    """AC: ``get_guide`` succeeds with no daemon dependency at all --
    deliberately constructs no fixture, starts no daemon, opens no
    socket. ``client.guides.Guides`` reads only the packaged
    ``aprilcam.guides`` module (UC-27.S-1: an agent's first tool call
    may be a guide request, before any daemon exists)."""
    text = Guides().get()
    assert isinstance(text, str)
    assert text.strip() != ""


# ---------------------------------------------------------------------------
# Camera-dependent session -- get_tags/get_tag/get_tag_history, get_frame,
# stream_images, register_tag/unregister_tag. Parameterized across every
# qualifying camera (ticket 010's enumeration); gates on a real
# get_frame and skips that one camera's cases individually on failure --
# the expected outcome in an agent run (no macOS camera TCC grant).
# ---------------------------------------------------------------------------


def pytest_generate_tests(metafunc):
    """Dynamic per-camera parameterization -- identical shape to
    ``test_full_calibrate_hardware.py``'s own hook. ``indirect=True`` so
    the :func:`identity` fixture (and, through it, :func:`camera_session`)
    receives each resolved ``CameraIdentity`` via ``request.param``."""
    if "identity" in metafunc.fixturenames:
        identities = resolve_all_hardware_camera_identities(playfield=_TARGET_PLAYFIELD)
        metafunc.parametrize(
            "identity", identities, indirect=True, ids=[i.slug for i in identities]
        )


@pytest.fixture
def identity(request):
    return request.param


@pytest.fixture
def camera_session(isolated_directories: Directories, identity) -> Iterator[Tuple[Daemon, object, str]]:
    """Gates on a real ``get_frame``, not on *identity* having merely
    qualified -- mirrors ``test_full_calibrate_hardware.py``'s own
    ``daemon`` fixture exactly. Yields ``(daemon, lifecycle, slug)``:
    *lifecycle* is kept around so
    :func:`test_stream_images_subscription_receives_a_frame_and_unsubscribes_cleanly_on_close`
    can reach the private ``DaemonService`` for its no-leaked-subscriber
    check the same way ``tests/aprilcam/daemon/test_streaming_integration.py``'s
    own real-socket idle-gating tests already do -- no public API
    surfaces a stream subscriber count."""
    before = _real_dirs_snapshot()
    lifecycle = daemon_main.build(isolated_directories)
    lifecycle.start()
    try:
        daemon = Discovery(directories=isolated_directories).connect()
        try:
            daemon.get_frame(identity.slug, deskewed=False, encoding="jpeg")
        except AprilCamError as exc:
            pytest.skip(
                f"{identity.slug!r} qualifies (config + calibration present "
                f"on this machine's real system state) but could not be "
                f"opened/read via the daemon just now -- {exc}. Expected "
                f"when the process running this test has no camera "
                f"permission grant (e.g. an agent session -- see "
                f"docs/knowledge/daemon-camera-grant-and-detection-"
                f"priming.md) or the device is genuinely busy elsewhere; "
                f"re-run from an operator Terminal session to exercise "
                f"this camera for real."
            )
        yield daemon, lifecycle, identity.slug
    finally:
        lifecycle.stop()
    after = _real_dirs_snapshot()
    assert after == before, (
        "the real ~/.config/aprilcam / ~/.local/share/aprilcam changed "
        "during a camera-dependent test run -- isolation leaked"
    )


#: The one camera the shared-daemon tests below target -- never the full
#: multi-camera parameterization ``camera_session``/``identity`` use.
#: Camera 3 is this rig's reliable one (AprilTag 1 + ArUco 1-10); camera
#: 5 (``arducam-ov9281-usb-camera``, also linked+calibrated to
#: ``main-playfield``) sees ArUco 1 only ~7/12 of the time and must never
#: be required for a "a tag was detected" assertion
#: (``docs/knowledge/daemon-camera-grant-and-detection-priming.md``'s
#: "Visibility is per-camera" section).
_RELIABLE_CAMERA_SLUG = "arducam-ov9782-usb-camera"


@pytest.fixture
def running_daemon_session() -> Iterator[Tuple[Daemon, str]]:
    """Connects to the operator's already-running daemon -- no isolation
    env vars, no spawned daemon, no config pushed -- and gates on a real
    ``get_frame`` against :data:`_RELIABLE_CAMERA_SLUG`, mirroring
    ``camera_session``'s own gate-on-open-not-enumeration contract
    (``docs/knowledge/daemon-camera-grant-and-detection-priming.md``:
    enumeration succeeds even when opens fail). Skips cleanly with
    remediation text if the named camera does not currently qualify, or
    if a real frame fetch fails."""
    identities = resolve_all_hardware_camera_identities(playfield=_TARGET_PLAYFIELD)
    if not any(identity.slug == _RELIABLE_CAMERA_SLUG for identity in identities):
        pytest.skip(
            f"{_RELIABLE_CAMERA_SLUG!r} is not currently attached and "
            f"configured+calibrated against {_TARGET_PLAYFIELD!r} on this "
            f"machine's real system state (qualifying cameras right now: "
            f"{sorted(i.slug for i in identities)}) -- link and calibrate it "
            f"with 'aprilcam camera config <file>' and 'aprilcam camera "
            f"calibrate {_RELIABLE_CAMERA_SLUG}' before running this test."
        )

    daemon = Discovery().connect()
    try:
        daemon.get_frame(_RELIABLE_CAMERA_SLUG, deskewed=False, encoding="jpeg")
    except AprilCamError as exc:
        pytest.skip(
            f"{_RELIABLE_CAMERA_SLUG!r} qualifies (config + calibration "
            f"present on this machine's real system state) but could not "
            f"be read via the running daemon just now -- {exc}. The "
            f"operator's daemon may be down or the camera busy elsewhere "
            f"-- re-run once it is reachable (see docs/knowledge/"
            f"daemon-camera-grant-and-detection-priming.md)."
        )
    yield daemon, _RELIABLE_CAMERA_SLUG


@pytest.mark.camera
def test_get_frame_succeeds_raw_and_deskewed(running_daemon_session):
    """Against the operator's already-running daemon (no isolation, no
    pushed config) -- camera 3 is already linked+calibrated to
    ``main-playfield`` on this machine's real, persisted state, so this
    test only reads. Assertions unchanged from the isolated-daemon
    version this replaces (ticket 012)."""
    daemon, slug = running_daemon_session

    raw = daemon.get_frame(slug, deskewed=False, encoding="jpeg")
    assert isinstance(raw, ImageFrame)
    assert raw.deskewed is False
    assert len(raw.data) > 0
    # HasField() discipline: a raw frame publishes no world<->pixel
    # mapping -- None, never a stale/zero value (client/DESIGN.md).
    assert raw.px_per_cm is None
    assert raw.world_extent is None

    deskewed = daemon.get_frame(slug, deskewed=True, encoding="jpeg")
    assert deskewed.deskewed is True
    assert len(deskewed.data) > 0
    # Once calibrated, these are populated -- not merely non-crashing.
    assert deskewed.px_per_cm is not None
    assert deskewed.world_extent is not None


@pytest.mark.camera
def test_get_tags_get_tag_and_get_tag_history_return_well_typed_results(running_daemon_session):
    """Against the operator's already-running daemon -- see
    :func:`running_daemon_session`. Assertions unchanged from the
    isolated-daemon version this replaces (ticket 012)."""
    daemon, slug = running_daemon_session

    frame = _prime_tags(daemon, slug)
    assert isinstance(frame, TagFrame)
    assert len(frame.tags) > 0, (
        f"no tags detected on {slug!r} against {_TARGET_PLAYFIELD!r} via the "
        f"running daemon after priming -- expected AprilTag 1 / ArUco 1-10 "
        f"to be visible on this camera (docs/knowledge/"
        f"daemon-camera-grant-and-detection-priming.md)"
    )

    # get_tag for a tag actually seen this frame -- picked from the live
    # detection result rather than a hardcoded id, since which specific
    # ids are reliable differs per camera on this rig (docs/knowledge/
    # daemon-camera-grant-and-detection-priming.md's "Visibility is
    # per-camera" section).
    detected = frame.tags[0]
    record = daemon.get_tag(slug, detected.tag)
    assert record is not None
    assert isinstance(record, TagRecord)
    # HasField() discipline: a calibrated camera populates world, never
    # defaults it to (0, 0) -- client/DESIGN.md's "never coerced" rule.
    assert record.world is not None

    # ...and the Optional-not-zero-valued half of that same discipline:
    # a tag genuinely absent from the field returns None, never a
    # zero-valued TagRecord.
    absent_tag = TagId(family=TagFamily.APRILTAG, number=999)
    assert daemon.get_tag(slug, absent_tag) is None

    history = daemon.get_tag_history(slug)
    assert isinstance(history, tuple)
    assert len(history) > 0
    assert all(isinstance(tf, TagFrame) for tf in history)

    filtered_history = daemon.get_tag_history(slug, tag=detected.tag, frames=5)
    assert isinstance(filtered_history, tuple)


@pytest.mark.camera
def test_stream_images_subscription_receives_a_frame_and_unsubscribes_cleanly_on_close(running_daemon_session):
    """Against the operator's already-running daemon -- see
    :func:`running_daemon_session`. Cannot reach ``DaemonService.
    _image_stream_producers`` the way the isolated-daemon version (ticket
    012) did -- that private state lives inside a separate,
    operator-owned process this test does not control, so there is no
    ``lifecycle`` object here to reach in through at all. Proves
    "unsubscribes cleanly" the only way a client-only test against a
    shared daemon can: ``close()`` raises nothing, and a plain
    ``get_frame`` right after still succeeds -- if ``close()`` had left
    the daemon's stream-handling state corrupted, that follow-up read
    would be the thing to catch it."""
    daemon, slug = running_daemon_session

    stream = daemon.stream_images(slug, deskewed=False)
    it = iter(stream)
    try:
        frame = next(it)
    finally:
        stream.close()

    assert isinstance(frame, ImageFrame)
    assert len(frame.data) > 0

    # The daemon is still healthy for other clients after this
    # subscription closed -- the black-box substitute for the isolated
    # version's direct producer-leak check (see docstring above).
    after_close = daemon.get_frame(slug, deskewed=False, encoding="jpeg")
    assert isinstance(after_close, ImageFrame)
    assert len(after_close.data) > 0


@pytest.mark.camera
def test_register_tag_and_unregister_tag_round_trip(camera_session):
    daemon, _lifecycle, slug = camera_session
    result = _link_and_calibrate(daemon, slug)
    assert result.succeeded is True, calibration_diagnosis(result, slug)

    tag = TagId(family=TagFamily.ARUCO, number=2)
    params = MountParameters(mount_x=3.0, mount_y=-1.5, mount_yaw_rad=0.4)

    warning = daemon.register_tag(tag, params)
    # mount_z defaults to 0.0 -- nothing for the parallax correction to
    # withhold, so no warning is expected (MountRegistration's own
    # docstring: "True in every other case, including mount_z <= 0").
    assert warning is None

    registered = daemon.list_tag_parameters()
    assert tag in registered
    assert registered[tag].params == params
    assert registered[tag].mount_z_applied is True

    daemon.unregister_tag(tag)
    after = daemon.list_tag_parameters()
    assert tag not in after
