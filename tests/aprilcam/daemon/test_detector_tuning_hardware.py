"""Camera-5 (``arducam-ov9281-usb-camera``) ``DetectorConfig`` tuning --
ticket 035-002, closing out the source issue
``clasi/sprints/035-.../issues/apriltag-1-detects-unreliably-on-arducam-
ov9281-usb-camera.md``: AprilTag 1 decodes on only 1-3 of 10 real polls
on camera 5 across a 150-500 exposure sweep, while all 10 ArUco markers
decode on effectively every poll on the *same* frames -- exposure is
already ruled out as the sole cause (no clean monotonic trend). This
module investigates whether per-camera
``vision.detector.DetectorConfig`` tuning
(``quad_decimate``/``corner_refine``, and if warranted the AprilTag-quad
earned constants) measurably improves AprilTag 1's rate without
regressing camera 5's ArUco recall, using the per-slug seam ticket
035-001 built (``daemon.detection._DETECTOR_CONFIG_OVERRIDES``).

**Camera 5 is targeted by its exact, known slug -- not discovered via
``tests/aprilcam/hardware_camera.py``.** That module's own docstring
records why it cannot be reused unmodified here: its discovery answers
"which attached camera is configured *and* calibrated against
``main-playfield`` in this machine's real, persisted system state," and
its own confirmed finding is that ``arducam-ov9281-usb-camera`` (this
exact camera) is attached and even independently calibrated on this
project's dev box, but **carries no playfield link at all** in that
persisted state -- so ``resolve_hardware_camera_slug()`` would never
resolve to it. This investigation is about one specific, already-named
physical device (the source issue names it throughout), so
:data:`CAMERA_5_SLUG` is a deliberate, narrow exception to the
"never hardcode a device name" rule the other hardware modules follow
for good reason (multiple qualifying cameras, contention) -- neither
applies here, since this ticket is not "any camera on this playfield,"
it is "this one."

**Two pieces, matching this ticket's own Implementation Plan
("manual/exploratory, not itself a committed test" + "new committed
regression test"):**

* :func:`sweep_detector_configs` -- a small, real, manual/exploratory
  sweep across :data:`_CANDIDATE_CONFIGS`, run directly against camera 5
  by a human at the physical rig (see its own docstring for the
  invocation). Not ``test_``-prefixed, so pytest never collects it; it
  prints a real per-config comparison table and returns the raw counts
  for a human to read and decide from -- it does not itself pick a
  winner or write anything.
* ``test_apriltag_1_detection_rate_and_aruco_recall_match_pinned_camera_5_baseline``
  -- the committed ``@pytest.mark.camera`` regression test. It is meant
  to pin whichever real ``(config, apriltag_1_rate, aruco_recall)``
  triple a human's sweep run settles on, failing on **either** direction
  of drift (regression *or* an unexplained improvement -- this ticket's
  own acceptance criteria are explicit that both must be visible, not
  just regressions, so a quietly-changed camera/lens/lighting condition
  is caught either way instead of only the "got worse" half).

**Real-hardware access was not available while building this module,
and no numbers below are fabricated to fill that gap.** Camera 5
enumerates fine (``camera.identity.resolve_all()`` sees the physical
device over AVFoundation), but opening it for capture from this
session's process raised, verbatim::

    OpenCV: not authorized to capture video (status 0), requesting...
    OpenCV: camera failed to properly initialize!
    RuntimeError: failed to open camera at index 1

This is a macOS TCC camera-capture authorization gap for the specific
process running this automated session, not evidence the camera is
unplugged, broken, or that the tuning hypothesis is wrong -- see
``docs/knowledge/2026-08-23-camera-5-detector-tuning-blocked-by-sandbox-
camera-permission.md`` for the full writeup, including the confirming
enumerate-succeeds/open-fails split that pins this as a capture-
authorization gap rather than a "device absent" one. Per this ticket's
own explicit contingency ("if you cannot measure on real hardware, do
not fabricate numbers -- write the test, verify it skips correctly, and
report clearly that the measurement itself is a stakeholder step at the
rig"), :data:`PINNED_APRILTAG_1_HITS` and friends below are ``None``
sentinels, not guessed numbers, and
``daemon.detection._DETECTOR_CONFIG_OVERRIDES`` stays exactly as ticket
001 left it (empty) -- adding an entry with untested field values would
be indistinguishable from guessing, which this ticket is explicitly told
not to do.

**No isolated ``APRILCAM_CONFIG_DIR``/``APRILCAM_STATE_DIR``/
``XDG_RUNTIME_DIR`` roots here, deliberately.** The dispatch context for
this ticket names the isolated-roots pattern
(``tests/aprilcam/daemon/test_camera_geometry_hardware.py:204-227``) as
the proven shape for hardware work that touches persisted state --
correct for a test that spins up a real daemon subprocess or pushes
``camera config``/``playfield config`` documents that land on disk under
``Directories()``. This module does neither: every capture here goes
through a directly-constructed ``Camera`` object, and ``Camera.
apply_config`` (see its own docstring) is a pure in-memory UVC push
through the backend's ``get_property``/``set_property`` -- no
``Directories()`` read or write occurs anywhere in this file's own code
path, so there is no ``~/.config/aprilcam``/``~/.local/share/aprilcam``
state for an isolated root to protect against. ``camera_position`` stays
untouched by construction, not by an isolation fixture.

**What a human with real camera-5 access does next:**

1. Aim camera 5 at ``main-playfield`` (as the source issue's own
   measurements already were) and run, from the repo root::

       uv run python -m tests.aprilcam.daemon.test_detector_tuning_hardware

   This prints a real sweep table across :data:`_CANDIDATE_CONFIGS`.
2. Pick the config with the best AprilTag-1 rate that does not visibly
   hurt ArUco recall (the source issue's and this module's own
   convention: worst-of-N-bursts ArUco count, not just the mean).
3. If a config measurably beats plain defaults: add it to
   ``daemon.detection._DETECTOR_CONFIG_OVERRIDES[CAMERA_5_SLUG]``. If
   nothing measurably helps: leave the table empty and update this
   module's/the ticket's documentation to say so plainly -- either
   outcome is an acceptable, honest result for this ticket (its own
   Description already anticipates a "documented physical limitation"
   ending as fully acceptable).
4. Fill in :data:`PINNED_DETECTOR_CONFIG_NAME`,
   :data:`PINNED_APRILTAG_1_HITS`, :data:`PINNED_ARUCO_MIN_RECALL` below
   from that same real run, then re-run
   ``uv run pytest tests/aprilcam/daemon/test_detector_tuning_hardware.py -m camera -v``
   -- the regression test starts actually pinning behavior instead of
   skipping.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

import pytest

from aprilcam.camera.backends.opencv_backend import OpenCVBackend
from aprilcam.camera.backends.uvc_util import UvcUtilBackend
from aprilcam.camera.camera import Camera
from aprilcam.camera.identity import resolve_all
from aprilcam.daemon.detection import _DETECTOR_CONFIG_OVERRIDES
from aprilcam.types import TagFamily
from aprilcam.vision.detector import DetectorConfig, TagDetector
from aprilcam.vision.pipeline import run_pipeline
from tests.aprilcam.daemon.conftest import hardware_test_camera_config

logger = logging.getLogger(__name__)

pytestmark = pytest.mark.camera

#: The one physical device this whole module investigates -- see the
#: module docstring for why this is a deliberate, narrow exception to
#: "never hardcode a device name."
CAMERA_5_SLUG = "arducam-ov9281-usb-camera"

#: main-playfield's marker set (``config/aprilcam-old/playfields/
#: main-playfield.json``): AprilTag 1 at centre, ArUco 1-10 (perimeter
#: 1-8 plus internal 9-10) -- matches ``test_detection_hardware.py``'s
#: own ground truth for the same field.
EXPECTED_ARUCO_COUNT = 10

#: Bursts per config -- matches the source issue's own "10 polls" sweep
#: convention exactly, so a sweep result is directly comparable to the
#: issue's recorded 1/10, 3/10, 2/10 baseline numbers.
_BURSTS_PER_CONFIG = 10

#: Small, hand-picked candidate grid -- not a full cartesian sweep of
#: every ``DetectorConfig`` field, matching this ticket's Implementation
#: Plan ("sweep a small grid"). Each entry targets a specific hypothesis:
#: - ``default``: the control -- today's byte-identical behaviour for
#:   every camera, including camera 5.
#: - ``corner_refine_contour``/``corner_refine_none``: alternate corner-
#:   refinement modes -- subpixel refinement (today's default for every
#:   camera) can behave worse than a coarser mode on a marginal/noisy
#:   image; both directions (coarser modes) are worth ruling in or out.
#: - ``quad_decimate_1_5``: decimation coarser than the (already-finest,
#:   floor-clamped-at-1.0) default -- included as a sanity control per
#:   this ticket's acceptance criteria explicitly naming
#:   ``quad_decimate`` as one of the parameters to try, even though the
#:   *a priori* expectation is that coarsening only ever hurts a
#:   marginal tag.
#: - ``loosened_april_quad_thresholds``: directly targets the source
#:   issue's own hypothesis ("AprilTag 36h11 encodes more bits per unit
#:   area... more sensitive to marginal resolution/focus/decode margin")
#:   by loosening the three AprilTag-specific quad-detector earned
#:   constants (lower min white/black diff, lower min cluster pixels,
#:   higher max line-fit MSE all mean "accept a rougher quad") -- the
#:   ArUco-recall side of every measurement below is what catches this
#:   trading away too much precision (more false accepts) if it does.
_CANDIDATE_CONFIGS: Dict[str, DetectorConfig] = {
    "default": DetectorConfig(),
    "corner_refine_contour": DetectorConfig(corner_refine="contour"),
    "corner_refine_none": DetectorConfig(corner_refine="none"),
    "quad_decimate_1_5": DetectorConfig(quad_decimate=1.5),
    "loosened_april_quad_thresholds": DetectorConfig(
        april_min_wb_diff=1.0, april_min_cluster_pixels=3, april_max_line_fit_mse=40.0
    ),
}

#: Real-hardware baseline, ``None`` until a human fills these in from a
#: real :func:`sweep_detector_configs` run at the physical rig -- see the
#: module docstring's numbered "what a human does next" section. ``None``
#: means "not yet measured," not "measured as zero" -- the regression
#: test below skips cleanly on that distinction rather than asserting
#: against a fabricated number.
PINNED_DETECTOR_CONFIG_NAME: Optional[str] = None
PINNED_APRILTAG_1_HITS: Optional[int] = None  # out of _BURSTS_PER_CONFIG
PINNED_ARUCO_MIN_RECALL: Optional[int] = None  # out of EXPECTED_ARUCO_COUNT, worst burst


def _resolve_camera_5_index() -> Optional[Tuple[int, str]]:
    """``resolve_all()``'s index for :data:`CAMERA_5_SLUG`, plus its
    real ``device_name`` (``UvcUtilBackend`` needs the AVFoundation
    device name, not the slug), or ``None`` if not currently attached."""
    identities = resolve_all()
    for index, identity in identities.items():
        if identity.slug == CAMERA_5_SLUG:
            return index, identity.device_name
    return None


def _open_camera_5() -> Camera:
    """A real, opened :class:`Camera` for camera 5, wired with a real
    ``UvcUtilBackend`` control backend (matching every other direct
    ``Camera`` construction in this test directory -- pushing
    ``exposure``/``auto_exposure`` needs it; see ``conftest.py``'s module
    docstring). Raises (never skips itself -- see
    :func:`_skip_if_camera_5_unusable`/the ``camera_5`` fixture for the
    skip-cleanly wrapper both the sweep tool and the regression test
    actually use)."""
    resolved = _resolve_camera_5_index()
    if resolved is None:
        raise RuntimeError(f"{CAMERA_5_SLUG!r} not attached")
    index, device_name = resolved
    camera = Camera(CAMERA_5_SLUG, OpenCVBackend(index), control_backend=UvcUtilBackend(device_name))
    camera.open()
    return camera


@pytest.fixture
def camera_5():
    """Skip-cleanly wrapper around :func:`_open_camera_5` for the
    committed regression test. Distinguishes, in the skip message, "not
    attached" from "attached but not authorized to capture" -- the
    latter is exactly what this module's own docstring found while being
    built (a real macOS TCC gap in an unattended/sandboxed automation
    session, not a real hardware absence), and a future run hitting the
    same wall should get a message that points at permissions, not at
    "go buy a camera."
    """
    resolved = _resolve_camera_5_index()
    if resolved is None:
        pytest.skip(
            f"{CAMERA_5_SLUG!r} not attached to this machine -- resolve_all() "
            f"found: {sorted(i.slug for i in resolve_all().values())}. This "
            "module investigates one specific physical camera named in the "
            "source issue, not \"any attached camera\"."
        )
    index, device_name = resolved
    camera = Camera(CAMERA_5_SLUG, OpenCVBackend(index), control_backend=UvcUtilBackend(device_name))
    try:
        camera.open()
    except Exception as exc:
        pytest.skip(
            f"{CAMERA_5_SLUG!r} is enumerated (AVFoundation sees the "
            f"physical device at index {index}) but could not be opened "
            f"for capture: {exc!r}. In an unattended/sandboxed automation "
            "environment this is normally a macOS TCC camera-permission "
            "denial for the process running pytest, not a real hardware "
            "problem -- confirmed while building this module: OpenCV logs "
            "'not authorized to capture video' and the open raises "
            "RuntimeError even though the same device shows up in "
            "resolve_all(). Grant camera access to this process (System "
            "Settings > Privacy & Security > Camera) or run this suite "
            "from an already-authorized terminal session at the physical "
            "rig."
        )
    try:
        yield camera
    finally:
        camera.close()


def _measure_config(camera: Camera, config: Optional[DetectorConfig], *, bursts: int) -> Tuple[int, List[int]]:
    """One real config's worth of measurement: *bursts* fresh real
    captures against *camera*, run through a single ``TagDetector(config)``
    built once up front (never per-burst -- ``vision.detector``'s own
    module docstring: building an ``ArucoDetector`` is not free).

    Returns ``(apriltag_1_hits, aruco_counts)`` -- the number of bursts
    (out of *bursts*) AprilTag 1 was seen in, and the distinct-ArUco-id
    count seen in *each* burst (a list, not a sum) so a caller can check
    the *worst* burst's recall, matching the source issue's own "all 10
    ArUco markers detect on effectively every poll" framing (a per-poll
    claim, not an across-session union).
    """
    detector = TagDetector(config)
    apriltag_1_hits = 0
    aruco_counts: List[int] = []
    for _ in range(bursts):
        frame = camera.read()
        if frame is None:
            aruco_counts.append(0)
            continue
        gray, rescale = run_pipeline(frame)
        detections = detector.detect(gray, scale=rescale.scale)
        if any(d.family == TagFamily.APRILTAG and d.number == 1 for d in detections):
            apriltag_1_hits += 1
        aruco_counts.append(len({d.number for d in detections if d.family == TagFamily.ARUCO}))
    return apriltag_1_hits, aruco_counts


def sweep_detector_configs(*, bursts: int = _BURSTS_PER_CONFIG) -> Dict[str, Tuple[int, List[int]]]:
    """Manual/exploratory sweep against real camera-5 hardware -- **not**
    pytest-collected (no ``test_`` prefix), matching this ticket's own
    Implementation Plan ("manual/exploratory, not itself a committed
    test"). Run directly, with camera 5 aimed at ``main-playfield``::

        uv run python -m tests.aprilcam.daemon.test_detector_tuning_hardware

    Sweeps :data:`_CANDIDATE_CONFIGS` for real, using a real, swept
    exposure (:func:`~tests.aprilcam.daemon.conftest.hardware_test_camera_config`
    -- the same session-cached auto-exposure sweep every other module in
    this directory uses, held fixed across every candidate config so the
    comparison is not confounded by exposure drift between configs).
    Prints a real per-config table (AprilTag-1 hit rate, worst-burst and
    mean ArUco recall) and returns the raw counts. Does not itself pick a
    winner, write ``_DETECTOR_CONFIG_OVERRIDES``, or update this module's
    ``PINNED_*`` constants -- see the module docstring's numbered
    "what a human does next" section for that.

    Raises ``RuntimeError`` (does not skip -- this is a script entry
    point, not a pytest test) if camera 5 is not attached or not
    openable.
    """
    camera = _open_camera_5()
    try:
        exposure_config = hardware_test_camera_config(CAMERA_5_SLUG, playfield=None, camera=camera)
        camera.apply_config(exposure_config)
        camera.read()  # discard one frame so the sensor settles at the new exposure
        print(f"[sweep] {CAMERA_5_SLUG}: exposure settings={exposure_config.settings}")

        results: Dict[str, Tuple[int, List[int]]] = {}
        for name, config in _CANDIDATE_CONFIGS.items():
            apriltag_1_hits, aruco_counts = _measure_config(camera, config, bursts=bursts)
            results[name] = (apriltag_1_hits, aruco_counts)
            mean_aruco = sum(aruco_counts) / len(aruco_counts) if aruco_counts else 0.0
            print(
                f"[sweep] {name:<32} quad_decimate={config.quad_decimate} "
                f"corner_refine={config.corner_refine!r} "
                f"wb_diff={config.april_min_wb_diff} "
                f"cluster_px={config.april_min_cluster_pixels} "
                f"line_mse={config.april_max_line_fit_mse} "
                f"-> apriltag_1={apriltag_1_hits}/{bursts} "
                f"aruco_min={min(aruco_counts) if aruco_counts else 0}/{EXPECTED_ARUCO_COUNT} "
                f"aruco_mean={mean_aruco:.1f}/{EXPECTED_ARUCO_COUNT}"
            )
        return results
    finally:
        camera.close()


def test_apriltag_1_detection_rate_and_aruco_recall_match_pinned_camera_5_baseline(camera_5):
    """Pins camera 5's real AprilTag-1 detection rate, plus the paired
    no-ArUco-regression check, at whichever ``DetectorConfig``
    ``_DETECTOR_CONFIG_OVERRIDES`` currently holds for
    :data:`CAMERA_5_SLUG` -- this ticket's acceptance criteria: fails on
    a silent regression *and* on a silent improvement past the
    documented baseline (both directions must be visible), and proves no
    regression to ArUco recall from the per-camera tuning mechanism.

    **Currently skips**: :data:`PINNED_APRILTAG_1_HITS` is ``None`` --
    no real-hardware sweep has been run yet (this ticket's own hardware-
    access limitation; see the module docstring). Fill in the
    ``PINNED_*`` constants above from a real :func:`sweep_detector_configs`
    run, and the matching entry (or deliberate absence) in
    ``daemon.detection._DETECTOR_CONFIG_OVERRIDES``, and this test starts
    actually pinning behaviour instead of skipping.
    """
    if PINNED_APRILTAG_1_HITS is None or PINNED_ARUCO_MIN_RECALL is None:
        pytest.skip(
            "no real-hardware baseline measured yet for camera 5 -- run "
            "sweep_detector_configs() in this module for real against "
            "camera 5 at the physical rig, then fill in this module's "
            "PINNED_* constants and daemon.detection."
            "_DETECTOR_CONFIG_OVERRIDES[CAMERA_5_SLUG] from the result "
            "(ticket 035-002's own hardware-access limitation -- see this "
            "module's docstring)"
        )

    camera = camera_5
    exposure_config = hardware_test_camera_config(CAMERA_5_SLUG, playfield=None, camera=camera)
    camera.apply_config(exposure_config)
    camera.read()  # let the sensor settle at the swept exposure

    config = _DETECTOR_CONFIG_OVERRIDES.get(CAMERA_5_SLUG)
    apriltag_1_hits, aruco_counts = _measure_config(camera, config, bursts=_BURSTS_PER_CONFIG)
    min_aruco = min(aruco_counts) if aruco_counts else 0

    # Two-sided: a tight window around the pinned measurement, not a
    # one-sided "at least" floor -- an unexplained improvement must fail
    # exactly as loudly as a regression (this ticket's own acceptance
    # criteria, "both directions must be visible").
    assert apriltag_1_hits == pytest.approx(PINNED_APRILTAG_1_HITS, abs=1), (
        f"AprilTag 1 detected {apriltag_1_hits}/{_BURSTS_PER_CONFIG} bursts "
        f"using detector config {PINNED_DETECTOR_CONFIG_NAME!r} -- pinned "
        f"baseline is {PINNED_APRILTAG_1_HITS}/{_BURSTS_PER_CONFIG}. If "
        "this is a genuine, repeatable improvement, update "
        "PINNED_APRILTAG_1_HITS deliberately rather than loosening this "
        "tolerance; if it is a regression, investigate before doing "
        "either."
    )
    # One-sided: ArUco recall has no meaningful "too high" failure mode
    # (it is bounded at EXPECTED_ARUCO_COUNT already) -- only a floor
    # matters here, matching this ticket's "proves no regression" wording
    # (AC 4) rather than AC 3's two-sided AprilTag-1 wording.
    assert min_aruco >= PINNED_ARUCO_MIN_RECALL, (
        f"camera 5's ArUco recall dropped to {min_aruco}/{EXPECTED_ARUCO_COUNT} "
        f"in the worst of {_BURSTS_PER_CONFIG} bursts using detector config "
        f"{PINNED_DETECTOR_CONFIG_NAME!r} -- pinned baseline is "
        f"{PINNED_ARUCO_MIN_RECALL}/{EXPECTED_ARUCO_COUNT}. The per-camera "
        "detector tuning mechanism must not regress ArUco recall on "
        "camera 5."
    )


if __name__ == "__main__":
    sweep_detector_configs()
