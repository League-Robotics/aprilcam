"""Shared hardware-fixture state for ``tests/aprilcam/daemon/``'s
``@pytest.mark.camera`` modules (ticket 031-009, generalized out-of-process
alongside the ARDUCAM_SLUG discovery fix below).

**Why this file exists.** ``test_calibration_hardware.py``,
``test_streaming_hardware.py``, and ``test_detection_hardware.py`` each
push a ``CameraConfig`` for whichever real camera
``tests/aprilcam/hardware_camera.py`` resolves before running against real
hardware. Pushing an empty ``settings={}`` document pushes no exposure of
its own -- the camera ran at whatever UVC exposure the *previous* process
left on the shared device, since UVC control values persist on the device
across processes and are never reset just because a new process opens it.
Demonstrated, not inferred (see
``clasi/issues/hardware-tests-depend-on-ambient-camera-exposure-left-by-other-processes.md``
and sprint 031's own sprint.md Problem 7): the identical test file went
from 7 failed/3 passed (``missing_ids (1, 4)``) to 7 passed/3 skipped with
**no code change** -- only a live ``aprilcam playfield calibrate`` run in
between that happened to leave the device at ``exposure: 12``. The failure
messages that resulted blamed the physical field/camera aim, which
actively misled: it sent people to inspect hardware that was working.

**The original fix, and the limit it hit almost immediately.** Sprint
031's first pass pushed one explicit, hardcoded exposure constant
(``HARDWARE_TEST_EXPOSURE = 12``, ticket 003's own measured value for one
specific camera under one specific afternoon's room lighting) instead of
an empty ``settings={}``. That constant's own docstring already predicted
its failure mode: "it is expected to need re-tuning again if room lighting
changes materially." It needed re-tuning sooner than that, for a different
reason entirely -- this out-of-process fix, replacing every hardcoded
``ARDUCAM_SLUG`` in this suite with runtime discovery
(``tests/aprilcam/hardware_camera.py``), found that this very machine's
own real, currently-in-force camera config
(``~/.config/aprilcam/cameras/*/config.json``) had already drifted to
``exposure: 100`` by the time this fix was written -- on the *same*
physical camera the constant ``12`` was measured against. A single fixed
constant could not be trusted to transfer even to the one device it was
tuned for, let alone to whatever different camera model a future run's
discovery might resolve to instead (a different sensor's usable exposure
range has no reason to overlap with this one's).

**The fix now: measure it, per camera, per test session.** Ticket
031-009's own issue named the more robust alternative and deferred it as
out of scope: "a full auto-exposure sweep at fixture setup ... pick the
exposure that detects the most markers." :func:`_measure_best_exposure`
implements exactly that -- a short, real, one-time-per-session sweep
across :data:`_EXPOSURE_CANDIDATES` against the real camera, keeping
whichever value detects the most real ArUco/AprilTag markers, cached by
slug for the rest of the pytest session so repeated calls
(:func:`hardware_test_camera_config` is called many times per run across
these three modules) cost nothing after the first. This generalizes
correctly to whatever camera ``hardware_camera.py`` discovers, and
self-heals if the discovered camera's own best exposure drifts with room
lighting -- at the cost of a few real seconds of hardware I/O once per
run instead of a zero-cost constant.

That is why every missing-marker diagnosis in these modules should name
the exposure actually swept into place (:func:`calibration_diagnosis`
below), rather than asserting the camera is mis-aimed: so a future drift
is cheap to re-diagnose (the sweep already re-measures on every fresh
session) instead of costing another multi-issue investigation.

``auto_exposure=1`` (manual) must accompany ``exposure`` for the value
to actually take effect on this hardware -- ``camera.camera``'s
``_GATE_KEYS``/module docstring: "auto-exposure must be switched to
manual before an exposure value will stick." Pushing either key without
a real ``UvcUtilBackend`` wired as the camera's ``control_backend`` will
raise ``UnsupportedSetting`` on this exact camera (macOS/AVFoundation):
real-hardware validation (ticket 023-001, recorded in ``camera.camera``'s
module docstring) found ``cv2.VideoCapture.set()`` returns ``False`` for
every one of ``auto_exposure``/``exposure``/``gain``/... on this
hardware. Every fixture in this directory that constructs a ``Camera``
directly (bypassing ``DaemonService``'s own automatic
``control_backend_factory`` wiring) must wire
``control_backend=UvcUtilBackend(device_name)`` itself before pushing
:func:`hardware_test_camera_config`'s settings, matching
``daemon.service.DaemonService._camera_for``'s own production wiring
exactly.

**Avoiding a double-open during the sweep.** A caller that already holds
*slug*'s ``Camera`` open (e.g. an occlusion test's directly-constructed
probe camera) must pass it as :func:`hardware_test_camera_config`'s
*camera* argument rather than letting the sweep open its own -- two
concurrent opens of the same physical device index are not reliable on
this project's supported backends. Every other caller (going through a
remote ``GrpcDaemon`` with no client-side ``Camera`` object at all) omits
*camera*, and the sweep opens and closes a short-lived one of its own.
"""

