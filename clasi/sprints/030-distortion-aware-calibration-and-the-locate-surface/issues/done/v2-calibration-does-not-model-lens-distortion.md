---
status: done
sprint: '030'
tickets:
- 030-001
- 030-002
- 030-003
---

# v2 calibration does not model lens distortion (v1 regression, measured at 2.2 cm)

## Description

v2 maps pixels to world coordinates with a single homography and nothing
else. There is no intrinsic matrix and no distortion model anywhere in
the v2 tree — `grep -rn "undistort\|dist_coeff\|camera_matrix"
src/aprilcam/` returns zero hits outside tests.

`Calibration` (`src/aprilcam/calibration/interfaces.py`) carries exactly
`stale`, `calibrated_at`, `playfield`, and the homography.
`Calibration.to_world` / `to_pixel`
(`src/aprilcam/calibration/calibration.py:195`) are a single projective
transform.

**This is a regression from v1, not an omission by design.** v1
estimated distortion, stored it, and applied it:

- `archive/src/aprilcam/stream.py:433` — undistorts the frame before the
  deskew warp when `dist_coeffs` is present.
- `archive/src/aprilcam/config.py:455` — `undistort: bool` /
  `APRILCAM_UNDISTORT`, "Apply lens undistortion before deskew warp when
  intrinsics are present."

Both generations of file are sitting in the state directory right now:

| file | fields |
|---|---|
| `cameras/arducam-ov9782-usb-camera-15/calibration.json` (v1, stale) | homography, **camera_matrix**, **dist_coeffs** `[0.42, -2.43, ...]`, tags_used, rms_error |
| `cameras/arducam-ov9782-usb-camera/calibration.json` (v2, live) | homography, matched_ids, playfield, reprojection_error_cm, stale |

## Measured impact (secondary-playfield, hd-usb-camera, 2026-08-12)

Seven 46.5 mm AprilTags were placed on the field as temporary ground
truth. Their surveyed positions are unknown, but their *size* is known
and identical, which is enough to measure the calibration's internal
consistency.

Apparent tag size varies systematically with distance from the image
centre — **correlation between measured size and image radius^2 is
-0.96**. Tags near the centre measure ~5% large; tags near the edges
~5% small. That is textbook uncorrected barrel distortion.

Fitting a single radial coefficient (`k1 = 1.33e-07 px^-2`, principal
point pinned at image centre) and re-solving:

| | before | after |
|---|---|---|
| per-tag side spread | 4.389 - 4.925 cm (0.535) | 4.522 - 4.648 cm (0.126) |
| size inconsistency (1 sigma) | 4.67% | 1.56% |
| tag 12 (known origin) miss | 0.96 cm | 0.64 cm |

Reported positions move by **1.4 - 2.2 cm** (mean 1.57, max 2.21) once
distortion is modelled. On a 108.5 x 70.8 cm field that is roughly 2%.

## The error metric hides it

`reprojection_error_cm` measures residual **at the markers the solve was
fit to**. Distortion is absorbed there and re-emerges in the field
interior, so the stored number is not an accuracy figure:

- `hd-usb-camera`: `reprojection_error_cm = 2.96e-06` while being 2.2 cm
  off mid-field. (Four markers exactly determine a homography, so the
  residual is zero by construction — see the separate concern below.)
- `arducam-ov9782-usb-camera`: `0.168 cm` over 10 markers. Genuinely
  overdetermined, but still a marker residual, not interior accuracy.

A four-point calibration reporting `0.000 cm` is actively misleading: it
reads as "perfect" when it means "no redundancy". Worth either
suppressing the figure below 5 markers or labelling it as
degrees-of-freedom-limited.

## Scope of a fix

Applying distortion is cheap; **estimating** it is the work.

1. **Estimation** — needs multi-view capture (`cv2.calibrateCamera` over
   a checkerboard/ChArUco target, or many views of a known target). This
   is a new command, e.g. `aprilcam camera intrinsics`, and is per
   camera+resolution, not per playfield. It is separate from the
   existing per-playfield homography calibration and should persist
   independently of it (intrinsics survive the field moving; the
   homography does not).
2. **Storage** — add `camera_matrix` / `dist_coeffs` to `Calibration`
   and to `calibration.json`. Both fields optional, so an existing
   calibration keeps loading.
3. **Application** — two chokepoints. `to_world`/`to_pixel` undistort
   detected marker pixels before the homography; the deskew path
   undistorts the frame before the warp (what v1 did). Because both
   pixel->world paths are declared on the interface and implemented
   once, this stays contained.
4. **Solve order** — once intrinsics exist, the homography should be
   solved from undistorted marker pixels, otherwise the homography
   re-absorbs the distortion it was supposed to be freed from.

## Notes

The `k1` above is diagnostic-grade only: one view, seven tags, one
radial term, principal point assumed at the image centre. It
demonstrates the size of the problem and should not be shipped as a
calibration constant. A proper multi-view estimate is the real fix.

Also unresolved: after distortion correction the mean measured side is
4.564 cm against a confirmed 4.65 cm nominal, a residual ~1.9% scale
gap. Candidate causes are the field's 108.5 x 70.8 span being measured
to a different reference than the ArUco marker centres, or residual
model error from the single-parameter fit. Not separable from the
current data.
