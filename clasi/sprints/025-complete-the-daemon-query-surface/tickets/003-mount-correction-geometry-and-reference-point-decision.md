---
id: '003'
title: Mount-correction geometry and reference-point decision
status: done
use-cases:
- SUC-002
depends-on: []
github-issue: ''
issue: complete-the-daemon-query-surface.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Mount-correction geometry and reference-point decision

## Description

Add the pure geometry functions that turn an observed tag pose into
the robot's pose, in `calibration/geometry.py` alongside the existing
`pixel_to_world`/`world_to_pixel`/`apply_homography` functions (no
daemon state, no I/O — the same shape as every function already in
this module). This ticket is the foundation for ticket 004 (registry
service + detection wiring), which consumes this math but does not
itself decide it.

**Implements this sprint's two resolved decisions as code, not just
prose** (see `sprint.md`'s Design Rationale for the full reasoning):

- **Reference point is client-defined, not daemon-mandated.** The
  function accepts whatever `mount_x`/`mount_y`/`mount_z`/`mount_yaw`
  it is given and applies them mechanically; it has no notion of, and
  makes no attempt to verify, what physical point on the robot they
  are measured from. The recommended default (centre of rotation) is
  a guide-level (ticket 006) recommendation, never enforced in code.
- **Frame convention: the robot's own frame, +x forward, +y left,
  rotating with `mount_yaw`** — carried forward unchanged from v1's
  `x_mm forward, y_mm left` (`archive/src/aprilcam/daemon/
  mobile_tags.py`'s own docstring), rather than inventing a new sign
  convention.

**Math, in order**:
1. **Parallax** — project the tag's observed world position down
   through `mount_z` onto the field plane. A tag elevated above the
   field is seen from an oblique camera angle at an apparent position
   offset from its true ground footprint; correcting this requires the
   camera's known geometry, already available via `Calibration`
   (`calibration/interfaces.py`).
2. **Offset** — rotate `(mount_x, mount_y)` by the tag's observed
   `yaw_rad` into the world frame and add it to the parallax-corrected
   position. This is what "must be the robot's frame for yaw to
   compose correctly" (UC-12's own resolved point) means concretely.
3. **Yaw** — add `mount_yaw` to the observed `yaw_rad` to produce the
   reported heading/yaw, so a tag mounted rotated relative to the
   robot reports the robot's true orientation (UC-12.S-4).

**This math is genuinely new, not a port.** `docs/design/
development-plan.md` Stage 6 names "parallax + mobile-tag mount
correction" as in-scope reuse from v1, but v1's implementation lived
entangled inside `CameraPipeline`/`core/aprilcam.py`'s detection loop
(`archive/src/aprilcam/daemon/mobile_tags.py`'s own docstring: "applied
to world-position reporting by `CameraPipeline`") — there is no
isolated, portable parallax function in the archive to lift directly.
Derive it fresh as a pure function; do not attempt to extract v1's
entangled version.

**Documentation updates this ticket owns**:
- `types.py`'s `MountParameters` docstring: remove the "Open (UC-12)"
  framing on the reference-point paragraph, stating the resolved
  convention instead.
- `docs/design/use-cases/daemon.md`: add a "Decisions resolved
  2026-08-03" table recording (a) the reference-point convention is
  client-defined (recommended default: centre of rotation), (b) the
  frame/sign convention (robot's own frame, +x forward, +y left,
  rotating with `mount_yaw`), and (c) mount-parameter changes are
  **not retroactive** — only new frames use updated parameters,
  buffered history keeps whatever was in effect when it was built
  (ticket 004 implements this behaviourally; this ticket records the
  resolution since it answers a UC-12 open question, not a
  ticket-004-specific implementation detail).

## Acceptance Criteria

- [x] New pure functions in `calibration/geometry.py` implementing
      parallax projection (via `mount_z`) and offset/yaw composition
      (via `mount_x`/`mount_y`/`mount_yaw`), taking a `WorldPoint` +
      `yaw_rad` + `MountParameters` and returning the corrected
      `WorldPoint` + `yaw_rad`.
- [x] UC-12.S-2 (synthetic): a tag at a known elevation (`mount_z` =
      20cm), viewed from a known oblique camera geometry, corrects to
      a position measurably closer to hand-computed ground truth than
      the uncorrected position.
- [x] UC-12.S-3 (synthetic): an off-centre, elevated tag (`mount_x` =
      8, `mount_y` = 3, `mount_z` = 15) corrects to the robot's
      reference point; as the synthetic "robot" rotates in place, the
      corrected position stays fixed while the raw tag position sweeps
      a circle around it.
- [x] UC-12.S-4 (synthetic): a tag mounted rotated 90 degrees
      (`mount_yaw` = 90deg) on a robot driving east reports corrected
      heading 0 (east), not 90 degrees.
- [x] `types.py`'s `MountParameters` docstring updated to state the
      resolved reference-point and frame convention (no longer marked
      "Open").
- [x] `docs/design/use-cases/daemon.md` gains a "Decisions resolved
      2026-08-03" table entry for the reference-point convention, the
      frame/sign convention, and the not-retroactive resolution.
- [x] `uv run mypy` clean; functions are pure (no daemon state, no
      I/O), matching this module's existing style.

## Implementation Plan

**Approach**: pure math first, tested standalone with hand-computed
synthetic cases (no daemon, no detection engine, no hardware) — this
isolates the geometry from ticket 004's registry/wiring concerns,
mirroring the same split tickets 001/002 use for feature matching.

**Files**: `src/aprilcam/calibration/geometry.py` (extend),
`src/aprilcam/types.py` (docstring only),
`docs/design/use-cases/daemon.md` (decisions table).

**Testing plan**:
- **Existing tests to run**: `uv run pytest
  tests/aprilcam/calibration/test_geometry.py -v` (confirm no
  regression to the existing pure-geometry suite this ticket extends),
  plus the full headless suite.
- **New tests to write**: synthetic hand-computed cases for parallax,
  offset, and yaw composition, added to
  `tests/aprilcam/calibration/test_geometry.py` alongside the existing
  geometry convention suite (`docs/design/design.md` §6's "sacrosanct
  tests" — these never get relaxed).
- **Verification command**: `uv run pytest -m "not camera"` and
  `uv run mypy`.

**Documentation updates**: `types.py` docstring,
`docs/design/use-cases/daemon.md` decisions table (both described
above).
