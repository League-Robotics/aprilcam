---
status: done
---

# Deskewer ignores the calibration's lens model, so the deskewed image and the tag overlay disagree radially

## Description

Seen 2026-09-05 on camera 2 (`hd-usb-camera`, `secondary-playfield`,
lens model fx 1853, k1 -0.298, reprojection 0.49 cm). Ground AprilTags
10, 14 and 16 near the field corners are reported by the daemon exactly
where the stored calibration (`undistort_point` + homography) puts them,
but in the deskewed image they appear about 1.0-1.2 cm further from the
centre, so the boundary/hat overlay in `playfield view` sits ~8 px inside
each tag. Tag 15 at the centre is unaffected, which is the signature of a
radial (lens) disagreement rather than a shifted calibration.

### Cause

`calibration/deskew.py` `Deskewer.deskew` builds
`cv2.getPerspectiveTransform` from the four world corners (through
`Calibration.to_pixel`, which *does* apply the lens model) to the
canvas, then `cv2.warpPerspective`s the **distorted** raw frame. Only
the four corners are therefore exact; everything between keeps the lens
distortion. Meanwhile `get_tags` and the stream's embedded tag positions
go through the lens model, so image and overlay can only agree at the
corners and near the centre.

### Needed

1. When the calibration carries a lens model, the deskewer must
   undistort as part of the warp -- e.g. compose
   `cv2.initUndistortRectifyMap` with the perspective matrix into one
   `cv2.remap`, built once per `Deskewer` instance (the geometry is
   frozen for a stream's life, see `daemon/streaming.py`'s
   `_DeskewContext`), so image and overlay agree by construction and
   the per-frame cost stays one remap.
2. A test that warps a synthetic distorted grid and checks
   overlay/image agreement at the edges to < 1 px; a calibration with no
   lens model must produce today's output unchanged.
3. Revisit the lens fit itself once the image is undistorted: the
   plumb-line estimate for this camera is k1 ≈ -0.12
   (`docs/knowledge/2026-09-04-secondary-playfield-resurvey-and-lens-model.md`),
   while the fitted -0.30 absorbs marker placement error and so
   exaggerates the mismatch. An honest fit needs accurately surveyed
   mid-edge markers (4-7) or more points.

### Not related

The ~5 cm offsets on robot tags 53 and 57 on the same field are parallax
(`mount_z` 11.2/11.6 cm at camera height 144 cm over nadir (3.47, -2.85))
plus the registered `mount_x` offsets -- by design: the overlay marks the
robot's ground centre, not the tag's image. Reproduced by hand to within
0.5 cm.

## Resolution (2026-09-05, out of process)

`Deskewer` now builds, once per instance, `cv2.remap` lookup tables that
compose the inverse perspective (canvas -> undistorted raw pixel) with the
forward lens model (OpenCV radial k1-k6 and tangential p1/p2), and warps
each frame with one `remap` when the calibration carries a lens model; a
homography-only calibration takes the original `warpPerspective` path,
byte-identical. Verified on camera 2's real frame: ground tags 10, 14, 16
and 15 in the deskewed image now agree with the daemon's reported world
positions to 0.0-0.1 cm (previously 0.9-1.2 cm); steady-state cost 1.7 ms
per 800x509 frame after a one-time 59 ms map build. Tests:
`tests/aprilcam/calibration/test_deskew.py` lens-model section. Item 3
(revisit the over-strong fitted k1) remains open as operator work.
