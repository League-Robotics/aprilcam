# Pose-based field calibration: conventions, gates, and how to author a sized definition

Sprint 045. Field calibration (`aprilcam camera calibrate`) now solves the
camera's planar pose from the four corners of every sized marker, through the
camera's measured lens model, and derives the homography from that pose. The
old centre-point fit survives as the fallback. This entry records what a
future reader would otherwise have to rediscover. Design home:
`src/aprilcam/calibration/DESIGN.md`.

## Terminology

User-facing text says **lens model** (the camera's intrinsics: focal length,
principal point, distortion; `camera intrinsics`, alias `camera lens`) and
**field calibration** (where the field plane is relative to the camera plus the
field's coordinate frame; `camera calibrate`).

## Sign and orientation conventions (pinned by `tests/aprilcam/calibration/test_pose.py`)

- Field frame: x east, y north, z up, centimetres, origin at the field centre.
  A looking-down camera's rotation is `diag(1, -1, -1)` (no mirror).
- Marker corners are in the detector's native order TL, TR, BR, BL. An upright
  marker has its top edge to the north: TL = centre + (-s/2, +s/2).
- A quarter turn counts counter-clockwise **seen from above**: one turn puts TL
  at the south-west and the top edge faces west. Orientation `+90` means one
  such turn, `-90` three.
- Homography = inverse of `K [r1 r2 t]`, mapping an **undistorted** pixel to
  field cm (what `Calibration.to_world` applies after `undistort_point`).
- The IPPE ambiguity ratio is IPPE's runner-up/best error taken **before**
  Levenberg-Marquardt refinement; LM drags the runner-up onto the same minimum
  and erases the signal.
- The deskewed image y flip still happens exactly once, in
  `world_to_deskewed_pixel`; the ChArUco target path flips y once too (board
  rows grow southward, field y grows north).

## Marker orientation is detected, not declared

There is no rotation key in the definition. The pose solver tries the four
quarter turns per marker and keeps the best-of-four corner residual
(`pose.py detect_marker_rotation`): with four or more markers it seeds a centre
homography, scores each marker's four turns in cm, refits the pose and rescores
(up to two passes); with fewer than four it enumerates all 4^n combinations by
pose rms. A lone marker is inherently ambiguous (ratio about 1), hence the
rotation-ratio gate below. Real fields mix orientations, so declaring them
would only invite mistakes.

## Detector settings for calibration

The calibrator's detector runs with inverted-marker detection **off**
(`DetectorConfig.detect_inverted`) and sub-pixel corner refinement pinned.
With inverted detection on, corners were pulled about 8 px inward and the pose
fit was wrong while every number still looked plausible. The live detection
loop is unchanged. Each `MarkerObservation` keeps its four corners, averaged
over the capture frames.

## Gates as tuned (`calibration/gates.py`)

| Gate | Threshold | Why |
|---|---|---|
| corner rms | 1.5 px | clean solves give <= 0.5 px; 1 cm survey error gives 1.8 px |
| field rms | 1.0 cm | the project's existing centre-point threshold; clean <= 0.08 cm |
| scale agreement | 5 % | clean <= 1.2 %; markers printed 3 % / 10 % large read 3.7 % / 9.8 % |
| angular spread | 10 deg | four corner markers span about 51 deg; one marker about 2.5 deg |
| IPPE ambiguity ratio | 1.3 | borrowed from the intrinsics collector (see caveat below) |
| leave-one-out | 2 cm | clean <= 0.22 cm; a marker displaced d cm reads d cm |
| rotation ratio | 2.0 | right turn beats runner-up >= 40x at 0.3 px; lone marker about 1 |
| layout extent ratio | 0.2 | min/max singular value of the centred marker centres |

Synthetic noise sweep behind them (camera 144 cm up, tilt 4 deg, 1280x720,
fx 1000, k1 -0.25, 46 mm markers, worst of 40 seeds):

| Corner noise | 0.1 px | 0.3 px | 0.6 px | 1.0 px |
|---|---|---|---|---|
| rms (px) | 0.16 | 0.49 | 0.98 | 1.64 |
| rms (cm) | 0.03 | 0.08 | 0.15 | 0.26 |
| worst leave-one-out (cm) | 0.07 | 0.20 | 0.39 | 0.65 |
| scale \|ratio - 1\| | 0.003 | 0.007 | 0.016 | 0.030 |
| nadir error (cm) | 0.08 | 0.23 | 0.46 | 0.77 |
| min rotation ratio | 122 | 41 | 21 | 12 |

Height error <= 0.12 cm throughout. Four corner markers at 0.3 px: nadir
<= 0.36 cm, height <= 0.07 cm. Thresholds are provisional until confirmed live
(ticket 013).

### IPPE ambiguity near fronto-parallel cameras

A camera looking straight down at a single marker has two nearly equal pose
solutions (the planar mirror ambiguity). The sweep found the ratio flags a lone
marker only about half the time (median 1.5, min 1.0), so the marker-count rule
and the layout gate carry the weight; for a spread layout the ratio is a
backstop. Do not trust the ratio alone.

### The layout-extent gate (found by the sweep, not in the plan)

Two diagonal markers, or three in a row, pass the rms and ambiguity gates yet
leave roll about the baseline unconstrained: nadir error up to 30 cm (two
diagonal) and 81 cm (three in a row) at 0.3 px noise. `layout_extent_ratio`
catches it (L-shaped three markers read 0.48; near-lines 0.05-0.11). A tight
cluster 20 cm across is caught by the angular-spread gate instead.

## Strategy, policy and degradation

- **Pose path** iff the lens store holds a measured model at the frame size and
  a matched on-plane marker has a size. Otherwise the **centre-point path**
  runs and the result carries `method_reason` (for example "no measured lens
  model", "lens measured at a different resolution", "lens source is legacy",
  "definition has no marker size").
- **Policy** per marker: `required` (default when unspecified) must be seen or
  the attempt fails naming the missing ids; `helpful` markers only sharpen the
  solve, and a helpful one that is missing is reported, never a failure.
  Helpful markers join the centre-point path too.
- **Elevated** markers (`height_cm > 0`) are skipped by the pose solve.
- **Confidence ladder:** `full`; `reduced` for exactly two markers; `low` for a
  single marker, an ambiguous rotation, or a weak layout; `fail` when rms,
  scale or leave-one-out trips, with the gate named and
  "(worst leave-one-out: marker N, X cm ...)" appended so a swapped or
  mis-surveyed marker is identified. A failure stores nothing; the previous
  calibration survives.
- `camera calibrate --verbose` prints the per-marker leave-one-out table
  (`CalibrationResult.leave_one_out_cm`, not persisted). `camera list` shows
  `calibrated (pose, full)`.

## Parallax uses the pose-derived camera position

`DaemonService._camera_geometry_for` precedence: a fresh pose calibration
(method pose, not stale, height and nadir set) > the configured
`camera_position` > the `(0, 0)` / height 0 sentinel (no correction).
`camera locate` prints a comparison note when its solve differs from a fresh
pose calibration by more than 5 cm in nadir or height.

## The ChArUco calibration target

A definition may carry `calibration_target` (kind `charuco`, a board spec string,
`centre_x_cm`, `centre_y_cm`, `rotation_deg` counter-clockwise seen from above).
`intrinsics/target.py` `ChArUcoTargetObserver` is injected into the Calibrator
by `DaemonService`; its corners pool with marker corners (label
`pose: markers+target`). On synthetic data the board agrees with the marker
solve to 0.385 cm (acceptance was 0.5 cm). A target-only definition is valid.

## Authoring a sized definition

1. Measure the printed marker's outer black square in mm. Put it in
   `playfield.marker_size_mm` (field default) and override per entry with
   `aruco_tags[].size_mm` only where sizes differ.
2. Survey each marker **centre** in field cm as before. Do not declare
   orientation.
3. Mark non-essential markers `"policy": "helpful"` if the field should
   calibrate with them covered; leave the rest `required` (the default).
4. Without a size on any on-plane marker the field stays on the centre-point
   path. `main-playfield.json` carries 46 mm. The **secondary field's marker
   size is still unknown**, so `secondary-playfield.json` has no size and stays
   on the centre-point path; do not invent one.

## Field facts, 2026-10-16 (main field)

- Markers are 46 mm. Orientations: ids 1, 2, 3 upright; 4 is +90; 8 is -90;
  5, 6, 7, 9, 10 are upside down.
- The reprinted ids 9 and 10 were physically placed **swapped** relative to the
  survey. The definition is kept as surveyed (9 west, 10 east); the physical
  tags are to be exchanged.
- Worked example for the leave-one-out gate: with the swap, the ten-marker
  centre-point fit was **30 cm rms**; with the swap corrected it is **0.23 cm
  rms and 0.75 cm worst leave-one-out**. The 2 cm leave-one-out gate would have
  named marker 9 or 10 immediately.

## Deferred: tag-free calibration from the table itself

Detecting the white welding table's rectangle, drawn cross and coloured patches
needs no markers. The prototype in
`radio-robot-lib/config/playfields/secondary-playfield/fieldtools.py`
reproduces the four corner markers to 0.1 cm with the cross visible and
2.5 cm when a robot covers it. It is a lighting and occlusion problem and still
needs something to say which edge is north. Recorded so it is not rediscovered;
not built (sprint 045 issue "Explored and deferred").

## Removed

The single-view intrinsics estimator and `camera calibrate --with-intrinsics`
are gone (CLI, MCP, daemon). `lens_source: estimated` files still load; the
proto fields are reserved. The extra edge-middle markers existed to average away
lens distortion that a measured lens model now removes; see
`2026-09-04-secondary-playfield-resurvey-and-lens-model.md`.
