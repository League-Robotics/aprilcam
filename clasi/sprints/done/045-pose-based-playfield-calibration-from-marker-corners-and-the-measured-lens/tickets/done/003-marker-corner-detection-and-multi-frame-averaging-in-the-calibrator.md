---
id: '003'
title: Marker corner detection and multi-frame averaging in the Calibrator
status: done
use-cases:
- SUC-001
depends-on:
- '001'
github-issue: ''
issue: pose-based-playfield-calibration-with-marker-corners-and-measured-lens.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Marker corner detection and multi-frame averaging in the Calibrator

## Description

`Calibrator._detect_averaged` currently returns one `PixelPoint` per tag (the
centre), discarding the four corners the detector already produces. Add
corner-preserving, multi-frame averaged observations so the pose solve has its
input, without changing what the existing centre-point path and the (still
present) estimator consume.

New cv2-free value type `MarkerObservation` (in `calibration/pose.py`'s
neighbour or `calibration/interfaces.py`; decide at implementation, it must
stay importable without cv2): `tag_id`, `centre: PixelPoint`, `corners:
tuple[PixelPoint x4]` in ArUco order (TL, TR, BR, BL), `frames: int`, and
`corner_spread_px` (max per-corner stddev across frames, a cheap noise
measure). New `Calibrator._detect_markers(frames)` returns
`Dict[TagFamily, Dict[int, MarkerObservation]]`; `_detect_averaged` becomes a
thin derivation from it (centres), so behaviour is byte-identical for
existing callers.

Averaging must be per-corner and order-consistent. Verify the detector's
corner order matches `TagRecord.pixel_corners` ("verified equal to the
boundary/hat order") and that `run_pipeline`'s rescale (`scale=rescale.scale`)
returns corners in the original frame's pixels. Confirm the ArUco detector
uses subpixel corner refinement by default (`vision/detector.py` defaults
`CORNER_REFINE_SUBPIX`); if the calibrator's configuration can override it,
pin subpixel for calibration bursts.

## Acceptance Criteria

- [x] `MarkerObservation` exists, cv2-free, frozen dataclass declaring fields
      (no properties, no `typing.Protocol`).
- [x] `_detect_markers` averages corners across frames; a marker seen in a
      subset of frames averages over those frames.
- [x] `_detect_averaged` output unchanged for every existing test.
- [x] Synthetic test: markers rendered with `cv2.aruco.generateImageMarker`,
      warped by a known homography, recovered corners within 0.3 px of truth
      (and the corner order is the marker's own TL/TR/BR/BL: a marker rendered
      rotated by 90 or 180 degrees, e.g. upside down, is NOT silently
      renumbered -- the pose solve (ticket 004) resolves the orientation
      from that order, so it must be preserved exactly).
- [x] Noise across frames reduces `corner_spread_px` as expected (jittered
      sub-pixel shifts averaged).

## Implementation Plan

**Approach.** Extend the existing accumulation loop to also collect
`detection.corners`; compute mean per corner.

**Files to modify:** `src/aprilcam/calibration/calibration.py`
(`_detect_averaged`, new `_detect_markers`), possibly
`src/aprilcam/calibration/interfaces.py` (value type).

**Files to create:** tests in `tests/aprilcam/calibration/test_calibration.py`
or a new `test_marker_observations.py`.

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/calibration -x`
- **New tests to write**: as listed in the acceptance criteria; use a fake
  frame source and the real `TagDetector`.
- **Verification command**: `uv run pytest tests/aprilcam/calibration`

## Documentation Updates

Calibrator docstring for `_detect_averaged` updated; DESIGN.md in ticket 012.
