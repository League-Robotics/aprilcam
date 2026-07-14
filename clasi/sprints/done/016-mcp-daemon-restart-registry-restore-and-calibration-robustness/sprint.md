---
id: '016'
title: MCP daemon-restart registry restore and calibration robustness
status: done
branch: sprint/016-mcp-daemon-restart-registry-restore-and-calibration-robustness
use-cases:
- SUC-001
- SUC-002
- SUC-003
issues:
- mcp-playfield-registry-not-restored-after-daemon-restart.md
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 016: MCP daemon-restart registry restore and calibration robustness

## Goals

Restore the playfield/calibration admin surface (`open_camera` rehydrate,
`create_playfield`, `calibrate_playfield`, `set_camera_playfield`) after an
`aprilcamd` daemon restart, without requiring an MCP-server restart and
without clobbering valid session state on a transient connection blip. Add
a non-blocking dark-frame/zero-detection advisory signal for the OV9782 tag
camera's exposure fragility. Fix a latent gap in the `calibration_stale`
flag so it reflects a real, resettable condition rather than persisting
from an earlier, unrelated excursion.

## Problem

`_on_daemon_connect()` (`server/mcp_server.py:312`) is the only populator of
`playfield_def_registry`, via the daemon's `ListPlayfields` RPC.
`_ensure_daemon_client()` (`server/mcp_server.py:349-360`) fires it only
when the module-level `_daemon_client` is `None` — i.e. on the very first
connection. When `aprilcamd` dies and restarts while the long-lived MCP
server process keeps running, the persistent gRPC channel object stays
non-`None` and transparently reconnects at the transport layer, so the
populate hook never re-fires. `playfield_def_registry` stays permanently
empty after any daemon restart: `open_camera` stops auto-rehydrating,
`create_playfield` reports `"calibrated": false`, and
`calibrate_playfield`/`set_camera_playfield` fail with "Playfield ... not
found in registry. Available playfields: [(none)]" — even though live tag
detection and world-coordinate reporting recover on their own via the tag
stream socket. On-disk state (`calibration.json`, `config.json`,
`playfields/*.json`) is fully intact; only the MCP server's in-memory cache
is stale.

Two secondary items, both confirmed in scope by the stakeholder and
investigated during planning:

- **Exposure fragility**: the OV9782 tag camera runs a very short fixed
  exposure (`exposure-time-abs: 6`, `gain: 2`) tuned for a well-lit field.
  A small lighting dip blanks detection entirely, and there is currently no
  signal anywhere in the daemon that would make this condition observable —
  confirmed by code search: no brightness/histogram check, no zero-detection
  counter, nothing beyond the existing `cap.read()` failure counter (which
  only catches a dead capture device, not a dim-but-valid frame).
- **Stale threshold**: `info.json`'s `calibration_stale: true` fired from a
  reported ~3px corner shift. Investigation found this is well under the
  default 25px `DEFAULT_MOVEMENT_THRESHOLD_PX`
  (`calibration/geometry.py:58`) used by the live-boundary check in
  `core/playfield.py`, so "the threshold is too strict" is not supported by
  the evidence. The same investigation found `_calibration_stale` is set on
  excursion but never reset back to `False` once markers return within
  threshold — a genuine, code-confirmed defect that better explains a
  stale-looking flag persisting from an earlier, larger transient (e.g.
  during the camera reopen that accompanies a daemon restart).

## Solution

