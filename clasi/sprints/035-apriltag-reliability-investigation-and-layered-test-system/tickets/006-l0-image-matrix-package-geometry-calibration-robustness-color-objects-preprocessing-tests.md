---
id: '006'
title: 'L0 image-matrix package: geometry, calibration-robustness, color-objects,
  preprocessing tests'
status: exception
use-cases:
- UC-10
depends-on:
- '005'
github-issue: ''
issue: aprilcam-v2-layered-test-system.md
completes_issue: false
exception:
  thrown_by: programmer
  thrown_at: '2026-08-24T00:26:41.238388+00:00'
  attempted: 'Implemented and verified three of the ticket''s four assertion families
    (test_geometry.py, test_calibration_robustness.py, test_preprocessing.py) against
    the real fs-20260823 fixtures, all passing with real, earned data. For the fourth,
    test_color_objects.py, searched the entire codebase (src/aprilcam/vision/, daemon/,
    mcp/tools/, playfield/query.py, and the archive) for any live color/object classifier
    the daemon could call, since the ticket requires routing through the daemon''s
    own vision.pipeline/vision.detector entry points, never a parallel reimplementation.
    None exists. Considered testing playfield.query.FeatureQuery''s static "where
    is X" survey lookup instead, but rejected it: that lookup never touches pixel
    data, so it would return the identical answer for every white-balance-varied fixture
    -- a vacuous pass across the exact stress axis this family is supposed to exercise
    (test plan Sec.6.5), which is precisely the "silently under-testing" anti-pattern
    ticket 005''s own test_world_accuracy.py explicitly refuses to produce (it skips
    rather than vacuously passing on images with no surveyed-tag detections). Considered
    writing a minimal classifier myself, but rejected that too: it would be new production
    vision code, far outside a test-writing ticket''s scope, disruptive with three
    other agents concurrently active in this same checkout, and would not fix the
    underlying architecture conflict anyway.'
  conflict: 'docs/design/design.md Sec.6 "Feature roles" rule: "ArUco + AprilTags
    are calibration ground truth; colored rectangles/dots are queryable surveyed positions
    only, never vision-confirmed." src/aprilcam/vision/DESIGN.md Sec.6 independently
    confirms the same gap as a known limitation: "No object/color/contour detection
    lives in this subsystem... Whatever backs the MCP get_objects tool is not implemented
    anywhere in src/aprilcam/vision/ as of this review." The ticket''s acceptance
    criterion for test_color_objects.py ("each is found within position tolerance
    across the white-balance variation axis") and the L0 test plan''s item 5 ("classifier
    finds each surveyed colored object within position tolerance") both presuppose
    a live vision-confirmation capability that the current, explicitly-stated architecture
    forbids for this feature class.'
  surface: user-visible
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

- [x] `test_geometry.py`: using two tags with known east/north
      relationships from the `main-playfield` survey, asserts +x east /
      +y north / north-up on the deskewed image for at least one
      confirmed-fixture image per camera — the axis-mirror regression
      check the world-coordinates-first principle (design.md §2 rule 5a)
      requires.
- [x] `test_calibration_robustness.py`: all 10 ArUco markers (corners +
      internal) are detected across the full variation range for each
      camera, documenting which configs a real calibration attempt would
      survive (a config where fewer than the required set is detected is
      an expected, named exception, not a silent pass).
- [ ] `test_color_objects.py`: where the confirmed field state includes
      surveyed rectangles/dots, each is found within position tolerance
      across the white-balance variation axis (the stress axis for this
      family per §6.5). **BLOCKED — exception thrown, see Handoff Notes.**
      No live color/object classifier exists anywhere in this codebase,
      and `design.md` Sec.6's "Feature roles" rule states rectangles/dots
      are "queryable surveyed positions only, never vision-confirmed" —
      building one would override that architecture decision, which is
      outside this ticket's (test-writing) scope.
- [x] `test_preprocessing.py`: detection with/without the highpass
      illumination-flattening stage (`vision.pipeline.PipelineConfig.use_highpass`)
      is compared across the exposure sweep and reported — informational
      only, marked as such, never blocking the gate on its own.
- [x] Renaming/removing `tests/images/` skips every test in this module
      cleanly, same as ticket 005 (re-verified for this module
      specifically, not assumed inherited) — for the three implemented
      modules (`test_geometry.py`, `test_calibration_robustness.py`,
      `test_preprocessing.py`); `test_color_objects.py` was not created,
      see the blocked criterion above.
- [x] One mutation check per family (geometry, calibration-robustness,
      color-objects), recorded in the PR/commit — geometry and
      calibration-robustness done (see Handoff Notes); color-objects is
      not applicable since that file was never created (blocked
      criterion above).

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

## Handoff Notes (2026-08-23, ticket 006 implementation)

**Three of four assertion families implemented**: `test_geometry.py`,
`test_calibration_robustness.py`, `test_preprocessing.py`. All three
reuse ticket 005's `conftest.py` discovery machinery unchanged (no
parallel discovery logic), and every detection call goes through
`vision.pipeline.run_pipeline` + `aprilcam.daemon.detection.
_default_detector_factory(slug).detect` — grep-verified, no second
detection code path. `test_geometry.py` additionally defines its own
`pytest_generate_tests` (a test module implementing this hook alongside
`conftest.py`'s own — both fire, parametrizing different-named
arguments) to reduce the discovered fixtures to one representative
image per camera, since its acceptance criterion asks for "at least one
confirmed-fixture image per camera," not every fixture.

