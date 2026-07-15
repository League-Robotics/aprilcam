---
id: 018
title: Enforce one aprilcamd daemon per machine
status: done
branch: sprint/018-enforce-one-aprilcamd-daemon-per-machine
use-cases:
- SUC-001
- SUC-002
issues:
- enforce-one-aprilcamd-daemon-per-machine.md
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 018: Enforce one aprilcamd daemon per machine

## Goals

Guarantee that only ONE `aprilcamd` daemon process runs per machine. A
second start attempt must refuse by default with a clear error and non-zero
exit, and only take over the existing daemon when given an explicit
`--force`. Close both the process-lifecycle gap (per-user singleton guard)
and the network-layer gap (silent multi-bind on the gRPC TCP port) that let
duplicate daemons coexist undetected.

## Problem

A debugging session exposed that multiple `aprilcamd` daemons can run on one
machine simultaneously — at one point 8 MCP servers were live against a
confused mix of daemon instances (some from different venvs), which made a
hardware bug nearly impossible to diagnose because code fixes appeared not
to "take" (calls were being served by stale daemons). The daemon is meant to
be the single vision authority and sole camera owner; duplicate daemons
contend for the camera and scatter state.

Two confirmed, independent root causes:

1. **The singleton guard today is per-USER, not per-machine.**
   `DaemonServer.run()` (`src/aprilcam/daemon/server.py:107-141`) takes an
   exclusive `fcntl.flock` on `config.daemon_pidfile`
   (`<socket_dir>/aprilcamd.pid`), but `socket_dir` is uid/runtime-namespaced
   (`config.py:_default_dirs()`, lines 310-350: `/run/aprilcam` for root,
   else `$XDG_RUNTIME_DIR/aprilcam` / `/run/user/<uid>/aprilcam` /
   `$TMPDIR/aprilcam-<uid>`). Two daemons with different `socket_dir`s (e.g.
   a systemd instance vs. a developer's ad hoc `python -m aprilcam.daemon`
   run) both flock successfully and coexist.
2. **TCP `SO_REUSEPORT` is left at gRPC's default-enabled setting (primary
   cause).** `make_grpc_server()` (`src/aprilcam/daemon/grpc_server.py:1112-1147`)
   calls `grpc.server(...)` with no `options=` kwarg and calls
   `server.add_insecure_port(addr)` for each transport without inspecting
   the return value (confirmed: `for addr in transports:
   server.add_insecure_port(addr)`, line 1145 — return discarded). gRPC's
   C-core defaults to `GRPC_ARG_ALLOW_REUSEPORT=1`, so N daemons bind
   `[::]:5280` with zero error and the kernel load-balances RPCs between
   them.

## Solution

Two independent, mutually-reinforcing guards, both portable to macOS and
Linux/Raspberry Pi (the daemon runs on both):

1. **A machine-global `flock` on a fixed path `/tmp/aprilcamd.lock`**
   (overridable via `APRILCAM_DAEMON_LOCK`, the sanctioned escape hatch for
   test isolation, paired with the existing `APRILCAM_DAEMON_PORT`
   pattern). Created `0666` via `os.fchmod` so any daemon-running user can
   rewrite the pid — `flock` is advisory and works regardless of the pid
   text's owner, and the OS auto-releases it on process death (preserving
   `kill -9` recovery). Stale-lock recovery uses lock-first-then-rewrite
   (attempt `flock` unconditionally; a dead holder's lock is already
   released by the kernel) instead of the current code's racy
   unlink-and-retry. The lock file is never unlinked, including on graceful
   shutdown, to avoid a create/lock race window. `daemon_pidfile` is
   retained unchanged as a local, best-effort cache for `status`/`stop`.
2. **Disabling gRPC `SO_REUSEPORT`** on the TCP port
   (`options=[("grpc.so_reuseport", 0)]`) and checking each
   `add_insecure_port()` return value — a `0` return raises
   `DaemonPortInUseError` (TCP) or `DaemonBindError` (Unix socket).

Refuse-by-default: a second start finds the lock held, reads the existing
pid, and raises `DaemonAlreadyRunningError(existing_pid)` — `run()` raises
instead of silently returning, so callers (systemd, scripts, the CLI) see a
real non-zero exit (`sys.exit(3)`) and a clear "aprilcamd already running
(pid N)" message.

`--force` takeover sequence: prefer the existing graceful Shutdown RPC
(`DaemonControl.shutdown()`) → fall back to `SIGTERM` on the lock-file pid →
`SIGKILL` after a timeout → wait for the flock to free (reusing the
`_cmd_restart` wait-loop pattern) → acquire. `aprilcam daemon restart`
becomes `start --force`. The systemd unit's `ExecStart` always passes
`--force` so systemd wins over any stray instance on boot or restart.

Full design detail, rationale, and diagrams are in `architecture-update.md`.

## Success Criteria

- SUC-001: a second daemon start — including one with a *different*
  `socket_dir` than the running daemon, the exact gap that exists today —
  is refused by default with a clear pid-identifying error and exit code 3.
  With `SO_REUSEPORT` disabled, a second TCP bind attempt fails loudly
  rather than silently sharing the port. A stale lock left by a `kill -9`'d
  daemon is reclaimed without manual cleanup and without a race.
- SUC-002: `--force` (via `python -m aprilcam.daemon --force`, `aprilcam
  daemon start --force`, or `aprilcam daemon restart`) reliably takes over
  a running daemon — preferring graceful shutdown, escalating only as
  needed — leaving exactly one daemon holding the lock and one listener on
  port 5280. `aprilcam daemon status` reports the correct machine-global
  pid after takeover. The systemd unit always launches with `--force`.
