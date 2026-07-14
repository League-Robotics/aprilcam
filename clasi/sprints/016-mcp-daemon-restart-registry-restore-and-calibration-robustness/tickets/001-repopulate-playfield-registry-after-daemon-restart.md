---
id: '001'
title: Repopulate playfield registry after daemon restart
status: open
use-cases: [SUC-001]
depends-on: []
github-issue: ''
issue: mcp-playfield-registry-not-restored-after-daemon-restart.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Repopulate playfield registry after daemon restart

## Description

`_on_daemon_connect()` (`src/aprilcam/server/mcp_server.py:312`) is the only
populator of the module-level `playfield_def_registry`, via the daemon's
`ListPlayfields` RPC. `_ensure_daemon_client()`
(`src/aprilcam/server/mcp_server.py:349-360`) fires it only when the
module-level `_daemon_client` is `None` — i.e. on the very first
connection. When `aprilcamd` dies and restarts while the long-lived `aprilcam
mcp` process keeps running, the persistent gRPC channel object stays
non-`None` and transparently reconnects at the transport layer, so the
populate hook never re-fires. `playfield_def_registry` stays permanently
empty after any daemon restart: `open_camera` stops auto-rehydrating,
`create_playfield` reports `"calibrated": false`, and
`calibrate_playfield`/`set_camera_playfield` fail with `"Playfield ... not
found in registry. Available playfields: [(none)]"` — even though live tag
detection and world-coordinate reporting recover on their own via the tag
stream socket, since on-disk state is fully intact and only the MCP
server's in-memory cache is stale.

This ticket adds a daemon-identity signal (`boot_id`) so the MCP server can
tell a genuine daemon restart apart from a transient channel blip, and
repopulates the registry when (and only when) the daemon has actually
restarted — plus a belt-and-suspenders fallback for the case where the
identity signal isn't available (mixed-version deployments).

