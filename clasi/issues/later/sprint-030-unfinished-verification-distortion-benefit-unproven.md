---
status: pending
---

# Sprint 030 shipped the lens model but never proved it improves accuracy

## Description

Sprint 030 was chartered against a measured regression: v2 maps pixels to
world coordinates with a bare homography, v1 modelled distortion, and the
gap was measured at ~2.2 cm with an apparent-tag-size-vs-image-radius²
correlation of **−0.96**.

The sprint delivered the whole mechanism, and it works: optional
intrinsics on `Calibration`, point-level undistort/redistort in
`to_world`/`to_pixel`, a **constrained** `cv2.calibrateCamera` fit that
refuses v1-style overfits, opt-in estimation on `camera calibrate
--with-intrinsics`, carry-forward across recalibration, and a
`Feature.height_cm` schema for non-coplanar ground truth. Full suite
green (1486 passed).

**What it did not do is demonstrate that any of it improved accuracy on
real hardware.** Both of the sprint's own falsifiable checks came up
short, for physical rather than software reasons, and the sprint should
not be remembered as having closed the regression.

## Gap 1 — the −0.96 fixture was never re-measured

The original correlation was measured on **hd-usb-camera /
secondary-playfield**. That camera still cannot produce a lens model:

```
$ aprilcam camera calibrate hd-usb-camera --with-intrinsics
calibration failed: estimate_intrinsics requires at least 6 ArUco+AprilTag
ground-truth points surveyed on the linked playfield 'secondary-playfield';
found 4
```

Ticket 003 substituted an equivalent check on camera 3 using
`main-playfield`'s own 10 identically-sized ArUco markers and measured
correlations of **−0.52 to +0.40** across six live bursts — never
significant at n=10. That is a legitimate substitute and it weighs
against a large hidden distortion, but it is a different camera, a
different lens, and a different playfield from the one the regression was
measured on.

**To unblock**: survey at least 2 more ArUco/AprilTag ground-truth points
into `secondary-playfield` (4 → 6+), then re-run `--with-intrinsics` on
hd-usb-camera and re-measure the correlation.

## Gap 2 — the optical-height check was never run

The criterion: re-running `camera locate` on camera 3 should move the
solved height from **128.25 cm** toward the physically-measured **127 cm**,
on the theory that the extra ~1.25 cm was distortion the old model was
absorbing. Unlike the radial check, this criterion has no "or say so"
escape clause — it is simply **unmet, not falsified**.

It needs the two elevated reference fixtures from sprint 029: AprilTag
**15** over `dot-northeast-orange` (50, 30) on a **17.2 cm** stand, and
AprilTag **14** over `dot-southwest-orange` (−50, −30) on a **15.0 cm**
stand. Ticket 003 attempted the run and got the honest "not detected"
refusal. **The stakeholder removed those stands deliberately during
sprint 029 and confirmed they were not needed again** — so this is a
retired fixture, not a lost one, and re-staging is a real decision rather
than a restoration.

**To unblock**, either:
- **(a)** temporarily re-stage tags 14/15 on their stands and re-run
  `aprilcam camera locate`; or
- **(b)** *preferred* — survey 2-3 **permanently** elevated tags into
  `main-playfield` with x/y/z, using the `Feature.height_cm` schema
  ticket 002 already shipped. This makes the check reproducible on demand
  forever instead of needing a re-stage each time, and simultaneously
  gives the non-coplanar intrinsics path its first real ground truth
  (see sprint 030 Open Question 7).

## Gap 3 — the fitted lens model is not stable

Independent of the two blocked checks, ticket 003 found that repeated
`--with-intrinsics` runs on camera 3, under identical conditions,
produced **fx from 504 to 661 px and k1 from 0.0069 to 0.0135**, while
held-out reprojection residual stayed flat at ~2.7-3.2 px.

Flat residual with wandering parameters is a ridge in the cost surface:
the fx/depth trade-off ticket 002 identified is *softened* by camera 3's
tilt but not eliminated. **Any single stored lens model is one draw from
that ridge, not a measurement.** The currently-stored value
(`fx = 511.185`, `k1 = 0.006877`) should be treated accordingly.

This is also the strongest argument for (b) above: genuine z-variation in
the ground truth is what collapses the ridge, and ticket 002 demonstrated
that synthetically (`test_elevated_mode_resolves_the_fx_depth_degeneracy_even_at_exact_nadir`).

## Suggested resolution

Do (b) — permanent elevated tags in `main-playfield` — and it addresses
Gap 2 and Gap 3 together, and gives the elevated code path its first
non-synthetic exercise. Gap 1 is independent and needs the
`secondary-playfield` survey.

Until then, the honest statement of sprint 030's outcome is: **the lens
model is built, tested, and shipping; whether it improves real-world
accuracy is unmeasured.**

## Related

- Sprint 030 `sprint.md` Success Criteria and Open Questions 4, 6, 7.
- Sprint 030 ticket 003 completion notes and
  `docs/knowledge/2026-08-20-camera-3-distortion-reverification.md`.
- `v2-calibration-does-not-model-lens-distortion.md` — the source issue,
  closed by this sprint on mechanism, not on measured benefit.

## Sprint 045 ticket 008 note (left open, narrowed)

Sprint 045 deleted the estimator. Gap 3 (unstable fitted lens) and the
`--with-intrinsics` unblock steps in Gap 1 are superseded. What survives is
the verification question itself: does a lens model improve real-world
accuracy? Now answerable with sprint 044's *measured* lens: (a) re-measure
the radial-error correlation (original -0.96) and (b) re-run `camera
locate` on camera 3 to see if the solved height moves from 128.25 toward
127 cm. Sprint 045 ticket 013 (live runbook) should carry this check.
