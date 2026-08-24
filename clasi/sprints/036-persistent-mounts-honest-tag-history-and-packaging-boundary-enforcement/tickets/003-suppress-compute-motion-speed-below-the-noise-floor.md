---
id: '003'
title: Suppress compute_motion speed below the noise floor
status: open
use-cases: [SUC-002]
depends-on: ["002"]
github-issue: ''
issue: tag-history-gaps-and-jitter-speed-mislead-mcp-consumers.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Suppress compute_motion speed below the noise floor

## Description

`src/aprilcam/vision/motion.py`'s `compute_motion` suppresses
`heading_rad`/`world_velocity` to `None` when two-sample displacement
falls below `MIN_RELIABLE_DISPLACEMENT_CM` (0.5 cm) — but always
returns `speed = displacement / dt` regardless. Ticket 022-007's
original reasoning ("a small speed value honestly means roughly
stationary") holds only when `dt` is on the order of a second or more.
At tight query cadence (confirmed live: ~0.25s between two MCP calls),
the same ~0.25 cm of detector jitter that the displacement threshold
exists to catch produces `speed = jitter/dt ≈ 1 cm/s` — small in
absolute terms but confidently non-zero, misleading a consumer into
believing a stationary tag is moving.

**Design decision (already made during sprint planning — implement
as specified, do not re-litigate):** suppress `speed` to `None`
alongside `heading_rad`/`world_velocity` whenever two-sample
displacement is below `MIN_RELIABLE_DISPLACEMENT_CM`, regardless of
`dt`. This is the smallest change, consistent with the codebase's
"cannot compute is `None`, never a fabricated number" contract
(`types.py`'s world-first contract), and requires no new constant —
the existing distance threshold already does the job once applied to
all three output fields instead of two. See this sprint's `design/`
overlay (`clasi/sprints/036-.../design/vision-DESIGN.diff.md`) for the
full rationale and the accepted consumer-visible consequence: real
motion slower than roughly `MIN_RELIABLE_DISPLACEMENT_CM / dt` now
reads as `None` instead of a small positive number (e.g. ~2 cm/s at a
0.25s query cadence) — the correct trade of "no signal" over
"confident wrong signal."

## Acceptance Criteria

- [ ] `compute_motion` returns `(None, None, None)` for
      `(velocity, speed, heading_rad)` when two-sample displacement is
      below `MIN_RELIABLE_DISPLACEMENT_CM` — `speed` is no longer the
      exception that stays a real float at this magnitude.
- [ ] The "fewer than two points / zero-`dt`" early-return path is
      unaffected (still `(None, None, None)`, unrelated to this
      change).
- [ ] Unit tests in `tests/aprilcam/vision/test_motion.py` cover the
      displacement-threshold boundary at both **small `dt`** (e.g.
      ~0.25s, the case that motivated this fix) and **large `dt`**
      (e.g. several seconds), confirming `speed` is `None` below the
      threshold and a real float above it, at both time scales.
- [ ] `vision/motion.py`'s module docstring is revised: remove the
      now-false claim that speed is "bounded near zero for jitter" as
      a *safe* reading (it is bounded in magnitude, but at short `dt`
      that magnitude was still misleadingly non-zero) — state instead
      that `speed` is suppressed below the same displacement threshold
      as `heading_rad`/`velocity`, and why (the dt-dependence
      argument above).
- [ ] `src/aprilcam/types.py`'s `TagRecord` docstring is checked for
      any claim about `speed` semantics; update only if one exists
      (as of sprint planning, `TagRecord`'s docstring does not
      describe `speed` specifically, so likely no change is needed —
      confirm rather than assume).
- [ ] Append a short "motion field interpretation" note to the section
      ticket 002 added to `AGENT_GUIDE.md`: `speed` being `None`
      alongside `heading_rad`/`world_velocity` means "below the
      reliable-displacement threshold," not "not computed" — point at
      `MIN_RELIABLE_DISPLACEMENT_CM`'s value (0.5 cm) informally
      (do not hardcode it if it might change; describe it as "the
      same noise floor `heading_rad` uses").
- [ ] Check `src/aprilcam/ROBOT_API_GUIDE.md` for any claim that
      `speed` stays populated when `heading`/`velocity` go `None` —
      none is expected to exist (confirmed during sprint planning),
      but confirm rather than assume; add a one-line clarification
      only if needed.

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/vision/test_motion.py tests/aprilcam/daemon/test_detection.py -q`
  (the latter in case any daemon-level test asserts a specific `speed`
  value for a near-stationary tag).
- **New tests to write**: boundary-behavior cases in
  `tests/aprilcam/vision/test_motion.py` as described above.
- **Verification command**: `uv run pytest tests/aprilcam/vision/test_motion.py tests/aprilcam/daemon/test_detection.py -q`
