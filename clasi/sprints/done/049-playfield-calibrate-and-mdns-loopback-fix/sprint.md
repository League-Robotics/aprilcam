---
id: 049
title: playfield calibrate and mDNS loopback fix
status: done
branch: sprint/049-playfield-calibrate-and-mdns-loopback-fix
use-cases:
- SUC-001
- SUC-002
- SUC-003
- SUC-004
issues:
- move-calibrate-to-playfield-calibrate.md
- daemon-advertises-loopback-over-mdns-when-started-before-network.md
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 049: playfield calibrate and mDNS loopback fix

## Goals

1. `aprilcam playfield calibrate [CAMERA]` replaces `aprilcam camera
   calibrate`, which stays one release as a deprecated alias.
2. The daemon never advertises a loopback address over mDNS, and the
   client never dials a loopback record it browsed from elsewhere.

## Problem

- `calibrate` solves a camera's pose against the playfield's surveyed
  tags, yet lives under `camera` beside `intrinsics` and `flatfield`
  (which describe the camera itself). It is a playfield operation.
- `aprilcamd` started before the network is up falls back to
  `127.0.0.1` in `_primary_routable_ipv4` and publishes it. Remote clients
  then dial their own loopback, reach their local daemon, and list it
  under the remote host's name (seen on angst/gala, 2026-11-04).

## Solution

- Add `playfield calibrate [CAMERA]` (optional positional). It reuses
  `_connect` (HOST-N aware, from the uncommitted `cli/playfield.py`
  change) and `resolve_playfield_camera` when CAMERA is omitted. The
  resolver's "pass --camera" hint becomes a parameter so calibrate says
  "pass CAMERA". `camera calibrate` keeps its behaviour, prints a
  deprecation line to stderr, and its help says deprecated. Every
  user-facing pointer is repointed.
- `MdnsAdvertiser` never registers a loopback address: with no routable
  IPv4 it defers and retries in the background, registers when one
  appears, re-registers when the address changes, and logs the advertised
  address(es). Address lookup, clock/sleep and the retry thread are
  injectable so tests use no network. `_browse_zeroconf` prefers a
  non-loopback address from `parsed_addresses()` and drops a record that
  has only loopback addresses.

## Success Criteria

- `aprilcam playfield calibrate --help` shows positional `CAMERA`.
- `aprilcam playfield calibrate angst-4` reaches angst's daemon.
- Omitted CAMERA resolves via the playfield link, or errors telling the
  user to pass CAMERA.
- `aprilcam camera calibrate X` still works and warns on stderr.
- grep finds no user-facing `camera calibrate` pointer other than the
  deprecated alias itself (historical `docs/knowledge/*` and archived
  sprints excluded).
- Unit tests cover mDNS deferral/re-registration and the client loopback
  filter, with no real network.

## Scope

### In Scope

- New `playfield calibrate`; `camera calibrate` deprecation alias.
- Parameterised hint in `resolve_playfield_camera`.
- Committing the existing uncommitted `_connect` HOST-N change in
  `cli/playfield.py` and `tests/aprilcam/cli/test_host_n_cli.py`.
- Repointing hint strings and docs: `cli/playfield.py`, `cli/_common.py`,
  `cli/camera.py`, `daemon/streaming.py`, `view/viewer.py`,
  `view/intrinsics_model.py`, `AGENT_GUIDE.md`, the cli / calibration /
  daemon / view DESIGN.md files, `docs/design/use-cases/*.md`,
  `README.md`, `deploy/README.md` if it mentions it.
- Check the MCP `calibrate` tool description reads as a playfield pose
  operation (text only; no rename).
- mDNS advertiser deferral/re-registration/logging; client loopback filter.

### Out of Scope

- Renaming `calibrate`; any daemon RPC or storage change
  (`cameras/<slug>/calibration.json` stays per camera).
- Removing `camera calibrate` (next release).
- Historical `docs/knowledge/*` entries and archived sprints.
- End-to-end intrinsics-against-remote-camera test mentioned in the issue
  (needs angst hardware; noted, not planned here).
- `AGENTS.md`, `uv.lock`, `docs/knowledge/2026-11-02-daemon-cpu-cost-per-frame.md`
  are unrelated working-tree changes and must not be touched or committed.

