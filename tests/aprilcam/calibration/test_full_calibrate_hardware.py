"""Parameterized full-calibrate flow against every qualifying camera on
this machine's real ``main-playfield`` rig (sprint 035, ticket 010).

``tests/aprilcam/daemon/test_calibration_hardware.py`` established the
single-camera version of every assertion this module re-asserts, always
against whichever ONE camera :func:`tests.aprilcam.hardware_camera.
resolve_hardware_camera_slug` picked. That module is untouched by this
ticket (its own hardware-test suite must keep passing unmodified). This
module instead parameterizes those same real-hardware assertions across
**every** camera :func:`tests.aprilcam.hardware_camera.
resolve_all_hardware_camera_identities` finds genuinely configured and
calibrated against ``main-playfield`` -- on this project's own dev rig,
that is cameras 3 (``arducam-ov9782-usb-camera``) and 5
(``arducam-ov9281-usb-camera``); camera 2 (``hd-usb-camera``) is linked
to a *different* playfield (``secondary-playfield``, no checked-in
ground-truth geometry yet) and is out of scope for this specific
main-playfield flow -- see ``resolve_all_hardware_camera_identities``'s
own module docstring for why it is still independently enumerable
against that other playfield.

**Dynamic per-camera parameterization, not a fixture list.** Uses
``pytest_generate_tests`` (a collection-time hook) rather than a static
``@pytest.mark.parametrize`` list, because the candidate set is only
known at collection time, by inspecting this machine's real, persisted
system state (never hardcoded -- matches ``hardware_camera.py``'s own
"never hardcode a device name" rule). ``resolve_all_hardware_camera_
identities`` skips cleanly (module-wide) when zero cameras qualify at
all; when at least one qualifies, each becomes its own parameterized
test id (the camera's slug), so **one unavailable camera skips only its
own case, never the whole parameterized set** -- the specific gap this
ticket's own acceptance criteria describe.

**Enumeration qualifying a camera is not proof it can be opened right
now -- gate on a real open/frame fetch, individually, per camera.**
``resolve_all_hardware_camera_identities`` answers "is this camera
configured and calibrated against this playfield" from pure filesystem
state; it deliberately never attempts to open a device (see its own
module docstring's "Enumeration is not availability" section). This
module's own :func:`daemon` fixture is what performs the real
open/frame-fetch check -- immediately after starting a private per-test
daemon, it fetches one frame from the parameterized camera and, if that
fails, skips *that one parameterized case* with a remediation message,
rather than letting the whole test error out. This is exactly the
failure mode ``docs/knowledge/daemon-camera-grant-and-detection-
priming.md`` documents: enumeration keeps succeeding even when a
process (an agent session with no macOS camera TCC grant, in
particular) cannot actually open the device -- only a real open or
frame fetch reveals that.

**Private per-test daemon, real camera, throwaway config/state --
mirrors ``test_calibration_hardware.py``'s own ``daemon`` fixture
exactly**, just parameterized over ``identity`` instead of a single
module-level ``ARDUCAM_SLUG``. Every ``set_playfield``/
``set_camera_config``/``calibrate`` call below targets this private,
``tmp_path``-backed daemon instance -- never the real system daemon, and
never any file under the real ``config_dir``/``state_dir`` (this
ticket's own "camera_position is sacred" instruction is about the real,
persisted system config, which this module never touches).
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import grpc
import pytest

from aprilcam.calibration.calibration import MAX_REPROJECTION_ERROR_CM
from aprilcam.client.daemon_client import GrpcDaemon
from aprilcam.config.directories import Directories
from aprilcam.daemon import __main__ as daemon_main
from aprilcam.errors import AprilCamError
from aprilcam.proto import aprilcam_v2_pb2_grpc
from tests.aprilcam.calibration.test_calibration import main_playfield_definition
from tests.aprilcam.daemon.conftest import calibration_diagnosis, hardware_test_camera_config
from tests.aprilcam.hardware_camera import resolve_all_hardware_camera_identities

pytestmark = pytest.mark.camera

#: main-playfield is the only playfield this module has checked-in
#: ground-truth geometry for (``main_playfield_definition`` /
#: ``MAIN_PLAYFIELD_ARUCO_POSITIONS`` in ``test_calibration.py``) --
#: see the module docstring for why camera 2's secondary-playfield link
#: is out of scope here.
_TARGET_PLAYFIELD = "main-playfield"

#: Resolved once per collection, not once per parameterized test
#: function -- ``resolve_all_hardware_camera_identities`` performs a
#: real (best-effort) availability probe per candidate
#: (``hardware_camera.py``'s own informative-diagnostics precedent),
#: which is wasted real hardware I/O if repeated for every one of this
#: module's test functions. ``pytest_generate_tests`` below is called
#: once per test function in this module, so this cache keeps that cost
#: to exactly one resolution regardless of how many tests use
#: ``identity``.
_resolved_identities_cache = None


def _resolved_identities():
    global _resolved_identities_cache
    if _resolved_identities_cache is None:
        _resolved_identities_cache = resolve_all_hardware_camera_identities(
            playfield=_TARGET_PLAYFIELD
        )
    return _resolved_identities_cache


def pytest_generate_tests(metafunc):
    """Dynamic per-camera parameterization -- see the module docstring's
    "Dynamic per-camera parameterization" section. ``indirect=True`` so
    the :func:`identity` fixture below (and, through it, :func:`daemon`,
    which depends on ``identity``) receives each resolved
    ``CameraIdentity`` via ``request.param`` -- not just test functions
    that name ``identity`` directly."""
    if "identity" in metafunc.fixturenames:
        identities = _resolved_identities()
        metafunc.parametrize(
            "identity", identities, indirect=True, ids=[i.slug for i in identities]
        )


@pytest.fixture
def identity(request):
    """The one real, resolved ``CameraIdentity`` this parameterized case
    is running against -- see :func:`pytest_generate_tests`."""
    return request.param


def _short_tmp_dir(prefix: str) -> Path:
    """A short, top-level ``/tmp`` directory -- unix domain sockets have
    a ~103-character path limit on macOS/Linux, and pytest's
    ``tmp_path`` nests too deeply (matches
    ``test_calibration_hardware.py``'s identically-named helper)."""
    return Path(tempfile.mkdtemp(prefix=prefix, dir="/tmp"))


@pytest.fixture
def daemon(tmp_path, identity):
    """A private, real daemon (``daemon.__main__.build``), started and
    stopped fresh for each parameterized test function -- mirrors
    ``test_calibration_hardware.py``'s own ``daemon`` fixture exactly,
    generalized to the parameterized ``identity`` instead of a single
    module-level slug.

    **Gates on a real open/frame fetch, not on ``identity`` having
    qualified.** Immediately after the private daemon starts, this
    fetches one frame from ``identity.slug``; a failure here (e.g. this
    process has no camera TCC grant, or the device is genuinely busy
    right now) skips *this one parameterized case* cleanly rather than
    letting every assertion below fail confusingly on an unopened
    camera -- see the module docstring's "gate on a real open/frame
    fetch" section.
    """
    short_dir = _short_tmp_dir("ac-010-")
    try:
        directories = Directories(config_dir=tmp_path / "config", state_dir=tmp_path / "state")
        lock_path = short_dir / "daemon.lock"
        socket_path = short_dir / "control.sock"
        lifecycle = daemon_main.build(directories, lock_path=lock_path, socket_path=socket_path)
        lifecycle.start()
        try:
            channel = grpc.insecure_channel(f"unix:{socket_path}")
            grpc.channel_ready_future(channel).result(timeout=5)
            stub = aprilcam_v2_pb2_grpc.AprilCamV2Stub(channel)
            grpc_daemon = GrpcDaemon(stub, target="localhost")
            try:
                grpc_daemon.get_frame(identity.slug, deskewed=False, encoding="jpeg")
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
            yield grpc_daemon
        finally:
            lifecycle.stop()
    finally:
        shutil.rmtree(short_dir, ignore_errors=True)


def _link_and_calibrate(daemon: GrpcDaemon, identity):
    """Push the real main-playfield definition, link *identity*'s camera
    to it, and calibrate -- the setup every test below shares. Targets
    only the private, ``tmp_path``-backed *daemon* passed in, never the
    real system daemon (module docstring)."""
    daemon.set_playfield(main_playfield_definition())
    daemon.set_camera_config(hardware_test_camera_config(identity.slug, playfield=_TARGET_PLAYFIELD))
    return daemon.calibrate(identity.slug)


# ---------------------------------------------------------------------------
# AC: every qualifying camera against main-playfield sees all 10 required
# ArUco ids and calibrates successfully -- parameterized generalization of
# test_calibration_hardware.py's test_full_set_calibration_matches_all_
# ten_required_aruco_ids.
# ---------------------------------------------------------------------------


def test_full_set_calibration_matches_all_ten_required_aruco_ids(daemon, identity):
    result = _link_and_calibrate(daemon, identity)

    assert result.succeeded is True, (
        f"calibration failed against the real field for {identity.slug!r} -- "
        f"{calibration_diagnosis(result, identity.slug)}"
    )
    assert result.matched_ids == tuple(range(1, 11))
    assert result.missing_ids == ()


# ---------------------------------------------------------------------------
# AC: every qualifying camera's reprojection error stays under the
# threshold -- parameterized generalization of test_calibration_hardware.
# py's test_full_set_calibration_reprojection_error_stays_under_the_new_
# threshold.
# ---------------------------------------------------------------------------


def test_full_set_calibration_reprojection_error_stays_under_the_new_threshold(daemon, identity):
    result = _link_and_calibrate(daemon, identity)

    assert result.succeeded is True, (
        f"calibration failed against the real field for {identity.slug!r} -- "
        f"{calibration_diagnosis(result, identity.slug)}"
    )
    assert result.reprojection_error_cm is not None
    assert result.reprojection_error_cm < MAX_REPROJECTION_ERROR_CM, (
        f"real main-playfield solve for {identity.slug!r} measured "
        f"{result.reprojection_error_cm:.3f} cm, which does not clear the "
        f"{MAX_REPROJECTION_ERROR_CM:.3f} cm threshold."
    )


# ---------------------------------------------------------------------------
# AC (sprint 035 ticket 010): --with-intrinsics / estimate_intrinsics
# re-asserted across the parameterized camera set, not just the single
# camera test_calibration_hardware.py's own test_with_intrinsics_against_
# main_playfield_stores_a_lens_model happens to resolve. That existing
# single-camera test only ever exercises camera 3 (arducam-ov9782), which
# this ticket's own live-confirmed measurement found rejected
# ("implausible focal length") for being mounted too close to exactly
# nadir -- meaning it never actually reaches the "stores a lens model"
# assertion for real. Parameterizing across every qualifying camera gives
# this a genuine chance to prove "at least one of the qualifying cameras
# reports coplanar-constrained" empirically, camera by camera: each
# camera either hits the same known near-nadir rejection (skipped,
# individually, same diagnosed reason as the existing single-camera
# test) or actually asserts the lens-model contract below.
# ---------------------------------------------------------------------------


def test_with_intrinsics_stores_a_lens_model_or_hits_the_known_near_nadir_rejection(daemon, identity):
    daemon.set_playfield(main_playfield_definition())
    daemon.set_camera_config(
        hardware_test_camera_config(identity.slug, playfield=_TARGET_PLAYFIELD)
    )

    result = daemon.calibrate(identity.slug, estimate_intrinsics=True)

    if result.succeeded is False and "implausible focal length" in (result.reason or ""):
        pytest.skip(
            f"{identity.slug!r}'s real mount is (as of this ticket's own "
            f"live-confirmed measurement) too close to exactly nadir for "
            f"the coplanar-constrained default to produce a trustworthy "
            f"focal length -- correctly rejected rather than silently "
            f"stored (reason: {result.reason!r})."
        )

    assert result.succeeded is True, (
        f"--with-intrinsics failed against the real field for {identity.slug!r} "
        f"for a reason other than the known near-nadir degeneracy -- "
        f"{calibration_diagnosis(result, identity.slug)}"
    )
    assert result.intrinsics_estimated is True
    assert result.intrinsics_mode == "coplanar-constrained"
    assert result.elevated_features_used == 0
