---
status: in-progress
sprint: '011'
tickets:
- 011-004
- 011-005
- 011-006
- 011-007
---

# Static-camera deskew from the saved homography (tolerate intermittent markers)

For a fixed (non-moving) camera, derive the deskew warp from the saved
calibration homography instead of requiring all four ArUco corner markers
to be detected live in the same frame. This makes deskew work in the
"degraded" case where reference markers are only found in some frames —
exactly the extreme-angle global-shutter camera case.

## Background / why current deskew fails

Deskew today is gated entirely on a **live** playfield polygon built from
detecting all four ArUco corner markers (IDs `{0,1,2,3}` or `{1,3,5,7}`) in
a single frame ([core/playfield.py](../../src/aprilcam/core/playfield.py),
`_order_poly` / `get_polygon`); if `get_polygon()` is `None`,
`display._update_deskew` returns early
([ui/display.py:62](../../src/aprilcam/ui/display.py#L62)) and the view
never warps.

This is independent of calibration:

- Calibration accumulates detections over **30 frames** as a union
  (`detect_all_tags`,
  [calibration/homography.py:137](../../src/aprilcam/calibration/homography.py#L137)),
  so a marker seen in even 1 of 30 frames counts. The "9 tags" success
  message therefore does **not** imply all 4 corners are co-visible in any
  single live frame.
- Deskew re-detects every live frame and needs all 4 corners **at once**.
  From an extreme low/side angle the corners rarely coincide, so the
  polygon never forms — even though calibration succeeded and a valid
  homography was saved.

The saved calibration already contains everything deskew needs:
`homography`, `camera_matrix`, `dist_coeffs`, and `playfield` width/height
(see `data/aprilcam/cameras/global-shutter-camera/calibration.json`). We
are throwing that away and re-deriving the geometry from scratch every
frame.

## Core insight

The camera does not move after calibration. So:

1. The deskew polygon is computable **with no live detection**: the four
   playfield corners in world space are `(0,0), (W,0), (W,H), (0,H)`; map
   them through `H⁻¹` to get their fixed pixel positions. That IS the
   deskew source polygon.
2. Any **static** reference marker's pixel position is fixed too. If it was
   located at calibration time, we know where it is now, whether or not the
   detector happens to find it this frame. Intermittent detection of static
   markers can be tolerated by holding their known positions.
3. Barrel distortion can be removed first using the saved `camera_matrix` /
   `dist_coeffs`, so the warped result is genuinely flat.

## Proposed approach

**A. Homography-derived deskew (primary fix).**
Add a deskew path that builds the warp from the persisted homography:
compute corner pixels via `H⁻¹ · [world_corner]`, then the same
`getPerspectiveTransform` → `warpPerspective` as today. Requires zero live
ArUco corners. Prefer this when a saved homography exists; fall back to the
current live-corner polygon otherwise.

**B. Persist the reference geometry.**
Save the corner pixel positions (and ideally each static reference marker's
calibration-time pixel position + world position) into `calibration.json`,
and seed `Playfield(polygon=…)` at load. The data model already accepts a
`polygon` arg but nothing populates it from calibration today. This also
removes the cold-start delay where deskew waits for a live corner set.

**C. Static-marker fill-in (the "inconsistently found" ask).**
In a "static camera" mode, treat the static reference markers (ArUco
corners + AprilTag 1, per the rule above) as having fixed pixel locations.
Accept them found in only some frames; for a reference marker not detected
this frame, hold its known position rather than dropping it. This
stabilizes both the polygon and the playfield reference set under flicker.
Dynamic AprilTags (ID ≠ 1) are unaffected — always live.

**D. Optional distortion correction.**
Undistort the frame with the saved `camera_matrix` / `dist_coeffs` before
the perspective warp for a flatter top-down image.

## Static vs dynamic markers (defined rule)

The set of fixed reference markers is known and fixed for this playfield:

- **Static (freezable):** all ArUco 4x4 corner markers, **and AprilTag
  ID 1**.
- **Dynamic (always live, never frozen):** every other AprilTag (ID ≠ 1).
  These are mounted on robots and must be detected fresh every frame; they
  must never be held at a stored position.

So the static-camera logic applies to: ArUco corners + AprilTag 1. AprilTag
1 gives a second persistent anchor beyond the corners — useful for
movement-invalidation checks (below) and as an extra fixed point if a
corner is occluded. Make this set configurable (default:
`aruco_corners + apriltag:1`) so other playfields can declare their own
fixed markers, but encode this as the default.

## Open questions

- **Movement invalidation.** If the camera *is* bumped, frozen geometry
  becomes wrong. Detect this: when live detections of the static reference
  markers (ArUco corners and AprilTag 1) disagree with their stored pixel
  positions beyond a threshold, drop the static assumption and warn /
  prompt re-calibration. AprilTag 1, being away from the frame edges, is a
  good sentinel for this even when corners are occluded.
- The static set is fixed by rule (ArUco corners + AprilTag 1) but should
  be a config value; decide where it lives (per-playfield config vs
  per-camera calibration record).
- Config flag to enable static-camera mode (default on when a saved
  homography exists?), and how it composes with the existing live-corner
  deskew path.
- Whether deskew should warp to pixel-space (corner polygon) or directly to
  a metric top-down view scaled by `W×H` cm (cleaner, since H is known).

## Acceptance criteria

- With a saved calibration, the camera deskews **without** detecting any
  ArUco corner in the live stream (verified by physically covering a corner
  marker — deskew still works).
- The extreme-angle global-shutter camera, which calibrates but does not
  currently deskew, produces a flat top-down view.
- Dynamic (robot-mounted) tags are still tracked frame-by-frame and are
  never frozen at stale positions.
- If the camera is moved after calibration, the system detects the
  disagreement and surfaces it rather than silently deskewing with a stale
  transform.
- Optional distortion correction visibly flattens residual barrel curvature
  using the saved coefficients.

## Relation to other issues

Complements [[persistent-camera-registry-with-stable-identity-and-enumeration]]:
both push per-camera state (identity, geometry) into persisted records so
the daemon survives reconnects and intermittent detection without restarts.
