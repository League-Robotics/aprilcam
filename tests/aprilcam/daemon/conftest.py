"""Shared hardware-fixture state for ``tests/aprilcam/daemon/``'s
``@pytest.mark.camera`` modules (ticket 031-009).

**Why this file exists.** ``test_calibration_hardware.py``,
``test_streaming_hardware.py``, and ``test_detection_hardware.py`` each
push a ``CameraConfig`` for the real ``arducam-ov9782-usb-camera``
before running against real hardware. Pushing an empty ``settings={}``
document pushes no exposure of its own -- the camera ran at whatever
UVC exposure the *previous* process left on the shared device, since
UVC control values persist on the device across processes and are
never reset just because a new process opens it. Demonstrated, not
inferred (see
``clasi/issues/hardware-tests-depend-on-ambient-camera-exposure-left-by-other-processes.md``
and this sprint's own sprint.md Problem 7): the identical test file
went from 7 failed/3 passed (``missing_ids (1, 4)``) to 7 passed/3
skipped with **no code change** -- only a live ``aprilcam camera
calibrate`` run in between that happened to leave the device at
``exposure: 12``. The failure messages that resulted blamed the
physical field/camera aim, which actively misled: it sent people to
inspect hardware that was working.

**The fix, and its known limit.** Push an explicit exposure instead --
:data:`HARDWARE_TEST_CAMERA_SETTINGS` below -- so every hardware test in
this directory starts from a known, not-inherited device state, rather
than reading ``settings={}`` and hoping the last process left something
usable. ``12`` is sprint 030 ticket 003's own measured value (see
``docs/knowledge/2026-08-20-camera-3-distortion-reverification.md``'s
exposure sweep table: exposure 11-13 reliably saw all 11 required
markers -- 10 ArUco + AprilTag 1 -- under that afternoon's room
lighting, while every other tested value in the 5-100 range saw far
fewer). It is a known, explicit, *documented* dependency, not a silent
one -- and, per this sprint's own Design Rationale ("fixed exposure
constant, not a config-driven value, for hardware test fixtures"), it
is expected to need re-tuning again if room lighting changes
materially. A config-read alternative was rejected there for
reintroducing the identical ambient-dependency bug one layer further
away; a full auto-exposure sweep at fixture setup was named as the more
robust long-term answer but ruled out of scope for this sprint (see
sprint.md Scope -> Out of Scope) and is recorded here, again, as a
follow-on idea -- not built.

That is why every missing-marker diagnosis in these modules should name
this constant's value directly (:func:`calibration_diagnosis` below),
rather than asserting the camera is mis-aimed: so a future drift is
cheap to re-diagnose (re-sweep exposure, per the linked knowledge
entry's method) instead of costing another multi-issue investigation.

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
:data:`HARDWARE_TEST_CAMERA_SETTINGS`, matching
``daemon.service.DaemonService._camera_for``'s own production wiring
exactly.
"""

from __future__ import annotations

from typing import Optional

from aprilcam.config.interfaces import CameraConfig

#: sprint 030 ticket 003's measured value for arducam-ov9782-usb-camera
#: aimed at main-playfield under that ticket's room lighting -- see this
#: module's docstring for the full sweep table and provenance. A known,
#: explicit, documented dependency that will need re-tuning if room
#: lighting drifts materially (sprint.md Design Rationale).
HARDWARE_TEST_EXPOSURE = 12

#: The settings document every hardware fixture below pushes in place
#: of ``settings={}``. ``auto_exposure`` (switched to manual) is a
#: precondition for ``exposure`` to take effect at all -- see this
#: module's docstring.
HARDWARE_TEST_CAMERA_SETTINGS = {"auto_exposure": 1, "exposure": HARDWARE_TEST_EXPOSURE}


def hardware_test_camera_config(slug: str, *, playfield: Optional[str] = None) -> CameraConfig:
    """A ``CameraConfig`` carrying :data:`HARDWARE_TEST_CAMERA_SETTINGS`
    instead of an empty ``settings={}`` block -- the fix this module
    exists to share, so the exposure value and its provenance are not
    triplicated across the three hardware test files. *playfield*
    defaults to ``None`` for callers that only need the exposure pushed
    (e.g. a raw ``Camera.apply_config`` call) and never link/calibrate
    against a playfield through this exact document."""
    return CameraConfig(name=slug, playfield=playfield, settings=HARDWARE_TEST_CAMERA_SETTINGS)


def calibration_diagnosis(result) -> str:
    """Diagnostic text for a failed/incomplete ``CalibrationResult``,
    naming the exposure this module's fixtures explicitly pushed instead
    of asserting the camera is mis-aimed (ticket 031-009, SUC-009's Main
    Flow step 3: "the failure message names the exposure actually in
    force ... it does not assert the camera is mis-aimed"). Every
    hardware test in this directory that can fail on missing markers
    after a :data:`HARDWARE_TEST_CAMERA_SETTINGS` push routes its
    message through this helper, so the diagnosis stays consistent and
    does not quietly regress back to blaming the field.
    """
    if result.missing_ids:
        return (
            f"missing required marker id(s) {list(result.missing_ids)} "
            f"(matched {list(result.matched_ids)}) at this fixture's "
            f"explicitly-pushed exposure={HARDWARE_TEST_EXPOSURE} "
            "(auto_exposure=manual -- sprint 030 ticket 003's measured "
            "baseline, see this module's docstring). If detection has "
            "genuinely degraded at this exposure, re-sweep it (method: "
            "docs/knowledge/2026-08-20-camera-3-distortion-reverification.md) "
            "before assuming a physical or camera-aim problem."
        )
    return f"calibration failed: {result.reason}"


def undetected_marker_diagnosis(observed) -> str:
    """Diagnostic text for a single expected marker/tag not appearing in
    a raw (non-calibrating) detection burst -- the same "name the
    exposure, don't blame the aim" reasoning as
    :func:`calibration_diagnosis`, for callers with a plain
    observed-detections list rather than a ``CalibrationResult``."""
    return (
        f"not detected this burst (observed: {observed}) at this "
        f"fixture's explicitly-pushed exposure={HARDWARE_TEST_EXPOSURE} "
        "(auto_exposure=manual -- sprint 030 ticket 003's measured "
        "baseline, see this module's docstring). If detection has "
        "genuinely degraded at this exposure, re-sweep it (method: "
        "docs/knowledge/2026-08-20-camera-3-distortion-reverification.md) "
        "before assuming a physical or camera-aim problem."
    )
