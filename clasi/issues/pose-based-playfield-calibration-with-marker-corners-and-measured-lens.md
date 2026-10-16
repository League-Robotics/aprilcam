---
status: pending
---

# Pose-based playfield calibration: marker corners plus the measured lens model, so a field needs fewer tags

## Description

Playfield calibration (`aprilcam camera calibrate`, UC-04/UC-15) still
solves a plane-to-plane homography from **one point per ArUco marker**
(the centre), requires **every** marker the definition lists, and keeps
`--with-intrinsics`, the single-coplanar-view lens estimator that sprint
044 made obsolete. The ten-marker main field and the surveyed edge-middle
markers existed largely to average away lens distortion the homography
cannot model (the HD camera's barrel pushed edge-middle markers 2-3 cm
outside the corner line; see
`docs/knowledge/2026-09-04-secondary-playfield-resurvey-and-lens-model.md`).
Now that every camera can carry a **measured** lens model
(`cameras/<slug>/intrinsics.json`, sprint 044), that reason is gone, and
the calibrator is discarding three quarters of what it detects: each
marker gives four corners, and with a known camera matrix and a known
printed marker size those corners give a full planar pose (IPPE), not just
a homography. The intrinsics collector built in sprint 044 already does
exactly that solve on the ChArUco board.

Terminology for the docs: the lens model is the camera's *intrinsics*
(focal length, principal point, distortion: properties of the camera and
lens alone). The playfield calibration is the *extrinsic* half (where the
field plane is relative to the camera) plus the definition of the field's
coordinate frame (origin, axes, scale). User-facing text should say "lens
model" and "field calibration"; `aprilcam camera lens` may alias
`camera intrinsics`.

## What to build

**Solve by pose, derive the homography.** Undistort every detected
marker corner through the measured lens, run a planar PnP over all
corners of all matched markers (marker size from the definition), and
derive from the pose (a) the homography the rest of the system already
consumes (`Calibration.homography`, `to_world`/`to_pixel`, deskew: nothing
downstream changes shape) and (b) the camera position over the field
(height, nadir point, tilt), stored as provenance and used by the
parallax correction for mounted tags instead of a separately supplied
value. Without a measured lens model, fall back to today's centre-point
homography and say so in the result.

**Definition carries marker size and policy.** `aruco_tags` entries gain a
printed `size_mm` (per entry or a field default). The definition states
which markers are *required* and which are *helpful*; the
"every listed marker or fail" rule becomes per-field policy rather than
code. Existing definitions (no size, no policy) keep today's behaviour.

**Honest gates, not a point count.** Accept on: corner reprojection error
(px and cm) under a threshold; agreement between the two scales available
(marker print size vs. surveyed marker spacing); minimum angular spread of
the used corners across the frame. Report every number. Degrade with a
label: all markers best; two or more fine with a note; one marker yields a
pose-only calibration flagged low confidence rather than a refusal.

**Fewer tags.** With the above, the honest minimum field is four corner
markers (16 points, overdetermined) and a two-marker diagonal is
acceptable. Also support a *ChArUco board laid flat at the origin* as a
calibration target (24+ corners, self-identifying, the sprint 044 detector
reused directly): the board defines origin, axes and scale to sub-mm, then
is removed. The definition names the board spec and its placement.

**Delete the old estimator.** Remove `--with-intrinsics` /
`estimate_intrinsics` and `_estimate_intrinsics`/`_fit_intrinsics` and
their sanity bounds (the fx/k1 ridge makes the fit non-physical); the
`estimated` lens source stays readable for old `calibration.json` files
only. Close
`clasi/issues/estimate-intrinsics-crashes-with-exactly-six-ground-truth-points.md`
and `clasi/issues/later/coplanar-intrinsics-rejected-on-a-second-camera-reconsider-the-fx-plausibility-gate.md`
as superseded.

**Cheap staleness.** With a pose, the daemon can watch any visible marker
at low rate and flag a bumped camera or moved table (`calibration_stale`
from evidence, not only from config changes). Scope this as a stretch item.

## Explored and deferred

- **Tag-free calibration from the table itself** (edge/line detection of
  the white welding table's rectangle, the drawn cross and coloured
  patches): a prototype in
  `radio-robot-lib/config/playfields/secondary-playfield/fieldtools.py`
  reproduces the four corner markers to 0.1 cm with the cross visible and
  2.5 cm when a robot covers it. Attractive for a field with crisp edges,
  but it is a lighting and occlusion problem and still needs something to
  say which edge is north. Record the option; do not build it in this
  issue.

## Acceptance

- On the secondary field (four corner markers) with camera 3's measured
  lens, `camera calibrate 3` uses 16 corners, reports corner reprojection
  error and both scales, and passes the gate; the result names
  `lens model: measured` and the derived camera height/nadir.
- A two-marker definition calibrates with a warning; a one-marker
  definition calibrates flagged low confidence; a definition that marks
  all markers required fails exactly as today when one is covered.
- A ChArUco board placed at the origin of a field with no markers yields a
  calibration whose reprojection of the removed-board corners agrees with
  the marker-based solve on the same camera to within 0.5 cm.
- `--with-intrinsics` is gone from CLI, MCP and daemon; old files with an
  `estimated` lens still load.
- Parallax for a mounted tag uses the pose-derived camera position; the
  existing camera-2 numbers (144 cm over nadir (3.47, -2.85)) are
  reproduced within tolerance.
- Hardware-free tests on synthetic markers and boards; a live runbook on
  cameras 2, 3, 4 at sprint close.
