---
status: pending
---

# AprilTag 1 detects unreliably on `arducam-ov9281-usb-camera` while all 10 ArUco markers detect solidly

## Description

On camera 5 (`arducam-ov9281-usb-camera`, the mono/global-shutter
camera), AprilTag 1 detects intermittently while all 10 ArUco markers
(2-10 plus corners) detect on effectively every poll, on the same
frames where AprilTag 1 fails.

## Measurements (2026-08-23)

Repeated `aprilcam camera tags 5` polls (10 polls each), varying
exposure to check whether it's an exposure problem:

| exposure | AprilTag 1 detected |
|---|---|
| 150 | 1/10 |
| 300 | 3/10 |
| 500 | 2/10 |

No value tested got above 3/10, and there's no clean monotonic trend —
this doesn't look like a plain brightness/exposure problem. ArUco
markers detect reliably across this entire exposure range on the same
frames.

Visually, a captured raw frame (see the sibling investigation in
`docs/knowledge/2026-08-23-v1-known-good-arducam-ov9782-exposure-settings.md`)
shows AprilTag 1 looking reasonably sharp at normal viewing scale — no
obvious blur or occlusion to the eye.

## Hypothesis (unconfirmed)

AprilTag 36h11 encodes more bits per unit area than the 4x4 ArUco
dictionary, so it's more sensitive to marginal resolution/focus/decode
margin. This may be a real physical limitation of this camera's
fixed-focus lens and mount distance rather than something a UVC
exposure setting can fix — but only exposure has actually been ruled
out here; focus and detector-parameter tuning have not been checked.

## Proposed investigation

- Check physical focus on this camera's lens (if adjustable) with
  AprilTag 1 specifically in frame.
- Compare AprilTag decode success against a printed AprilTag held
  closer to the camera, to isolate whether this is purely a
  distance/size-in-frame effect.
- Consider whether the vision pipeline's per-family detection
  parameters (quad decimation, decode sharpening) are implicitly tuned
  around the other (color) camera and don't suit this one.

## Current state / workaround

Settled on `exposure=300` as this camera's working config — best of
the three tried, doesn't regress anything else (ArUco, general
visibility), even though it doesn't fix AprilTag reliability.

**Not blocking**: ArUco alone gives full playfield coverage (10
markers) for homography/calibration on this camera. Only the single
AprilTag-based origin anchor is affected.

## Related

- `docs/knowledge/2026-08-23-v1-known-good-arducam-ov9782-exposure-settings.md`
  — the sibling camera-3 exposure investigation this was found during.