## Test Strategy

Unit tests only, no hardware or real network. CLI: parser tests for the
positional CAMERA and `--help`; HOST-N routing with the `FakeDiscovery`
pattern in `tests/aprilcam/cli/test_host_n_cli.py`; omitted-camera
resolution (one / zero / several) and error wording; deprecated alias
warns on stderr and still behaves identically; `playfield image` error
wording unchanged. mDNS: fake `zeroconf_factory`, injected address lookup
and a fake clock/sleep drive deferral, later registration, re-registration
on address change, and stop() cancelling the retry. Client: injected
`ServiceInfo`-like objects exercise the loopback filter and
non-loopback preference. Scoped runs: `tests/aprilcam/cli/`,
`tests/aprilcam/daemon/test_mdns.py`, `tests/aprilcam/client/`.

## Architecture

**Sizing: Substantial** - touches three modules (`cli`, `daemon/mdns`,
`client/discovery`) across two independent concerns, plus hint strings in
`view` and `daemon/streaming`. Borderline against Compact (no new
cross-module dependency, no direction change, no data-model change), so
the heavier tier is chosen per the borderline rule. No component diagram:
the two concerns do not compose and no dependency edge is added or
changed, so a diagram would show nothing (sprint 020 precedent).

### Architecture Overview

#### What Changed

- **`cli/playfield.py`** gains `calibrate` (noun-correct home). It is a
  thin adapter: `_connect` -> optional `resolve_playfield_camera` ->
  `Daemon.calibrate` -> the existing result rendering. The rendering and
  `--verbose` handling live in `cli/calibrate.py`; that module's
  `cmd_calibrate` body is reused (not copied) so both commands print
  identically. `cli/calibrate.py` keeps ownership of calibrate rendering;
  `cli/playfield.py` wires the new parser and `cli/camera.py` keeps the
  deprecated alias, which prints the stderr line then calls the shared
  body.
- **`cli/_common.py` `resolve_playfield_camera`** gains a keyword
  `hint` (default `"--camera"`) used in both error strings, so
  `playfield image`/`view` wording is unchanged and calibrate passes
  `"CAMERA"`.
- **`daemon/mdns.py` `MdnsAdvertiser`** stops falling back to loopback.
  `_primary_routable_ipv4` returns `None` when none exists. `start()`
  registers if an address exists; otherwise it logs once and starts a
  daemon retry thread that polls (injectable lookup, interval, and wait
  function), registers when an address appears, and re-registers if the
  address later changes. `stop()` stops the thread then unregisters. The
  advertised address(es) are logged at INFO. TXT keys stay exactly
  `version` and `host` (the client self-filter keys on them).
- **`client/discovery.py` `_browse_zeroconf`** picks the first
  non-loopback entry of `parsed_addresses()`; a record with only loopback
  addresses is dropped (logged at INFO).
- **Hint strings/docs**: repoint to `playfield calibrate` (see Scope).

#### Impact on Existing Components

`cli/camera.py` and `cli/calibrate.py` keep working. The `MdnsAdvertiser`
public surface (`start(tcp_port, hostname=)`, `stop()`) is unchanged, so
`daemon/__main__.py` wiring is untouched. No daemon RPC, proto, or
storage change.

### Design Rationale

**Decision: optional positional CAMERA** (stakeholder-fixed). It matches
`camera calibrate`, which is the only argument that matters; a
`--camera` flag would be the lone flag-addressed camera in the group.
Consequence: the resolver hint must be parameterised.

**Decision: advertiser owns its retry.** Alternatives: have
`DaemonLifecycle` retry the `advertise` hook, or require systemd ordering.
The lifecycle hook is a one-shot by contract and systemd ordering is the
thing that already failed (`network-online.target` does not guarantee a
Wi-Fi address). Keeping retry inside `MdnsAdvertiser` leaves the lifecycle
untouched and keeps "advertising is never load-bearing" local to one
class. Consequence: one background thread, joined in `stop()`.

**Decision: poll, not netlink.** Polling the routable-address lookup
every few seconds is portable and testable with an injected lookup; a
netlink/ifaddr watcher adds a dependency and platform code for a
once-per-boot race.

