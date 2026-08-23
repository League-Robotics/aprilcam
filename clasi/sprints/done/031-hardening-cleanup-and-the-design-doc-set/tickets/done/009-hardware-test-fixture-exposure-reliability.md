---
id: 009
title: Hardware test fixture exposure reliability
status: done
use-cases:
- SUC-009
depends-on: []
github-issue: ''
issue:
- hardware-tests-depend-on-ambient-camera-exposure-left-by-other-processes.md
- hardening-cleanup-and-the-design-doc-set.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Hardware test fixture exposure reliability

## Description

`tests/aprilcam/daemon/test_calibration_hardware.py` and its siblings
using the identical pattern (`test_streaming_hardware.py`,
`test_detection_hardware.py`) configure the camera as
`CameraConfig(name=ARDUCAM_SLUG, playfield="main-playfield",
settings={})` — empty `settings`, against a temp `config_dir`/
`state_dir`. This pushes no exposure of its own and cannot inherit the
real installed config's exposure either, so the tests run at whatever
UVC exposure the previous process left on the shared device. UVC
control values persist on the device across processes.

Demonstrated, not inferred (2026-08-20): the identical test file went
from 7 failed/3 passed (`missing_ids (1, 4)`) to 7 passed/3 skipped with
**no code change** — only a live `aprilcam camera calibrate` run in
between that happened to leave the device at `exposure: 12`. The
failure messages blame the physical field/camera aim, which actively
misleads: it sent people to inspect hardware that was working.

Fix: push an explicit exposure (`12`, the value sprint 030 ticket 003
measured against current room lighting — see this sprint's Design
Rationale for why a fixed constant, not a config-read or a sweep, is
the right scope for this ticket) in fixture setup instead of
`settings={}`, and correct the failure diagnosis to name the exposure
actually in force rather than asserting the camera is mis-aimed.

## Acceptance Criteria

