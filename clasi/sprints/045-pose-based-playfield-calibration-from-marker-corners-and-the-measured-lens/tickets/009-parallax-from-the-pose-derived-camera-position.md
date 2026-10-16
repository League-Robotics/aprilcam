---
id: 009
title: Parallax from the pose-derived camera position
status: done
use-cases:
- SUC-005
depends-on:
- '005'
- '006'
github-issue: ''
issue: pose-based-playfield-calibration-with-marker-corners-and-measured-lens.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Parallax from the pose-derived camera position

## Description

Make the pose-derived camera position the input to mounted-tag parallax.
Today `DaemonService._camera_geometry_for` reads operator-asserted
`CameraConfig.camera_position` (set by hand or `aprilcam camera locate`) and
falls back to `(0,0), height 0` ("nothing to correct"). New rule (Decision 5,
Open Question 4): prefer `Calibration.camera_geometry()` (pose, not stale),
else configured `camera_position`, else the existing sentinel. The rule is
two lines in `_camera_geometry_for`; the comparison logic lives on
`Calibration` (ticket 005), keeping the 3,100-line service thin.

Also update `_any_camera_located` (it reuses `_camera_geometry_for`, so it
inherits the behaviour -- confirm), the "not located yet" warning text at
`service.py` ~1871 (a pose calibration now also locates the camera), and
`aprilcam camera locate` to print a comparison when both a pose position and a
solved position exist and differ by more than 5 cm (informational; `locate`
itself is unchanged and stays the fallback for centre-point fields).

## Acceptance Criteria

- [x] With a fresh pose calibration, `_camera_geometry_for(slug)` returns the
      pose nadir/height even when no `camera_position` is configured.
- [x] A stale pose calibration, a centre-point calibration, or none falls
      back to the configured value, then the sentinel (existing tests keep
      passing).
- [x] Camera-2 numbers reproduced: synthetic calibration at 144 cm over
      nadir (3.47, -2.85); a tag registered with `mount_z` ~11 cm is
      projected to the floor ~5 cm inboard exactly as with the configured
      value (compare against `geometry.project_tag_to_field_plane` with the
      same inputs), end to end through `DetectionEngine`.
- [x] The "no camera located" warning no longer fires for a pose-calibrated
      camera.
- [x] `camera locate` prints the pose-vs-solved comparison when applicable.

## Implementation Plan

**Files to modify:** `src/aprilcam/daemon/service.py`,
`src/aprilcam/cli/locate.py`, `src/aprilcam/daemon/detection.py` docstrings.
No change to `calibration/geometry.py` (the projection math is already exact
for any camera centre).

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/daemon/test_detection.py tests/aprilcam/daemon/test_service.py tests/aprilcam/calibration/test_geometry.py -x`
- **New tests to write**: precedence matrix; camera-2 end-to-end;
  `locate` comparison output.
- **Verification command**: `uv run pytest tests/aprilcam/daemon tests/aprilcam/calibration tests/aprilcam/cli`

## Documentation Updates

`_camera_geometry_for` docstring; ticket 012 for prose docs.
