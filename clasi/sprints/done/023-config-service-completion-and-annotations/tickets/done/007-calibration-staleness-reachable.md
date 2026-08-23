---
id: '007'
title: Calibration staleness reachable
status: done
use-cases:
- SUC-006
depends-on: []
github-issue: ''
issue: config-service-completion-and-annotations.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Calibration staleness reachable

## Description

`Calibration.stale` is hardcoded `False` at construction
(`calibration/calibration.py`, in `Calibrator.calibrate`) and nothing in
the codebase ever sets it otherwise — a camera bumped after calibration
keeps reporting confident, plausible, wrong world coordinates forever
(the design's worst failure mode). Make it real via two triggers
(sprint.md Design Rationale Decisions 5 and 6):

1. **Opportunistic drift check**, piggybacked on frames
   `DetectionEngine.run_burst` already reads (no independent poll loop
   — preserves the idle-efficiency budget, dev-plan Stage 8): at most
   once per **10 seconds** per calibrated camera (confirmed interval),
   re-detect the calibration's stored static ArUco markers in the frame
   already in hand and compare to their stored positions; a drift above
   **2.0 cm** (confirmed tolerance) marks the calibration stale.
2. **Camera reopen**: a successful self-heal reopen (or an explicit
   close/open) also marks the relevant calibration stale, via a new
   `Camera.on_reopen` constructor-injected callback (mirrors
   `DetectionEngine`'s existing `calibration_lookup` injection pattern)
   — `camera.camera` gains no notion of calibration itself;
   `DaemonService` owns the mark-stale logic and supplies the callback.

**Thread safety — pin this explicitly, do not rediscover it the hard
way.** Both triggers write `self._calibrations` from whatever thread is
servicing the triggering RPC (`daemon/rpc.py`'s server runs a
`ThreadPoolExecutor`, `max_workers=10`). The mark-stale helper **must**
take `self._lock` exactly like every other `_calibrations` mutation
already does in `DaemonService.calibrate` — two concurrent bursts on the
same camera must not race a stale flip.

Wire the flag through: `CameraState.calibration_stale`/
`ImageFrame.calibration_stale` are already real fields, **already read
by `cli/_common.py`'s `camera list` "(stale)" flag and
`view/viewer.py`'s mid-view advisory line (UC-18)** — not by
`cli/camera.py`'s tag formatting or `client/renderer.py`, which do not
reference this field. Get this attribution right when wiring; a prior
draft of this sprint's planning got it wrong and it was corrected during
self-review — do not reintroduce the error. `TagFrame.calibration_stale`
is a declared field with no reader yet; wire `daemon/detection.py`'s
`run_burst` to populate it from the (possibly-just-updated) calibration
so it stops being silently always-`False` too.

## Acceptance Criteria

- [x] `Calibration.stale` is settable via
      `dataclasses.replace(calibration, stale=True)` from a new
      mark-stale path in `DaemonService`, guarded by `self._lock`.
- [x] `DetectionEngine.run_burst` performs the opportunistic drift
      check at most once per 10 s per camera slug, reusing the
      already-read frame; a camera with no calibration, or not
      queried, incurs zero extra detection work (headless test
      asserting call counts).
- [x] A fixture frame with stored static-marker positions shifted by
      more than 2.0 cm flips `stale` on the next drift check (headless
      test).
- [x] A fixture/frame with drift under 2.0 cm does **not** flip `stale`
      (false-positive guard, headless test).
- [x] `Camera` gains an optional `on_reopen: Optional[Callable[[],
      None]]` constructor parameter, invoked at the end of a
      successful `_attempt_self_heal`; `DaemonService` wires it to a
      closure marking that camera's calibration stale.
- [x] A camera reopen flips `stale` even with zero detectable pixel
      drift (headless test with a mocked backend forcing a reopen).
- [x] Concurrent mark-stale calls (simulated: two threads calling the
      mark-stale path for the same camera) do not corrupt
      `self._calibrations` — guarded by `self._lock` (headless
      concurrency test).
- [x] `TagFrame.calibration_stale` and `ImageFrame.calibration_stale`
      reflect real calibration state (no longer hardcoded `False`).
- [x] Re-calibrating a stale camera clears `stale` (already true by
      construction — `calibrate()` always writes `stale=False` on a
      fresh success; add a regression test asserting this explicitly
      now that staleness is reachable).
- [x] `@pytest.mark.camera`: physically move camera 3 (or simulate via
      a corner-shifted fixture frame for a headless-equivalent check)
      after calibrating; confirm the daemon reports the calibration
      stale rather than continuing to serve confident wrong positions.
      Done via the documented headless-equivalent alternative
      (`test_end_to_end_drift_via_the_real_daemon_service_wiring_flips_stale`
      in `tests/aprilcam/daemon/test_service.py`) — room lights were off
      for this session (marker detection fails, per ticket 006's
      identical finding) and camera 3 was not physically disturbed, per
      this ticket's own instruction. Physically bumping camera 3 to
      confirm the live path is still open for the stakeholder to verify
      by hand.
- [x] `uv run mypy` clean; baseline 629 passed / 1 skipped stays green;
      zero `typing.Protocol`.

## Implementation Plan

**Files to modify:**
- `src/aprilcam/calibration/calibration.py` — no change to
  `Calibration`'s shape (already has `stale`); possibly a small helper
  for drift comparison against stored static markers if not already
  present.
- `src/aprilcam/daemon/detection.py` — `DetectionEngine.run_burst`: add
  the "is a drift check due" step; per-camera last-checked timestamp;
  populate `TagFrame.calibration_stale` from the (current or
  just-updated) calibration.
- `src/aprilcam/daemon/service.py` — new `_mark_stale(slug)` helper
  (locked); wire it as `Camera`'s `on_reopen` callback in
  `_camera_for`; wire it as the drift check's mutation point (via
  `DetectionEngine`'s injected `calibration_lookup`-style callable, or
  a new injected callable — match the existing injection pattern).
- `src/aprilcam/camera/camera.py` — `__init__`: add `on_reopen`
  parameter; `_attempt_self_heal`: invoke it after a successful
  reopen.

**Archive-reuse pointers:** none identified — staleness detection did
not exist in v1 in this form (`Calibration.stale` is a v2-only field);
this is new logic, not a conversion.

## Testing

- **Existing tests to run**: `tests/aprilcam/daemon/test_detection.py`,
  `tests/aprilcam/camera/test_camera.py`,
  `tests/aprilcam/daemon/test_service.py`, full `uv run pytest` for the
  629/1 baseline.
- **New tests to write**: headless — drift-check timing/threshold using
  fixture frames with known-shifted marker positions (reuse the
  image-matrix fixtures per dev-plan Stage 0/6 conventions);
  `on_reopen` invocation on successful self-heal; concurrency test for
  `_mark_stale`'s locking. `@pytest.mark.camera` — live or
  fixture-simulated bump on camera 3.
- **Verification command**: `uv run pytest`; `uv run pytest -m camera`;
  `uv run mypy`.
