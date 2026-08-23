---
id: '007'
title: L0 synthetic degradation transforms and tests
status: open
use-cases: [UC-10]
depends-on: ['005']
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

- [ ] `tests/image_matrix/transforms.py` implements all seven named
      transforms as pure functions (`image: np.ndarray -> np.ndarray`,
      no I/O, no camera/detector dependency) with unit tests independent
      of any fixture (e.g. a synthetic solid-color or checkerboard array)
      confirming each transform's basic effect (brightness increases
      mean pixel value, gaussian_noise increases variance, etc.).
- [ ] `test_synthetic_degradation.py` applies a mild grid (parameters
      chosen to stay well inside the pipeline's known-good range) to at
      least one confirmed baseline fixture per camera and asserts full
      recall — same recall contract as `test_recall.py`.
- [ ] The same module applies a severe grid and asserts the *documented*
      boundary (a named, explicit expected-recall constant per severity
      level — not "assert failure," since a severe transform degrading
      recall is the expected, correct behavior being pinned).
- [ ] A regression that silently shifts the boundary (either direction)
      fails the severe-grid assertion, mirroring the recall family's
      "both directions visible" rule from ticket 005.
- [ ] Renaming/removing `tests/images/` skips this module's
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
