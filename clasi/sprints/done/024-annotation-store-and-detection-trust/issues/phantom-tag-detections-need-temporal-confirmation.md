---
status: in-progress
sprint: '024'
tickets:
- 024-006
- 024-007
---

# Phantom tag detections are reported as real, moving objects

## Description

The detector produces occasional false-positive ArUco decodes, and the
system currently presents them to consumers as ordinary tags — with
world coordinates, and if the phantom is seen more than once, with
**computed velocity and heading**.

Observed live on `main-playfield`, camera 3, four consecutive polls
seconds apart:

```
poll 1:  (no aruco 17)
poll 2:  aruco 17  world=(-59.4, -19.6)  yaw=0.086  heading=3.045  speed=0.14
poll 3:  (no aruco 17)
poll 4:  (no aruco 17)
```

and in an earlier session the same id appeared at `world=(50.1, -30.3)`
— a different place entirely. ArUco 17 is **not** on the field. Sprint
021's hardware verification also recorded "one honestly-reported
one-off spurious `aruco 17`, not reproducible in 7 other polls," so
this has been intermittently present since detection first shipped.

**Why this matters more than a cosmetic glitch.** A phantom that
flickers in and out at different positions is not just noise in a list
— the motion pipeline joins those sightings into a trajectory, so the
system reports a confident `speed` and `heading` for an object that
does not exist. That is precisely the "plausible-looking wrong answer"
class the world-first contract exists to prevent, and it is exactly
what a robot subscribing to tags would act on. The 0.5 cm displacement
threshold added in sprint 022 does not help: a phantom jumping tens of
centimetres clears it easily.

## Cause

Not yet diagnosed; candidates in rough order of likelihood:

- **Detection is stateless and single-frame.** `DetectionEngine.run_burst`
  reports whatever `TagDetector.detect` returns for one frame. There is
  no confirmation across frames, so a single bad decode becomes a
  first-class `TagRecord`.
- **ArUco 4x4 has weak error correction.** A 4x4 dictionary at small
  apparent size, on a high-contrast wood-grain background with
  shadows, has real false-decode probability. v1 may have tuned
  detector parameters against this — worth mining `archive/` per
  `CLAUDE.md`'s archive rule.
- **No plausibility gate.** The playfield definition knows exactly
  which ArUco ids are surveyed (1–10 here). Ids outside that set are
  legitimate for *mobile* tags, so they cannot simply be rejected —
  but they could be held to a higher confirmation bar than a surveyed
  static marker.

## Proposed fix

Design decision required, not just a code change. Options that should
be weighed and one chosen deliberately:

1. **Temporal confirmation** — a tag must be detected in N of the last
   M frames before it is reported. Strongest fix, and it composes with
   the ring buffer that already exists. Cost: latency for a genuinely
   new tag entering the field, which matters for a fast robot.
2. **Detector parameter tuning** — tighten the ArUco detector's
   acceptance (error-correction rate, border/perimeter constraints).
   Cheap, no latency cost, but harder to reason about and may reduce
   true-positive range.
3. **Report confidence rather than filter** — add a field so consumers
   can decide. Honest, but pushes the problem onto every consumer and
   contradicts the daemon's role as the single vision authority.
4. **Suppress motion for unconfirmed tags** — narrower version of (1):
   report the sighting but withhold `heading_rad`/`world_velocity`/
   `speed` until the tag has been seen consistently. This kills the
   worst symptom (a phantom with a trajectory) without adding latency
   to a tag's first appearance.

Whatever is chosen must not break the mobile-tag case: a robot's tag
legitimately appears, moves fast, and leaves.

## Verification

- Poll a static, clean field 20+ times; no id outside the surveyed set
  is reported. (Today ArUco 17 appears roughly 1 in 4.)
- A genuinely new tag placed on the field is reported, and the latency
  before it appears is measured and documented.
- A phantom that does appear never carries a computed velocity or
  heading.
- The mobile-tag path still tracks a real moving tag correctly.

## Related

- `src/aprilcam/vision/detector.py`, `src/aprilcam/daemon/detection.py`
  (`run_burst`, the ring buffer that a confirmation window would use)
- `src/aprilcam/vision/motion.py` — where the phantom trajectory is
  computed
- Sprint 021 ticket 007 completion notes — the first recorded sighting
- `docs/design/use-cases/daemon.md` UC-09, UC-10
