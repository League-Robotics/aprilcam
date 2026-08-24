---
id: '007'
title: L0 synthetic degradation transforms and tests
status: done
use-cases:
- UC-10
depends-on:
- '005'
github-issue: ''
issue: aprilcam-v2-layered-test-system.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# L0 synthetic degradation transforms and tests

## Description

Adds pure `ndarray -> ndarray` synthetic-degradation transforms
(`exposure_scale`, `gamma`, `contrast`, `brightness`, `gaussian_blur`,
`gaussian_noise`, `jpeg_recompress`) and a test module that applies a
mild and a severe grid of them to confirmed baseline fixture images,
asserting full recall on the mild grid and the documented degradation
boundary on the severe grid. This complements ticket 005/006's captured
sweeps (real camera variation) with synthetic stress that does not
require re-capturing hardware — e.g. testing noise/blur robustness the
physical capture matrix does not directly vary.

## Acceptance Criteria

- [x] `tests/image_matrix/transforms.py` implements all seven named
      transforms as pure functions (`image: np.ndarray -> np.ndarray`,
      no I/O, no camera/detector dependency) with unit tests independent
      of any fixture (e.g. a synthetic solid-color or checkerboard array)
      confirming each transform's basic effect (brightness increases
      mean pixel value, gaussian_noise increases variance, etc.).
