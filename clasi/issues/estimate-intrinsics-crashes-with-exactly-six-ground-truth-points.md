---
status: pending
---

# `calibrate(estimate_intrinsics=True)` crashes with exactly six ground-truth points instead of refusing cleanly

## Description

Seen 2026-09-04 on camera 2 (`hd-usb-camera`, `secondary-playfield`)
with exactly six surveyed ArUco markers (four corners plus north- and
south-middle). `calibrate(camera, estimate_intrinsics=True)` surfaced
an OpenCV exception through the RPC instead of a `CalibrationResult`:

```
OpenCV(5.0.0) .../modules/calib/src/calibration.cpp:1359: error:
(-5:Bad argument) There should be less vars to optimize (having 8) than
the number of residuals (8 = 2 per point) in function
'calibrateCameraInternalSchur'
```

`calibration/calibration.py` documents six as the minimum point count
for intrinsics estimation, but the held-out split
(`_held_out_split`) hands `cv2.calibrateCamera` only the fit subset --
four points, eight residuals -- against eight free parameters, which
OpenCV refuses. The floor therefore has to account for the split (or
the split has to be skipped below some count), and whatever the floor
is, falling below it must produce a `succeeded=False` result with a
`reason`, never an uncaught exception.

The plain six-point homography on the same burst gave a 1.28 cm
reprojection error, just over the 1.0 cm gate, with the classic
barrel-distortion signature (edge midpoints 2-3 cm outside the
corner-to-corner line). This camera is known to have strong distortion
(`docs/knowledge/2026-08-20-camera-3-distortion-reverification.md`),
so the lens model is exactly what this field needs, and the crash
blocks it.

### Needed

1. A correct minimum for intrinsics estimation that accounts for the
   held-out split (or skip the split under a threshold), enforced as a
   clean refusal with a reason naming the count needed.
2. A test with exactly six points that asserts the refusal shape.
3. Operator guidance in the CLI/MCP result: how many more surveyed
   points to add.

### Context for the field

`secondary-playfield` currently surveys only the four corners again
(markers 4 and 5 are on the table but left out of the definition until
the lens model can be fitted). Adding west-middle, east-middle and a
centre marker (nine points) is the plan once this is fixed.
