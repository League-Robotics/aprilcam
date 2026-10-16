---
status: pending
---

# Coplanar intrinsics rejected on a second camera — the fx-plausibility gate may be the wrong test

## Description

Sprint 030 shipped opt-in lens-distortion estimation
(`camera calibrate --with-intrinsics`). It has now been attempted
against **two different physical cameras on the real field, and rejected
both times**:

| camera | mount | result |
|---|---|---|
| `arducam-ov9782-usb-camera` | near-nadir | rejected — `fx=310.5px`, 0.24x image width |
| `arducam-ov9281-usb-camera` | significantly off-centre | rejected — `fx=166.3px`, 0.13x image width |

The single success (camera 3, 2026-08-20, `fx=511.185`, 0.40x) now looks
like a lucky draw rather than a measurement: sprint 030 ticket 003 found
that repeated runs on that same camera under identical conditions
produced **fx from 504 to 661 px and k1 from 0.0069 to 0.0135**, while
held-out reprojection residual stayed **flat** at ~2.7-3.2 px.

An off-centre mount was expected to break the fx/depth degeneracy by
introducing obliquity. It did not — the OV9281's rejection is *further*
outside the plausibility band than the near-nadir camera's. That
hypothesis is dead.

## Nothing is blocked — state this clearly

Homography calibration is unaffected and works well on both cameras:
`arducam-ov9281-usb-camera` calibrated first try, 10/10 ArUco matched,
**0.172 cm** reprojection error (better than camera 3's 0.186). The
system is fully usable. What is unavailable is the *distortion
refinement* sprint 030 added, so v2 currently behaves as it did before
that sprint.

## The observation that reframes this

**v1 has distortion coefficients and works fine in the field. v2 now
stores none.** So on distortion specifically, v2 is currently *behind*
v1 — and by our own choice, not by a limitation.

v1 stored `dist_coeffs [0.42, -2.43, ...]`, which sprint 030 correctly
identified as an unconstrained overfit (k2 = -2.43 is not a plausible
lens property). Yet the stakeholder reports v1 working well in practice.
That is evidence worth taking seriously rather than explaining away.

A plausible reason: **for a fixed camera viewing a plane, what matters is
the composite pixel-to-world map, not physically-correct intrinsics.**
v1 never asked whether its focal length was believable — it undistorted
the points and let the homography absorb the remainder. An "unphysical"
fx paired with a compensating k1 can still reduce real reprojection
error.

v2's `_sanity_check_intrinsics` gates on **fx plausibility** — a
physical-realism test ("expected roughly 0.3x-3.0x for an ordinary
lens"). But the property we actually want is *does this model reduce
error*, and sprint 030 ticket 002 **already computes exactly that**:
held-out reprojection residual, fit on a subset of surveyed points and
validated against the remainder. It is computed, logged, and then not
used as the accept/reject criterion.

## Proposed investigation — before any hardware work

Try changing the gate from "is this a physically believable lens" to
"does this measurably beat no correction on held-out points":

- Accept a fit when its held-out residual improves on the
  no-distortion baseline by some margin; reject when it does not.
- Keep the fx bound as a *warning* rather than a veto, or widen it —
  it is still useful signal that the fit is not physically meaningful,
  which matters if the intrinsics are ever used for anything beyond this
  one plane (they currently are not).
- Re-run against both cameras and report held-out residual with and
  without the stored model.

If that works, both cameras get a usable lens model **with no hardware
work at all**, and this issue closes without touching the field.

## If it does not work: the elevated-tag route

Sprint 030 ticket 002 proved synthetically that elevated ground truth
resolves this exact degeneracy, even at a mathematically exact 0 deg
mount tilt. The thresholds it implemented
(`MIN_ELEVATED_FEATURES_FOR_ELEVATED_MODE`, `MIN_ELEVATED_Z_SPREAD_CM`):

- **at least 3 surveyed features with `height_cm > 0`**, and
- **z-spread of at least 5.0 cm**, computed as `max(height) - min(height)`
  across all surveyed points. Since the field markers sit at 0, this is
  simply *how high the tallest elevated tag is*.

The tags do **not** need to be at different heights from one another —
three tags all at 6 cm satisfies both thresholds. The 5 cm floor is
conservative: synthetic trials converged focal length to within a few
percent of truth at z-spreads as low as **3 cm** at exactly 0 deg tilt
(the hardest case), and 5 cm keeps a centimetre of margin above that.

Practically: three small blocks 5-10 cm tall with tags on top, positions
surveyed into the playfield definition via the `Feature.height_cm`
schema ticket 002 already shipped. Sprint 029's temporary stands were
15-17 cm — far more elevation than is actually required.

This would also give the elevated code path its first exercise against
real (non-synthetic) ground truth, which sprint 030 shipped untested.

## Related

- `sprint-030-unfinished-verification-distortion-benefit-unproven.md` —
  the parent record of what sprint 030 did and did not prove. This issue
  is the new evidence: a second camera, a second rejection, and a
  specific hypothesis about why the gate may be wrong.
- `v2-calibration-does-not-model-lens-distortion.md` — the original
  regression, closed by sprint 030 on mechanism rather than on measured
  benefit.
- Sprint 030 ticket 002 completion notes (Open Questions 5 and 6) and
  ticket 003's stability finding.

## Resolution

Superseded by sprint 045 ticket 008: the single-view estimator and its
fx-plausibility gate were deleted. The fit was non-physical (fx/k1 ridge);
the measured ChArUco lens model (sprint 044) replaces it, so there is no
gate left to reconsider.
