---
id: '002'
title: Machine-global lock in DaemonServer
status: open
use-cases:
- SUC-001
- SUC-002
depends-on:
- '001'
github-issue: ''
issue: enforce-one-aprilcamd-daemon-per-machine.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Machine-global lock in DaemonServer

## Description

Replaces `DaemonServer.run()`'s per-user pidfile flock guard
(`src/aprilcam/daemon/server.py:107-141`, which locks
`config.daemon_pidfile` under a uid/runtime-namespaced `socket_dir`) with a
machine-global guard that locks `config.daemon_lock_path` (added in ticket
001, fixed default `/tmp/aprilcamd.lock`). This closes the confirmed root
cause: two daemons with different `socket_dir`s currently both flock
successfully and coexist.

Key behavior changes from the current code:

- The lock is acquired via a new `_acquire_global_lock()` method: `os.open`
  with `O_RDWR | O_CREAT` at mode `0o666`, then `os.fchmod` to `0o666`
  (world-writable so any daemon-running user can rewrite the pid — the
  `flock` itself is what enforces exclusivity, not file permissions), then
  `fcntl.flock(fd, LOCK_EX | LOCK_NB)`.
- On contention (`BlockingIOError`): read the existing pid from the lock
  file's contents. If `force` is `False` (default), raise
  `DaemonAlreadyRunningError(existing_pid)` — **do not** silently return.
  If `force` is `True`, run the takeover sequence (implemented in ticket
  004 as a helper this ticket calls or stubs — see Implementation Plan) and
  retry the lock acquisition.
- Stale-lock recovery uses **lock-first-then-rewrite**, not the current
  code's unlink-and-retry (lines 125-134): since a dead process's `flock` is
  already released by the kernel, a plain `flock(LOCK_EX | LOCK_NB)` on the
  existing (not recreated) file simply succeeds. Only after the lock is held
  does the code `ftruncate` + write the new pid. This removes the current
  TOCTOU race in the stale-file branch.
