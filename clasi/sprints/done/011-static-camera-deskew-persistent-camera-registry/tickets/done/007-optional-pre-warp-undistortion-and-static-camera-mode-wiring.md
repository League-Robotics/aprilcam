---
id: '007'
title: Optional pre-warp undistortion and static-camera mode wiring
status: done
use-cases:
- SUC-004
depends-on:
- '005'
- '006'
github-issue: ''
issue: static-camera-deskew-from-homography.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Optional pre-warp undistortion and static-camera mode wiring

## Description

Final deskew ticket. Add optional distortion correction before the warp and
wire the static-camera mode flag through the pipeline. Completes the
static-camera-deskew issue.

1. **Optional pre-warp undistortion.** When static-camera mode is on and the
   saved calibration has `camera_matrix` + `dist_coeffs`, undistort the frame
   (`CameraCalibration.undistort`, already implemented) before
   `warpPerspective` in `ui/display.py`'s deskew path. When intrinsics are
   absent, the step is a no-op and deskew still works.
2. **Static-camera mode wiring.** Thread a static-camera mode flag from config
   into `Playfield`/`PlayfieldBoundary`. Per the stakeholder decision: auto-on
   when a saved homography exists, with a config override to disable. Implement
   the auto-on-with-override behavior and document it; the override path also
   supports explicit-flag-only if preferred later.
3. **`px_per_cm` config knob.** Thread the metric-deskew output scale
   (`px_per_cm`, introduced in ticket 005) from config into the deskew path,
   with the sensible default from ticket 005. This is the single place the
   metric warp's output resolution is configured.

This ticket assumes tickets 005 (seeded geometry + shared warp helper) and 006
(fill-in/invalidation) are in place.

## Acceptance Criteria

- [x] When saved intrinsics exist and static-camera mode is on, undistortion is
  applied before the warp and visibly flattens residual barrel curvature.
- [x] When intrinsics are absent, the undistortion step is a no-op and deskew
  still produces a warped view.
- [x] Static-camera mode is auto-on when a saved homography exists and can be
  overridden (disabled) by config.
- [x] `px_per_cm` is wired from config into the metric deskew path with the
  ticket-005 default; overriding it changes the deskewed output resolution.
- [x] The full issue acceptance criteria hold end-to-end: a fixed camera with a
  saved calibration deskews with no live corner; the extreme-angle global-
  shutter camera produces a flat metric top-down view (output sized by `W×H` and
  `px_per_cm`); dynamic tags stay live; a moved camera is detected and surfaced;
  optional undistortion flattens curvature.
- [x] `uv run pytest` passes.

## Implementation Plan

- **Approach**: Add the undistort call at the deskew site behind the
  mode+intrinsics guard; add the mode flag plumbing through `Playfield`
  construction with the auto-on-when-homography default.
- **Files to modify**: `src/aprilcam/ui/display.py`,
  `src/aprilcam/core/playfield.py`, and the config/pipeline glue that
  constructs `Playfield` (`src/aprilcam/config.py` /
  `src/aprilcam/daemon/camera_pipeline.py`) for the mode flag.
- **Files to create**: none.
- **Testing plan**: see Testing.
- **Docs**: document the static-camera mode default and the optional
  undistortion in the deskew docstring and any user-facing config docs.

## Testing

- **Existing tests to run**: `uv run pytest tests -k "deskew or display or
  playfield"`, then full `uv run pytest`.
- **New tests to write**:
  - undistortion applied: with intrinsics + mode on, assert `undistort` is
    invoked before the warp (spy/monkeypatch); output differs from the
    no-undistort path.
  - no-op: with no intrinsics, deskew still returns a warped frame and
    `undistort` is a pass-through.
  - mode default: a `Playfield` built with a saved homography has static mode
    on by default; a config override turns it off.
  - Mark OpenCV-dependent tests `needs_cv2`.
- **Verification command**: `uv run pytest`
