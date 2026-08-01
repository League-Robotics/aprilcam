---
id: '004'
title: World coordinates in detection
status: open
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

- [ ] **A tag detected at pixel positions corresponding to
      physically-east movement on the fixture's known calibration
      reports a strictly increasing `world.x` across frames;
      physically-north movement reports strictly increasing
      `world.y`** — the mirror-catching check, asserted against
      fixtures with a known calibration and a known east/north
      pixel-motion sequence (the fixture-level twin of SUC-007's
      physical hardware check).
- [ ] `yaw_rad` for a tag at a known rotation matches
      `atan2(...)`-derived expectation within tolerance, 0=east, CCW
      positive, using ticket 003's calibrated geometry.
- [ ] Once at least two world-position samples exist for a tag,
      `heading_rad`/`world_velocity`/`speed` are populated and
      non-`None`, and `heading_rad` equals `atan2(world_vy, world_vx)`
      computed from the same two samples (regression-pins
      `vision/motion.py`'s existing contract against a real call path
      for the first time).
- [ ] A single-sample tag (no prior history) reports
      `heading_rad=None`/`world_velocity=None`/`speed=None` — not
      zero-valued — consistent with `vision/motion.py`'s existing
      "insufficient history" behavior from sprint 021.
- [ ] `world_corners` preserve the `(front-left, front-right,
      back-right, back-left)` order and are converted through the
      identical homography used for `world` — never independently
      recomputed.
- [ ] An **uncalibrated** camera's `TagRecord`s remain byte-for-byte
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