- The lock file is **never unlinked**, including during graceful shutdown —
  `_release_pidfile`'s existing pattern (`LOCK_UN` + `close()`, no
  `unlink`) is extended to the new lock file, for the same
  race-avoidance reason (see architecture-update.md "Decision: never unlink
  the lock file").
- The existing local `daemon_pidfile` write is **retained**, immediately
  after the global lock succeeds, so `status`/`stop` (unchanged in this
  ticket — see ticket 004) continue to work exactly as today.
- `DaemonServer.__init__` gains a `force: bool = False` parameter, stored
  for `run()` to consult.
- `run()` changes from returning silently on refusal to **raising**
  `DaemonAlreadyRunningError` — callers (`daemon/__main__.py`,
  `cli/daemon_cli.py`) are updated in ticket 004 to catch it; until then,
  this ticket's own tests catch it directly.

See `architecture-update.md` "What Changed" (`daemon/server.py` bullet) and
"Design Rationale" (lock-first-then-rewrite, never-unlink, `--force`
takeover sequence, run() raises) for full reasoning.

## Acceptance Criteria

- [ ] `DaemonServer.__init__` accepts `force: bool = False`.
- [ ] `run()` acquires an exclusive flock on `config.daemon_lock_path`
      (not `config.daemon_pidfile`) as its singleton guard.
- [ ] **(a)** A second `DaemonServer` constructed with a *different*
      `socket_dir`-derived config but the *same* `daemon_lock_path` as a
      running first instance is refused — reproduces and closes the
      confirmed today's-gap scenario.
- [ ] **(d)** Refuse-by-default: when the lock is held and `force=False`,
      `run()` raises `DaemonAlreadyRunningError` carrying the existing
      daemon's pid (read from the lock file contents), instead of silently
      returning.
- [ ] **(e)** A stale lock file left behind by a process that died via
      `kill -9` (simulate: hold the lock in a subprocess, kill -9 it, then
      start a new `DaemonServer`) is reclaimed by the next `run()` call
      without unlinking the file and without raising — the new process
      acquires the lock and overwrites the pid.
  - [ ] The local `daemon_pidfile` is still written after global-lock
      acquisition, unchanged in format/location from today.
- [ ] The lock file is never unlinked by `_shutdown()`/the release path —
      verify via a test that starts, gracefully stops, and restarts a
      `DaemonServer` reusing the same lock path, confirming the second
      `run()` succeeds without needing lock-file recreation.
- [ ] `test_pidfile_lock_prevents_duplicate`
      (`tests/test_daemon_server.py:164`) is replaced (not merely
      supplemented) by the new lock-path-based tests above, since it only
      covers the shared-pidfile case that no longer reflects the real
      guard.
- [ ] All other existing tests in `tests/test_daemon_server.py` and
      `tests/test_daemon_spawn_race.py` remain green.

## Implementation Plan

### Approach

Add `_acquire_global_lock()` as a new private method on `DaemonServer`,
called from `run()` in place of the current inline pidfile-flock block.
Keep the method boundary narrow: it only touches `config.daemon_lock_path`
and returns/raises; it does not know about gRPC or camera pipelines. The
existing local-pidfile write stays inline in `run()` right after the global
lock succeeds (same relative position as today, just now gated by the new
lock instead of being the lock itself).

For the `force=True` takeover path: this ticket needs *a* takeover
mechanism to satisfy its own tests, but the full CLI-facing sequence
(Shutdown RPC → SIGTERM → SIGKILL → wait-for-flock-free) is ticket 004's
scope because it needs `DaemonControl` (client-side) wiring that doesn't
belong in `daemon/server.py`. Recommended split: this ticket implements the
*local* half — `_acquire_global_lock(force=True)` reads the existing pid,
sends `SIGTERM`, waits briefly, escalates to `SIGKILL` after a timeout, then
retries the flock (pure `os`/`signal` primitives, no gRPC client dependency,
keeping `daemon/server.py` free of a `client/` import). Ticket 004 layers
the graceful Shutdown-RPC-first step on top, at the CLI level, before ever
constructing a `force=True` `DaemonServer` — i.e. `daemon_cli.py`'s takeover
helper calls `DaemonControl.shutdown()` first and only spawns/execs the new
daemon with `--force` as the fallback. Document this split explicitly in
both tickets so the full sequence (RPC → SIGTERM → SIGKILL → wait → acquire)
is unambiguously covered exactly once, with no gap and no duplication.

### Files to create/modify

- `src/aprilcam/daemon/server.py`:
  - Replace lines 107-141 (the inline pidfile-open/flock/stale-retry block)
    with a call to a new `_acquire_global_lock(self) -> None` method.
  - New method: open/fchmod/flock `self._config.daemon_lock_path` per the
    Description above; on `BlockingIOError`, read pid, raise
    `DaemonAlreadyRunningError(existing_pid)` unless `self._force`, in which
    case perform the local SIGTERM/SIGKILL escalation (see Approach) then
    retry once.
  - After the global lock succeeds, keep the existing local
    `daemon_pidfile` open/write logic (currently interleaved with the old
    guard; separate it into its own small block so it's clearly
    independent of the singleton mechanism).
  - Add `force: bool = False` to `__init__`, store as `self._force`.
  - Update the class docstring's "The daemon owns" list (currently says "An
    exclusive flock on the pidfile (prevents duplicate daemons)") to
    describe the new two-lock arrangement (global lock = singleton guard;
    local pidfile = status/stop convenience cache).
  - Update `_release_pidfile` (lines 257-267) or add a parallel
    `_release_global_lock` — decide based on whether one fd or two fds are
    held; release both without unlinking either.
- `src/aprilcam/errors.py`: no changes (consumed from ticket 001).
- `src/aprilcam/config.py`: no changes (consumed from ticket 001).
- `tests/test_daemon_server.py`:
  - Remove/replace `test_pidfile_lock_prevents_duplicate` (line 164).
  - Add tests for (a), (d), (e) as specified in Acceptance Criteria, using
    `tmp_config`-style fixtures with `daemon_lock_path` pointed at a
    per-test tmp file (never the real `/tmp/aprilcamd.lock`).

### Testing plan

- New tests per (a), (d), (e) above, using threads (matching the existing
  `t1 = threading.Thread(target=server1.run, daemon=True)` pattern already
  used in the test being replaced) so both "daemons" run in-process without
  actual `subprocess.Popen`.
- For (e)'s `kill -9` simulation: since an in-process thread can't be
  `kill -9`'d independently of the test process, use a real subprocess (a
  minimal helper script or `multiprocessing.Process` with `SIGKILL` via
  `os.kill(pid, signal.SIGKILL)`) that holds the flock, then verify a
  fresh `DaemonServer.run()` in the test process reclaims it.
- `uv run pytest tests/test_daemon_server.py tests/test_daemon_spawn_race.py`
  — targeted run first, then full `uv run pytest`.

### Documentation updates

- Update the `DaemonServer` class docstring (top of `server.py`) to
  describe the machine-global lock and its relationship to the local
  pidfile, replacing the current "exclusive flock on the pidfile" bullet.