**Decision: client filter in addition to daemon fix.** Old daemons in the
field and other implementations can still publish loopback; the client
must not trust it.

### Migration Concerns

None for data. `camera calibrate` is removed in a later release; scripts
using it get a stderr warning meanwhile. Already-deployed daemons need the
new version installed and `systemctl restart aprilcamd` to gain the fix;
the client filter helps immediately.

### Architecture Review (self, full)

- Consistency: Sprint Changes match the overlay docs. APPROVE.
- Codebase alignment: `cmd_calibrate` already uses `connect_for_camera`;
  `_connect` exists uncommitted; `MdnsAdvertiser` has the
  `zeroconf_factory` seam to extend. Feasible.
- Design quality: advertiser stays one-sentence cohesive ("publish the
  daemon's reachable address over mDNS"); no new cross-module edge; no
  cycles. Fan-out unchanged.
- Anti-patterns: none. Retry thread is single-owner state (no shared
  mutable state beyond a lock-guarded registration handle). Shotgun
  surgery limited to string repointing, done mechanically in one ticket.
- Risks: thread lifecycle at daemon shutdown (join with timeout in
  `stop()`); re-registering must unregister the old record first to avoid
  `NonUniqueNameException`. Both noted in ticket 003.
- Verdict: **APPROVE**.

### Open Questions

- Retry interval and give-up: planned as poll every 5 s, indefinitely,
  address-change check at the same cadence; stakeholder may prefer a cap.
- Multiple interfaces: this sprint advertises the single primary routable
  IPv4 (as today). Advertising all interface addresses is deferred.

## Use Cases

### SUC-001: Calibrate a playfield from the playfield noun
Parent: UC-04, UC-15

- **Actor**: Operator
- **Preconditions**: A camera is linked to a playfield and sees its tags.
- **Main Flow**:
  1. Operator runs `aprilcam playfield calibrate [CAMERA]`.
  2. CAMERA (slug, number, or HOST-N) selects the daemon and camera; if
     omitted, the single linked present camera is used.
  3. Result prints exactly as `camera calibrate` did.
- **Postconditions**: `cameras/<slug>/calibration.json` updated on success.
- **Acceptance Criteria**:
  - [ ] Positional CAMERA incl. `angst-4` routes to that host's daemon
  - [ ] Omitted CAMERA: one linked camera resolves; zero or several is an
        error telling the user to pass CAMERA
  - [ ] `--help` shows positional CAMERA

### SUC-002: Old command still works, with a pointer
Parent: UC-04

- **Actor**: Operator with scripts
- **Main Flow**: Runs `aprilcam camera calibrate X`; stderr shows a
  deprecation line naming `playfield calibrate`; behaviour is unchanged.
- **Acceptance Criteria**:
  - [ ] Warning on stderr, result unchanged, help says deprecated
  - [ ] No other user-facing pointer to `camera calibrate` remains

### SUC-003: Daemon never advertises loopback
Parent: UC-13

- **Actor**: `aprilcamd` starting before the network is up
- **Main Flow**: No routable IPv4 at start -> defer, retry; address
  appears -> register and log it; address changes -> re-register.
- **Acceptance Criteria**:
  - [ ] Nothing registered while only loopback exists
  - [ ] Registers once an address appears; re-registers on change
  - [ ] Advertised address(es) logged; `stop()` cancels the retry

### SUC-004: Client ignores loopback records
Parent: UC-13

- **Actor**: Any client browsing mDNS
- **Main Flow**: A browsed record with only loopback addresses is dropped;
  with several addresses a non-loopback one is used.
- **Acceptance Criteria**:
  - [ ] Loopback-only record dropped; non-loopback preferred

## GitHub Issues

None.

## Definition of Ready

- [x] Sprint planning document is complete
- [x] Architecture review passed
- [x] Stakeholder has approved the sprint plan

## Tickets

| # | Title | Depends On |
|---|-------|------------|
| 001 | Add playfield calibrate [CAMERA] and deprecate camera calibrate | - |
| 002 | Repoint all camera calibrate pointers to playfield calibrate | 001 |
| 003 | mDNS: never advertise loopback (daemon deferral and client filter) | - |

Tickets execute serially in the order listed.
