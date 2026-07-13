---
id: '005'
title: Implement daemon client (ensure_running, ControlClient, spawn-race guard)
status: done
use-cases:
- SUC-001
- SUC-006
- SUC-007
- SUC-008
depends-on:
- '004'
github-issue: ''
issue: aprilcam-daemon-architecture.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Implement daemon client (ensure_running, ControlClient, spawn-race guard)

## Description

Create `src/aprilcam/daemon/client.py`. This module provides two things:

1. `ensure_running(config) -> ControlClient` — the shared helper every entry
   point (MCP server, `aprilcam view`, `aprilcam calibrate`) calls at startup.
   It connects to the control socket if the daemon is already running, or
   forks it and waits if not.

2. `ControlClient` — a thin wrapper around the control socket connection that
   serializes RPC calls as newline-terminated JSON and deserializes responses.

This is the only module that calls `subprocess.Popen` to spawn the daemon.

## Acceptance Criteria

- [ ] `src/aprilcam/daemon/client.py` created.
- [ ] `ensure_running(config: Config) -> ControlClient` implements the
  6-step algorithm from the issue:
  1. Try to connect to `<socket_dir>/control.sock`. If success, return
     `ControlClient(sock)`.
  2. Take exclusive flock on `<socket_dir>/aprilcamd.spawn.lock`.
  3. Re-check the socket.
  4. Fork daemon via `subprocess.Popen([sys.executable, "-m", "aprilcam.daemon"],
     start_new_session=True, stdout=DEVNULL, stderr=<log_file>)`.
  5. Poll the socket every 50ms for up to 5 seconds.
  6. Release flock, connect, return `ControlClient`.
- [ ] `ensure_running` raises `RuntimeError` with a clear message if the
  daemon does not appear within 5 seconds.
- [ ] Two threads calling `ensure_running()` concurrently (with no daemon
  running) result in exactly one daemon process being spawned.
- [ ] `ControlClient.rpc(cmd: str, **kwargs) -> dict` sends a JSON request
  and returns the parsed JSON response dict.
- [ ] `ControlClient.close()` closes the underlying socket.
- [ ] `ControlClient` usable as a context manager (`__enter__` / `__exit__`).
- [ ] Log file for daemon stderr is written to `<data_dir>/aprilcamd.log`.

## Implementation Plan

### Approach

`ensure_running` uses `fcntl.flock(LOCK_EX)` on a lock file. The double-check
after acquiring the lock handles the race where two clients both found the
daemon absent before either got the lock. `ControlClient` wraps a single
connected `socket.socket`; `rpc()` sends one JSON line and reads one JSON line.

### Files to Create

- `src/aprilcam/daemon/client.py`

### Files to Modify

None.

### Notes

- The spawn lock file is `<socket_dir>/aprilcamd.spawn.lock` (separate from
  the daemon's own pidfile lock to avoid deadlock).
- `subprocess.Popen` with `start_new_session=True` detaches the daemon from
  the client's process group (survives client exit).
- Stderr log path: `<data_dir>/aprilcamd.log` (append mode).

### Testing Plan

Covered by T009. Key test: two threads call `ensure_running()` with the daemon
not running; use a mock `subprocess.Popen` (or a real test daemon binary) to
verify only one spawn occurs. Additional: `ControlClient.rpc()` round-trip
using `socket.socketpair()`.

### Documentation Updates

Docstring on `ensure_running` describing the 6-step algorithm and the flock
rationale.
