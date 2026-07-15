---
id: '003'
title: Disable gRPC SO_REUSEPORT and check bind results
status: in-progress
use-cases:
- SUC-001
depends-on:
- '001'
github-issue: ''
issue: enforce-one-aprilcamd-daemon-per-machine.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Disable gRPC SO_REUSEPORT and check bind results

## Description

Closes the second, independent root cause behind duplicate daemons silently
co-serving traffic: `make_grpc_server()`
(`src/aprilcam/daemon/grpc_server.py:1112-1147`) builds `grpc.server(...)`
with no `options=` kwarg, so gRPC's C-core defaults to
`GRPC_ARG_ALLOW_REUSEPORT=1`. Combined with the fact that each
`server.add_insecure_port(addr)` call's return value is currently discarded
(line 1145: `for addr in transports: server.add_insecure_port(addr)`), N
daemons can bind `[::]:5280` with zero error and the kernel silently
load-balances RPCs between them — confirmed as the primary cause of the
incident that motivated this sprint (calls appeared to be served by a stale
daemon because they *were*, non-deterministically).

This ticket:

1. Passes `options=[("grpc.so_reuseport", 0)]` to `grpc.server(...)`,
   disabling reuseport so a second bind attempt on an occupied port fails
   instead of silently succeeding.
2. Captures each `add_insecure_port(addr)` return value. A return of `0`
   (bind failed) raises `DaemonPortInUseError` for a TCP address
   (`[::]:<port>`) or `DaemonBindError` for a Unix socket address
   (`unix:<path>`) — both added in ticket 001.

This is independent of ticket 002's lock mechanism: it protects against
silent co-serving even in scenarios the lock doesn't cover (e.g. a
misconfigured `APRILCAM_DAEMON_LOCK` override that points two daemons at
different lock files, or a daemon predating this sprint's lock rollout on a
mixed-version host). See `architecture-update.md` "What Changed" and "Why"
tables.

## Acceptance Criteria

- [ ] `make_grpc_server()` passes `options=[("grpc.so_reuseport", 0)]` to
      `grpc.server(...)`.
- [ ] Each `add_insecure_port(addr)` call's return value is captured (not
      discarded).
- [ ] A `0` return for a TCP address (`[::]:<port>` or similar) raises
      `DaemonPortInUseError`.
- [ ] A `0` return for a Unix socket address (`unix:<path>`) raises
      `DaemonBindError`.
- [ ] **(b)** New test: with `SO_REUSEPORT` disabled, a second `grpc.Server`
      built by `make_grpc_server()` and pointed at the same TCP port as a
      first, already-bound server raises `DaemonPortInUseError` when it
      attempts to bind (use an ephemeral port obtained via a bind-to-0
      probe, not the real 5280, per the sprint's test-isolation rule).
  - [ ] A successful bind (port free, or first call in the test) does not
      raise and returns/proceeds normally — confirm no regression to the
      happy path used by every other daemon test.
- [ ] Unix-socket bind-failure path is exercised by at least one test (can
      reuse `tests/test_daemon_server.py`'s existing unix-socket test
      fixtures for a "path already bound" scenario) and raises
      `DaemonBindError`.
- [ ] All existing tests in `tests/test_daemon_server.py`,
      `tests/test_daemon_spawn_race.py`, and any gRPC-server-construction
      tests remain green — reflection registration and existing transports
      list behavior are unchanged by this ticket.

## Implementation Plan

### Approach

Minimal, localized change to `make_grpc_server()` — add the `options=`
kwarg to the existing `grpc.server(...)` call, and replace the bare `for
addr in transports: server.add_insecure_port(addr)` loop with one that
captures and checks each return value, distinguishing TCP vs. Unix
addresses by their string prefix (`addr.startswith("unix:")` vs. not) to
choose which exception to raise. No change to the function's signature or
its caller (`daemon/server.py`'s `run()`, which already calls
`make_grpc_server(transports, self._servicer)` and will need a
`try/except` — but wiring that catch is ticket 004's job at the
`__main__.py`/CLI layer; this ticket's own tests catch the raised exception
directly against `make_grpc_server()`).

### Files to create/modify

- `src/aprilcam/daemon/grpc_server.py`:
  - In `make_grpc_server()` (line 1112 area), change
    `server = grpc.server(futures.ThreadPoolExecutor(max_workers=max_workers))`
    to pass `options=[("grpc.so_reuseport", 0)]`.
  - Replace the `for addr in transports: server.add_insecure_port(addr)`
    loop (line 1144-1145) with a loop that captures
    `bound_port = server.add_insecure_port(addr)`, and if `bound_port ==
    0`, raises `DaemonPortInUseError(f"failed to bind {addr}")` when
    `addr` is a TCP address, else `DaemonBindError(f"failed to bind
    {addr}")` for a `unix:` address.
  - Add `from ..errors import DaemonBindError, DaemonPortInUseError` (or
    the project's existing relative-import style — check how
    `grpc_server.py` currently imports from `..errors`, if at all, and
    match it).
- `tests/test_daemon_server.py` (or a new `tests/test_grpc_server.py` if
  gRPC-server-construction tests don't currently live in
  `test_daemon_server.py` — check first): add tests for (b) as specified
  above.

### Testing plan

- Unit test: bind two `grpc.Server` instances (via `make_grpc_server`) to
  the same ephemeral TCP port sequentially — first succeeds, second raises
  `DaemonPortInUseError`. Use `socket.socket()` bind-to-0 trick to obtain a
  free port, matching the "no hardware, ephemeral ports" pattern the issue
  specifies.
- Unit test: bind two servers to the same Unix socket path — second raises
  `DaemonBindError`.
- Regression: existing tests that construct a `DaemonServer`/call
  `make_grpc_server` with normally-available ports/paths must still pass
  unchanged (confirms `options=` doesn't break the happy path).
- `uv run pytest tests/test_daemon_server.py` targeted, then full `uv run
  pytest`.

### Documentation updates

- Update `make_grpc_server()`'s docstring to note that `SO_REUSEPORT` is
  disabled and that a bind failure now raises rather than being silently
  ignored — this is a behavior change future readers of the code need to
  know about (the current docstring says only "The caller must call
  `server.add_insecure_port(addr)`" with no mention of failure handling).
