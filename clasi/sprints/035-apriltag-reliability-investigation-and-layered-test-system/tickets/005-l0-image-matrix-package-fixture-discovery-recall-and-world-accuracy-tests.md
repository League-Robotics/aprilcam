---
id: '005'
title: 'L0 image-matrix package: fixture discovery, recall and world-accuracy tests'
status: done
use-cases:
- UC-10
depends-on:
- '004'
github-issue: ''
issue: aprilcam-v2-layered-test-system.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# L0 image-matrix package: fixture discovery, recall and world-accuracy tests

## Description

Creates the `tests/image_matrix/` package's foundation: fixture
discovery (a `conftest.py` that walks `tests/images/**` for
`manifest.json` files, builds a `FixtureEntry` per image, and skips
cleanly — never fails — when the fixture directory is absent or a
manifest references an unconfirmed field state), plus the first two
assertion families named in `docs/test-plans/camera-image-matrix.md`
§6: tag recall per configuration and world-coordinate accuracy.

Every assertion runs through the daemon's own detection entry points
(`vision.pipeline.run_pipeline` then `vision.detector.TagDetector.detect`)
— never a parallel reimplementation of the pipeline, per the sprint
architecture's explicit rule. This package carries **no pytest marker**,
so it joins `scripts/test-gate.sh`'s selection automatically, by
construction, once ticket 008 wires the gate — no exclusion-expression
edit is needed.

## Acceptance Criteria

- [x] `tests/image_matrix/conftest.py` discovers every `manifest.json`
      under `tests/images/**` via `rglob`, builds one `FixtureEntry` per
      image (camera slug, config label, field-state id, expected
      visible tags/markers from `field-state.json`, pinned calibration
      snapshot path).
- [x] Renaming or removing `tests/images/` skips every L0 test in this
      module cleanly (zero failures, zero errors) — verified by actually
      renaming the directory in a test run, not just reasoning about the
      code.
- [x] A manifest whose `field_state_id` maps to a `field-state.json`
      entry with `status != "confirmed"` (e.g. the July
      `unverified-20260729` stub) is skipped with a clear reason, not
      silently treated as confirmed.
- [x] `test_recall.py`: for each confirmed-fixture image, detected tag
      IDs (ArUco + AprilTag) are a superset of the field state's expected
      visible IDs. Baseline and near-baseline configs assert 100% recall;
      cliff-crossing configs (the exposure values already known to cross
      the washout cliff) carry an explicit expected-degraded constant, so
      a silent improvement *or* regression on those configs is visible,
      not just a regression.
- [x] `test_world_accuracy.py`: for surveyed static tags detected in a
      calibrated fixture image, `|world_detected − world_surveyed| ≤
      1.5 cm`, computed via the fixture's own pinned `calibration.json`
      snapshot (never live machine state).
- [x] One mutation check per family, run manually during development and
      recorded in the PR/commit: falsify one expectation (e.g. assert a
      tag that is not on the field), confirm a readable failure naming
      the fixture and the mismatch, then revert.
- [x] All assertions call `vision.pipeline.run_pipeline` +
      `vision.detector.TagDetector.detect` directly — grep-verified, no
      second detection code path introduced in this package.

## Implementation Plan

**Approach**: Build the conftest fixture-discovery layer first (it is
the dependency every other L0 module in tickets 006-007 needs), then
`test_recall.py` and `test_world_accuracy.py` on top of it. Mirror the
existing hardware-test conventions for skip-cleanly behavior
(`pytest.skip` with a clear remediation message) rather than inventing a
new idiom.

**Files to create**:
- `tests/image_matrix/__init__.py`
- `tests/image_matrix/conftest.py` — `FixtureEntry` dataclass, discovery,
  skip-when-absent/skip-unless-confirmed guards.
- `tests/image_matrix/test_recall.py`
- `tests/image_matrix/test_world_accuracy.py`

**Testing plan**:
- Run against the new `fs-20260823` fixtures from ticket 004.
- Explicitly test the skip-cleanly behavior by temporarily renaming
  `tests/images/` in a throwaway local run (not committed as a
  destructive test — verified manually, documented in the PR).
