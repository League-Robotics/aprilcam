---
status: in-progress
sprint: '036'
tickets:
- 036-002
- 036-003
---

# Tag history can span hours, but the MCP surface promises "~10 seconds" — and stationary tags report ~1 cm/s speed

## What happened

2026-08-23: an agent consumer working over MCP reported that detection is
no longer a continuous ~4 Hz stream. Consecutive ring-buffer frame indices
340→341→342 spanned **94 minutes**, then **41 seconds**. It also observed
frame 340 reporting **0.97 cm/s `speed` on a physically stationary tag**.
Its conclusion: frames are produced on demand, interactive use is fine,
but anything computing motion from consecutive history frames must mind
the gaps.

## Investigation: the gaps are by design, the surprise is not

Demand-driven detection is documented, intentional architecture — not a
regression:

- `src/aprilcam/daemon/detection.py` (module docstring): sprint 021
  Architecture Decision 1 — `run_burst` runs once per `get_tags`/`get_tag`
  call, synchronously; "an idle daemon runs zero detections by
  construction."
- Sprint 027 ticket 003 added `daemon.streaming.TagStreamProducer`, which
  drives bursts at a steady wall-clock cadence **only while at least one
  tag-stream subscriber exists** for that camera. With no subscriber, the
  buffer grows only on demand — "intentional, not a regression."

So a camera queried occasionally over MCP has a 300-frame buffer spanning
hours, exactly as observed. The problem is that **the agent-facing surface
never says so**:

1. The `get_tag_history` MCP tool docstring
   (`src/aprilcam/mcp/tools/perception.py`, ~line 179) promises "Recent
   per-frame tag history from the daemon's ring buffer (UC-26, roughly
   the last 10 seconds)" — unconditionally. That claim is only true while
   a stream subscriber is active.
2. `src/aprilcam/AGENT_GUIDE.md` says nothing about detection cadence,
   ring-buffer semantics, wall-clock gaps, or how to interpret
   `speed`/`heading`/`velocity`. An agent reading everything it is given
   has no way to know history frames may be hours apart — except by
   noticing the per-frame `timestamp` on its own (the data *is* there:
   `TagFrame.timestamp` and `frame_index` are populated, so consumers can
   mind the gaps once they know they must).

## The second finding: the speed noise floor scales with 1/dt

`vision/motion.py` computes motion from the two most recent samples using
their **real timestamps** — so the 94-minute gap itself is harmless
(jitter/5640 s ≈ 0.0001 cm/s). The 0.97 cm/s reading comes from the other
end of the scale: two bursts close together in time.

`compute_motion` suppresses `heading_rad`/`world_velocity` below
`MIN_RELIABLE_DISPLACEMENT_CM` (0.5 cm) but **always reports `speed` =
displacement/dt**. The module docstring claims speed is "bounded near
zero for jitter" — but that bound is jitter/dt, which grows without bound
as dt shrinks. Two back-to-back MCP calls ~0.25 s apart turn the measured
~0.25 cm detector jitter into ~1 cm/s of confident-looking speed on a tag
that has not moved. 0.97 cm/s × ~0.25 s ≈ 0.24 cm — exactly the known
jitter magnitude, which is almost certainly what the agent saw.

The ticket 022-007 fix drew the magnitude/direction line assuming "a small
speed value honestly means roughly stationary" — that argument silently
assumed dt on the order of a second or more. At streaming cadence or with
rapid interactive calls, jitter speed is not small.

## Proposed fix

Two independent pieces:

1. **Tell consumers the truth about cadence** (documentation, low risk):
   - Fix the `get_tag_history` tool docstring: the buffer holds the last
     300 *bursts*, which map to ~10 wall-clock seconds only while a tag
     stream is active; otherwise bursts happen per query and frames may be
     arbitrarily far apart. Point at the per-frame `timestamp` as the
     authority — never assume even spacing.
   - Add a short section to `AGENT_GUIDE.md` (and `ROBOT_API_GUIDE.md` if
     it makes the same implicit promise) covering burst-on-demand
     detection, history semantics, and motion-field interpretation
     (`speed` present with `heading`/`velocity` None means
     below-noise-floor displacement).

2. **Stop reporting jitter-dominated speed as signal** (design decision
   needed): when displacement is below `MIN_RELIABLE_DISPLACEMENT_CM`,
   `speed` is noise/dt, not a measurement. Options, roughly in order of
   invasiveness:
   - Suppress `speed` to `None` alongside heading/velocity below the
     displacement threshold (revisits ticket 022-007's argument with the
     small-dt counterexample above).
   - Report speed but cap/floor its interpretation by exposing the
     underlying `displacement_cm` and/or `dt` on the record so consumers
     can judge.
   - Only report speed when displacement clears the threshold **or** dt is
     large enough that jitter/dt is below some honest ceiling.

## Related

- `src/aprilcam/daemon/detection.py` — module docstring documenting
  demand-driven bursts (sprint 021 Decision 1, sprint 027 ticket 003)
- `src/aprilcam/vision/motion.py` — `compute_motion`,
  `MIN_RELIABLE_DISPLACEMENT_CM`, ticket 022-007's magnitude/direction
  reasoning
- `src/aprilcam/mcp/tools/perception.py` — `get_tag_history` docstring
  with the unconditional "~10 seconds" claim
- `src/aprilcam/AGENT_GUIDE.md` — no cadence/history/motion guidance