**`test_color_objects.py` is blocked — exception thrown against this
ticket.** Investigated whether any color/object classifier exists
anywhere in `src/aprilcam/` (searched `vision/`, `daemon/`, `mcp/
tools/`, `playfield/query.py`) — none does. `vision/DESIGN.md` §6
states this explicitly: "No object/color/contour detection lives in
this subsystem... Whatever backs the MCP `get_objects` tool is not
implemented anywhere in `src/aprilcam/vision/` as of this review."
`docs/design/design.md` §6's "Feature roles" rule is even more direct:
"ArUco + AprilTags are calibration ground truth; colored rectangles/
dots are queryable surveyed positions only, **never vision-confirmed**."
Considered testing `playfield.query.FeatureQuery` (the static "where is
the blue dot" survey lookup) instead, but that would be vacuous across
the white-balance axis this family is supposed to stress (a survey
position never changes regardless of which image was captured) — a
"silently under-testing" vacuous pass exactly the kind ticket 005's own
`test_world_accuracy.py` explicitly refuses to write. Considered writing
a minimal classifier myself, but that would override `design.md`'s
explicit architecture rule and is far outside a test-writing ticket's
scope, especially with three other agents concurrently active in this
checkout. Threw a ticket exception (`thrown_by="programmer"`,
`surface="user-visible"` — sprint.md's own SUC-001 Main Flow step 3
names "color-object" assertions as part of the L0 suite's described
behavior) rather than silently redefining or skipping the acceptance
criterion.

**Real-run verification** (throwaway local copy of
`tests/images/field-state.json` with `states["fs-20260823"]["status"]`
temporarily forced to `"confirmed"`, never committed — same pattern
ticket 005 used): `uv run pytest tests/image_matrix/test_geometry.py
tests/image_matrix/test_calibration_robustness.py tests/image_matrix/
test_preprocessing.py tests/image_matrix/test_recall.py tests/
image_matrix/test_world_accuracy.py -q` → **175 passed, 48 skipped**
(3 geometry cases — one baseline image per camera; 55 calibration-
robustness cases; 28 preprocessing cases against the exposure sweep, 27
skipped for non-exposure configs; 89 from ticket 005's own two
modules). `git diff` on `field-state.json` confirmed clean after
reverting.

**Renamed-directory check** (`mv tests/images tests/images.bak`, ran
the three new modules, `3 skipped` with "fixture directory ... does not
exist," restored the directory): confirmed clean both ways, for this
module specifically.

**Mutation checks**, both against the same throwaway-confirmed copy:
- **Geometry**: swapped `main-playfield`'s east/west reference pair
  (`east_id=4, west_id=8` → `east_id=8, west_id=4`) → both camera 3 and
  camera 5's baseline cases failed readably, naming the fixture and the
  exact deskewed pixel coordinates (`east marker (ArUco 8, deskewed
  x=1.0px) is not further right than the west marker (ArUco 4, deskewed
  x=802.4px)`); reverted, re-ran, 3/3 pass.
- **Calibration-robustness**: added a bogus id (6) to camera 3's
  `exp-40` degraded set (`{2,3,4,5,10}` → `{2,3,4,5,6,10}`) → the single
  `fs-20260823/arducam-ov9782-usb-camera/exp-40` case failed readably,
  naming the fixture and the exact mismatch (`documented
  calibration-robustness boundary changed -- ... now sees [2, 3, 4, 5,
  10]`); reverted, re-ran, passes.
- **Color-objects**: not applicable — the file was never created (see
  the blocked criterion above).

**A note on concurrency**: partway through this work, `tests/images/
field-state.json`'s throwaway `"confirmed"` flip was found reverted to
`"unverified"` on disk without this agent's action — most likely another
concurrently-dispatched agent's git operation touched the working tree
(three other tickets were active in this same checkout). The flip was
simply re-applied for the remaining verification runs and reverted
again at the end; no ticket-006 file was affected.
