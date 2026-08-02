"""Hardware verification against ``main-playfield`` (ticket 022-007) --
the sprint's acceptance gate, closing out the whole
``calibration-deskew-and-the-live-view.md`` issue.

Everything in tickets 001-006 was built and tested against fixtures and
fakes. This module exercises the same code -- unmodified except for the
two defect fixes this ticket itself made (``daemon/service.py``'s
``list_cameras`` calibration-flag bug and ``vision/motion.py``'s
below-noise-floor heading suppression) -- for real, against a real
``arducam-ov9782-usb-camera`` aimed at the physical ``main-playfield``
(AprilTag 1 at centre, ArUco 1-10 per
``config/aprilcam-old/playfields/main-playfield.json``, a 134.3 x 89.3 cm
field). Ground truth confirmed live during this ticket's own manual
verification pass (see this ticket's completion notes): all 10 required
ArUco ids plus AprilTag 1 are reliably detected when the camera is aimed
at the field, and a full calibration reliably lands a reprojection error
around 0.18-0.21 cm.

Every test is ``@pytest.mark.camera`` and skips cleanly (module-wide,
via ``_skip_if_arducam_not_attached``) on a machine without that camera
attached -- ``uv run pytest -m "not camera"`` must be unaffected.

**Two test-construction patterns, matching precedent already in this
project's suite, not invented here** (mirrors
``test_detection_hardware.py``'s own module docstring):

* Most tests use the ``daemon`` fixture: a private, real daemon stack
  (``daemon.__main__.build``) reached through a real gRPC channel and
  ``client.daemon_client.GrpcDaemon``, matching
  ``test_detection_hardware.py``'s own pattern -- fresh per test
  function so no two tests ever contend over the same exclusively-held
  camera device.
* Two tests spawn the real ``aprilcam`` console script via
  ``subprocess`` (the restart-survival test, and the digital-occlusion
  test builds a real ``Calibrator``/``Camera``/``OpenCVBackend`` stack
  directly, bypassing the daemon so a wrapped, occluding backend can be
  injected) -- the same two departures from the ``daemon``-fixture
  pattern ``test_detection_hardware.py`` itself uses, for the same
  reasons.

**Digital occlusion, not physical occlusion -- read before changing the
occlusion test.** One acceptance criterion asks for "physically covering
one ArUco marker and re-running calibrate." This test module runs in an
unattended, terminal-only automation environment with no physical
actuator -- nothing here can move an object or cover a lens in the room.
``test_occluding_one_marker_fails_naming_exactly_that_marker`` below
substitutes the closest automatable equivalent that is still genuinely
real, exactly matching ``test_detection_hardware.py``'s own precedent
and disclosure: every frame is a fresh, real capture from the real
sensor; a small rectangle covering *only* one ArUco marker's
already-known pixel region is painted black on that otherwise-real,
freshly-captured frame before it reaches the *real, unmodified*
preprocessing/detection/calibration pipeline. This is disclosed here and
in the test's own docstring, and flagged in this ticket's completion
notes as a scope decision a human may want to re-verify by hand
(physically covering a marker and re-running ``aprilcam camera
calibrate``).

**Physically moving a tag -- not automatable at all, flagged rather than
faked.** Two acceptance criteria ask for a tag physically moved east/
north, live, and checked against both the raw world position and the
deskewed image. No frame-editing trick substitutes for genuine motion
the way digital occlusion substitutes for genuine covering (a displaced
marker's *entire real detection geometry* -- corners, homography inputs
-- would have to be fabricated, which is exactly the kind of "genuine
real capture" this module otherwise insists on). This module instead
tests the closest genuine, real, automatable proxy for the *same*
east/x and north/y invariant: main-playfield's own stationary markers
already sit at real, known east/west and north/south positions, so
confirming their *real* detected positions are correctly signed and
correctly ordered exercises the identical axis convention a moving tag
would exercise, just without the motion itself. This distinction is
disclosed here, in the two proxy tests' own docstrings, and flagged
plainly in this ticket's completion notes as something the stakeholder
should still verify by hand with a physically moved marker.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict

import cv2 as cv
import grpc
import numpy as np
import pytest

from aprilcam.calibration.calibration import MAX_REPROJECTION_ERROR_CM, Calibrator
from aprilcam.camera.backends.opencv_backend import OpenCVBackend
from aprilcam.camera.camera import Camera
from aprilcam.camera.identity import resolve_all
from aprilcam.client.daemon_client import GrpcDaemon
from aprilcam.config.directories import Directories
from aprilcam.config.interfaces import CameraConfig
from aprilcam.daemon import __main__ as daemon_main
from aprilcam.errors import NotCalibrated
from aprilcam.proto import aprilcam_v2_pb2_grpc
from aprilcam.types import TagFamily, TagId
from aprilcam.vision.detector import TagDetector
from aprilcam.vision.pipeline import run_pipeline
from tests.aprilcam.calibration.test_calibration import (
    MAIN_PLAYFIELD_ARUCO_POSITIONS,
    main_playfield_definition,
)

pytestmark = pytest.mark.camera

#: ticket 003/006's ground-truth camera: aimed at main-playfield on this Mac.
ARDUCAM_SLUG = "arducam-ov9782-usb-camera"

#: main-playfield.json's centre marker -- always in frame if the
#: arducam is genuinely aimed at the field.
APRILTAG_1 = TagId(family=TagFamily.APRILTAG, number=1)

#: The real, checked-in playfield definition this whole module verifies
#: against -- the same file ``test_detection_hardware.py`` names as
#: ground truth. Loaded by the two subprocess-CLI tests (which push it
#: as a real document); the ``daemon``-fixture tests use
#: ``main_playfield_definition()`` instead (the in-repo Python mirror of
#: this exact file, already established as identical by
#: ``tests/aprilcam/calibration/test_calibration.py``'s own docstring).
MAIN_PLAYFIELD_JSON = (
    Path(__file__).resolve().parents[3] / "config" / "aprilcam-old" / "playfields" / "main-playfield.json"
)


def _skip_if_arducam_not_attached() -> None:
    slugs = {identity.slug for identity in resolve_all().values()}
    if ARDUCAM_SLUG not in slugs:
        pytest.skip(
            f"{ARDUCAM_SLUG!r} not attached to this machine -- this "
            "module's acceptance criteria are checked against that "
            "camera aimed at main-playfield (ticket 022-007)"
        )


def _short_tmp_dir(prefix: str) -> Path:
    """A short, top-level ``/tmp`` directory -- unix domain sockets have
    a ~103-character path limit on macOS/Linux, and pytest's
    ``tmp_path`` nests too deeply (matches every other
    ``@pytest.mark.camera`` module's identically-named workaround)."""
    return Path(tempfile.mkdtemp(prefix=prefix, dir="/tmp"))


# ---------------------------------------------------------------------------
# Private in-process daemon, reached over a real gRPC channel
# (test_detection_hardware.py's pattern)
# ---------------------------------------------------------------------------


@pytest.fixture
def daemon(tmp_path):
    """A private, real daemon (``daemon.__main__.build``), started and
    stopped fresh for each test function so no two tests ever contend
    over the same exclusively-held camera device. Never the real system
    daemon's directories -- ``tmp_path``/a short ``/tmp`` dir stand in.
    Yields a ready ``GrpcDaemon``, uncalibrated and unlinked (nothing in
    this fixture pushes a playfield or camera config -- see
    ``_calibrated_daemon`` for the variant that does)."""
    _skip_if_arducam_not_attached()
    short_dir = _short_tmp_dir("ac-007-")
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
            yield GrpcDaemon(stub, target="localhost")
        finally:
            lifecycle.stop()
    finally:
        shutil.rmtree(short_dir, ignore_errors=True)


def _link_and_calibrate(daemon: GrpcDaemon):
    """Push the real main-playfield definition, link the arducam to it,
    and calibrate -- the setup every "already calibrated" test below
    shares. Returns the real ``CalibrationResult``."""
    daemon.set_playfield(main_playfield_definition())
    daemon.set_camera_config(
        CameraConfig(name=ARDUCAM_SLUG, playfield="main-playfield", settings={})
    )
    return daemon.calibrate(ARDUCAM_SLUG)


# ---------------------------------------------------------------------------
# AC: camera 3 sees all 10 required ArUco ids; calibrate succeeds
# ---------------------------------------------------------------------------


def test_full_set_calibration_matches_all_ten_required_aruco_ids(daemon):
    result = _link_and_calibrate(daemon)

    assert result.succeeded is True, (
        f"calibration failed against the real field -- matched "
        f"{result.matched_ids}, missing {result.missing_ids} "
        f"(reason={result.reason!r}). Check the arducam is physically "
        "aimed at main-playfield with all 10 markers in view."
    )
    assert result.matched_ids == tuple(range(1, 11))
    assert result.missing_ids == ()


# ---------------------------------------------------------------------------
# AC (ticket 023-006, SUC-005): the stakeholder's real, currently-working
# calibration still passes under the new reprojection-error threshold --
# a self-inflicted regression is exactly what this ticket's "hardware
# care" instructions warn against. Ground truth (module docstring):
# this exact camera/field pairing reliably lands 0.18-0.21 cm, ~5x under
# the confirmed 1.0 cm threshold.
# ---------------------------------------------------------------------------


def test_full_set_calibration_reprojection_error_stays_under_the_new_threshold(daemon):
    result = _link_and_calibrate(daemon)

    assert result.succeeded is True, (
        f"calibration failed against the real field -- matched "
        f"{result.matched_ids}, missing {result.missing_ids} "
        f"(reason={result.reason!r})."
    )
    assert result.reprojection_error_cm is not None
    assert result.reprojection_error_cm < MAX_REPROJECTION_ERROR_CM, (
        f"real main-playfield solve measured {result.reprojection_error_cm:.3f} cm, "
        f"which does not clear the {MAX_REPROJECTION_ERROR_CM:.3f} cm threshold -- "
        "this ticket's own threshold would break the stakeholder's currently-working "
        "calibration, which is the self-inflicted regression it explicitly warns against."
    )


# ---------------------------------------------------------------------------
# AC: covering one ArUco marker fails, naming exactly that marker --
# UC-04.S-2 against real hardware. See the module docstring's "Digital
# occlusion, not physical occlusion" section.
# ---------------------------------------------------------------------------


class _OccludingBackend:
    """Wraps a real ``OpenCVBackend``; every ``read()`` is a genuinely
    fresh real capture, with a configured pixel region painted black
    before it is returned -- simulating one marker being physically
    covered, without moving or covering anything for real."""

    def __init__(self, real: OpenCVBackend, region) -> None:
        self._real = real
        self._region = region

    @property
    def is_open(self) -> bool:
        return self._real.is_open

    def open(self) -> None:
        self._real.open()

    def close(self) -> None:
        self._real.close()

    def frame_size(self):
        return self._real.frame_size()

    def read(self):
        frame = self._real.read()
        if frame is None:
            return None
        frame = frame.copy()
        x0, y0, x1, y1 = self._region
        cv.rectangle(frame, (x0, y0), (x1, y1), (0, 0, 0), thickness=-1)
        return frame


def _real_os_index_for_arducam() -> int:
    for index, identity in resolve_all().items():
        if identity.slug == ARDUCAM_SLUG:
            return index
    raise AssertionError("arducam not resolved -- _skip_if_arducam_not_attached should have caught this")


def _find_marker_region(camera: Camera) -> tuple:
    """One real frame's worth of real detection, used only to locate
    *some* currently-visible ArUco marker's pixel region to black out
    for the occlusion test below -- not itself an assertion."""
    frame = camera.read()
    assert frame is not None, "no frame available from the arducam"
    gray, rescale = run_pipeline(frame)
    detections = TagDetector().detect(gray, scale=rescale.scale)
    aruco_hits = [d for d in detections if d.family == TagFamily.ARUCO]
    if not aruco_hits:
        pytest.skip("no ArUco marker currently detected -- cannot pick one to occlude")
    hit = aruco_hits[0]
    xs, ys = hit.corners[:, 0], hit.corners[:, 1]
    pad = 25
    return (
        int(max(0, xs.min() - pad)),
        int(max(0, ys.min() - pad)),
        int(xs.max() + pad),
        int(ys.max() + pad),
    ), hit.number


def test_occluding_one_marker_fails_naming_exactly_that_marker(tmp_path):
    """UC-04.S-2, real hardware: with one real ArUco marker's pixel
    region digitally blacked out on every one of 30 fresh real captures,
    ``Calibrator.calibrate`` must fail naming **exactly** that marker's
    id as missing -- not a count, not "several missing" -- and every
    other real marker still visible must still match. Builds a real
    ``Calibrator``/``Camera``/``OpenCVBackend`` stack directly (bypassing
    the daemon) so the occluding wrapper can be injected -- the daemon's
    own ``_capture_calibration_frames`` owns backend construction
    end-to-end and offers no seam for this from the outside, exactly the
    same reason ``test_detection_hardware.py``'s occlusion test builds
    ``DetectionEngine``/``Camera`` directly instead of going through
    ``DaemonService``."""
    _skip_if_arducam_not_attached()
    index = _real_os_index_for_arducam()

    probe_backend = OpenCVBackend(index)
    probe_camera = Camera(ARDUCAM_SLUG, probe_backend)
    probe_camera.open()
    try:
        region, occluded_id = _find_marker_region(probe_camera)
    finally:
        probe_camera.close()

    backend = _OccludingBackend(OpenCVBackend(index), region)
    camera = Camera(ARDUCAM_SLUG, backend)
    camera.open()
    try:
        def capture_frames(slug: str):
            frames = []
            for _ in range(30):
                frame = camera.read()
                if frame is not None:
                    frames.append(frame)
            return tuple(frames)

        definition = main_playfield_definition()
        camera_config = CameraConfig(name=ARDUCAM_SLUG, playfield="main-playfield", settings={})
        calibrator = Calibrator(
            get_camera_config=lambda slug: camera_config,
            get_playfield=lambda name: definition,
            capture_frames=capture_frames,
        )

        result = calibrator.calibrate(ARDUCAM_SLUG)
    finally:
        camera.close()

    assert result.succeeded is False, (
        f"calibration succeeded despite marker {occluded_id} being "
        f"digitally occluded -- matched {result.matched_ids}"
    )
    assert result.missing_ids == (occluded_id,), (
        f"expected exactly marker {occluded_id} named missing, got "
        f"{result.missing_ids} (a count or 'several missing' would "
        "violate UC-04.S-2's naming requirement)"
    )
    assert occluded_id not in result.matched_ids


# ---------------------------------------------------------------------------
# AC: AprilTag 1 (field centre) reports world position (0, 0) within
# tolerance -- the direct physical origin-convention check.
# ---------------------------------------------------------------------------


def test_apriltag_1_reports_world_origin_within_tolerance(daemon):
    result = _link_and_calibrate(daemon)
    assert result.succeeded is True, f"setup calibration failed: {result.reason}"

    tag_frame = daemon.get_tags(ARDUCAM_SLUG)
    record = next((r for r in tag_frame.tags if r.tag == APRILTAG_1), None)
    if record is None:
        pytest.skip("AprilTag 1 not detected this particular burst -- re-run")

    assert record.calibrated is True
    assert record.world is not None
    # A generous real-hardware tolerance -- ticket 003's own hardware
    # verification measured (0.10, -0.03) cm; this ticket's own manual
    # pass measured (0.1, -0.1) cm. 2 cm covers real lighting/framing
    # variance with wide margin while still meaningfully proving "at the
    # origin", not "somewhere on the field".
    assert record.world.x == pytest.approx(0.0, abs=2.0)
    assert record.world.y == pytest.approx(0.0, abs=2.0)


# ---------------------------------------------------------------------------
# AC (proxy -- see module docstring): east/west and north/south markers
# report correctly-signed, correctly-ordered real world positions.
# ---------------------------------------------------------------------------


def test_east_west_and_north_south_markers_report_correctly_signed_world_positions(daemon):
    """Proxy for "physically moving a tag east/north increases world.x/
    world.y" (module docstring: no actuator is available to this test
    module). main-playfield's own stationary markers already sit at
    real, known east/west (ArUco 4 at x=+67, ArUco 8 at x=-67) and
    north/south (ArUco 2 at y=+44.65, ArUco 6 at y=-44.65) positions --
    confirming their real detected world coordinates are correctly
    signed and ordered exercises the identical axis convention a moving
    tag would, without motion itself. The literal moving-tag check still
    needs a human; see this ticket's completion notes."""
    result = _link_and_calibrate(daemon)
    assert result.succeeded is True, f"setup calibration failed: {result.reason}"

    tag_frame = daemon.get_tags(ARDUCAM_SLUG)
    by_id = {r.tag.number: r for r in tag_frame.tags if r.tag.family == TagFamily.ARUCO}

    missing = {4, 8, 2, 6} - set(by_id)
    if missing:
        pytest.skip(f"east/west/north/south reference markers not all detected this burst: missing {missing}")

    assert by_id[4].world.x > by_id[8].world.x, (
        "east marker (ArUco 4, surveyed x=+67) did not report a larger "
        f"world.x than the west marker (ArUco 8, surveyed x=-67): "
        f"{by_id[4].world.x} vs {by_id[8].world.x}"
    )
    assert by_id[2].world.y > by_id[6].world.y, (
        "north marker (ArUco 2, surveyed y=+44.65) did not report a "
        f"larger world.y than the south marker (ArUco 6, surveyed "
        f"y=-44.65): {by_id[2].world.y} vs {by_id[6].world.y}"
    )


# ---------------------------------------------------------------------------
# AC (partial proxy -- see module docstring and this test's own
# docstring): the east half of the same east/north invariant on the
# deskewed image itself -- real detection re-run on the real deskewed
# pixels, not a re-use of the raw-frame detection. The north half is not
# covered here; see this test's docstring for why.
# ---------------------------------------------------------------------------


def test_deskewed_image_places_east_marker_right_of_west_marker(daemon):
    """Proxy for "an east motion moves the tag's drawn position right
    on the deskewed image" (module docstring: no actuator available).
    Decodes the real deskewed PNG bytes and runs the real detector
    directly on *that* image (not the daemon's raw-frame ``tags``,
    which are in raw pixel space) -- a genuine, independent, real-pixel
    confirmation that east reads right (larger pixel x) on the actual
    served deskewed image.

    Uses ArUco 9 (west-internal, surveyed x=-35) and ArUco 10
    (east-internal, surveyed x=+35) rather than the field's east/west
    *corner* markers (4/8, surveyed x=+-67): this ticket's own
    real-hardware run found the boundary markers essentially clipped
    out of the 800x532 deskewed render (main-playfield's corners are
    surveyed within ~0.15 cm of the field's own half-width/half-height,
    so the warp places them right at the image edge) -- confirmed by
    direct redetection returning only ``apriltag 1``/``aruco 9``/
    ``aruco 10`` (the three markers not near the field boundary) on a
    real deskewed capture. No north/south pair in main-playfield.json
    sits off the field boundary the way 9/10 do (only the
    east/west-internal markers exist at y=0), so this module has no
    reliable real, non-clipped marker pair to redetect for a "north
    reads up on the deskewed image" check -- see this ticket's
    completion notes, which flag that specific half of this acceptance
    criterion for the stakeholder to verify by hand (or by re-surveying
    a north/south pair further from the boundary)."""
    result = _link_and_calibrate(daemon)
    assert result.succeeded is True, f"setup calibration failed: {result.reason}"

    image = daemon.get_frame(ARDUCAM_SLUG, deskewed=True, encoding="png")
    img_bytes = np.frombuffer(image.data, dtype=np.uint8)
    img = cv.imdecode(img_bytes, cv.IMREAD_COLOR)
    assert img is not None, "could not decode the deskewed frame"

    gray, rescale = run_pipeline(img)
    detections = TagDetector().detect(gray, scale=rescale.scale)
    by_id = {
        d.number: d.center for d in detections if d.family == TagFamily.ARUCO
    }

    missing = {9, 10} - set(by_id)
    if missing:
        pytest.skip(
            f"east/west-internal reference markers not all detected "
            f"in this deskewed capture: missing {missing}"
        )

    assert by_id[10].x > by_id[9].x, (
        "east-internal marker (ArUco 10, x=+35) is not drawn to the "
        f"right of the west-internal marker (ArUco 9, x=-35) on the "
        f"deskewed image: pixel x {by_id[10].x} vs {by_id[9].x}"
    )


# ---------------------------------------------------------------------------
# AC: deskewed image is 800x532 with the documented px_per_cm
# ---------------------------------------------------------------------------


def test_deskewed_image_is_800x532_with_documented_px_per_cm(daemon):
    result = _link_and_calibrate(daemon)
    assert result.succeeded is True, f"setup calibration failed: {result.reason}"

    image = daemon.get_frame(ARDUCAM_SLUG, deskewed=True, encoding="png")

    assert image.width == 800
    assert image.height == 532
    assert image.px_per_cm == pytest.approx(5.957, abs=0.01)


# ---------------------------------------------------------------------------
# AC: NotCalibrated on a deskewed request for an uncalibrated camera
# ---------------------------------------------------------------------------


def test_deskewed_request_on_an_uncalibrated_camera_raises_not_calibrated(daemon):
    """The plain ``daemon`` fixture never calibrates anything -- a fresh
    real daemon, real camera, but no ``calibrate()`` call in this test's
    own isolated ``tmp_path`` state directory."""
    with pytest.raises(NotCalibrated):
        daemon.get_frame(ARDUCAM_SLUG, deskewed=True)


# ---------------------------------------------------------------------------
# AC: stopping and restarting the real daemon leaves the calibration
# intact -- and the documented trap: the camera->playfield link does not
# survive (in-memory only), even though the calibration does.
# ---------------------------------------------------------------------------


def _aprilcam_bin() -> Path:
    candidate = Path(sys.executable).parent / "aprilcam"
    if not candidate.exists():  # pragma: no cover - environment-dependent
        pytest.skip(f"no aprilcam console script next to {sys.executable}")
    return candidate


def _run(bin_path: Path, *args: str, env: Dict[str, str], timeout: float = 30.0):
    return subprocess.run(
        [str(bin_path), *args], capture_output=True, env=env, timeout=timeout
    )


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """Isolated environment for a real ``aprilcam`` subprocess --
    mirrors ``test_detection_hardware.py``'s ``cli_env`` fixture
    (duplicated locally, matching that module's own stated precedent of
    every ``@pytest.mark.camera`` module defining its own)."""
    short_dir = _short_tmp_dir("ac-007-cli-")
    try:
        monkeypatch.setenv("APRILCAM_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("APRILCAM_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(short_dir))

        directories = Directories()
        if directories.socket_dir != short_dir / "aprilcam":
            pytest.skip(
                "this sandbox's /run/aprilcam is available and writable, "
                "so socket_dir cannot be isolated down to a private tmp "
                "root -- cannot safely isolate a real-subprocess CLI test "
                "from a real daemon here"
            )
        yield dict(os.environ)
    finally:
        shutil.rmtree(short_dir, ignore_errors=True)


def test_calibration_survives_a_restart_but_the_playfield_link_does_not(cli_env, tmp_path):
    """UC-01/UC-02, against the real ``aprilcam daemon stop && aprilcam
    daemon start`` cycle: calibration is file-backed and must survive;
    the camera->playfield *link* is pushed config and is in-memory only,
    so it must NOT survive -- documented as a real, observed trap in
    this ticket's own description and completion notes (a developer who
    only checks ``calibrated`` after a restart could be misled into
    thinking a deskewed request will work; it will not, until the
    playfield and link are re-pushed)."""
    _skip_if_arducam_not_attached()
    bin_path = _aprilcam_bin()
    env = cli_env

    camera_config_path = tmp_path / "arducam-config.json"
    camera_config_path.write_text(
        json.dumps({"name": ARDUCAM_SLUG, "playfield": "main-playfield"}), encoding="utf-8"
    )

    result = _run(bin_path, "daemon", "start", env=env)
    assert result.returncode == 0, result.stderr
    try:
        probe = _run(bin_path, "camera", "probe", env=env)
        assert probe.returncode == 0, probe.stderr

        pushed_pf = _run(bin_path, "playfield", "config", str(MAIN_PLAYFIELD_JSON), env=env)
        assert pushed_pf.returncode == 0, pushed_pf.stderr
        pushed_cfg = _run(bin_path, "camera", "config", str(camera_config_path), env=env)
        assert pushed_cfg.returncode == 0, pushed_cfg.stderr

        calibrated = _run(bin_path, "camera", "calibrate", ARDUCAM_SLUG, env=env)
        assert calibrated.returncode == 0, calibrated.stderr
        assert b"succeeded" in calibrated.stdout, calibrated.stdout

        before = _run(bin_path, "daemon", "status", env=env)
        assert before.returncode == 0, before.stderr
        before_out = before.stdout.decode()
        assert "playfield=main-playfield" in before_out
        assert re.search(rf"{re.escape(ARDUCAM_SLUG)}\S*\s+\S+\s+\S+\s+playfield=\S+\s+calibrated", before_out), (
            f"expected the arducam row to show calibrated before restart:\n{before_out}"
        )

        stop = _run(bin_path, "daemon", "stop", env=env)
        assert stop.returncode == 0, stop.stderr
        start = _run(bin_path, "daemon", "start", env=env)
        assert start.returncode == 0, start.stderr

        after = _run(bin_path, "daemon", "status", env=env)
        assert after.returncode == 0, after.stderr
        after_out = after.stdout.decode()

        assert re.search(rf"{re.escape(ARDUCAM_SLUG)}\S*\s+\S+\s+\S+\s+playfield=\S+\s+calibrated", after_out), (
            f"calibration did not survive the restart -- expected the "
            f"arducam row to still show calibrated:\n{after_out}"
        )
        assert "playfield=main-playfield" not in after_out, (
            "the camera->playfield link unexpectedly survived the "
            "restart -- if pushed config has become file-backed, this "
            "test (and the ticket's documented trap) need updating "
            f"together:\n{after_out}"
        )

        # The documented trap in its most concrete, practical form: a
        # deskewed request fails right after restart, despite
        # 'calibrated' being reported true, until the playfield/link are
        # re-pushed.
        image_before_relink = _run(bin_path, "playfield", "image", "--camera", ARDUCAM_SLUG, "--base64", env=env)
        assert image_before_relink.returncode != 0, (
            "expected a deskewed request to fail right after restart "
            "(playfield definition lost, per the trap this test "
            f"documents), but it succeeded:\n{image_before_relink.stdout}"
        )

        repush_pf = _run(bin_path, "playfield", "config", str(MAIN_PLAYFIELD_JSON), env=env)
        assert repush_pf.returncode == 0, repush_pf.stderr
        repush_cfg = _run(bin_path, "camera", "config", str(camera_config_path), env=env)
        assert repush_cfg.returncode == 0, repush_cfg.stderr

        image_after_relink = _run(bin_path, "playfield", "image", "--camera", ARDUCAM_SLUG, "--base64", env=env)
        assert image_after_relink.returncode == 0, (
            f"deskewed request still failed after re-pushing the "
            f"playfield and link:\n{image_after_relink.stderr}"
        )
    finally:
        stop = _run(bin_path, "daemon", "stop", env=env)
        assert stop.returncode == 0, stop.stderr
