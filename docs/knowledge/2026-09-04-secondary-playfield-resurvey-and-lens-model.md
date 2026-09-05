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
