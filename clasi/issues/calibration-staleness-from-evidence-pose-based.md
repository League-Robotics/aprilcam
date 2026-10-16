---
status: pending
---

# Calibration staleness from evidence (pose-based)

## Description

Deferred from sprint 045 ticket 014 (optional, dropped at sprint close on
2026-10-16 so the sprint could ship the pose-based calibrator). Nothing in
sprint 045 depends on it.

Today `calibration_stale` flips only on camera reopen and on the
opportunistic `compute_drift_cm` check against matched markers (the
`DetectionEngine` drift hook). With a stored pose calibration
(`Calibration.method == "pose"`, camera height/nadir/tilt persisted), any
visible known marker can be re-solved at low rate against the stored camera
pose: reproject each visible matched marker's corners through the stored
lens and pose and compare with the detected corners (px) and field position
(cm). A persistent disagreement beyond tolerance flips stale via
`DaemonService._mark_stale`; insufficient evidence (marker covered, dark
frame) returns "cannot verify", never "confirmed" (UC-11.S-3 stance,
matching `compute_drift_cm`'s `None`).

## What to build

- Keep it inside the existing drift hook (no new thread): a pose-aware
  variant beside `compute_drift_cm`, used when the calibration is a pose
  one, with hysteresis (N consecutive disagreeing checks) and the existing
  check cadence.
- Centre-point calibrations keep the existing drift behaviour unchanged.
- No extra per-frame cost: reuse the detections already produced.

## Acceptance

- Synthetic: shifting the camera pose by a few cm or degrees flips stale
  within the hysteresis window; an unmoved camera with realistic corner
  noise never does over a long run.
- A single visible marker suffices; zero visible reads "cannot verify".
- Calibration DESIGN.md staleness section updated.

See sprint 045's `sprint.md` (Decision 5 and the dropped ticket 014) and
`docs/knowledge/2026-10-16-pose-based-field-calibration.md` for the pose
conventions this builds on.

## Stakeholder direction, 2026-10-16: a survey marker and cheap bump detection

Several of these playfields get bumped and must not be trusted afterwards.
A bump is almost never a pure rotation; it is a translation. So:

- **Survey marker.** One marker per field is a fixed, known reference that
  calibration never solves for: on the main playfield it is **AprilTag 1 at
  (0, 0)**, always. The definition should declare it as the survey marker
  (not just another feature), calibration records where it was seen, and
  every later check compares against that.
- **Cheap check.** At low rate, re-detect the survey marker (and any other
  visible known marker) and compare its world position through the stored
  calibration with its declared position. If the survey marker is no longer
  at (0, 0) beyond tolerance, the field has been bumped: flag
  `calibration_stale` with the measured offset, and prefer **compensating
  the translation** (shift the stored mapping by the measured delta, note
  it as a compensation with the residual) over demanding a recalibration,
  which stays the fallback when the residual after compensation is too
  large (a rotation or a camera move).
- The existing `compute_drift_cm` hook and the pose-based reprojection
  check above are the places this lives; this item defines what "bumped"
  means and which marker is the authority.

See also `datum-tag-defines-the-playfield-frame.md` (2026-10-16): the datum
tag is the authority this item checks against; "bumped" means the datum no
longer reads (0, 0, 0°) through the stored mapping.
