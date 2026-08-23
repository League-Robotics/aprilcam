---
id: '001'
title: Widen DetectorFactory to per-camera detector tuning
status: done
use-cases:
- UC-09
depends-on: []
github-issue: ''
issue: apriltag-1-detects-unreliably-on-arducam-ov9281-usb-camera.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Widen DetectorFactory to per-camera detector tuning

## Description

Today `daemon.detection.DetectionEngine` builds one `TagDetector` per
camera slug through a single, zero-argument `DetectorFactory`
(`Callable[[], TagDetector]`, default `_default_detector_factory`
returning `TagDetector()`), lazily cached in `_detector_for(slug)`. Every
camera therefore gets byte-identical `vision.detector.DetectorConfig`
defaults — there is no mechanism today for camera 5
(`arducam-ov9281-usb-camera`) to use different `quad_decimate`/
`corner_refine` values than camera 3 without changing every camera at
once.

This ticket is pure plumbing: it widens `DetectorFactory` to
`Callable[[str], TagDetector]` (the camera slug) and adds an empty
per-slug override table that the next ticket (002) populates with one
entry. This ticket must not change detection behavior for any camera —
it only makes the next ticket possible without a second signature
change. `vision.detector.DetectorConfig` itself (a frozen dataclass) is
untouched; only how `daemon/detection.py` constructs a `TagDetector`
changes.

## Acceptance Criteria

- [x] `DetectorFactory` in `src/aprilcam/daemon/detection.py` is retyped
      `Callable[[str], TagDetector]`.
- [x] `_default_detector_factory(slug: str) -> TagDetector` looks up
      `slug` in a new, currently-empty `_DETECTOR_CONFIG_OVERRIDES: dict[str,
      DetectorConfig]` module constant; with no entries yet, it returns
      `TagDetector()` for every slug, identically to today.
- [x] `DetectionEngine._detector_for(slug)` calls
      `self._detector_factory(slug)` instead of `self._detector_factory()`.
- [x] `DetectionEngine.__init__`'s `detector_factory` parameter default
      updates accordingly; its docstring is updated to describe the new
      one-argument shape.
- [x] Any existing test double that constructs a `DetectionEngine` with a
      custom zero-arg `detector_factory` (grep
      `tests/aprilcam/daemon/` for `detector_factory=`) is updated to the
      one-argument shape.
- [x] No behavior change for any camera: existing daemon/detection unit
      and hardware tests pass unmodified in outcome (only the fixture
      signature, where applicable, changes).

## Implementation Plan

**Approach**: Widen the type alias, the default factory function, and
the one call site together in a single commit, since they must stay in
sync. Add the override table as a plain module-level dict so ticket 002
is a one-line addition, not a second factory-shape change.

**Files to modify**:
- `src/aprilcam/daemon/detection.py` — `DetectorFactory` type alias,
  `_default_detector_factory`, `DetectionEngine._detector_for`, the new
  `_DETECTOR_CONFIG_OVERRIDES` constant (empty in this ticket).
- `tests/aprilcam/daemon/` — update any test double passing a custom
  `detector_factory=` to the new one-argument callable shape (grep
  first; do not assume none exist).

**Testing plan**:
- Run the full existing `daemon/detection` unit test module to confirm
  zero behavior change.
- Add one small unit test: `_default_detector_factory("any-slug")`
  returns a `TagDetector` built from `DetectorConfig()` defaults when
  `_DETECTOR_CONFIG_OVERRIDES` is empty (documents the "no override yet"
  contract this ticket ships).
- **Verification command**: `uv run pytest tests/aprilcam/daemon/test_detection.py tests/aprilcam/vision/ -v`

**Documentation updates**:
- None required this ticket — `daemon/DESIGN.md`'s existing description
  of "one `TagDetector` per camera" stays accurate; ticket 002 is the
  one that adds a documented per-camera tuning fact worth a DESIGN.md
  note, once there is a real override to describe.
