---
id: '005'
title: Client library minimum surface
status: done
use-cases:
- UC-01
- UC-13
- UC-14
- UC-17
- UC-25
depends-on:
- '004'
github-issue: ''
issue: daemon-bring-up-with-basic-camera-cli.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Client library minimum surface

## Description

Depends on ticket 004 (the client wraps the gRPC surface ticket 004
defines). Implements the cv2-free client library that every adapter
(CLI now, MCP/ROS later) sits on:

- `client/discovery.py` — `Discovery` Protocol: `find_all()` and
  `connect()`. Implements **Architecture Decision 2**: `find_all()`
  this sprint returns at most one `DaemonInfo` (the local daemon, probed
  via the well-known local unix-socket path / pidfile — genuinely
  useful, not a stub that lies), no real mDNS broadcast/browse.
  `connect(host)` for an explicit host is a direct connect (unix socket
  for local/`None`, TCP for a named remote host), bypassing discovery
  entirely — this is what makes `-d HOST` work without any mDNS
  machinery.
- `client/daemon_client.py` — the `Daemon` Protocol client-side: a
  typed wrapper over the ticket-004 gRPC stub for exactly
  `list_cameras`, `probe_cameras`, `get_frame`, `status`, marshalling
  proto messages to/from `types.py` dataclasses and mapping gRPC errors
  to `aprilcam.errors` types (`ConnectionFailed`, `UnknownCamera`,
  `NotCalibrated` is not reachable this sprint since no deskewed frame
  path exists yet — but `ConnectionFailed`/`DiscoveryFailed` are).
- `client/lifecycle.py` — the `DaemonLifecycle` Protocol client-side:
  local process control (`start()` spawns `daemon/__main__.py` as a
  subprocess, `stop()` sends a clean-shutdown signal, `status()` checks
  the pidfile/lock). Refuses `-d remote` for lifecycle verbs at this
  layer (or is refused one layer up in the CLI — this ticket decides
  which and documents it; either satisfies UC-13.S-4).

**Reuse from archive**: convert the process-control parts of
`archive/src/aprilcam/daemon/__main__.py` (151 lines) and
`archive/src/aprilcam/cli/_daemon.py` (196 lines — useful as reference
for lifecycle edge cases, tangled with v1's CLI framework so extract
logic, don't import the module) for `client/lifecycle.py`'s subprocess
spawn/signal handling. Do not convert `archive/src/aprilcam/daemon/mdns.py`
this sprint (Architecture Decision 2 — deferred).

## Acceptance Criteria

- [x] `Discovery.connect(None)` connects to the local daemon over the
      unix socket (ticket 004's listener).
- [x] `Discovery.connect("some-remote-host")` attempts a direct TCP
      connect and raises `ConnectionFailed(host, reason)` when nothing
      answers — distinct in type from a successful-but-empty discovery
      result.
- [x] `Discovery.find_all()` with a local daemon running returns exactly
      one `DaemonInfo` with `is_local=True`, `reachable=True`; with no
      daemon running, returns an empty tuple (not an error) — matching
      UC-13.S-2's "empty list, not an error" while being honest that no
      network discovery happened (module docstring states this
      explicitly per Architecture Decision 2).
- [x] `client/daemon_client.py`'s `list_cameras()` / `probe_cameras()` /
      `get_frame()` / `status()` round-trip correctly against a running
      daemon (headless, using ticket 004's fake-camera daemon fixture).
- [x] `DaemonLifecycle.start()` / `.stop()` / `.status()` round-trip
      against a real local daemon process (`@pytest.mark.camera` or a
      lighter marker if it can run without cameras attached — zero
      cameras is a valid start per UC-01.S-2, so this can likely run
      headless).
- [x] `DaemonLifecycle.start()` refuses when the lock is already held,
      surfacing `AlreadyRunning` from ticket 004 through to the client
      caller unchanged (no re-wrapping that loses the holder identity).

## Testing

- **Existing tests to run**: `tests/aprilcam/daemon/test_service.py`,
  `test_lifecycle.py` (ticket 004) — the client wraps both.
- **New tests to write**: `tests/aprilcam/client/test_discovery.py`
  (local-only `find_all`/`connect` behavior, headless);
  `tests/aprilcam/client/test_daemon_client.py` (RPC wrapper round-trips
  against ticket 004's fake-camera daemon, headless);
  `tests/aprilcam/client/test_lifecycle.py` (real subprocess start/stop
  round trip — can run headless since zero cameras is a valid start).
- **Verification command**: `uv run pytest tests/aprilcam/client/`
