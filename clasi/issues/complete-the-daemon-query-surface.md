---
status: pending
---

# Complete the daemon's query surface (features, mounts, guides)

## Description

Three capabilities are **declared on `Daemon` but raise
`NotImplementedError`**. Each is a tool the MCP server must expose, so
they land before it rather than forcing the wire layer to be built
twice.

### 1. `find_feature` — "where is the blue dot?" (UC-08)

Fuzzy match against the playfield definition's surveyed features by
name, colour, kind, or cardinal: `"the blue dot"`, `"blue dot west"`,
and the exact slug all reach `dot-west-blue`. Answered from the
**surveyed definition, not from vision** — no camera, no calibration
required. That is a real limitation to state rather than hide: if
someone physically removed the blue dot, this still reports where it is
supposed to be.

**Ambiguity returns every match, never an arbitrary pick** (UC-08.S-2):
"the orange dot" matches four features on `main-playfield` and all four
come back. No match returns empty (UC-08.S-3) — asking for "the orange
triangle" must not return the nearest orange thing as though it were a
triangle. `FeatureQuery.find` in `playfield/interfaces.py` already
specifies this; `Daemon.find_feature` is its RPC face.

CLI: `aprilcam playfield where <query>`.

### 2. The mobile-tag registry (UC-12)

`register_tag`, `unregister_tag`, `list_tag_parameters`. This is the
difference between reporting where a **tag** is and where the **robot**
is — currently the system only does the former, which is the weaker
half of the core job (JTBD-01).

`MountParameters` (already in `types.py`) carries `size_cm`,
`mount_x`/`mount_y`/`mount_z`, and `mount_yaw_rad`. Applied per frame:
project the tag from its mount height down onto the field plane
(parallax), then apply the X/Y offset and yaw so reported heading is
the *robot's*. Calibration stays robot-agnostic — these are runtime
registrations, in-memory only, **not** calibration data.

**The session-start contract matters** (resolved 2026-07-31): mounts do
not survive a restart, so a client expecting its tag to have a height or
orientation registers it when it connects, every session. That must be
in the packaged guide (below) or robots will silently get tag poses
instead of robot poses after every daemon restart.

Open, still: the robot's "reference point" needs a stated convention —
geometric centre, drive-axle centre, or client-defined. Offsets are
meaningless without it. Decide it in this sprint.

CLI: something under `aprilcam tags mount` or `camera mount` — pick a
spelling and record it.

### 3. `get_guide` — the system carries its own instructions (UC-27)

An MCP agent works entirely through tool calls and **cannot read
repository files**. Everything it needs — that world coordinates are cm
with +x east and +y north, that headings are 0 = east CCW positive,
that a tag id needs a family, that calibration precedes world
coordinates, that mounts and annotations are re-registered per session
— is knowledge the system has and the agent does not. Shipping it *with*
the software is what stops it drifting from a document nobody updated.

`Guides` in `client/interfaces.py` is the packaged accessor that works
with **no daemon running** (UC-27.S-1 — an agent's first question may be
"what is this and how do I use it?"). `Daemon.get_guide` serves the
same texts over the wire.

Open (UC-27): whether the guide is partly **generated** — the actual
tool list, this daemon's real camera names, the playfields it holds — 
rather than purely static. A guide naming the real cameras is far more
useful, but only a daemon can produce it, which conflicts with the
no-daemon-needed property. Resolve deliberately.

## Cause

Sprints 020–024 built the perception and configuration core. These
three were declared in the interface skeletons during the design work
and verified sufficient by the sufficiency traces, but no sprint has
claimed them yet.

## Verification

- `aprilcam playfield where "the orange dot"` returns all four matches
  on `main-playfield`, with positions, and no camera is opened.
- Register a mount for a tag, and the reported position moves to the
  robot's reference point rather than the tag's; unregister, and it
  reverts.
- Fetch the guide with **no daemon running**, and it contains the world
  frame, tag addressing, bring-up order, and the register-per-session
  rule.

## Related

- `src/aprilcam/daemon/interfaces.py` — the three declared surfaces
- `src/aprilcam/playfield/interfaces.py` `FeatureQuery`,
  `src/aprilcam/client/interfaces.py` `Guides`, `types.py`
  `MountParameters`
- `docs/design/use-cases/daemon.md` UC-08, UC-12, UC-27
- Sprint 025 in `docs/design/development-plan.md`'s sequence
