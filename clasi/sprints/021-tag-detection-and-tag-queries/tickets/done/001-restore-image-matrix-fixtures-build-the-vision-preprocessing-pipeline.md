---
id: '001'
title: Restore image-matrix fixtures; build the vision preprocessing pipeline
status: done
use-cases:
- SUC-001
- SUC-002
depends-on: []
github-issue: ''
issue: tag-detection-and-tag-queries.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Restore image-matrix fixtures; build the vision preprocessing pipeline

## Description

`src/aprilcam/vision/` is empty. This ticket lays its first floor: the
frame → detection-ready-image pipeline (dev-plan Stage 4), built as a
named, inspectable sequence of pure functions rather than logic buried
inside a detector. It also restores the substrate every later
headless test in this sprint needs: the captured image-matrix fixtures
and the tool that regenerates them, both still sitting in `archive/`
per Stage 1's "moved, not deleted" policy and never yet moved back
(Stage 0's "Remaining" item, never closed).

**Note `completes_issue: false`** — this ticket is one of six against
`tag-detection-and-tag-queries.md`; the issue archives only when the
last one (007) completes.

## Acceptance Criteria

- [x] `archive/tests/images/` (both camera subdirectories, all `.jpg`
      files, both `manifest.json` files, `field-state.json`) is moved
      to `tests/images/` at the repo root — a `git mv`, not a copy,
      so history follows the files.
- [x] `archive/testdev/capture_matrix.py` is moved to
      `testdev/capture_matrix.py`. A basic import/sanity check
      confirms it does not reference modules deleted in Stage 1; if it
      does, note the specific breakage in this ticket's implementation
      notes as a known gap rather than silently patching in new
      functionality to make it fully runnable — restoring the static
      fixtures is this ticket's job, not repairing the capture tool's
      full functionality.
- [x] **Do not touch `tests/images/field-state.json`'s content.** Its
      `status` is `"unverified"` with empty tag lists — leave it
      exactly as archived. Do not fabricate a "confirmed" state or
      populate `visible_april_tags`/`visible_aruco_tags` yourselves;
      that is stakeholder work outside this ticket (sprint.md's
      Architecture Open Question 1).
- [x] `vision/pipeline.py` exists with discrete, named, pure functions
      for: grayscale conversion, illumination flattening (highpass
      divide), CLAHE, sharpen, and downscale-with-coordinate-rescale.
      Each stage is `image -> image` (or `image -> (image, rescale)`
      for the downscale stage) with declared parameters — not one
      monolithic `_preprocess` method.
- [x] A configured sequence of stages, applied to a fixture image, is
      **deterministic**: the same fixture in produces a byte-identical
      image out on repeat runs (no globals, no hidden state).
- [x] The downscale stage's rescale-back maps a known point to within
      ε of its original position (coordinate integrity — this is where
      geometry discipline starts, per dev-plan Stage 4).
- [x] `uv run mypy` stays clean on the new module; `uv run pytest`
      stays green including `tests/test_typing.py`.

## Testing

- **Existing tests to run**: `uv run pytest` (full suite; baseline is
  216 passed — this ticket should net-add tests, not regress the
  count) and `uv run mypy` directly.
- **New tests to write**: `tests/aprilcam/vision/test_pipeline.py` —
  NO cameras, fixtures only (`tests/images/` after the restore above).
  Per-stage deterministic-output tests (statistical properties:
  flattening normalizes an illumination gradient — variance of block
  means drops; grayscale shape/dtype correct); the downscale/rescale
  round-trip test described above; a pipeline-composition test proving
  a configured sequence is deterministic across repeat runs on the
  same fixture. This is a self-contained correctness test against
  today's `vision/pipeline.py`, not a comparison against v1 output —
  v1's `core/detector.py` is gone by the time this sprint closes it
  out.
- **Verification command**: `uv run pytest tests/aprilcam/vision/ -v`
  then the full `uv run pytest`.

## Implementation Plan

### Approach

1. `git mv archive/tests/images tests/images` and `git mv
   archive/testdev/capture_matrix.py testdev/capture_matrix.py`
   (create `testdev/` if it does not already exist at the repo root).
2. Read `archive/src/aprilcam/core/detector.py`'s `_preprocess`
   static method and the `proc_width` scale/rescale block inside
   `TagDetector.detect` (both around lines 152-243 of that file) —
   this is the algorithm to convert, not the structure to copy.
   Extract each concern into its own function in `vision/pipeline.py`:
   `to_grayscale`, `flatten_illumination` (the highpass-divide,
   parameterized by kernel size), `apply_clahe`, `sharpen`,
   `downscale_with_rescale` (returns the resized image plus a scale
   factor / rescale callable the caller applies to detected points).
3. Compose them behind a small, explicit `PipelineConfig` (or similar)
   dataclass naming which stages are enabled and their parameters —
   mirrors `archive/src/aprilcam/core/detector.py`'s `DetectorConfig`
   fields (`use_highpass`, `highpass_ksize`, `use_clahe`,
   `use_sharpen`, `proc_width`) but scoped to preprocessing only —
   detector-specific fields (`quad_decimate`, `corner_refine`, etc.)
   belong to ticket 003, not here.
4. No camera access, no threading, no I/O beyond reading a fixture
   image in tests — matches the archive module's own stated
   constraint ("no camera access, no threading, no ring buffers").

### Files to create/modify

- `tests/images/` (new, moved from `archive/tests/images/`)
- `testdev/capture_matrix.py` (new, moved from `archive/testdev/`)
- `src/aprilcam/vision/pipeline.py` (new)
- `tests/aprilcam/vision/__init__.py` (new, empty — matches the
  existing `tests/aprilcam/<subsystem>/` convention)
- `tests/aprilcam/vision/test_pipeline.py` (new)

### Documentation updates

None required by this ticket specifically — `docs/design/design.md` §8
already describes the fixtures in present tense (assuming they exist);
restoring them makes that description true rather than requiring a
doc change.

## Implementation Notes

**`archive/tests/images/` restore.** Straightforward `git mv
archive/tests/images tests/images` — 28 `.jpg` files (19 arducam,
9 hd-usb, matching design.md §8's count), both `manifest.json` files,
and `field-state.json` moved with history intact. `field-state.json`'s
content was not touched — `git diff` on it is empty; `status` is still
`"unverified"` with empty tag lists.

**`testdev/capture_matrix.py` location — a real discrepancy found and
resolved, not a literal `archive/testdev` → `testdev` move.**
`archive/testdev/` did not exist at ticket start (confirmed via `find`
and `git log --follow`): a prior commit (`0ed4819`, a broad "Design v2"
commit predating this sprint) had already moved
`archive/testdev/{capture_matrix.py,capture_matrix.yaml,
detect_pipeline.py,detect_test.py,sample_frame.jpg}` to
`tests/testdev/`, and ticket 020-007 subsequently added
`tests/testdev/conftest.py` with a `collect_ignore` to stop
`detect_test.py` (which matches pytest's `*_test.py` glob but has no
`import cv2`-free path) from breaking whole-suite collection.

That left the tooling live at `tests/testdev/`, one level away from
every reference to it: `docs/design/design.md` §8, `development-plan.md`
(three separate mentions), `docs/test-plans/camera-image-matrix.md`, this
ticket's own acceptance criterion text, and every script's own `Usage`
docstring (`python testdev/capture_matrix.py ...`) all name the
repo-root `testdev/`, never `tests/testdev/`. `tests/__init__.py`'s own
docstring already treated `testdev/` as logically outside the
`tests.aprilcam` package tree. Per this ticket's explicit instruction to
"check what is already in place before copying" rather than blindly
follow the archive-path step, the discrepancy was resolved by moving the
whole directory one more hop: `git mv tests/testdev testdev` (history
preserved for all five files). This also happens to make the 020-007
`collect_ignore` workaround unnecessary for the default `pytest`
invocation (`testpaths = ["tests"]` no longer reaches `testdev/` at
all) — left in place regardless, since it still protects a direct
`pytest testdev/`/`pytest .` invocation that bypasses `testpaths`.
Updated the two docstrings that described the old location
(`testdev/conftest.py`, `tests/__init__.py`) for accuracy; no behavior
change beyond the move itself.

**`testdev/capture_matrix.py` import sanity check — known gap, not
repaired (per the ticket's explicit scope boundary).** `import
testdev.capture_matrix` fails; a full `ast`-based scan of its
`aprilcam.*` imports plus individual import attempts found:

- `aprilcam.camera.camutil` — module does not exist (Stage 1 deletion;
  no `camutil.py` anywhere under `src/aprilcam/camera/`).
- `aprilcam.camera.camera_config` (`load_camera_config`) — module does
  not exist; camera config now lives under `aprilcam.config`.
- `aprilcam.camera.registry.dir_slug` — `registry.py` exists but no
  longer exports `dir_slug`.
- `aprilcam.config.Config` — the `config` package exists but no longer
  exports a `Config` class (see `config/directories.py`,
  `config/interfaces.py` for what replaced it).
- `aprilcam.core.detector` (`DetectorConfig`, `TagDetector`) — the
  entire `aprilcam.core` package is gone; this is exactly the module
  ticket 003 rebuilds as `vision/detector.py`.
- `aprilcam.camera.identity` — imports cleanly; unaffected.

Per the acceptance criterion's own instruction, none of this was
patched — restoring the static fixtures is this ticket's job, not
repairing the capture tool. `testdev/detect_test.py` and
`testdev/detect_pipeline.py` (siblings moved in the same `git mv`, not
separately audited) are very likely in the same state; not exercised
here as they are outside this ticket's acceptance criteria.

**`vision/pipeline.py` design decisions.**

- Stage order in `run_pipeline` is grayscale → downscale → illumination
  flattening → CLAHE → sharpen — v1's actual execution order in
  `TagDetector.detect` (downscale happens *before* the
  highpass/CLAHE/sharpen block), not the prose-listing order used in
  the ticket text and the sprint's module table. Preserved deliberately:
  running the expensive Gaussian blur on the already-shrunk image
  matches the dev plan's "efficiency is a requirement, not a hope" rule
  for this exact pipeline. Documented at length in the module docstring
  so ticket 003 does not "fix" the order back to the prose listing.
- `Rescale` is a small frozen dataclass (`scale: float`) with a
  `to_original(x, y)` method, matching the "any new value type is a
  frozen `@dataclass(kw_only=True)`" rule (sprint.md Test Strategy). It
  stores the *analytic* pre-rounding scale factor (matching v1's own
  `pts / scale` approach) rather than recomputing from the rounded
  output dimensions, so the round trip is exact to floating-point
  precision, not just "close." Ticket 003 can divide an `(N, 2)` corner
  array by `.scale` directly for the batch case; documented in
  `Rescale`'s docstring rather than adding a second method for it.
- `PipelineConfig` carries only preprocessing fields (`use_highpass`,
  `highpass_ksize`, `use_clahe`, `clahe_clip_limit`,
  `clahe_tile_grid_size`, `use_sharpen`, `proc_width`) — no detector
  fields (`quad_decimate`, `corner_refine`, family selection, AprilTag
  quad-detector thresholds all stay out, per the ticket's explicit
  scope line and sprint.md Step 2's "changes independently" module
  boundary).
- CLAHE's `clip_limit`/`tile_grid_size` were hardcoded inline in v1
  (`clipLimit=2.0, tileGridSize=(8, 8)`, never configurable); made into
  `PipelineConfig` fields here with those same values as defaults,
  since nothing in v1 ever varied them and a config field costs nothing.

**What ticket 003 needs to know**, restated from the module docstring:
call `run_pipeline(image_bgr, PipelineConfig(...))` for the common case
— one call returns the detection-ready grayscale image and the
`Rescale` needed to map detected corners back to the original frame.
Call the individual stage functions (`to_grayscale`,
`flatten_illumination`, `apply_clahe`, `sharpen`,
`downscale_with_rescale`) directly only when an intermediate result is
needed for its own sake — they are public, tested, and exported via
`vision.pipeline.__all__` for exactly that reason.

**Test suite**: `uv run pytest tests/aprilcam/vision/ -v` — 20 passed.
Full `uv run pytest` — 236 passed (was 216; net +20, no regressions).
`uv run mypy` — "Success: no issues found in 40 source files."
