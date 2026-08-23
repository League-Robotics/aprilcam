---
status: done
sprint: '034'
tickets:
- 034-001
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

## Root cause found (2026-08-23) — the detector is NOT the problem

A full v1-vs-v2 detector comparison (parameters, preprocessing,
resolution, pixel format) found the two pipelines **byte-identical** on
every effective parameter: same cv2.aruco DICT_APRILTAG_36h11, same
adaptive-threshold windows (3/53/4), same CORNER_REFINE_SUBPIX, same
mono8 highpass-flattened full-resolution input. (All five `aprilTag*`
DetectorParameters are inert in BOTH versions — they only apply under
CORNER_REFINE_APRILTAG — do not tune them.)

The reported unreliability is v2's **confirmation gate amplifying a
marginal raw rate**, `daemon/detection.py`:

1. `CONFIRMATION_STREAK = 3` (`detection.py:282`): a tag id must
   appear in 3 strictly consecutive bursts before it is reported at
   all. One `get_tags` call = one burst (`service.py:692`). Reported
   rate ≈ raw_rate³: raw ~0.45-0.6 for AprilTag 1 on this camera →
   the observed 1-3/10; ArUco raw ~0.97 → 0.91, "solid". Measured
   live 2026-08-23: 12 polls → AprilTag present in 1.
2. **No disappearance hysteresis** (`detection.py` `_confirm_detections`):
   the streak map is rebuilt each burst from only the ids present, so
   a confirmed tag that misses ONE burst drops instantly and pays the
   full 3-burst re-confirmation. The method docstring records this as
   an accepted trade-off; combined with a marginal raw rate it makes
   the tag effectively invisible.
3. v1 never had this cliff: it reported every raw hit, bridged gaps
   with Lucas-Kanade optical flow on 2 of every 3 frames, and
   suppressed phantoms with a playfield-polygon filter instead of a
   temporal gate.

The streak exists on real evidence (6/40 raw frames carried phantom
off-field ids — ticket 024-007) and must stay for *appearance*.

## Proposed fix

Add **disappearance grace** to `DetectionEngine._confirm_detections`:
once an id is confirmed, keep reporting it (with its last-detected
record or simply keep it eligible) through up to N missed bursts (or a
time horizon), resetting only when the grace expires. Phantom
suppression is preserved — an unconfirmed id still needs
`CONFIRMATION_STREAK` consecutive hits to get in. Constant
constructor-injectable like `confirmation_streak` itself. Raising the
raw rate (optical-flow bridging, CORNER_REFINE_APRILTAG) is possible
follow-up work, not this issue.
