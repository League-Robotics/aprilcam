---
status: pending
---

# Fresh `calibration.json` omits the `pipeline` block, so the live loop never re-enables 4x4 ArUco detection

## Correction to the original diagnosis

This issue was first filed claiming that missing top-level fields
(`field_width_cm`, `field_height_cm`, `world_origin`, `aruco_tags`)
caused `world_xy = (0,0)`. **That was wrong.** After a successful
`aprilcam calibrate`, the written `calibration.json` **still** has those
top-level fields as `None`, yet world coordinates work correctly (tag
100 reads `(62.1, 0.0)` cm; tag 1 reads `(0,0)` because it *is* the
A1-centred origin). The world mapping is carried by the populated
`homography` + `playfield` + `corner_pixels` + `static_markers` fields,
not by those top-level scale fields. So the `(0,0)` symptom was entirely
explained by the companion issue
([[stale-registry-record-orphans-camera-from-its-calibration-dir]]) —
the daemon reading the wrong (`-15`) dir / an old homography-only file —
plus an occluded corner marker at calibration time.

## The real, narrower gap

A freshly-written `calibration.json` from `calibrate_from_playfield_def`
contains **no `pipeline` block** (`pipeline: None`). The daemon's
runtime detection loop only enables `detect_aruco_4x4` when
`self._calibration.pipeline` carries `detect_aruco_4x4: true`
([daemon/camera_pipeline.py:375-388], flag default `False` in
[core/aprilcam.py:70]). Consequence:

- Live `aprilcam tags` / `get_tags` shows **AprilTags but zero ArUco
  4x4 markers**, even on a calibrated camera, because the loop never
  looks for them.
- This does **not** affect AprilTag world coordinates (those work via
  the stored homography). It only matters for consumers that want the
  ArUco corner markers in the live stream, or for any live/continuous
  re-homography that depends on seeing the corners each frame.

The calibration path itself detects 4x4 ArUco fine — it runs its own
unconditional `detect_aruco_4x4()` in
[calibration/homography.py:44]. The gap is purely that the *runtime
loop* flag is off by default and the calibration writer doesn't persist
a `pipeline` block to turn it on.

## Fix direction

- Have the calibration writer persist a `pipeline` block (at minimum
  `detect_aruco_4x4: true`) when the playfield uses 4x4 ArUco corners,
  so a calibrated camera's live loop detects the corners too.
- OR make the daemon default `detect_aruco_4x4` on when the linked
  playfield defines `aruco_tags`, independent of the calibration file.
- Optionally: a validation/reporting guard so clients can tell "world
  calibration present" from "deskew-only", surfaced via
  `get_calibration` / daemon status.

## Status

Not blocking current camera use — world coordinates for AprilTags are
live after re-calibration. This is a follow-up so the live ArUco stream
and any corner-based live re-homography work without manual config.
