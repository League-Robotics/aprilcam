---
status: done
sprint: '016'
tickets:
- 016-001
- 016-002
- 016-003
---

# MCP server playfield registry not repopulated after aprilcamd daemon restart

## Summary

When the `aprilcamd` daemon process dies and restarts while the
long-lived `aprilcam mcp` server keeps running, the MCP server's
`playfield_def_registry` is never repopulated. Live detection recovers
on its own (the tag stream socket reconnects and world coordinates keep
flowing), but the entire playfield/calibration **admin** surface stays
broken until the MCP server itself is restarted.

## Reported by

Another agent during a field session (aprilcam 0.20260620.7, macOS).
Camera `arducam-ov9782-usb-camera` (Arducam OV9782 USB, local index 3),
1280×800, linked to playfield `main-playfield` (134.3×89.3 cm). Robot
tracked via mobile tag id 100.

Two processes were involved: the MCP server (`aprilcam mcp`, ran
continuously the whole time) and the `aprilcamd` daemon (died and was
restarted mid-session when field lights dropped and the short-exposure
camera went near-black).

## Broken symptoms after daemon restart (MCP server unchanged)

- `open_camera(pattern="arducam")` → returns `camera_id` but **no**
  `playfield_id` / `playfield_name` (no auto-rehydrate; previously it did).
- `create_playfield(camera_id)` → `{"calibrated": false}` (was `true`).
- `calibrate_playfield(playfield_id, camera_height_cm=127)` → error:
  `Playfield 'main-playfield' not found in registry. Available
  playfields: [(none)]`.
- **But** `get_tags` still returns populated `world_xy` **and**
  `orientation_yaw` (e.g. tag 100 at world (-4.6, 0.3) cm, yaw -164.8°).
  Live detection with world coordinates is unaffected — only the
  calibration/registry admin path is broken.

## On-disk state is fully intact

Nothing is missing on disk; the daemon cwd is the correct data dir:

- `data/aprilcam/cameras/arducam-ov9782-usb-camera/calibration.json` —
  homography present, `rms_error: 0.133`, `calibrated_playfield:
  "main-playfield"`, static markers with world coords.
- `.../config.json` — `playfield: "main-playfield"`, resolution
  `[1280,800]`, `camera_position.height: 127`.
- `.../info.json` — `"calibration_stale": true` (written during restart;
  corner markers shifted ~3 px between stored and freshly-detected
  positions — homography itself still good).
- `data/aprilcam/playfields/main-playfield.json` — present (3767 bytes).

## Root cause (confirmed in code, master @ `src/aprilcam/server/mcp_server.py`)

- `_on_daemon_connect(client)` (mcp_server.py:312) is the **only** thing
  that populates `playfield_def_registry`: it clears it and repopulates
  via the daemon's `ListPlayfields` RPC. It replaced an old
  `load_all(local_dir)` disk scan.
- `_ensure_daemon_client()` (mcp_server.py:349-360) calls
  `_on_daemon_connect()` **only when `_daemon_client is None`** — i.e.
  on the first connection.
- When `aprilcamd` dies and restarts, the module-level `_daemon_client`
  (a persistent gRPC channel) is still non-None and transparently
  reconnects to the new daemon. So `_ensure_daemon_client()` returns
  early and **never re-fires** `_on_daemon_connect()`.
  `playfield_def_registry` stays empty.
- The `[(none)]` message is raised at `calibration/calibration.py:1414`
  when `playfield_def_registry.list()` is empty.
- The manual `connect_daemon` tool (mcp_server.py:1613) **does** re-fire
  the hook (line 1707), but nothing fires it automatically on daemon
  death. The daemon-restart path has no equivalent.

## Expected

After a daemon restart, the MCP server should re-populate its playfield
definition registry (and the camera→playfield rehydrate state) so that
`open_camera` rehydrates and `calibrate_playfield` /
`set_camera_playfield` / `create_playfield` work again — consistent with
live detection, which recovers on its own.

## Fix direction (for planning, not prescriptive)

Detect that the underlying daemon has changed/restarted and re-run
`_on_daemon_connect()` — e.g. re-fire the hook when an RPC round-trip
reveals a new daemon instance, on a detected channel reconnect, or
lazily when the registry is found empty but the daemon is reachable.
Avoid clobbering valid session state on transient blips.

## Impact

Any `aprilcamd` restart while the MCP server persists leaves the entire
playfield/calibration admin surface (auto-rehydrate, `create_playfield`
calibrated flag, `calibrate_playfield`, `set_camera_playfield`) broken
until the MCP server is restarted — even though detection and world
coordinates are fine.

## Secondary items (IN SCOPE for this sprint per stakeholder)

Stakeholder confirmed all three items belong in the same sprint. The
planner should scope items 2 and 3 (they need investigation — the fix
shape is not yet known):

1. **Registry restore (primary).** Repopulate `playfield_def_registry`
   and rehydrate state after a daemon restart. Root cause and fix
   direction above.
2. **Exposure fragility.** The very short exposure
   (`exposure-time-abs: 6`, `gain: 2`) makes the whole pipeline fragile
   to lighting dips — a small dim blanks detection entirely. Needs
   investigation: is the mitigation a config change, a detection-side
   robustness improvement, or an operator-facing warning? Likely a
   larger investigation than item 1.
3. **Stale threshold tuning.** The `calibration_stale: true` flag got
   set from only a ~3 px corner shift, which may be stricter than
   intended. Note: the ~3 px corner-shift staleness is the daemon's
   live-boundary flag (`camera_pipeline.py:_maybe_update_stale_flag`
   reading `boundary.calibration_stale`), **distinct** from the
   slug/dimension staleness in
   `load_calibration_from_camera_dir` (calibration.py:467-504). The
   planner should confirm which threshold fired and whether it warrants
   loosening.
