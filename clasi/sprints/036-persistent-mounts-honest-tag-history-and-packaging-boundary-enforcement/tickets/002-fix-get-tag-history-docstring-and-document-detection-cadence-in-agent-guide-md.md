---
id: '002'
title: Fix get_tag_history docstring and document detection cadence in AGENT_GUIDE.md
status: in-progress
use-cases:
- SUC-002
depends-on: []
github-issue: ''
issue: tag-history-gaps-and-jitter-speed-mislead-mcp-consumers.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Fix get_tag_history docstring and document detection cadence in AGENT_GUIDE.md

## Description

`get_tag_history`'s MCP tool docstring
(`src/aprilcam/mcp/tools/perception.py`, ~line 179) unconditionally
promises "roughly the last 10 seconds." That is only true while a
`stream_tags` subscriber is active on that camera (sprint 021
Architecture Decision 1: `run_burst` runs once per `get_tags`/`get_tag`
call, so an idle daemon runs zero detections — sprint 027 ticket 003's
`TagStreamProducer` only drives a steady cadence while at least one
subscriber exists). With no subscriber, the 300-frame ring buffer
grows only on demand, and consecutive frames can be minutes or hours
apart — confirmed live on 2026-08-23 (frames 340→341→342 spanning 94
minutes then 41 seconds).

`AGENT_GUIDE.md` currently says nothing about this — an agent reading
the guide has no way to know history frames may be far apart except by
independently noticing each frame's own `timestamp`. This ticket is
the low-risk, docs-only half of the issue (the other half,
`compute_motion`'s speed-suppression fix, is ticket 003 — depends on
this one only in the sense that it appends to the section this ticket
creates, not because the code depends on the docs).

`ROBOT_API_GUIDE.md` was checked during sprint planning and already
documents the cadence gap correctly (its "Reading Tags: Polling vs
Streaming" section, ~lines 118-141, already says "consecutive
`get_tag_history` frames can be *minutes or hours* apart" and "do not
compute velocity or heading from consecutive history frames unless a
stream subscriber is running") — **no change needed there for cadence**.
Leave the speed-semantics wording in both guides for ticket 003, which
implements the actual behavior change.

## Acceptance Criteria

- [ ] `get_tag_history`'s docstring in
      `src/aprilcam/mcp/tools/perception.py` no longer claims
      "roughly the last 10 seconds" unconditionally — it states the
      buffer holds the last 300 *bursts*, which map to ~10 wall-clock
      seconds only while a tag stream is active, and points at each
      frame's own `timestamp` as the authority otherwise.
- [ ] A new section in `src/aprilcam/AGENT_GUIDE.md` (suggested:
      insert as a new numbered section after current §6 "Mounts and
      Annotations...", renumbering the current §7 "Command /
      Capability Overview" to §8) covers: demand-driven detection
      (idle daemon = zero detections), the ring-buffer/cadence gap
      without an active stream subscriber, and points at `timestamp`/
      `frame_index` as authoritative — mirroring
      `ROBOT_API_GUIDE.md`'s existing, already-correct framing rather
      than inventing new prose from scratch.
  - [ ] Leave a clear insertion point (e.g. a short "Motion field
        semantics" placeholder heading, or simply end the new section
        where cadence coverage ends) for ticket 003 to append the
        speed/heading/`None` interpretation guidance once that
        behavior lands — do not write speed-semantics prose here that
        ticket 003 would have to contradict.
- [ ] `ROBOT_API_GUIDE.md` is read and confirmed already accurate on
      cadence (no edit needed there for this ticket).
- [ ] No behavior change — this ticket touches documentation and one
      docstring only.

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/mcp/ -q`
  (confirms the docstring edit didn't break tool registration/schema
  generation).
- **New tests to write**: none — docs-only change, no new behavior to
  cover.
- **Verification command**: `uv run pytest tests/aprilcam/mcp/ -q`
