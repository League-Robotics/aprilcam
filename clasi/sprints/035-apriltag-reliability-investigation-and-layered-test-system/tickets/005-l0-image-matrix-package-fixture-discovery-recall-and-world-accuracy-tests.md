---
id: '005'
title: 'L0 image-matrix package: fixture discovery, recall and world-accuracy tests'
status: open
use-cases: [UC-10]
depends-on: ['004']
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

- [ ] `tests/image_matrix/conftest.py` discovers every `manifest.json`
      under `tests/images/**` via `rglob`, builds one `FixtureEntry` per
      image (camera slug, config label, field-state id, expected
      visible tags/markers from `field-state.json`, pinned calibration
      snapshot path).
- [ ] Renaming or removing `tests/images/` skips every L0 test in this
      module cleanly (zero failures, zero errors) — verified by actually
      renaming the directory in a test run, not just reasoning about the
      code.
- [ ] A manifest whose `field_state_id` maps to a `field-state.json`
      entry with `status != "confirmed"` (e.g. the July
      `unverified-20260729` stub) is skipped with a clear reason, not
      silently treated as confirmed.
- [ ] `test_recall.py`: for each confirmed-fixture image, detected tag
      IDs (ArUco + AprilTag) are a superset of the field state's expected
      visible IDs. Baseline and near-baseline configs assert 100% recall;
      cliff-crossing configs (the exposure values already known to cross
      the washout cliff) carry an explicit expected-degraded constant, so
      a silent improvement *or* regression on those configs is visible,
      not just a regression.
- [ ] `test_world_accuracy.py`: for surveyed static tags detected in a
      calibrated fixture image, `|world_detected − world_surveyed| ≤
      1.5 cm`, computed via the fixture's own pinned `calibration.json`
      snapshot (never live machine state).
- [ ] One mutation check per family, run manually during development and
      recorded in the PR/commit: falsify one expectation (e.g. assert a
      tag that is not on the field), confirm a readable failure naming
      the fixture and the mismatch, then revert.
- [ ] All assertions call `vision.pipeline.run_pipeline` +
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
