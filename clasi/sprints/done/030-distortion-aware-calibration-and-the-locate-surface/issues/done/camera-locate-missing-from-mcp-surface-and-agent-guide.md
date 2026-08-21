---
status: done
sprint: '030'
tickets:
- 030-004
---

# `camera locate` is invisible to MCP agents, so `mount_z` silently does nothing

## Description

Sprint 029 added `aprilcam camera locate` and made it a precondition for
parallax correction: a registered `mount_z` only moves a reported
position if the *observing camera* has a known `camera_position`
(`nadir` + `height_cm`). That precondition is reachable and documented
from exactly one direction — the CLI and the `robot` guide — and is
absent from both the MCP tool surface and the `agent` guide, which are
the only two things an MCP-only agent ever sees.

The result is a silent wrong answer, not an error:

1. `register_tag` rejects a nonzero `mount_z` only when the daemon holds
   **no calibration for any camera at all**
   (`daemon/service.py:1234`). A calibrated-but-never-located camera
   passes that check.
2. `_camera_geometry_for` falls back to nadir `(0, 0)`, height `0.0`
   when `config.camera_position is None`
   (`daemon/service.py:1311-1313`).
3. `project_tag_to_field_plane` short-circuits to the identity when
   `camera_height_cm <= 0.0` (`calibration/geometry.py:281`).

So the agent registers a mount, gets no error, and every subsequent
`get_tag`/`get_tags` reports the **raw tag's** position — exactly as if
`mount_z` were zero — while `mount_x`/`mount_y`/`mount_yaw_rad` continue
to apply normally. The failure is a plausible-looking number offset by
the tag's elevation, which will not announce itself.

Where the documentation stands:

- `ROBOT_API_GUIDE.md:98-104` states the constraint correctly and names
  `camera locate` as the fix.
- `AGENT_GUIDE.md`'s §7 command table (lines 129-148) lists eight
  `camera` subcommands and **never mentions `locate`**. §6, the section
  devoted to the mount-registration trap, covers only the
  per-session-restart rule, not the located-camera precondition.
- There is no `locate` MCP tool. The server exposes 27 tools; none
  solves camera geometry. An agent can *write* a `camera_position` via
  `set_camera_config`, but the whole point of `locate` is that nobody
  tape-measures those numbers — they are solved from surveyed elevated
  references.

The state is technically discoverable — `get_camera_config` round-trips
`camera_position` (`daemon/rpc.py:505`) — but nothing prompts an agent
to look, and neither `list_tag_parameters` nor `get_calibration` reports
whether the observing camera is located.

## Proposal

Smallest fix that closes the trap, in priority order:

1. **`AGENT_GUIDE.md`** — add `camera locate` to the §7 table, and add
   the precondition to §6 (the mount section) where an agent about to
   register a mount will actually read it: a nonzero `mount_z` needs
   both a calibrated *and* a located camera, and is inert without the
   second.
2. **Make the inert state observable in the perception path** — e.g.
   have `list_tag_parameters` (or a field on the tag record) indicate
   that a registered `mount_z` is not being applied because the
   observing camera has no `camera_position`. Silence is the actual
   defect here; a flag an agent can check is worth more than prose.
3. **Consider tightening `register_tag`** — extend the existing
   `NotCalibrated` guard so a nonzero `mount_z` also warns or errors
   when no located camera exists, rather than only when no calibration
   exists anywhere. Needs thought: a mobile tag is not bound to one
   camera, so the "any camera" reading that applies to calibration
   applies here too, and hard-failing may be wrong.
4. **Optionally, a `locate` MCP tool.** Locating is arguably an operator
   step done once per physical camera move, so CLI-only may be the right
   call — but if so, the `agent` guide should say that explicitly
   ("ask your operator to run this") rather than omitting it.

## Motivation

Found while writing onboarding instructions for other agents (Aug 2026):
the `agent` guide is the one document an MCP client is told to read
first, and following it end to end leads an agent straight into
registering a `mount_z` that does nothing. The v1 archive's parallax code
had the same class of problem — geometry silently defaulting instead of
refusing — which is precisely what sprint 029 set out to replace.

Related: [aprilcam-config-command-to-report-config-vars-and-file-locations.md](aprilcam-config-command-to-report-config-vars-and-file-locations.md)
— a camera's located/unlocated state is exactly the kind of resolved
configuration that issue wants surfaced.
