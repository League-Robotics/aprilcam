---
status: in-progress
sprint: 029
tickets:
- 029-001
- 029-002
- 029-003
- 029-004
---

# Camera geometry needed for real mount_z parallax correction

## Description

Sprint 025 wired the mobile-tag mount registry into detection.
`mount_x`/`mount_y`/`mount_yaw_rad` (the offset+yaw composition) work
and are verified on real hardware — registering an offset visibly moves
the reported robot position. **`mount_z` (parallax correction) does
not**, because nothing in the codebase models a camera's position or
height above the field.

`calibration/geometry.py`'s `project_tag_to_field_plane` needs
`camera_ground_position: WorldPoint` and `camera_height_cm: float` to
do its job. `DaemonService._camera_geometry_for` currently returns a
stopgap `(WorldPoint(0, 0), 0.0)` for every camera — height `0.0` is
`project_tag_to_field_plane`'s own documented "nothing to correct"
sentinel, so the function silently no-ops.

**Consequence**: a tag mounted at any height above the field plane
(which is the normal case — most robot tags are not lying flat on the
field) reports a position that is off by a parallax-shaped error
proportional to `mount_z` and the tag's distance from the camera's
optical axis. This is silent — nothing errors, the position just isn't
corrected.

## Cause

`CameraConfig` (`config/interfaces.py`) and the calibration pipeline
never asked "where is the camera, and how high is it" as a first-class
question — calibration solves a pixel↔world homography, which doesn't
require or produce that information. Sprint 025 needed it for the first
time.

## Proposed fix

Add camera position/height as real, configured data — most likely new
fields on `CameraConfig` (an operator-set value, since the daemon
cannot infer where its own camera is mounted from vision alone), with
the corresponding proto/loader/config-file plumbing sprints 020–023
already established for every other camera setting. Wire
`DaemonService._camera_geometry_for` to read the real values instead of
the stopgap.

Open: whether height is meters-above-field-plane (simplest) or whether
a fuller camera pose (position + tilt) is worth modeling now given deskew
already assumes a purely overhead, undistorted view — probably not; the
existing deskew/homography model's assumptions should bound this ticket's
scope rather than reopening them.

## Verification

- Register a mount with a nonzero `mount_z` for a tag at a known height
  and known offset from the camera's optical axis; the corrected world
  position matches a hand-measured expectation, not just the
  offset+yaw-only result.
- The existing `mount_x`/`mount_y`/`mount_yaw_rad` behavior (already
  correct) is unaffected.

## Related

- `src/aprilcam/calibration/geometry.py` — `project_tag_to_field_plane`
- `src/aprilcam/daemon/service.py` — `_camera_geometry_for` (the
  current stopgap)
- Sprint 025 ticket 004's completion notes — where this was found and
  the stopgap deliberately, honestly, left in place
- `docs/design/use-cases/daemon.md` UC-12
