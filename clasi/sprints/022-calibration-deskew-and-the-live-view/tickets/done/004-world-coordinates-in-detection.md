---
id: '004'
title: World coordinates in detection
status: done
use-cases:
- SUC-004
depends-on:
- '003'
github-issue: ''
issue: calibration-deskew-and-the-live-view.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# World coordinates in detection

## Description

Implements SUC-004 (parent UC-04 world activation, UC-09/UC-10
calibrated variant): wires a calibrated camera's stored `Calibration`
into `daemon/detection.py`'s burst orchestration, and activates
`vision/motion.py` — **already written and tested in sprint 021, inert
only because no camera has ever been calibrated until ticket 003**.
This ticket does not write new kinematics; it wires the existing,
tested module in and verifies it behaves correctly once fed real
world-position history.

Builds/modifies:

- `daemon/detection.py` — after building a burst's pixel-space
  `TagRecord`s (unchanged from sprint 021), check whether the camera
  has a `Calibration` (via `daemon/service.py`'s stored calibration,
  threaded through). If so: convert `pixel`/`pixel_corners` through
  `Calibration.to_world` to populate `world`/`world_corners`; compute
  `yaw_rad` from the observed front edge (`corners[0]`→`corners[1]`,
  per `design.md` §6's corner-order contract) using the same `atan2`,
  0=east, CCW-positive convention; set `calibrated=True`. Hand the
  resulting world-position history (this burst plus the ring buffer's
  recent entries for this tag) to `vision/motion.py` to populate
  `heading_rad`/`world_velocity`/`speed`. An **uncalibrated camera's
  path is completely untouched** — this is a conditional branch added
  around existing sprint-021 code, not a rewrite of it.

**Archive pointers**: none needed — `vision/motion.py` already exists
and is already tested; this ticket is integration, not new math. If the
existing `vision/motion.py` tests (`tests/aprilcam/vision/
test_motion.py`) need extending to cover the now-real call path, extend
them; do not modify the kinematics themselves without a specific reason
discovered during integration.

## Acceptance Criteria

- [x] **A tag detected at pixel positions corresponding to
      physically-east movement on the fixture's known calibration
      reports a strictly increasing `world.x` across frames;
      physically-north movement reports strictly increasing
      `world.y`** — the mirror-catching check, asserted against
      fixtures with a known calibration and a known east/north
      pixel-motion sequence (the fixture-level twin of SUC-007's
      physical hardware check).
- [x] `yaw_rad` for a tag at a known rotation matches
      `atan2(...)`-derived expectation within tolerance, 0=east, CCW
      positive, using ticket 003's calibrated geometry.
- [x] Once at least two world-position samples exist for a tag,
      `heading_rad`/`world_velocity`/`speed` are populated and
      non-`None`, and `heading_rad` equals `atan2(world_vy, world_vx)`
      computed from the same two samples (regression-pins
      `vision/motion.py`'s existing contract against a real call path
      for the first time).
- [x] A single-sample tag (no prior history) reports
      `heading_rad=None`/`world_velocity=None`/`speed=None` — not
      zero-valued — consistent with `vision/motion.py`'s existing
      "insufficient history" behavior from sprint 021.
- [x] `world_corners` preserve the `(front-left, front-right,
      back-right, back-left)` order and are converted through the
      identical homography used for `world` — never independently
      recomputed.
- [x] An **uncalibrated** camera's `TagRecord`s remain byte-for-byte
      identical in shape to sprint 021's output (`calibrated=False`,
      `world=None`, `heading_rad=None`, `world_velocity=None`,
      `speed=None`, `yaw_rad=None`, `world_corners=None`, only
      `pixel`/`pixel_corners` populated) — this capability must be
      purely additive, verified with an explicit regression test, not
      just an assumption.

## Testing

- **Existing tests to run**: `uv run pytest
  tests/aprilcam/daemon/test_detection.py
  tests/aprilcam/vision/test_motion.py` — the uncalibrated-path
  regression check above lives here.
