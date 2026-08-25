---
status: done
sprint: '036'
tickets:
- '007'
- 008
---

# Mount registrations (tag pose relative to robot, parallax correction) should persist across daemon restarts

## The problem

Tag mount registrations — `aprilcam tags mount register` / the
`register_tag` MCP tool, carrying `mount_x`/`mount_y`/`mount_yaw_rad`
(the tag's pose relative to the robot's reference point) and `mount_z`
(height above the field, driving the parallax correction) — live only in
daemon memory: `DaemonService._mount_params` is a plain dict
(`src/aprilcam/daemon/service.py`, ~line 401). Every daemon restart
silently discards them.

The failure mode is the worst kind: after a restart, queries succeed and
return the *raw tag's* pose instead of the *robot's* pose — a
plausible-looking number that is wrong, with no error and no warning.
`AGENT_GUIDE.md` §6 currently calls this out as "the rule most likely to
bite you" and pushes the burden onto every consumer to re-register at the
start of every session.

A mount registration describes physical reality — where the tag is bolted
to the robot. That does not change when the daemon restarts. It should be
persistent configuration, not per-session state.

## Proposed fix

Persist mount registrations in the daemon's on-disk configuration
directory (`src/aprilcam/config/directories.py` is the existing home for
daemon-side persisted state), alongside the playfield/camera config
documents that already survive restarts:

- `register_tag` writes through to disk; `unregister_tag` removes the
  entry; the daemon loads all persisted registrations at startup.
- Keep the existing precondition behavior on load: a nonzero `mount_z`
  restored into a daemon where the observing camera is calibrated but not
  located should behave exactly as a fresh registration does today
  (registration stands, height correction inert, `mount_z_applied`
  reports the truth).
- Update `AGENT_GUIDE.md` §6, which currently documents the
  per-session limitation prominently — and decide whether `list_tag_parameters`
  should distinguish "persisted" from anything else (probably not needed
  if everything persists).

## Open questions

- Should there be an explicit ephemeral option (`--session-only`) for
  temporary experiments, or is unregister good enough?
- Annotations are the other per-session state called out in
  `AGENT_GUIDE.md` §6. This issue deliberately covers **mounts only**;
  annotation persistence, if wanted, is a separate issue.

## Related

- `src/aprilcam/daemon/service.py` — `_mount_params` dict,
  `register_tag`/`unregister_tag`/`list_tag_parameters` (ticket 025-004,
  UC-12)
- `src/aprilcam/AGENT_GUIDE.md` §6 — "Mounts and Annotations Are
  Per-Session — Re-register Every Time"
- `src/aprilcam/calibration/geometry.py` — `correct_tag_pose`, the
  consumer of `MountParameters`