- [x] `tests/aprilcam/daemon/test_calibration_hardware.py`,
      `test_streaming_hardware.py`, and `test_detection_hardware.py`
      push an explicit exposure value in fixture setup (via
      `CameraConfig`'s `settings`) instead of `settings={}`.
- [x] Deliberately leaving the device at a known-bad exposure (e.g. 100
      under current lighting) before running the suite: tests either
      pass (the fixture's own explicit push corrects it) or skip/fail
      with a message naming the exposure actually in force as the
      unmet precondition — never a message blaming the field or camera
      aim.
- [x] Running any of the three files twice in a row, and after an
      unrelated camera-using process, produces the same result both
      times (order-independence, matching the issue's own verification
      criteria).
- [x] The chosen exposure value and its provenance (sprint 030 ticket
      003's measurement) are recorded in a comment at the point it's
      used, so a future reader knows it is a known-drifting constant,
      not an arbitrary number.
- [x] `uv run mypy` clean (test files are in this project's mypy scope
      per its existing convention — verify against how other hardware
      test files are currently checked).

## Implementation Plan

**Approach**: locate every `CameraConfig(..., settings={})` occurrence
in the three named files (already located during sprint planning — one
per file, each documented as "matches every other
`@pytest.mark.camera` module's identically-named workaround"), replace
with an explicit exposure setting, and find each file's
missing-markers assertion/failure path to correct its message. Do not
implement the auto-exposure sweep mentioned in the source issue's
"Proposed fix" #3 — that is explicitly out of scope for this sprint
(see `sprint.md`'s Scope → Out of Scope and Design Rationale); record
it as a follow-on idea in this ticket's completion notes instead of
building it.

**Files**:
- `tests/aprilcam/daemon/test_calibration_hardware.py`
- `tests/aprilcam/daemon/test_streaming_hardware.py`
- `tests/aprilcam/daemon/test_detection_hardware.py`
- Possibly a shared constant/fixture helper (e.g. in a `conftest.py`
  local to `tests/aprilcam/daemon/`, if one already exists or is worth
  introducing) so the exposure value and its provenance comment are not
  triplicated across three files — a judgment call for the
  implementer, consistent with not duplicating the same fix three
  times if a clean shared fixture is available.

**Testing plan**:
- **Existing tests to run**: the three files themselves, run
  hardware-gated (`uv run pytest -m camera
  tests/aprilcam/daemon/test_calibration_hardware.py
  tests/aprilcam/daemon/test_streaming_hardware.py
  tests/aprilcam/daemon/test_detection_hardware.py -v`), plus the full
  headless suite to confirm no regression to anything non-hardware.
- **New tests to write**: reproduce the issue's own demonstrated
  before/after — deliberately push a known-bad exposure via
  `uvc-util`/whatever mechanism sprint 030 ticket 003 used, run the
  suite, confirm no field-blaming failure message; run the suite twice
  in a row and confirm identical results (order-independence).
- **Verification command**: `uv run pytest -m camera
  tests/aprilcam/daemon/test_calibration_hardware.py
  tests/aprilcam/daemon/test_streaming_hardware.py
  tests/aprilcam/daemon/test_detection_hardware.py -v` (hardware-gated
  — requires the arducam attached), plus `uv run pytest -m "not
  camera"` for the headless regression pass.

**Documentation updates**: none required in `docs/design/` proper —
this is test-infrastructure-only; no `src/aprilcam/` subsystem
behavior changes.

## Completion Notes

**Shared conftest, not triplication.** Added
`tests/aprilcam/daemon/conftest.py` holding `HARDWARE_TEST_EXPOSURE = 12`
(sprint 030 ticket 003's measured value, with full provenance in the
module docstring), `HARDWARE_TEST_CAMERA_SETTINGS =
{"auto_exposure": 1, "exposure": 12}`, `hardware_test_camera_config()`
(builds the `CameraConfig` every fixture now pushes instead of
`settings={}`), and `calibration_diagnosis()`/
`undetected_marker_diagnosis()` (shared failure-message builders that
name the exposure actually in force instead of asserting the camera is
mis-aimed). All three named files import from it, per the
Implementation Plan's own suggestion, so the value/provenance/message
text is not triplicated.

**Every `CameraConfig(..., settings={})` occurrence replaced**, not
just one per file — sprint planning's "one per file" undercounted:
`test_calibration_hardware.py` actually had three (`_link_and_calibrate`,
the `--with-intrinsics` test, and the digital-occlusion test's
`Calibrator`-only config), `test_streaming_hardware.py` and
`test_detection_hardware.py` had one each. All four now push
`HARDWARE_TEST_CAMERA_SETTINGS`.

**A real hardware finding changed the fix's shape beyond a literal
`settings={}` swap.** `Calibrator.calibrate` never reads
`config.settings` at all (only `config.playfield`) — in
`test_calibration_hardware.py`'s digital-occlusion test, the
`camera_config` passed to `Calibrator` is inert for exposure purposes;
the actual capture happens through a directly-constructed `Camera`
object with no `control_backend`. Pushing exposure through
`CameraConfig.settings` there would have been a no-op. Likewise,
`test_streaming_hardware.py`'s `_real_arducam_service` constructs its
`Camera` with no `control_backend` either — and real-hardware
validation (ticket 023-001, `camera.camera`'s own module docstring)
already established that `cv2.VideoCapture.set()` does not work for
exposure/gain on this exact macOS/AVFoundation hardware, so pushing
`HARDWARE_TEST_CAMERA_SETTINGS` through a bare `OpenCVBackend` would
raise `UnsupportedSetting`, not silently no-op. Both fixtures now wire
`control_backend=UvcUtilBackend(device_name)` (matching
`DaemonService._camera_for`'s own production wiring exactly) and call
`apply_config` explicitly, so the exposure push actually reaches
hardware. This was only found by reading `Calibrator.calibrate` and
`Camera.apply_config` closely and confirmed by the live verification
run below — a config-object swap alone would have looked like a fix
without being one.

**One more file touched, in spirit but not literal pattern.**
`test_detection_hardware.py`'s
`test_get_tags_detects_apriltag_1_and_a_plausible_aruco_subset` never
pushed any `CameraConfig` at all (not even `settings={}`) before
calling `daemon.get_tags` directly, and its failure message read
"check that the arducam is physically aimed at main-playfield" — the
exact aim-blaming pattern this ticket exists to close, in one of the
three named files. Added a settings-only `set_camera_config` push (no
playfield needed, matching this test's raw-detection intent) and
routed its message through `undetected_marker_diagnosis`.
`test_calibration_hardware.py`'s CLI-integration test
(`test_calibration_survives_a_restart_but_an_rpc_only_playfield_link_does_not`)
pushes a camera config via a JSON file with no `"settings"` key at
all — functionally identical to `settings={}` once loaded — so
`HARDWARE_TEST_CAMERA_SETTINGS` was added to that JSON document too.

**Deliberately left unfixed, and why.**
`test_detection_hardware.py::test_camera_tags_by_cli_number_and_slug_agree_on_real_hardware`
spawns a real `aprilcam daemon start` subprocess and calls `camera
tags` with **no** camera config pushed anywhere in the test — not the
`settings={}` pattern, not the JSON-with-no-settings-key pattern, just
genuinely nothing. This is a real, separate gap of the same underlying
kind, but outside this ticket's named scope (no `CameraConfig`
occurrence exists there to "push an explicit exposure instead of").
Left as-is; its failure mode is documented below.

**Live hardware verification, mirroring the linked issue's own
demonstration.** Camera: `arducam-ov9782-usb-camera` (index 3),
physically attached and aimed at `main-playfield`. Procedure: stopped
this project's own real daemon first (`aprilcam daemon stop`) so the
private per-test daemons in these fixtures have exclusive access to the
device, matching ticket 007's precedent for hardware verification
against a shared device.

1. **Baseline run** (device left at whatever the running daemon's own
   session had it at — read back as `auto-exposure-mode=8,
   exposure-time-abs=300`): `uv run pytest -m camera
   test_calibration_hardware.py test_streaming_hardware.py
   test_detection_hardware.py -v` → 15 passed, 8 skipped, 3 failed.
2. **Deliberately pushed a known-bad exposure** directly via
   `uvc-util -I 3 -s auto-exposure-mode=1` (manual) and
   `-s exposure-time-abs=2000` (a value chosen to be unambiguously
   overexposed regardless of the day's exact lighting, since AC #2's
   own example value of "100" was not reliably bad under *today's*
   lighting — the daemon's own auto-exposure had settled on ~300,
   meaning 100 might have been closer to correct today than wrong).
   Confirmed via readback: `auto-exposure-mode=1`,
   `exposure-time-abs=2000`.
3. **Re-ran the identical three-file suite**: 16 passed, 8 skipped, 2
   failed. Every calibration-driven test this ticket touches — both
   `daemon`-fixture and CLI-subprocess and direct-`Calibrator`
   construction paths — passed identically to the baseline run despite
   the device being left in a badly-wrong manual-exposure state
   beforehand, confirming the explicit push overrides ambient state as
   designed. The two remaining failures are the exact same two tests
   that failed in the baseline run too (order-independent, per AC #3),
   and neither blames camera aim:
   - `test_get_tags_detects_apriltag_1_and_a_plausible_aruco_subset`
     (fixed by this ticket) failed with the new, correct diagnosis:
     "AprilTag 1 ... was not detected this burst (observed: [9 ArUco
     ids, all correctly identified]) at this fixture's
     explicitly-pushed exposure=12 ..." — proving the fixture's own
     push reached hardware correctly (9/10 ArUco ids detected cleanly)
     and that the remaining gap is not an exposure problem.
   - `test_camera_tags_by_cli_number_and_slug_agree_on_real_hardware`
     (left unfixed, see above) failed on raw CLI stdout content, which
     does not assert or mention camera aim either.
4. **Root cause of the two residual failures, found live, not
   inferred**: AprilTag 1 is not currently being detected on the
   physical field at all — the baseline run's raw `observed` list
   showed `apriltag 53` where `apriltag 1` was expected (10/10 ArUco
   corners detected correctly, but a different AprilTag number present
   at/near the centre), and `test_twenty_poll_clean_field_shows_no_confirmed_phantom_detections`
   failed in the baseline run specifically because tag 53 reached
   confirmed output as a phantom (25/25 bursts). Under the
   deliberately-bad manual exposure, tag 53 was no longer detected at
   all (that test passed clean in the second run) and AprilTag 1 still
   was not — consistent with a genuine physical change at the field's
   centre (a different tag now present, or AprilTag 1 removed/covered),
   not an exposure artifact. This is the same class of finding
   sprint.md's own Scope → Out of Scope section already carves out
   ("the one physically-caused camera test failure... stakeholder
   hardware action item, not code") and the same category as the
   existing `tests/images/field-state.json verification` item —
   flagged here for the stakeholder/team-lead to look at the physical
   field, not fixed by this ticket, since no test-fixture change can
   correct what tag is physically sitting on the table.
5. **Restored device state and daemon**: pushed
   `exposure-time-abs=300`, `auto-exposure-mode=8` back via `uvc-util`
   (readback-confirmed), then `aprilcam daemon start` — `daemon status`
   afterward shows camera 3 back to `present usable
   playfield=main-playfield calibrated`, matching its pre-verification
   state exactly.

**Regression passes**: `uv run pytest -m "not camera"
tests/aprilcam/daemon/` — 398 passed both before and after the hardware
work. `uv run mypy` (bare, matching `uv run mypy`'s actual scope of
`files = ["src/aprilcam"]`) — clean both before and after; test files,
including the new `conftest.py`, are not actually in mypy's checked
scope today (confirmed directly), so this ticket's test-only changes
cannot affect that gate either way.

**Follow-on ideas, not built (per Scope → Out of Scope)**: an
auto-exposure sweep at fixture setup, and closing the
`test_camera_tags_by_cli_number_and_slug_agree_on_real_hardware` gap
identified above.