- **New tests to write**: `tests/aprilcam/daemon/test_detection.py`
  additions — a `FakeBackend`-driven `Camera` plus a fixture
  `Calibration` (from ticket 003's test fixtures), asserting
  `TagRecord.world`/`world_corners`/`yaw_rad`/`calibrated` come out
  correct across a synthetic multi-frame sequence with known
  east/north pixel motion; the geometry convention suite's
  fixture-level east/north/heading assertions listed above.
- **Verification command**: `uv run pytest && uv run mypy` clean; zero
  `typing.Protocol`; baseline 388 passing, additive only.

## Implementation Notes

**Approach.** `daemon/detection.py`'s `DetectionEngine` gained a
`calibration_lookup: Callable[[str], Optional[Calibration]]`
constructor parameter (default: a function that always returns `None`,
reproducing sprint 021 exactly for every existing caller that never
passed it). `run_burst` calls it fresh on every burst — never cached —
so a camera that becomes calibrated mid-session picks up world
coordinates on its very next call, not just after a restart.
`daemon/service.py`'s `DaemonService.__init__` wires its own
`self.get_calibration` (already slug-resolving, per the ticket's
standing-rule note about `_resolve_slug_if_known`) into the default
`DetectionEngine()` it constructs — a one-line change
(`DetectionEngine(calibration_lookup=self.get_calibration)`), safe
because it is a bound-method *reference* resolved at call time, not at
that line, so it does not matter that `self._calibrations`/`self._lock`
are assigned later in `__init__`. Callers that inject their own
`detection=` (existing burst-counting tests) own their engine's
`calibration_lookup` themselves and are unaffected.

`_build_tag_record` (module-level, pure) takes the resolved
`Calibration` (or `None`) directly and produces either sprint 021's
exact uncalibrated shape or a populated one: `world` from
`Calibration.to_world(detection.center)`, `world_corners` from mapping
each of the four `pixel_corners` through the *same* `to_world` call, in
order — never independently recomputed — and `yaw_rad` from
`_front_edge_yaw(world_corners)`: `atan2` of the
`world_corners[0] -> world_corners[1]` vector, per the ticket's
explicit instruction to derive yaw from that literal edge (0=east, CCW
positive, the same numeric convention `heading_rad` uses).
`heading_rad`/`world_velocity`/`speed` are filled in afterward, by a
new `DetectionEngine._finish_record`/`_history_for_tag` pair: for a
calibrated hit, `_history_for_tag` reads that exact `TagId`'s already-
buffered `(timestamp, world)` samples (world non-`None` only) from the
ring buffer *before* this burst's new `TagFrame` is appended, appends
this burst's own new sample, and hands the result to the existing,
unmodified `vision.motion.compute_motion` — no new kinematics were
written, exactly as the ticket scoped. A record with `world is None`
(uncalibrated) short-circuits `_finish_record` before any of this runs.

**Verification method for the sign/yaw conventions**, three layers,
most to least isolated:

1. `tests/aprilcam/vision/test_motion.py` — unchanged (pure kinematics,
   still tested independently of any calibration fixture).
2. `tests/aprilcam/daemon/test_detection.py` — 8 new tests against a
   `_ScriptedDetector` double producing hand-built `Detection`s at known
   world positions/orientations, paired with a real (non-trivial)
   `Calibration` solved by `calibration.calibration.Calibrator` against
   `tests/aprilcam/calibration/test_calibration.py`'s own
   `main-playfield` fixtures (real ArUco rendering, real
   `cv2.findHomography` solve — not a hand-built matrix): calibrated
   world/world_corners/yaw_rad population, yaw at a known 37° rotation,
   strictly-increasing east/north motion across bursts, motion fields
   appearing only from the second sample with `heading_rad` pinned
   against an independently-computed `atan2`, and `world_corners`
   re-derived one-by-one from `pixel_corners` via the same
   `Calibration.to_world` to prove no independent recomputation. Plus
   one new `tests/aprilcam/daemon/test_service.py` integration test
   exercising the actual `DaemonService`-wired `DetectionEngine` (not
   an injected one) end to end: calibrate, then `get_tags`, confirming
   the real production wiring path, not just `DetectionEngine` in
   isolation.
3. **Real hardware** — camera 3 (`arducam-ov9782-usb-camera`), the
   stakeholder's live daemon, `main-playfield`'s existing
   `calibration.json` (unchanged since ticket 003: 10/10 matched,
   0.184 cm reprojection error). Daemon restarted to pick up this
   ticket's code; `/tmp/cam3.json` and `main-playfield.json` re-pushed
   (both ephemeral, per UC-02) — `calibration.json` itself needed no
   re-push, since it is file-backed and reloaded automatically at
   startup. `aprilcam camera tags 3` output (unedited):

   ```
   aruco 4        world=(67.2, 0.0)   heading=-0.35  speed=0.00
   aruco 1        world=(-67.0, 44.7) heading=-0.59  speed=0.00
   aruco 5        world=(67.0, -44.7) heading=-0.08  speed=0.00
   aruco 7        world=(-67.0, -44.7) heading=-2.44 speed=0.00
   aruco 6        world=(0.1, -44.7)  heading=0.60   speed=0.00
   aruco 8        world=(-67.0, -0.1) heading=-1.01  speed=0.00
   aruco 9        world=(-34.9, 0.1)  heading=0.73   speed=0.00
   aruco 3        world=(67.0, 44.7)  heading=-2.78  speed=0.00
   aruco 2        world=(0.2, 44.6)   heading=-1.91  speed=0.00
   ```

   All 9 visible ArUco markers land within ~0.2 cm of their surveyed
   positions (e.g. ArUco 1 surveyed (-67.0, 44.65) -> reported
   (-67.0, 44.7); ArUco 5 surveyed (67.0, -44.65) -> reported
   (67.0, -44.7)) — east markers (3/4/5) all positive x, west markers
   (1/7/8) all negative x; north markers (1/2/3) all positive y, south
   markers (5/6/7) all negative y. No mirroring, correct signs on both
   axes. `heading_rad`/`speed` are populated (small, near-zero, as
   expected for stationary markers with subpixel detection jitter)
   because the ring buffer already had a prior calibrated sample for
   each tag from an earlier query in this same verification session —
   the motion wiring is live end to end, not just in tests.

   **Deviation, reported plainly per the ticket's honesty requirement:
   AprilTag 1 (the origin marker) and ArUco 10 were not detected in
   this run.** A physical sheet of paper is currently sitting on the
   centre of the desk, occluding that part of the field (confirmed via
   `aprilcam camera image 3` — visible in the captured frame). This is
   a change to the physical setup since ticket 003 (whose own
   Implementation Notes recorded AprilTag 1 at (0.10, -0.03) cm during
   that ticket's live check, before whatever placed the paper there),
   not a code or geometry defect — I have no way to move objects on the
   physical desk from this session. The homography itself is unchanged
   from ticket 003 (identical `calibration.json`, confirmed by content
   above), so its correctness on 9 independently-checked points
   (matching survey to ~0.1-0.2 cm, exercising both perimeter and
   interior markers, both hemispheres of both axes) is strong indirect
   evidence it would still place AprilTag 1 near (0, 0) if visible, but
   this ticket does **not** claim to have directly re-confirmed that —
   only that it could not run the check, and why. Ticket 007 (dedicated
   hardware verification, this sprint's own designated venue for
   exactly this class of live check) should re-run this with the paper
   cleared, or note the same obstruction if it recurs.
