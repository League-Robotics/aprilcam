---
status: draft
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 016 Use Cases

## SUC-001: Playfield admin surface survives a daemon restart

Parent: UC (playfield calibration / admin workflow — see overview.md
"Playfield (Virtual Camera)")

- **Actor**: AI agent (MCP client) operating a long-lived MCP server session
  against a camera/playfield that has already been calibrated in a prior
  session.
- **Preconditions**:
  - The MCP server (`aprilcam mcp`) has been running and has already
    connected to `aprilcamd` at least once (module-level `_daemon_client`
    is non-`None`).
  - The camera has a valid `config.json` (linked playfield) and
    `calibration.json` on disk, owned by the daemon's data dir.
  - The `aprilcamd` daemon process dies and is restarted (by systemd,
    manually, or after a crash) while the MCP server process keeps running.
- **Main Flow**:
  1. The daemon restarts. Its gRPC server comes back up; the MCP server's
     persistent gRPC channel transparently reconnects (or the MCP server
     detects the channel is no longer good and reconnects).
  2. The MCP server detects that the daemon it is now talking to is not the
     same daemon instance it originally connected to (or detects that its
     playfield registry is stale relative to a reachable daemon).
  3. The MCP server re-runs the daemon-connect populate step — equivalent to
     `_on_daemon_connect()` — re-fetching `ListPlayfields` and repopulating
     `playfield_def_registry`, without requiring the agent or operator to
     restart the MCP server.
  4. The agent calls `open_camera(pattern=...)`. It rehydrates and returns
     `playfield_id` / `playfield_name` as it would have before the restart.
  5. The agent calls `create_playfield(camera_id)` and gets
     `{"calibrated": true}` (not a false regression caused solely by the
     restart).
  6. The agent calls `calibrate_playfield(...)` / `set_camera_playfield(...)`
     and both succeed against the repopulated registry.
- **Postconditions**: The playfield/calibration admin surface (rehydrate,
  `create_playfield`, `calibrate_playfield`, `set_camera_playfield`) behaves
  identically before and after a daemon restart, with no MCP server restart
  required. Live tag detection and world-coordinate reporting (which already
  recovers on its own via the tag stream socket) is unaffected either way.
- **Acceptance Criteria**:
  - [ ] After a simulated daemon restart, `open_camera` on an
        already-configured camera returns `playfield_id`/`playfield_name`
        without any MCP-server restart.
  - [ ] After a simulated daemon restart, `create_playfield` on that camera
        returns `"calibrated": true`.
  - [ ] After a simulated daemon restart, `calibrate_playfield` /
        `set_camera_playfield` do not raise "not found in registry" /
        "Available playfields: [(none)]" errors.
  - [ ] A transient RPC blip (e.g. one failed call that succeeds on retry)
        does NOT clobber a still-valid, already-populated
        `playfield_def_registry` — repopulation is triggered by an actual
        daemon-identity change or an empty-but-daemon-reachable registry,
        not by every reconnect event.
  - [ ] A regression test exercises this end-to-end against a fake/stub
        daemon that is restarted mid-test under a persistent MCP-server
        connection object.

## SUC-002: Operator gets a signal when the tag camera goes dark

Parent: UC (tag detection reliability — see overview.md "Tag Detection &
Tracking")

- **Actor**: Robotics developer / operator running a field session; AI agent
  consuming `get_tags`.
- **Preconditions**: A tag camera (e.g. the OV9782) is running with a short,
  fixed exposure (`exposure-time-abs: 6`, `gain: 2`) tuned for a
  well-lit field. Ambient lighting drops (e.g. field lights dim/flicker).
- **Main Flow**:
  1. The daemon continues capturing valid (non-`None`) frames from the
     camera, but the frames are too dark for AprilTag/ArUco detection —
     `get_tags` silently returns zero or near-zero tags.
  2. The daemon's pipeline recognizes a sustained darkness/zero-detection
     condition (distinct from a `cap.read()` failure, which is already
     handled by the existing reopen/self-heal path) and surfaces it as a
     signal — e.g. an `info.json` field, a log line, or an MCP-visible
     status — rather than the condition being invisible.
  3. An operator or agent observing the signal understands the camera is
     "blind due to lighting," not that detection or calibration itself is
     broken.
- **Postconditions**: A prolonged zero-detection/dark-frame condition is
  observable through existing status surfaces (`info.json`, logs, or an MCP
  tool) instead of manifesting only as silently-empty `get_tags` results.
- **Acceptance Criteria**:
  - [ ] The daemon computes a lightweight per-frame or windowed darkness /
        zero-detection signal for camera pipelines (no new heavy CV
        dependency).
  - [ ] The signal is surfaced somewhere already queryable (`info.json` field
        and/or a log warning at a bounded rate) without blocking or gating
        any existing operation.
  - [ ] The mitigation is scoped to detection/surfacing only in this sprint;
        automatic exposure/gain remediation is explicitly out of scope and
        recorded as a follow-up.

## SUC-003: Calibration-stale flag reflects a real, understood condition

Parent: UC (playfield calibration workflow — see overview.md "Playfield
(Virtual Camera)")

- **Actor**: Robotics developer / operator investigating why
  `calibration_stale: true` appeared in `info.json`; AI agent reading
  `open_camera`'s `calibration_stale` field.
- **Preconditions**: A camera has a valid calibration; live static-marker
  detection reports a small pixel-position shift (e.g. ~3px) relative to the
  stored reference during normal operation (e.g. around a daemon restart).
- **Main Flow**:
  1. The daemon's live-boundary geometric check
     (`core/playfield.py` `_update_static`, threshold in
     `_effective_movement_threshold()`) compares live static-marker pixel
     positions to stored reference positions.
  2. The sprint confirms, with evidence (logs/test), which of the two
     staleness mechanisms actually fired for the reported case, and whether
     the default 25px threshold is appropriate or needs adjustment — or
     whether the flag is failing to reset once set.
  3. The `calibration_stale` flag, once resolved (e.g. after the marker
     settles back within threshold, or after recalibration), behaves
     predictably rather than persisting indefinitely from a past transient
     excursion.
- **Postconditions**: The stale-flag mechanism's behavior is documented and,
  if a defect is found (e.g. a reset gap or a too-tight default), addressed
  or explicitly deferred with rationale. The flag remains advisory
  (non-blocking) unless the sprint explicitly decides otherwise.
- **Acceptance Criteria**:
  - [ ] The sprint's investigation identifies, with code citations, exactly
        which staleness path(s) can produce `true` for a small (~3px) marker
        shift under the current default configuration.
  - [ ] If the flag is found to never reset once set (a latent defect
        independent of threshold tuning), that is fixed or explicitly
        ticketed.
  - [ ] Any threshold change is justified by evidence, not applied
        speculatively.
  - [ ] The flag continues to be purely advisory (no new blocking behavior)
        unless a ticket explicitly proposes and justifies gating.