See `architecture-update.md` §1 ("Daemon-restart detection: `boot_id` +
reconnect-triggered repopulate") for full design rationale, including why
`boot_id` is piggybacked on the existing `ListPlayfields` RPC rather than a
new RPC, and why the check runs unconditionally per call rather than on a
timer.

## Acceptance Criteria

- [ ] `proto/aprilcam.proto`: `ListPlayfieldsResponse` gains a `string
      boot_id = N;` field (additive; no new RPC; no existing field
      renumbered). Stubs regenerated (`src/aprilcam/proto/aprilcam_pb2*.py`).
- [ ] Daemon (`src/aprilcam/daemon/grpc_server.py` or the appropriate
      startup module) generates one `boot_id = uuid.uuid4().hex` per
      process at startup and stamps it onto every `ListPlayfieldsResponse`.
- [ ] `src/aprilcam/client/control.py`: `DaemonControl.list_playfields()`
      response carries `boot_id` through to the caller (pass-through field
      or +1 model field, whichever fits the existing wrapper pattern).
- [ ] `src/aprilcam/server/mcp_server.py`: new module-level
      `_daemon_boot_id: Optional[str] = None`.
- [ ] `_ensure_daemon_client()` calls a new `_maybe_repopulate_registry(client)`
      helper on every invocation (not only when `_daemon_client is None`).
      The helper calls `list_playfields()` (reusing the existing RPC, not
      an additional one), compares the returned `boot_id` to
      `_daemon_boot_id`, and:
      - On first connect or on a **changed** `boot_id`: runs the existing
        `_on_daemon_connect` body (clear + repopulate
        `playfield_def_registry`) and updates `_daemon_boot_id`.
      - On an **unchanged** `boot_id`: no-op — the registry is already
        correct. This is the mechanism that must NOT clobber valid,
        already-populated state on a transient connection blip.
- [ ] Empty-registry fallback: the `create_playfield`, `calibrate_playfield`,
      and `set_camera_playfield` handlers (all of which consult
      `playfield_def_registry`) call `_maybe_repopulate_registry(client)`
      once, at the point where they would otherwise fail because the
      registry is empty, before raising their existing error. This covers
      mixed-version deployments where the connected daemon doesn't yet
      stamp `boot_id` (old daemon → new MCP server), where the `boot_id`
      comparison alone can't detect the restart.
- [ ] `connect_daemon` tool (`mcp_server.py:1613`) continues to work
      unchanged — it already re-fires `_on_daemon_connect` explicitly on
      every manual reconnect; update it to also refresh `_daemon_boot_id`
      from the new connection so the two mechanisms stay consistent.
- [ ] Regression test simulating a daemon restart under a persistent MCP
      connection: a fake/stub `DaemonControl` (pattern from
      `tests/test_connect_daemon.py`'s `MagicMock`-based
      `_inject_fake_client` helper) whose `list_playfields()` return value
      changes `boot_id` between calls to simulate a restart. Assert:
      - Registry starts populated from the first `boot_id`.
      - A call with the same `boot_id` leaves the registry untouched
        (no spurious clear+repopulate — assert the repopulate body, e.g.
        `playfield_def_registry.clear`, is NOT invoked again).
      - A call with a **different** `boot_id` (simulated restart) clears
        and repopulates the registry with the new daemon's playfields.
      - After repopulation, `open_camera` rehydration,
        `create_playfield`, and `calibrate_playfield` succeed against the
        new registry contents without an MCP-server restart.
- [ ] Unit test for the empty-registry fallback: registry empty, daemon
      reachable and returns playfields on retry (e.g. old-daemon
      simulation with no `boot_id` change signal) → handler recovers
      instead of raising `"Available playfields: [(none)]"`.
- [ ] `uv run pytest` green; no regression in existing
      `tests/test_connect_daemon.py`, `tests/test_daemon_control.py`,
      `tests/test_015_003_mcp_tag_tools_daemon.py`.

## Testing

- **Existing tests to run**: `tests/test_connect_daemon.py`,
  `tests/test_daemon_control.py`, `tests/test_daemon_server.py`,
  `tests/test_daemon_protocol.py`, `tests/test_015_003_mcp_tag_tools_daemon.py`.
- **New tests to write**:
  - `boot_id`-change-triggers-repopulate / `boot_id`-unchanged-is-noop test
    (the core regression test for this ticket, described above under
    Acceptance Criteria).
  - Empty-registry-fallback-recovers test.
  - Daemon-side test that `boot_id` is stable across multiple
    `ListPlayfields` calls within one process lifetime and generated fresh
    per process (if a way to spin up two daemon instances in-process is
    practical; otherwise a unit test on the servicer's `boot_id` attribute
    is sufficient).
- **Verification command**: `uv run pytest`

## Implementation Plan

**Approach**: additive proto change + one new client-side comparison
function, wired into the single existing choke point
(`_ensure_daemon_client()`) that every tool handler already goes through.
No new RPC, no new module.

**Files to create/modify**:
- `proto/aprilcam.proto` — add `boot_id` field to `ListPlayfieldsResponse`.
- `src/aprilcam/proto/aprilcam_pb2.py`, `aprilcam_pb2_grpc.py` — regenerate
  (via the project's existing proto-compile script, e.g.
  `scripts/compile_proto.py` if present — check for the exact regen command
  used by prior sprints, e.g. Sprint 015 ticket 001).
- `src/aprilcam/daemon/grpc_server.py` (or daemon startup module) — generate
  and stamp `boot_id`.
- `src/aprilcam/client/control.py` — ensure `boot_id` flows through
  `list_playfields()`.
- `src/aprilcam/server/mcp_server.py` — `_daemon_boot_id` module global;
  `_maybe_repopulate_registry()`; wire into `_ensure_daemon_client()`;
  wire empty-registry fallback into `create_playfield`,
  `calibrate_playfield`, `set_camera_playfield` handlers; update
  `connect_daemon` to refresh `_daemon_boot_id`.
- `tests/test_daemon_restart_registry.py` (new) — the regression tests
  described above.

**Testing plan**: unit/regression tests only, no live hardware or live
daemon required (fake gRPC client pattern, consistent with the existing
`test_connect_daemon.py` suite).

**Documentation updates**: none required beyond the architecture doc
already written; if `ROBOT_API_GUIDE.md` documents daemon-restart behavior
anywhere, a one-line note may be added but is not required for acceptance.