from __future__ import annotations

import logging
from typing import Dict, Optional, Sequence

from aprilcam.camera.backends.opencv_backend import OpenCVBackend
from aprilcam.camera.backends.uvc_util import UvcUtilBackend
from aprilcam.camera.camera import Camera
from aprilcam.camera.identity import resolve_all
from aprilcam.config.interfaces import CameraConfig
from aprilcam.vision.detector import TagDetector
from aprilcam.vision.pipeline import run_pipeline

logger = logging.getLogger(__name__)

#: Candidate exposure values probed by :func:`_measure_best_exposure` --
#: spans low (bright rooms/sensitive sensors) to high (dim rooms), wide
#: enough to cover both this suite's original ticket-003 baseline (12,
#: measured one specific afternoon) and the real production config this
#: machine had separately drifted to by the time this sweep replaced that
#: fixed constant (100 -- see this module's docstring).
_EXPOSURE_CANDIDATES: Sequence[int] = (3, 6, 12, 25, 50, 80, 100, 150, 200)

#: One real sweep result per camera slug, cached for the lifetime of the
#: pytest session -- repeated hardware fixtures in this directory ask for
#: the same slug's exposure many times per run; this makes every call
#: after the first free instead of re-driving hardware each time.
_measured_exposure_cache: Dict[str, int] = {}


def _count_markers(camera: Camera) -> int:
    """Total ArUco+AprilTag detections in one fresh real capture from
    *camera* -- the issue's own suggested sweep metric ("pick the exposure
    that detects the most markers"), not any one specific id, so this
    works regardless of which markers a given camera/playfield pairing
    happens to see."""
    frame = camera.read()
    if frame is None:
        return 0
    gray, rescale = run_pipeline(frame)
    return len(TagDetector().detect(gray, scale=rescale.scale))


def _open_temporary_camera(slug: str) -> Optional[Camera]:
    """A short-lived, directly-constructed ``Camera`` for *slug*, wired
    with a real ``UvcUtilBackend`` exactly like every other direct
    construction in this test directory -- or ``None`` if *slug* cannot
    currently be resolved/opened (e.g. genuinely busy in another process),
    in which case :func:`_measure_best_exposure` falls back rather than
    failing the whole test session over a sweep that is best-effort by
    nature."""
    identities = resolve_all()
    index = next((idx for idx, ident in identities.items() if ident.slug == slug), None)
    identity = identities.get(index) if index is not None else None
    if identity is None:
        return None
    camera = Camera(slug, OpenCVBackend(index), control_backend=UvcUtilBackend(identity.device_name))
    try:
        camera.open()
    except Exception as exc:  # pragma: no cover - environment-dependent
        logger.warning("%s: could not open for an exposure sweep: %s", slug, exc)
        return None
    return camera


def _measure_best_exposure(slug: str, *, camera: Optional[Camera] = None) -> int:
    """A lightweight, real auto-exposure sweep against *slug* (ticket
    031-009's own issue, floated and deferred there as "the more robust
    long-term answer" -- see this module's docstring for why the fixed
    constant it shipped instead did not survive contact with a second
    camera). Tries each of :data:`_EXPOSURE_CANDIDATES` for real, counts
    real marker detections at each, and returns whichever exposure
    detected the most.

    *camera*, when given, must already be open and wired with a real
    ``control_backend`` -- the sweep drives it directly rather than
    opening a second, conflicting handle on the same device (see the
    module docstring's "avoiding a double-open" section). When omitted,
    this opens and closes its own short-lived ``Camera`` for *slug*.

    Cached per *slug* for the pytest session -- see
    :data:`_measured_exposure_cache`. Falls back to this suite's original
    ticket-003 baseline (12) if *slug* cannot be resolved/opened here at
    all: every real caller already has its own
    ``_skip_if_..._not_attached`` guard before it ever reaches this point,
    so that should not happen in practice, but a best-effort sweep must
    degrade rather than crash the whole test session if it does.
    """
    if slug in _measured_exposure_cache:
        return _measured_exposure_cache[slug]

    owns_camera = camera is None
    if camera is None:
        camera = _open_temporary_camera(slug)
    if camera is None:
        fallback = 12
        logger.warning(
            "%s: not resolvable for an exposure sweep, falling back to the "
            "ticket-003 baseline exposure=%d",
            slug,
            fallback,
        )
        return fallback

    try:
        best_exposure = _EXPOSURE_CANDIDATES[0]
        best_count = -1
        for exposure in _EXPOSURE_CANDIDATES:
            camera.apply_config(
                CameraConfig(
                    name=slug, playfield=None, settings={"auto_exposure": 1, "exposure": exposure}
                )
            )
            camera.read()  # discard one frame so the sensor settles at the new exposure
            count = max(_count_markers(camera) for _ in range(2))
            logger.info("%s: exposure=%d -> %d marker(s) detected", slug, exposure, count)
            if count > best_count:
                best_count = count
                best_exposure = exposure
    finally:
        if owns_camera:
            camera.close()

    logger.info("%s: sweep picked exposure=%d (%d marker(s))", slug, best_exposure, best_count)
    _measured_exposure_cache[slug] = best_exposure
    return best_exposure