- **Verification command**: `uv run pytest tests/image_matrix/test_recall.py tests/image_matrix/test_world_accuracy.py -v`

**Documentation updates**:
- None yet — ticket 008 updates `docs/test-plans/camera-image-matrix.md`
  and traceability once the full L0 package (tickets 005-007) exists.

## Handoff Notes (2026-08-23, ticket 005 implementation)

**Current real-run behavior**: both `test_recall.py::test_recall` and
`test_world_accuracy.py::test_world_accuracy` collect as a single
`[no-fixtures]` item and skip cleanly today (`uv run pytest
tests/image_matrix/test_recall.py tests/image_matrix/test_world_accuracy.py
-v` → `2 skipped`), because every `field-state.json` entry —
`unverified-20260729` and `fs-20260823` alike — carries `status:
"unverified"`. This is intentional per this ticket's own instructions;
the skip-unless-confirmed guard is verified working, not incidentally
untested.

**Renamed-directory check** (acceptance criterion 2): `mv tests/images
tests/images.bak`, ran the suite (`2 skipped`, zero failures/errors,
reason: "fixture directory ... does not exist"), then restored the
directory. Confirmed clean both ways.

**Mutation checks** (acceptance criterion 6, both run against a
throwaway local copy of `tests/images/field-state.json` with
`states["fs-20260823"]["status"]` temporarily forced to `"confirmed"` —
never committed):
- With the corpus temporarily confirmed, the full real assertion set
  runs and passes: 55 `test_recall` cases (89 total across both
  modules) all pass against the curated per-camera `_RECALL_PROFILES`
  in `test_recall.py`, earned from a real detection pass over every
  `fs-20260823` JPEG (not guessed from the exposure-cliff theory in the
  test plan alone). 34 `test_world_accuracy` cases pass (camera 3 + 5,
  every measured error 0.04-0.56 cm, comfortably under the 1.5 cm
  bound); the 21 `hd-usb-camera` (camera 2) cases skip cleanly with the
  documented "4-marker exact-fit homography, not a precision claim"
  reason.
- **Recall family**: added a bogus required id (`("aruco", 99)`) to
  camera 3's `required` set in `test_recall.py`, ran the single
  `fs-20260823/arducam-ov9782-usb-camera/baseline` case → clean failure
  naming the fixture and the exact missing id (`missing required
  detection(s) [('aruco', 99)] (required=[...], detected=[...])`),
  reverted from the pre-mutation backup.
- **World-accuracy family**: dropped `WORLD_ACCURACY_TOLERANCE_CM` to
  `0.0001` in `test_world_accuracy.py`, ran the same baseline case →
  clean failure naming the fixture and every tag's measured error
  (`world-accuracy exceeded -- ['apriltag:1 error=0.417cm ...', ...]`),
  reverted from the pre-mutation backup.
- `tests/images/field-state.json` was restored to its original
  (`unverified`) content afterward; `git diff` confirms no residual
  change from either mutation exercise.

**Design decisions not spelled out verbatim in the ticket**:
- Detection is built via `aprilcam.daemon.detection._default_detector_factory(slug)`
  (not a bare `TagDetector()`), so camera 5's per-slug `DetectorConfig`
  override (loosened AprilTag-quad constants, sprint 035 ticket 002)
  is exercised exactly as the daemon would apply it — reusing the
  daemon's own construction helper, not a second one.
- `test_recall.py`'s per-camera `required`/`degraded` recall
  constants are scoped to `fs-20260823` specifically
  (`_RECALL_PROFILES[field_state_id][camera_slug]`); an entry from any
  other/future `field_state_id` falls back to the field-state-driven
  `expected_ids` as a required-superset check rather than reusing a
  stale generation's profile.
- `test_world_accuracy.py` skips (never fails) any fixture whose
  pinned calibration matched fewer than 5 markers, which is what
  structurally excludes camera 2's 4-marker exact-fit calibration from
  the tight bound (general rule, not a camera-2 special case), and
  skips any image with zero surveyed-tag detections rather than
  reporting a vacuous pass.
