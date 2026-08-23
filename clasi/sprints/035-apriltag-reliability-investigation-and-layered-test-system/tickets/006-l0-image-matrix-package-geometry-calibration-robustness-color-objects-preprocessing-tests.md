---
id: '006'
title: 'L0 image-matrix package: geometry, calibration-robustness, color-objects, preprocessing tests'
status: open
use-cases: [UC-10]
depends-on: ['005']
github-issue: ''
issue: aprilcam-v2-layered-test-system.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# L0 image-matrix package: geometry, calibration-robustness, color-objects, preprocessing tests

## Description

Adds the remaining four offline assertion families from
`docs/test-plans/camera-image-matrix.md` §6, built on ticket 005's
fixture-discovery conftest: geometry-convention checks, corner/
calibration robustness across the exposure sweep, color/object
detection, and preprocessing effectiveness (informational only). Same
rule as ticket 005: every assertion goes through the daemon's own
`vision.pipeline`/`vision.detector` entry points, never a parallel
reimplementation.

## Acceptance Criteria

- [ ] `test_geometry.py`: using two tags with known east/north
      relationships from the `main-playfield` survey, asserts +x east /
      +y north / north-up on the deskewed image for at least one
      confirmed-fixture image per camera — the axis-mirror regression
      check the world-coordinates-first principle (design.md §2 rule 5a)
      requires.
- [ ] `test_calibration_robustness.py`: all 10 ArUco markers (corners +
      internal) are detected across the full variation range for each
      camera, documenting which configs a real calibration attempt would
      survive (a config where fewer than the required set is detected is
      an expected, named exception, not a silent pass).
- [ ] `test_color_objects.py`: where the confirmed field state includes
      surveyed rectangles/dots, each is found within position tolerance
      across the white-balance variation axis (the stress axis for this
      family per §6.5).
- [ ] `test_preprocessing.py`: detection with/without the highpass
      illumination-flattening stage (`vision.pipeline.PipelineConfig.use_highpass`)
      is compared across the exposure sweep and reported — informational
      only, marked as such, never blocking the gate on its own.
- [ ] Renaming/removing `tests/images/` skips every test in this module
      cleanly, same as ticket 005 (re-verified for this module
      specifically, not assumed inherited).
- [ ] One mutation check per family (geometry, calibration-robustness,
      color-objects), recorded in the PR/commit.

## Implementation Plan

**Approach**: Reuse ticket 005's `conftest.py` fixture-discovery
machinery unchanged — this ticket adds test modules only, no new
discovery logic. Geometry assertions reuse the same east/north survey
facts `tests/aprilcam/calibration/test_geometry.py` already encodes for
live hardware, cited rather than re-derived.

**Files to create**:
- `tests/image_matrix/test_geometry.py`
- `tests/image_matrix/test_calibration_robustness.py`
- `tests/image_matrix/test_color_objects.py`
- `tests/image_matrix/test_preprocessing.py`

**Testing plan**:
- Run against the `fs-20260823` fixtures from ticket 004.
- **Verification command**: `uv run pytest tests/image_matrix/test_geometry.py tests/image_matrix/test_calibration_robustness.py tests/image_matrix/test_color_objects.py tests/image_matrix/test_preprocessing.py -v`

**Documentation updates**:
- None yet — ticket 008 covers doc/traceability updates for the whole
  L0 package.
