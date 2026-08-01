---
status: pending
---

# Tag detection and tag queries

## Description

The second v2 vertical slice, per `docs/design/development-plan.md`'s
sprint sequence (2026-07-31): make the daemon **see tags**, and expose
that from the shell. Stages 4–5 of the plan (image pipeline, then
AprilTag + ArUco detection) plus the detection slice of stage 8
(demand-driven scheduling and the ring buffer).

**Daemon:**

- The frame → detection-ready image pipeline (`vision/`, stateless
  frame-level vision consumed by the daemon).
- AprilTag 36h11 and ArUco 4x4 detection, producing `TagRecord`s with
  the boundary/hat contract already designed in `types.py`: four
  corners ordered front-left, front-right, back-right, back-left, plus
  `yaw_rad` (tag facing, distinct from `heading_rad` which is motion).
- `Daemon.get_tags(camera)`, `get_tag(camera, tag)`, and
  `get_tag_history(camera, tag, frames)` — the bounded ring buffer
  (~10s), with gaps left explicit rather than interpolated (UC-26).
- Velocity, speed, and heading computation across frames.
- **Demand-driven scheduling**: no detection runs when nobody is
  asking. A one-shot query is exactly one detection burst; an idle
  daemon does no detection at all. The development plan treats this as
  a requirement with CPU-budget tests, not an optimization.
- Frames embed their `TagFrame` when detection is running
  (`ImageFrame.tags`), so one fetched frame carries everything needed
  to draw.

**Uncalibrated is the honest state this sprint.** Calibration, deskew,
and playfield definitions arrive in the *next* slice, so every camera
here is uncalibrated by construction. That is expressible by design and
must be reported as such, never faked: `TagRecord.world` is `None`,
`calibrated` is `False`, and `pixel`/`pixel_corners` are the only
representation available (UC-09, UC-10). A pixel position presented as
though it were world coordinates is the exact failure mode the
world-first contract exists to prevent.

**CLI:** a tag-listing command over the above. Note the dependency
problem to resolve deliberately rather than by accident: the command
surface in `docs/design/use-cases/cli.md` names `aprilcam playfield
tags`, but playfields do not exist until the next sprint, and the
daemon API is camera-addressed always (UC-06's resolved rule). Decide
and record whether this sprint ships `camera tags <camera>`, or
`playfield tags --camera X`, or defers the playfield-addressed spelling
— and update cli.md to match whatever is chosen.

**Acceptance:** UC-09 (one tag by family and id, including the
not-detected-vs-detected-at-origin distinction), UC-10 (all tags, no
pagination, empty field returns an empty frame), UC-26 (tag history
with explicit gaps), and the detection-efficiency budget from stage 8.

## Cause

Sprint 020 delivered a daemon that owns cameras and serves raw frames,
but it does no vision at all — the daemon is the sole vision authority
by design and currently exercises none of it. Everything downstream
(calibration, deskew, world coordinates, annotations) sits on top of
detection.

## Proposed fix

Implement against the interface skeletons, which the sufficiency traces
verified cover these use cases: `Daemon.get_tags`/`get_tag`/
`get_tag_history` in `src/aprilcam/daemon/interfaces.py`, and the
`TagRecord`/`TagFrame`/`TagId`/`TagFamily` types in
`src/aprilcam/types.py`. `src/aprilcam/vision/` is currently empty and
is this sprint's new subsystem.

Mine `archive/src/aprilcam/` for how v1 solved detection — the
preprocessing ladder, the ArUco/AprilTag detector setup, the
`-(N+1)` ArUco id encoding that must **never** leave the daemon — per
the archive rule in `CLAUDE.md`: take the approach and the earned
constants, not the structure.

Geometry conventions are sacrosanct (`docs/design/design.md` §6): tag
yaw and heading are both `0 = east, CCW positive`; corner order is
front-first; ArUco N is internally `-(N+1)` and no client ever sees a
negative tag id.

## Verification

- Detect the real AprilTag and ArUco markers on `main-playfield` with
  the attached cameras; ids match what is physically on the field.
- Uncalibrated records report `world=None`, `calibrated=False`, and
  usable pixel corners — and the CLI says so rather than printing
  zeros.
- Tag history returns explicit gaps for a tag that left the frame.
- An idle daemon with no subscribers and no queries runs zero
  detections over 10s.

## Related

- `docs/design/development-plan.md` stages 4–5, and stage 8's
  efficiency budget
- `docs/design/use-cases/daemon.md` UC-09, UC-10, UC-26
- `docs/design/use-cases/daemon-interface-trace.md` — UC-09/UC-10 were
  the cleanest traces in the set; the interfaces are known sufficient
- Sprint 020 — delivered the camera ownership this builds on
