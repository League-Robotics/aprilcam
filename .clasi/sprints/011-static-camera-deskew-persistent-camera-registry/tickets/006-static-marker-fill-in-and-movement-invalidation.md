---
id: '006'
title: Static-marker fill-in and movement-invalidation
status: open
use-cases: [SUC-002, SUC-003]
depends-on: ['005']
github-issue: ''
issue: static-camera-deskew-from-homography.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Static-marker fill-in and movement-invalidation

## Description

Make the static-camera path robust to flicker and to camera movement, building
on the seeded geometry from ticket 005.

In `core/playfield.py` `PlayfieldBoundary`:

1. **Static-marker fill-in.** In static-camera mode, treat the static set
   (ArUco corners + AprilTag 1, configurable via the per-camera
   `static_marker_ids` from ticket 004) as having fixed pixel positions. When a
   static marker is not detected in a frame, hold its stored calibration-time
   pixel position rather than dropping it. This stabilizes both the polygon and
   the static reference set under intermittent detection. Dynamic AprilTags
   (ID ≠ 1) are taken only from the current frame and are **never** held.
2. **Movement invalidation.** When static markers *are* detected live, compare
   their live pixel positions to the stored positions. If any disagrees beyond
   a configurable threshold, drop the static assumption and raise a warning
   flag the pipeline can surface (AprilTag 1, away from the frame edges, is the
   sentinel even when corners are occluded). The proposed surfacing channel is
   a log warning plus a flag in `info.json` (final channel is an open question
   in the architecture doc; implement the log warning + a boolean flag accessor
   that the pipeline/`info.json` writer can consume).

This ticket does not change dynamic-tag handling beyond ensuring dynamic tags
are excluded from the static hold set.

## Acceptance Criteria

- [ ] A static marker missing from a frame retains its stored pixel position;
  the polygon/static set remains stable across the gap.
- [ ] Dynamic (robot-mounted) AprilTags (ID ≠ 1) are tracked frame-by-frame and
  are never held at a stored position.
- [ ] When a live static-marker position disagrees with its stored position
  beyond the threshold, the static assumption is dropped and a warning is
  surfaced (log + a queryable flag).
- [ ] AprilTag 1 triggers invalidation even when corner markers are occluded.
- [ ] The static set and threshold are configurable; the static set defaults to
  `aruco_corners + apriltag:1`.
- [ ] `uv run pytest` passes.

## Implementation Plan

- **Approach**: Extend `PlayfieldBoundary.update` with a static-hold map keyed
  by marker id and an invalidation comparison; expose a warning flag accessor.
  Keep dynamic-tag flow untouched.
- **Files to modify**: `src/aprilcam/core/playfield.py` (and the pipeline/
  `info.json` writer only to read the warning flag, if needed for surfacing).
- **Testing plan**: see Testing; drive fully synthetically (no camera).
- **Docs**: document static-camera mode behavior and the invalidation flag.

## Testing

- **Existing tests to run**: `uv run pytest tests -k "playfield or static"`,
  then full `uv run pytest`.
- **New tests to write**:
  - fill-in: seed static positions, feed a frame missing a static marker,
    assert the held position is used and the polygon is unchanged.
  - dynamic not held: omit a dynamic tag from a frame, assert it is absent (not
    held at a stale position).
  - invalidation trip: feed a static marker at a displaced position beyond the
    threshold, assert the static assumption drops and the warning flag is set.
  - sentinel: trip invalidation via AprilTag 1 while corners are occluded.
  - Mark OpenCV-dependent tests `needs_cv2`.
- **Verification command**: `uv run pytest`