def hardware_test_camera_config(
    slug: str, *, playfield: Optional[str] = None, camera: Optional[Camera] = None
) -> CameraConfig:
    """A ``CameraConfig`` carrying a real, freshly-swept exposure (see
    :func:`_measure_best_exposure`) instead of an empty ``settings={}``
    block -- the fix this module exists to share, so the exposure value
    and its provenance are not triplicated across this directory's
    hardware test files. *playfield* defaults to ``None`` for callers
    that only need the exposure pushed (e.g. a raw ``Camera.apply_config``
    call) and never link/calibrate against a playfield through this exact
    document. *camera*, when the caller already holds *slug* open, avoids
    a conflicting second open during the sweep -- see this module's
    docstring."""
    exposure = _measure_best_exposure(slug, camera=camera)
    settings = {"auto_exposure": 1, "exposure": exposure}
    return CameraConfig(name=slug, playfield=playfield, settings=settings)


def _exposure_note(slug: Optional[str]) -> str:
    """The exposure clause shared by :func:`calibration_diagnosis` and
    :func:`undetected_marker_diagnosis` -- names the real, swept value
    actually in force for *slug* when it is known (already measured this
    session), rather than a fixed, potentially-stale constant."""
    exposure = _measured_exposure_cache.get(slug) if slug is not None else None
    if exposure is None:
        return "this fixture's explicitly-pushed exposure (auto_exposure=manual)"
    return f"this fixture's explicitly-pushed exposure={exposure} (auto_exposure=manual)"


def calibration_diagnosis(result, slug: Optional[str] = None) -> str:
    """Diagnostic text for a failed/incomplete ``CalibrationResult``,
    naming the exposure this module's fixtures explicitly swept and
    pushed instead of asserting the camera is mis-aimed (ticket 031-009,
    SUC-009's Main Flow step 3: "the failure message names the exposure
    actually in force ... it does not assert the camera is mis-aimed").
    Every hardware test in this directory that can fail on missing
    markers after a :func:`hardware_test_camera_config` push routes its
    message through this helper, so the diagnosis stays consistent and
    does not quietly regress back to blaming the field. Pass *slug* (the
    camera actually used) for a message naming the real swept exposure;
    omitted, the message is generic.
    """
    if result.missing_ids:
        return (
            f"missing required marker id(s) {list(result.missing_ids)} "
            f"(matched {list(result.matched_ids)}) at {_exposure_note(slug)} -- "
            "this suite's own auto-exposure sweep already picked this as "
            "the best of several real candidates (see this directory's "
            "conftest.py). If detection has genuinely degraded further, "
            "re-run to force a fresh sweep next session, or investigate a "
            "physical/camera-aim problem."
        )
    return f"calibration failed: {result.reason}"


def undetected_marker_diagnosis(observed, slug: Optional[str] = None) -> str:
    """Diagnostic text for a single expected marker/tag not appearing in
    a raw (non-calibrating) detection burst -- the same "name the
    exposure, don't blame the aim" reasoning as
    :func:`calibration_diagnosis`, for callers with a plain
    observed-detections list rather than a ``CalibrationResult``. Pass
    *slug* for a message naming the real swept exposure; omitted, the
    message is generic."""
    return (
        f"not detected this burst (observed: {observed}) at {_exposure_note(slug)} -- "
        "this suite's own auto-exposure sweep already picked this as the "
        "best of several real candidates (see this directory's "
        "conftest.py). If detection has genuinely degraded further, re-run "
        "to force a fresh sweep next session, or investigate a physical/"
        "camera-aim problem."
    )
