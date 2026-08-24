"""Hardware verification: a printed, generated tag detects on the real
field (sprint 028 Hardware Constraints item 1; ticket 006).

**Scope, deliberately narrow.** ``TagGenerator``'s own correctness --
dimensions, DPI, dictionary content, the three UC-21 failure contracts --
is fully verified headless in ``tests/aprilcam/tags/test_generator.py``
(ticket 001). This module's only job is the one leg that genuinely cannot
be automated: whether a *printed copy* of a generated tag is reliably
detected once a human has printed it and placed it in view of a real
camera. Printing is a physical, manual act -- no code in this repository,
this test included, can drive a printer or place paper in front of a lens.

**How this test can still be real, not decorative.** It generates a real
tag image via the real ``TagGenerator`` (the one piece that *is*
automatable), then polls the real detection pipeline through a private,
real daemon (mirrors ``test_mobile_tags_hardware.py``'s ``daemon``
fixture) looking for that exact tag. If a human has, in fact, printed the
generated PNG at the size named in the skip/failure message and placed it
in front of the camera before this test runs, it genuinely detects it and
passes for real -- this has happened before in this project's history
(sprint 027 ticket 008's own experience: a hardware constraint assumed
"nothing this sprint can do" turned out, on actually running the suite,
to do something real). If nobody has done that -- the expected case on an
ordinary CI runner or an unattended dev box -- it skips cleanly with an
explicit message naming exactly what a human needs to do to make it run
for real, rather than silently passing or asserting a false failure.

Every test is ``@pytest.mark.camera`` and skips cleanly (module-wide, via
``_skip_if_arducam_not_attached``) on a machine with no camera that is
genuinely configured and calibrated against ``main-playfield`` (discovered
at run time via ``tests/aprilcam/hardware_camera.py``, never a hardcoded
device name) -- ``uv run pytest -m "not camera"`` is unaffected.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path

import grpc
import pytest

from aprilcam.client.daemon_client import GrpcDaemon
from aprilcam.config.directories import Directories
from aprilcam.daemon import __main__ as daemon_main
from aprilcam.daemon.detection import CONFIRMATION_STREAK
from aprilcam.errors import AprilCamError
from aprilcam.proto import aprilcam_v2_pb2_grpc
from aprilcam.tags.generator import TagGenerator
from aprilcam.types import TagFamily, TagId
from tests.aprilcam.hardware_camera import resolve_hardware_camera_slug

pytestmark = pytest.mark.camera

#: Resolved lazily, once per test, by :func:`_skip_if_arducam_not_attached`
#: against this machine's real, persisted system state (see
#: tests/aprilcam/hardware_camera.py) -- never hardcoded.
ARDUCAM_SLUG: str = ""

#: ArUco 42 -- deliberately outside main-playfield's surveyed 1-10 corner
#: set (``test_mobile_tags_hardware.py``'s ``_SURVEYED_ARUCO_IDS``), so a
#: positive detection here can only be this test's own freshly-generated,
#: freshly-printed tag -- never one of the field's permanent markers.
_PRINT_TARGET = TagId(family=TagFamily.ARUCO, number=42)
_PRINT_SIZE_CM = 8.0


def _skip_if_arducam_not_attached() -> None:
    global ARDUCAM_SLUG
    ARDUCAM_SLUG = resolve_hardware_camera_slug()


def _short_tmp_dir(prefix: str) -> Path:
    """A short, top-level ``/tmp`` directory -- unix domain sockets have
    a ~103-character path limit on macOS/Linux, and pytest's ``tmp_path``
    nests too deeply (matches every other ``@pytest.mark.camera``
    module's identically-named workaround)."""
    return Path(tempfile.mkdtemp(prefix=prefix, dir="/tmp"))


@pytest.fixture
def daemon(tmp_path):
    """A private, real daemon (``daemon.__main__.build``), started and
    stopped fresh for this test so it never contends with another test
    over the same exclusively-held camera device. Never the real system
    daemon's directories -- mirrors ``test_mobile_tags_hardware.py``'s
    ``daemon`` fixture exactly."""
    _skip_if_arducam_not_attached()
    short_dir = _short_tmp_dir("ac-006-taggen-")
    try:
        directories = Directories(
            config_dir=tmp_path / "config", state_dir=tmp_path / "state"
        )
        lock_path = short_dir / "daemon.lock"
        socket_path = short_dir / "control.sock"
        lifecycle = daemon_main.build(
            directories, lock_path=lock_path, socket_path=socket_path
        )
        lifecycle.start()
        try:
            channel = grpc.insecure_channel(f"unix:{socket_path}")
            grpc.channel_ready_future(channel).result(timeout=5)
            stub = aprilcam_v2_pb2_grpc.AprilCamV2Stub(channel)
            grpc_daemon = GrpcDaemon(stub, target="localhost")
            # _skip_if_arducam_not_attached (above) only proves ARDUCAM_SLUG
            # is configured/calibrated on this machine's real, persisted
            # state -- never that this process can actually open it right
            # now (tests/aprilcam/hardware_camera.py's own "enumeration is
            # not availability" contract). This module's only test needs a
            # real camera open, so gate here, matching tests/aprilcam/
            # calibration/test_full_calibrate_hardware.py's own ``daemon``
            # fixture.
            try:
                grpc_daemon.get_frame(ARDUCAM_SLUG, deskewed=False, encoding="jpeg")
            except AprilCamError as exc:
                pytest.skip(
                    f"{ARDUCAM_SLUG!r} qualifies (config + calibration present "
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


def test_printed_generated_tag_is_detected_on_real_hardware(daemon, tmp_path):
    """Generate ``ArUco 42`` via the real :class:`TagGenerator` (the
    automatable half of UC-21's printed-tag verification bullet), then
    poll the real detection pipeline for it.

    **The manual step this test cannot perform**: printing
    ``written[0]`` (an 8cm-edge PNG at 300 DPI) on paper and placing it
    in view of the real, discovered camera under test (see
    ``tests/aprilcam/hardware_camera.py``), aimed roughly at
    ``main-playfield`` or held up directly in front of the lens, before
    this test runs. If that has happened, the polling loop below finds
    it for real and this test passes for real. If it has not -- the
    expected state on an unattended CI runner or dev box -- this test
    skips cleanly, naming exactly that step, rather than asserting a
    false failure or silently passing.
    """
    out_dir = tmp_path / "printed-tag"
    written = TagGenerator().generate(
        _PRINT_TARGET.family,
        (_PRINT_TARGET.number,),
        size_cm=_PRINT_SIZE_CM,
        out_dir=out_dir,
        dpi=300,
    )
    assert len(written) == 1
    assert written[0].exists(), f"TagGenerator did not write {written[0]}"

    # Ticket 024-007's finding, reused here (see test_mobile_tags_hardware.py's
    # own _prime): a brand-new DetectionEngine's first burst can never
    # report anything confirmed (a streak of 1 never meets
    # CONFIRMATION_STREAK), so warm up with throwaway bursts before the
    # asserted-on polling loop below.
    for _ in range(max(CONFIRMATION_STREAK - 1, 0)):
        daemon.get_tags(ARDUCAM_SLUG)

    attempts = 10
    delay = 1.0
    last_observed: list = []
    for _ in range(attempts):
        frame = daemon.get_tags(ARDUCAM_SLUG)
        last_observed = [(r.tag.family.value, r.tag.number) for r in frame.tags]
        if any(r.tag == _PRINT_TARGET for r in frame.tags):
            print(
                f"\n[028-006 hardware] printed {_PRINT_TARGET.family.value} "
                f"{_PRINT_TARGET.number} detected for real "
                f"(observed this burst: {last_observed})"
            )
            return
        time.sleep(delay)

    pytest.skip(
        f"generated tag image at {written[0]} was not detected by "
        f"{ARDUCAM_SLUG!r} across {attempts} attempts over "
        f"~{attempts * delay:.0f}s -- this criterion requires a human to "
        f"physically print {written[0]} (an {_PRINT_SIZE_CM:g}cm-edge "
        "ArUco 4x4 marker, id 42, at 300 DPI) and hold or place it in "
        "view of the camera before re-running this test; printing itself "
        "cannot be automated in this environment (sprint 028 Hardware "
        f"Constraints item 1). Last observed tags this run: "
        f"{last_observed or '(none)'}"
    )