1. **Registry restore (primary)**: add a `boot_id` field
   (`uuid4().hex`, stamped once at daemon process startup) to the existing
   `ListPlayfieldsResponse` proto message — no new RPC, since
   `_on_daemon_connect` already calls `list_playfields()` unconditionally.
   `_ensure_daemon_client()` changes from "populate only on first connect"
   to "always compare the returned `boot_id` against the last-seen value;
   repopulate `playfield_def_registry` only when it changes." This is the
   mechanism that avoids clobbering valid state on a transient blip: an
   unchanged `boot_id` means the daemon process itself never restarted, even
   if the TCP connection blipped. A second, independent safety net covers
   version-skew (an MCP server upgraded ahead of an older daemon that
   doesn't yet stamp `boot_id`): any handler that finds
   `playfield_def_registry` empty retries the repopulate once, at the point
   of failure, before raising its error.
2. **Dark-frame advisory signal**: a windowed mean-luminance sample (cheap
   NumPy, no new CV dependency) plus the detector's already-computed
   per-frame tag count, checked in `daemon/camera_pipeline.py` alongside the
   existing `consecutive_failures` tracking. A sustained low-luminance +
   zero-tag condition sets a new `frame_dark: bool` on `info.json`, reusing
   the existing write-on-change pattern from `_maybe_update_stale_flag()`.
   Purely advisory — nothing gates or refuses an operation based on it, and
   no MCP tool surface changes in this sprint (see architecture-update.md
   Open Questions). Automatic exposure/gain remediation is explicitly
   deferred to a follow-up issue once this signal produces real field data.
3. **Calibration_stale reset fix**: `core/playfield.py`'s marker-tracking
   update gains a symmetric `False`-transition — when all tracked static
   markers are back within threshold, `_calibration_stale` clears, mirroring
   the existing set path. No threshold value changes in this sprint; that
   would be speculative given the evidence gathered.

Full design detail, rationale, and diagrams are in `architecture-update.md`.

## Success Criteria

- SUC-001: after a simulated daemon restart, `open_camera` rehydrates
  `playfield_id`/`playfield_name`, `create_playfield` returns
  `"calibrated": true`, and `calibrate_playfield`/`set_camera_playfield`
  succeed — all without an MCP-server restart. A transient blip that
  doesn't change `boot_id` does not trigger an unnecessary repopulate. A
  regression test exercises this against a fake/stub daemon restarted
  mid-test under a persistent MCP-server connection object.
- SUC-002: the daemon computes and surfaces a dark-frame/zero-detection
  signal in `info.json` (and a rate-limited log warning) without blocking
  or gating any existing operation.
- SUC-003: the investigation identifies, with code citations, which
  staleness path(s) can produce `true` for a small (~3px) marker shift; the
  confirmed reset gap is fixed; any threshold change is evidence-justified,
  not speculative; the flag remains purely advisory.

## Scope

### In Scope

- `boot_id` field on `ListPlayfieldsResponse` (proto + regen), stamped by
  the daemon at startup.
- `server/mcp_server.py`: `_maybe_repopulate_registry` compare-and-repopulate
  logic in `_ensure_daemon_client()`; empty-registry fallback at the
  `create_playfield`/`calibrate_playfield`/`set_camera_playfield` call
  sites.
- Regression test simulating a daemon restart under a persistent MCP
  connection (fake gRPC client, pattern from `tests/test_connect_daemon.py`).
- `daemon/camera_pipeline.py`: windowed luminance + zero-tag-count check;
  `info.json` `frame_dark` field; rate-limited log warning.
- `core/playfield.py`: `_calibration_stale` `False`-transition reset; a
  regression test asserting `False → True → False` transitions.

### Out of Scope

- Exposure/gain remediation strategy (auto-exposure, adaptive gain,
  detection-side gamma/CLAHE compensation, operator-facing alerting beyond
  `info.json`/logs) — deferred to a follow-up issue once the dark-frame
  signal produces real field data.
- Any `calibration_stale` threshold *value* change — not supported by
  evidence gathered this sprint.
- Surfacing `frame_dark` on any MCP tool response (`open_camera` etc.) —
  `info.json` + logs are sufficient for this sprint; can be a fast-follow.
- gRPC channel-state subscription (`channel_ready_future`/`subscribe`) as a
  daemon-restart detection mechanism — `boot_id` is the chosen mechanism;
  channel-state subscription is a plausible defense-in-depth addition, not
  committed here.
- A dedicated `GetDaemonInfo`/`Ping` RPC for daemon identity — `boot_id`
  piggybacks on the existing `ListPlayfields` call this sprint.

## Test Strategy

- Fake-daemon regression test for SUC-001: a `DaemonControl`-compatible
  stub (`MagicMock`-based, per `tests/test_connect_daemon.py`) that changes
  its returned `boot_id` between calls to simulate a restart; assert
  `playfield_def_registry` repopulates and stays correct across a
  no-op (unchanged `boot_id`) call.
- Unit test for the empty-registry fallback path (registry empty, daemon
  reachable, handler retries once before raising).
- Unit test for `core/playfield.py`'s `_calibration_stale` transitions:
  marker shift beyond threshold sets `True`; shift back within threshold
  sets `False`.
- Unit test for the `frame_dark` signal: synthetic dark frames + zero tag
  detections over the configured window set the flag; bright frames or
  nonzero detections keep it clear.
- All existing tests remain green.

## Architecture Notes

See `architecture-update.md` for full design, component/dependency
diagrams, and design rationale. This sprint preserves the Sprint 014
single-camera-owner invariant (the daemon's `CameraPipeline` remains the
sole `VideoCapture` owner) and the Sprint 015 daemon-is-sole-vision-authority
invariant (no pixel work — including the new luminance sampling — crosses
into `mcp_server.py`; it stays entirely inside `daemon/camera_pipeline.py`).
All changes are additive to existing proto messages and files; no data
migration, no breaking changes.

## GitHub Issues

(none yet)

## Definition of Ready

Before tickets can be created, all of the following must be true:

- [x] Sprint planning documents are complete (sprint.md, use cases, architecture)
- [x] Architecture review passed (APPROVE)
- [ ] Stakeholder has approved the sprint plan

## Tickets

| # | Title | Depends On |
|---|-------|------------|
| 001 | Repopulate playfield registry after daemon restart | — |
| 002 | Dark-frame / zero-detection advisory signal | — |
| 003 | Fix calibration_stale reset gap | — |

Tickets execute serially in the order listed. All three are mutually
independent (no cross-dependencies) and 001 is independently shippable as
the priority item.