- [x] `test_synthetic_degradation.py` applies a mild grid (parameters
      chosen to stay well inside the pipeline's known-good range) to at
      least one confirmed baseline fixture per camera and asserts full
      recall — same recall contract as `test_recall.py`.
- [x] The same module applies a severe grid and asserts the *documented*
      boundary (a named, explicit expected-recall constant per severity
      level — not "assert failure," since a severe transform degrading
      recall is the expected, correct behavior being pinned).
- [x] A regression that silently shifts the boundary (either direction)
      fails the severe-grid assertion, mirroring the recall family's
      "both directions visible" rule from ticket 005.
- [x] Renaming/removing `tests/images/` skips this module's
      fixture-dependent tests cleanly; the pure-transform unit tests (no
      fixture dependency) are unaffected and still run.

## Implementation Plan

**Approach**: `transforms.py` has zero dependency on
`tests/image_matrix/conftest.py`'s fixture discovery — build and unit
test it standalone first, then `test_synthetic_degradation.py` composes
it with ticket 005's fixture loader.

**Files to create**:
- `tests/image_matrix/transforms.py`
- `tests/image_matrix/test_transforms.py` (pure unit tests, no fixture
  dependency)
- `tests/image_matrix/test_synthetic_degradation.py`

**Testing plan**:
- `test_transforms.py` runs with zero fixtures present (pure functions
  over synthetic arrays).
- `test_synthetic_degradation.py` runs against the `fs-20260823`
  baseline fixtures.
- **Verification command**: `uv run pytest tests/image_matrix/test_transforms.py tests/image_matrix/test_synthetic_degradation.py -v`

**Documentation updates**:
- None yet — ticket 008 covers doc/traceability updates for the whole
  L0 package.

## Handoff Notes (2026-08-23, ticket 007 implementation)

**Files added**: `tests/image_matrix/transforms.py` (7 pure transforms),
`tests/image_matrix/test_transforms.py` (21 fixture-free unit tests),
`tests/image_matrix/test_synthetic_degradation.py` (mild + severe grid
tests). `conftest.py` was not touched — `test_synthetic_degradation.py`
calls `discover_fixture_entries()` directly and builds its own
`baseline_entry` parametrize list (filtered to `config_label ==
"baseline"`), deliberately using a parameter name other than
`fixture_entry` so conftest's package-wide `pytest_generate_tests` hook
does not also try to parametrize it over all 55 images.

**Current real-run behavior**: `uv run pytest
tests/image_matrix/test_transforms.py
tests/image_matrix/test_synthetic_degradation.py -v` → **21 passed, 18
skipped** (11 mild-grid cases + 7 severe-grid cases, each currently
collecting as a single `[..., no-fixtures]` skip because
`field-state.json`'s `fs-20260823` entry is `status: "unverified"` —
same guard `test_recall.py`/`test_world_accuracy.py` rely on).
`test_transforms.py`'s 21 unit tests have no fixture dependency and run
regardless.

**Renamed-directory check**: `mv tests/images tests/images.bak`, ran
both new modules together → 21 passed (unit tests, unaffected), 18
skipped (fixture-dependent, reason names the missing directory), zero
failures/errors. Restored the directory afterward.

**Measured mild/severe boundaries** (all numbers earned from a real
`run_pipeline` + `_default_detector_factory(slug).detect` pass over
each camera's actual `fs-20260823/<slug>/baseline.jpg`, transformed by
`transforms.py`'s own functions — not guessed):

- A first coarse sweep (7-8 values per transform, `exposure_scale` 0.5–
  2.0, `contrast` 0.5–2.0, `gamma` 0.5–2.2, `brightness` ±20 to ±90,
  `gaussian_blur` ksize 3–21, `gaussian_noise` sigma 5–80,
  `jpeg_recompress` quality 90 down to 1) showed camera 3
  (`arducam-ov9782-usb-camera`) and camera 5 (`arducam-ov9281-usb-camera`,
  required set already excluding ArUco 1 per this sprint's own facts)
  tolerate most of that range, but two markers — ArUco 7 on camera 5 and
  ArUco 2 on camera 2 (`hd-usb-camera`) — flip in and out unpredictably
  even at very small perturbations (e.g. camera 2's ArUco 2 detected at
  JPEG quality 99, missing at 98, detected again at 97/96, missing again
  at 95; `contrast` 1.02 detected but 1.04 missing). This is genuine
  threshold-edge behavior on this particular baseline image, not sweep
  noise — a finer 1-unit-step sweep around each flip confirmed it.
  Camera 5's ArUco 7 finding also corroborates a *pre-existing*
  `field-state.json` `per_camera` note for that camera calling out
  "ArUco 1 and ArUco 7 (far corners, per stakeholder) — INTERMITTENT",
  independent of this sweep.
- **Mild grid** (11 cases, one or two values per transform — see
  `_MILD_GRID` in `test_synthetic_degradation.py`): `exposure_scale`
  0.85/1.15, `gamma` 0.85/1.15, `contrast` 0.9/1.1, `brightness` ±15,
  `gaussian_blur` ksize=3, `gaussian_noise` sigma=3 (seed=1),
  `jpeg_recompress` quality=80. Verified (throwaway-confirmed run, see
  below) that every one of these leaves `_MILD_REQUIRED` fully intact
  for all three cameras. `_MILD_REQUIRED` is `_UNIVERSE` minus the two
  threshold-edge ids above (ArUco 7 for camera 5, ArUco 2 for camera 2)
  — held to the same "required set is per-camera and earned, never
  guessed" rule `test_recall.py` established, not a new global rule.
- **Severe grid** (7 cases, one clearly-degraded value per transform):
  `exposure_scale` 2.5, `gamma` 3.0, `contrast` 2.5, `brightness` -100,
  `gaussian_blur` ksize=17, `gaussian_noise` sigma=50 (seed=1),
  `jpeg_recompress` quality=3. Exact resulting `detected & _UNIVERSE`
  set pinned per (camera, case) in `_SEVERE_GRID` — 21 pinned constants
  total. Notable, real, differential findings pinned as-measured rather
  than smoothed over: `gaussian_blur` ksize=17 collapses camera 3 to 2
  ids and camera 5 to 1 id, but leaves camera 2 at full recall (its
  markers are evidently coarser/less detail-dependent); `contrast` 2.5
  leaves camera 3 and camera 5 at full recall but drops camera 2 to 2 of
  4 ids.

**What was capped, and why**: this module runs against exactly the
`"baseline"`-labeled image per camera (3 images), not all 55
`fs-20260823` JPEGs — ticket 005/006 already cover the full physical
capture matrix for real-camera-variation recall; this ticket's job is
synthetic-transform coverage on top of that, not a second full sweep.
Each grid is capped at one (mild) or two (severe, when both directions
of a transform were informative) representative parameter values per
transform, chosen from the wider manual sweep above, rather than a full
per-transform range × per-camera cross product — the direction and
rough magnitude of each transform's effect was established by the
sweep; multiplying every transform's full range against every camera
would add hundreds of detection calls for marginal additional signal.
Real runtime: the 54-case throwaway-confirmed run (mild + severe, all 3
cameras) completed in 5.5s.

**Mutation checks** (both run directly against the real module — no
throwaway copy needed since the assertions themselves are the mutation
target, not fixture data — reverted immediately after, `git diff`
confirmed clean):
- **Mild-grid family**: added a bogus id (`("aruco", 99)`) to camera 3's
  `_MILD_REQUIRED` entry, ran
  `test_mild_degradation_full_recall[exposure_scale-0.85-arducam-ov9782-usb-camera]`
  against a throwaway-confirmed `field-state.json` → clean failure
  naming the fixture, the case, and the exact missing id (`mild
  synthetic degradation dropped required detection(s) [('aruco', 99)]
  ...`). Reverted.
- **Severe-grid family**: added a bogus id (`aruco 9`) to camera 3's
  `exposure_scale-2.5` expected set, ran
  `test_severe_degradation_documented_boundary[exposure_scale-2.5-arducam-ov9782-usb-camera]`
  → clean failure naming the fixture, the case, and showing the exact
  expected-vs-detected set diff (pytest's own set-diff output, "Extra
  items in the right set: ('aruco', 9)"). Reverted.
- `tests/image_matrix/test_synthetic_degradation.py` was diffed against
  a pre-mutation backup after each revert and found byte-identical both
  times.

**Concurrency note**: `tests/images/field-state.json` is a *shared*
file across this sprint's concurrently-executing tickets (006 also
temporarily flips `fs-20260823` to `"confirmed"` for its own mutation
checks, per its own ticket's identical instructions). Mid-implementation
this ticket's own throwaway backup of that file was itself captured
already in a `"confirmed"` state (a concurrent ticket-006 run in
flight), so restoring from that backup left the file diffed against
HEAD. Caught by re-running `git diff tests/images/field-state.json`
before finishing rather than trusting the backup-restore alone; fixed
with `git checkout -- tests/images/field-state.json` to land back on
the exact committed (`"unverified"`) content. Final `git status
--short` shows no diff on that file. Worth flagging for ticket 008 or a
future sprint: this file cannot safely support two agents' concurrent
temporary-flip-and-revert cycles at once without a lock or per-ticket
copy — this run happened to catch it, but the pattern is inherently
racy in a shared (non-worktree) checkout.

**Design decisions not spelled out verbatim in the ticket**:
- Both transform grids run through `run_pipeline` + the daemon's own
  `_default_detector_factory(slug)` — identical to `test_recall.py`'s
  call pair, so camera 5's per-slug `DetectorConfig` override is
  exercised exactly as the live daemon would apply it.
- `_UNIVERSE` (full per-camera visible-id set) and `_MILD_REQUIRED`
  (trimmed for two threshold-edge ids, mild grid only) are deliberately
  separate dicts: the severe grid always intersects against `_UNIVERSE`
  (matching `test_recall.py`'s `degraded` dict, which also intersects
  against the full `required`, not a trimmed set) — only the *mild*
  full-recall claim needed trimming to avoid asserting 100% recall on
  markers already shown to sit at their own detection threshold.
- `gaussian_noise`'s determinism is verified twice: once directly in
  `test_transforms.py` (same seed → `np.array_equal`, different seed →
  not equal), and implicitly by every synthetic-degradation test using
  a fixed `seed=1` and re-running cleanly across the throwaway-confirmed
  pass and the mutation-check passes with identical detected-id results
  each time.
