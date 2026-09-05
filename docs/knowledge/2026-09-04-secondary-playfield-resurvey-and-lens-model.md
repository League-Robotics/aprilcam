---
date: 2026-09-04
tags: [calibration, secondary-playfield, hd-usb-camera, aruco, intrinsics, distortion, survey]
---

# Re-surveying `secondary-playfield` with nine points, and getting a lens model out of it

Camera 2 (`hd-usb-camera`) had a four-corner survey, which fits a
homography exactly and therefore proves nothing about accuracy. The
field was re-tagged 2026-09-04 to 110 x 70 cm with eight ArUco (4x4_50)
markers plus a temporary centre AprilTag.

## Gotchas hit, in order

1. **"Left" and "right" are the camera's, not the stakeholder's.** The
   world frame is defined from the camera's view (north = image top,
   east = image right). The stakeholder describing the layout from the
   table had left/right mirrored. Always dump detections with pixel
   positions (`TagDetector` on a `get_frame`) and write the definition
   from *that*, never from a verbal layout.
2. **Duplicate ids.** Twice a freshly printed marker turned out to be a
   duplicate of one already on the table (a second 2, then a second 5).
   The daemon's calibrate silently picks one; the offline decode shows
   both. Generate replacements with `cv2.aruco.generateImageMarker`
   (the same 4x4_50 dictionary) and verify they decode before printing.
3. **A six-point homography on this lens fails the 1 cm gate (1.2-1.3 cm)
   with the barrel-distortion signature**: under a corners-only fit, the
   edge-middle markers land 2-3 cm *outside* the corner-to-corner line.
   That is lens distortion, not placement.
4. **`estimate_intrinsics=True` with exactly six points crashes** inside
   OpenCV (held-out split leaves 8 residuals for 8 parameters); with
   seven points it fits garbage (k1 = -2.4, rejected by the sanity
   bound). Issue:
   `clasi/issues/estimate-intrinsics-crashes-with-exactly-six-ground-truth-points.md`.
5. **With nine points it works**: fx 1853 px, principal point at the
   image centre, k1 = -0.298; reprojection 0.49 cm; live survey error
   mean 0.45 cm, max 1.0 cm at the north-middle marker. An offline
   one-parameter fit on seven points had already shown the fx/k1 ridge
   (any fx from 600 to 2000 gives the same 0.43 cm with a matching k1),
   so the stored fx is not a physical measurement -- it is one point on
   that ridge and is only valid together with its k1.

## State left

Definition (`~/.config/aprilcam/playfields/secondary-playfield.json`,
pushed live): 2 NW, 0 NE, 3 SW, 1 SE at (+-55, +-35); 4 N (0, 35);
5 S (0, -35); 6 E (55, 0); 7 W (-55, 0); AprilTag 15 at (0, 0),
**temporary** -- delete it from `april_tags` when it leaves the table
(calibration itself needs only the ArUco set). Camera 2 calibrated with
the fitted lens model stored in its `calibration.json`.

## Later the same day: the drawn lines say the lens is rectilinear

A plumb-line check on camera 2's raw frame (fit k1 so the field's long
drawn lines are straightest; `radio-robot-lib/config/playfields/secondary-playfield/fieldtools.py analyze`)
found the lines already straight to **0.64 px rms** over >1000 px, best
correction k1 = -0.12 at f = 1853 (0.61 px). The daemon's nine-point fit
(k1 = -0.30) actually *bends* those lines (0.74 px). So the 1.2-1.3 cm
six-point homography residual was **marker placement** (the edge-middle
markers sit 0.9-2.7 cm off their nominal spots), and the lens model the
daemon stored is compensating placement, not distortion. Consequences:

- Camera 2's current calibration still reports the markers to 0.45 cm
  mean and the cross centre within ~1 cm, so it is fine to run on. But
  it is a fudge: positions between markers are warped by ~0.1 cm, and it
  would go wrong if the middle markers were ever moved to their nominal
  spots without recalibrating.
- The honest survey would be the corners (assumed exact) plus the
  *measured* positions of 4-7 from the line-based frame, and no lens
  model. Not done yet; recorded here so nobody "fixes" the lens model
  back to zero without also fixing the survey.
- A tag-free deskew from the cross + coloured patches reproduces the
  four corner markers to 0.1 cm (2.5 cm when a robot covers the cross);
  the field template and tooling live in
  `radio-robot-lib/config/playfields/secondary-playfield/`.

## 2026-09-05: why the overlay sat off the tags in `playfield view`

Two unrelated effects, diagnosed by re-detecting tags in the deskewed
frame and comparing with the daemon's reported world positions:

- **Robot tags (registered `mount_z` ~11 cm, camera 2 located at 144 cm
  over nadir (3.47, -2.85)) are ~5 cm inside their image position by
  design** -- parallax projection to the floor plus the registered
  `mount_x` offset. The overlay marks the robot's ground centre, not the
  tag's image. Reproducible by hand to 0.5 cm from the raw pixels.
- **Ground tags were ~1 cm / 8 px off radially, zero at the centre**,
  because `Deskewer` warped the *distorted* raw frame with a plain
  four-corner perspective transform while positions went through the
  lens model. Fixed the same day (deskewer remaps through the lens
  model; see `clasi/issues/done/deskewer-ignores-the-lens-model-...md`).
  With the fix, ground tags agree to 0.1 cm.

The fitted k1 of -0.30 is still stronger than the plumb-line estimate
(-0.12); it absorbs mid-edge marker placement error. World coordinates
are within ~1 cm of the survey regardless, but an honest lens fit needs
accurately surveyed markers 4-7 or a centre marker added to the definition.