- Both criteria verified without hardware: tests use `APRILCAM_DAEMON_LOCK`
  overrides and ephemeral ports; a manual end-to-end pass (documented in the
  issue's Verification section) confirms real-process behavior on a dev
  machine.

## Scope

### In Scope

- `config.py`: `daemon_lock_path` field + `APRILCAM_DAEMON_LOCK` env
  (default `/tmp/aprilcamd.lock`), registered in `CONFIG_VARS`.
- `errors.py`: `DaemonAlreadyRunningError(existing_pid)`,
  `DaemonPortInUseError`, `DaemonBindError`.
- `daemon/server.py`: `_acquire_global_lock()` replacing the per-user
  pidfile guard; `force: bool` on `DaemonServer.__init__`; `run()` raises on
  refusal; lock-first-then-rewrite stale handling; no unlink on shutdown;
  local `daemon_pidfile` write retained.
- `daemon/grpc_server.py`: `grpc.server(..., options=[("grpc.so_reuseport",
  0)])`; checked `add_insecure_port` return raising the new errors.
- `daemon/__main__.py`: `--force` flag; catches the new errors; prints a
  clear message; `sys.exit(3)`.
- `cli/daemon_cli.py`: `--force` on `start`; threaded through
  `_spawn_daemon`; `restart` == `start --force`; wait-loop repointed at the
  global lock; `status` documented as reporting the best-effort local pid
  cache.
- `--force` takeover sequence (Shutdown RPC → SIGTERM → SIGKILL → wait →
  acquire).
- `deploy/aprilcamd.service`: `--force` added to `ExecStart`; comment that
  `PrivateTmp` must stay off.
- Test suite: new/replacement tests per the Test Strategy below.

### Out of Scope

- Tooling for intentional multi-tenant hosts (multiple mutually distrusting
  users each wanting an independent daemon on the same machine) — the
  `APRILCAM_DAEMON_LOCK`/`APRILCAM_DAEMON_PORT` overrides are the documented
  escape hatch; no additional management tooling is built this sprint.
  (architecture-update.md Open Question 2)
- Reconciliation between `daemon status`'s local pidfile cache and the
  machine-global lock file when they disagree due to manual tampering —
  `daemon_pidfile` is documented as best-effort; active reconciliation is
  deferred. (architecture-update.md Open Question 4)
- Re-wiring or removing the legacy, currently-uncalled
  `daemon/client.py:ensure_running()` — left as-is; the new guard protects
  it transparently if it is ever re-activated. (architecture-update.md Open
  Question 3)
- Any change to the Sprint 014 remote-daemon/discovery model
  (`daemon_host`/`daemon_port`, mDNS, `client/discovery.py`) — this sprint
  is purely about singleton enforcement on one machine, not multi-machine
  discovery.
- TLS or authentication on the gRPC transport — unrelated to this sprint's
  scope (already a known, documented limitation from Sprint 014).

## Test Strategy

Extend `tests/test_daemon_server.py`. All tests use unique tmp lock paths
via `APRILCAM_DAEMON_LOCK` and free ephemeral ports — no hardware, no
touching the real `/tmp/aprilcamd.lock` or port 5280.

- **(a)** A second `DaemonServer` with a *different* `socket_dir` but the
  *same* lock path is refused — the exact gap that exists today.
- **(b)** With `SO_REUSEPORT` disabled, a second TCP bind attempt on the
  same port raises `DaemonPortInUseError`.
- **(c)** `--force` takes over a running daemon: the old instance stops, the
  new instance holds the lock.
- **(d)** Refuse-by-default raises `DaemonAlreadyRunningError` (and, at the
  `__main__` layer, exits code 3 with "already running (pid N)" in the
  output). **Replaces** `test_pidfile_lock_prevents_duplicate`
  (`tests/test_daemon_server.py:164`), which only covers the shared-pidfile
  case and does not exercise the different-`socket_dir` gap.
- **(e)** A stale lock file left behind after a `kill -9` is reclaimed by
  the next start attempt without unlinking and without a race.
- Existing tests (`tests/test_daemon_spawn_race.py` and the rest of
  `tests/test_daemon_server.py`) remain green; any test that directly
  exercised the old pidfile-based refusal message is updated to match the
  new error type/message.

## Architecture Notes

See `architecture-update.md` for full design, component/dependency
diagrams, and design rationale. This sprint preserves the Sprint 014
remote-daemon/discovery model and the Sprint 015 daemon-is-sole-vision-
authority invariant unchanged — it is scoped entirely to daemon process
lifecycle (startup singleton enforcement, `--force` takeover) and gRPC TCP
bind safety. No proto changes, no data-model changes, no new RPCs.
Two independent guards are used because neither alone is sufficient: the
lock stops a second daemon from starting; the reuseport fix stops silent
co-serving on the port even if the lock is bypassed (e.g. via a
misconfigured `APRILCAM_DAEMON_LOCK` override) or predates this sprint's
rollout on a given host.

## GitHub Issues

(none yet)

## Definition of Ready

Before tickets can be created, all of the following must be true:

- [x] Sprint planning documents are complete (sprint.md, use cases, architecture)
- [x] Architecture review passed (APPROVE)
- [x] Stakeholder has approved the sprint plan (design pre-approved in
      `clasi/issues/enforce-one-aprilcamd-daemon-per-machine.md`; team-lead
      to record the gate)

## Tickets

| # | Title | Depends On |
|---|-------|------------|
| 001 | Config lock path and new daemon error types | — |
| 002 | Machine-global lock in DaemonServer | 001 |
| 003 | Disable gRPC SO_REUSEPORT and check bind results | 001 |
| 004 | --force flag, refusal exit code, and CLI takeover sequence | 002, 003 |
| 005 | systemd unit --force and PrivateTmp documentation | 004 |

Tickets execute serially in the order listed.
