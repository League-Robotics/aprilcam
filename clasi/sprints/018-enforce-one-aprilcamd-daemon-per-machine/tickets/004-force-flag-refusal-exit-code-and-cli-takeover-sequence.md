---
id: '004'
title: --force flag, refusal exit code, and CLI takeover sequence
status: done
use-cases:
- SUC-001
- SUC-002
depends-on:
- '002'
- '003'
github-issue: ''
issue: enforce-one-aprilcamd-daemon-per-machine.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# --force flag, refusal exit code, and CLI takeover sequence

## Description

Wires the machine-global lock (ticket 002) and bind-safety checks (ticket
003) up to the process entrypoint and the `aprilcam daemon` CLI, completing
the user/operator-facing behavior: refuse by default with a clear,
non-zero-exit error; take over cleanly with `--force`.

Two layers:

1. **`daemon/__main__.py`** (`main()`): add a `--force` argparse flag,
   passed to `DaemonServer(config, force=args.force, ...)`. Wrap the
   `.run()` call in a `try/except` catching `DaemonAlreadyRunningError`,
   `DaemonPortInUseError`, and `DaemonBindError` (all from ticket 001,
   raised by tickets 002/003); print a clear message — for
   `DaemonAlreadyRunningError`, exactly `"aprilcamd already running (pid
   N)"` with the real pid interpolated — and call `sys.exit(3)` (a real,
   distinguishable non-zero code for systemd/scripts, distinct from the
   existing `sys.exit(1)` used for the "no transport enabled" argument
   error).
2. **`cli/daemon_cli.py`**:
   - Add `--force` to the `start` subparser; thread it through `_cmd_start`
     → `_spawn_daemon` so a detached spawn launches with `-m aprilcam.daemon
     --force` when requested (today `_spawn_daemon`, lines 35-56, always
     Popens with no extra argv).
   - Redefine `restart` to always request `--force` semantics for the new
     daemon's launch (it already stops the old daemon first via
     `_cmd_stop`) — i.e. `_cmd_restart` calls `_cmd_start(..., force=True)`
     regardless of whether `-f`/`--force` was passed to `restart` itself
     (restart's whole point is "stop the old one and definitely start a new
     one").
   - Repoint `_cmd_restart`'s wait-loop (lines 248-284, currently flocking
     `config.daemon_pidfile` directly) at `config.daemon_lock_path` instead
     — it must wait for the *global* lock to free, not the local pidfile,
     since that's now the real singleton guard.
   - `status` continues to read `config.daemon_pidfile` for the pid it
     displays; update its help text/docstring to note this is the
     machine-global daemon's pid under normal operation (best-effort local
     cache — see architecture-update.md Open Question 4), not an
     independent guard.
   - Implement the **`--force` takeover sequence** as a CLI-level helper
     that runs *before* constructing/spawning a `force=True` daemon: (1)
     attempt the existing graceful `DaemonControl.shutdown()` RPC against
     the currently-running daemon; (2) on failure/timeout, `SIGTERM` the
     pid read from the lock file; (3) `SIGKILL` after a bounded timeout if
     still alive; (4) wait for the global lock to become free (reusing/
     extending the existing wait-loop pattern, now repointed per above);
     (5) only then spawn/exec the new daemon with `--force` (which itself
     retries the lock acquisition per ticket 002's local escalation as a
     defense-in-depth fallback, not the primary mechanism — the CLI-level
     graceful-first sequence is what SUC-002's acceptance criteria are
     actually verifying end-to-end).

See `architecture-update.md` "What Changed" (`daemon/__main__.py`,
`cli/daemon_cli.py` bullets) and "Design Rationale" ("`--force` performs
Shutdown-RPC → SIGTERM → SIGKILL → wait-for-free → acquire" and "`run()`
raises instead of silently returning on refusal").

## Acceptance Criteria

- [x] `python -m aprilcam.daemon --force` is a valid invocation; without
      `--force` (default), a second daemon start against an already-locked
      machine prints `"aprilcamd already running (pid N)"` (real pid) to
      stderr and exits with code 3 — not the current silent-success exit 0.
- [x] `DaemonPortInUseError`/`DaemonBindError` raised from ticket 003's
      bind-check are also caught in `main()` with a clear message and exit
      code 3 (share the same catch/exit-3 path as
      `DaemonAlreadyRunningError`, with a message specific to each error
      type).
- [x] `aprilcam daemon start --force` and a detached spawn triggered by
      `_cmd_start` both result in the daemon process actually running with
      `--force` (verify via the spawned process's argv or via behavioral
      test — it successfully takes over a pre-existing daemon).
- [x] `aprilcam daemon restart` always performs a `--force`-equivalent
      start for the new instance, regardless of whether the old daemon
      was already stopped cleanly or needed escalation.
- [x] `_cmd_restart`'s wait-loop polls `config.daemon_lock_path` (not
      `config.daemon_pidfile`) for lock availability before starting the
      new daemon.
- [x] **(c)** New/updated test: `--force` takeover of a running daemon
      succeeds end-to-end — old instance's process exits, new instance
      holds the lock, `aprilcam daemon status`-equivalent check shows the
      new pid. Exercise via the CLI helper functions directly (not a real
      subprocess daemon, per the "no hardware" testing constraint — use the
      same fake/stub-daemon patterns already used elsewhere in
      `tests/test_daemon_server.py`/`tests/test_daemon_spawn_race.py`).
- [x] Takeover sequence order is verified: graceful Shutdown RPC is
      attempted before any signal; `SIGTERM` is sent before `SIGKILL`;
      `SIGKILL` only happens after a bounded timeout elapses with the
      process still alive.
- [x] `aprilcam daemon status` reports the correct machine-global pid after
      a `--force` takeover (i.e. the new daemon's pid, once it has written
      its local `daemon_pidfile`).
- [x] All existing tests in `tests/test_daemon_server.py` and
      `tests/test_daemon_spawn_race.py` remain green; any test asserting the
      old exit-0-on-duplicate behavior is updated to expect exit 3.

## Implementation Plan

### Approach

Layer the CLI-level takeover sequence *above* ticket 002's local
lock-escalation fallback rather than duplicating it: `daemon_cli.py` owns
the graceful-RPC-first step (it already has `DaemonControl` available,
unlike `daemon/server.py`, which must stay free of a `client/` import to
avoid a daemon → client dependency direction violation — see
architecture-update.md's Dependency Graph, which keeps `daemon/server.py`
a leaf). `daemon/server.py`'s own `force=True` handling (from ticket 002)
remains a narrower, local SIGTERM/SIGKILL-only fallback for callers that
construct `DaemonServer` directly without going through the CLI (e.g.
`__main__.py` itself, or tests) — it does not need to know about gRPC
Shutdown RPCs.

### Files to create/modify

- `src/aprilcam/daemon/__main__.py`:
  - Add `--force` to `_parse_args` (mirrors the existing `--unix`/`--tcp`
    boolean-flag style).
  - Pass `force=args.force` to the `DaemonServer(...)` construction (line
    108-114).
  - Wrap `.run()` in `try/except (DaemonAlreadyRunningError,
    DaemonPortInUseError, DaemonBindError) as exc:` — print the
    type-specific message to `sys.stderr`, `sys.exit(3)`.
  - Import the three exceptions from `aprilcam.errors`.
- `src/aprilcam/cli/daemon_cli.py`:
  - Add `--force`/`-f` to the `start` subparser's argument group (near the
    existing `-v`/`-d`/`--unix-path`/`--tcp-port` args, lines ~300-326).
  - Update `_spawn_daemon` signature to accept `force: bool = False` and
    append `"--force"` to the `Popen` argv when set.
  - Update `_cmd_start` to accept/thread a `force` parameter through to
    `_spawn_daemon` (detached path) and to the foreground
    `DaemonServer(config).run()` call (line 81, becomes
    `DaemonServer(config, force=force).run()`).
  - Add a new helper, e.g. `_force_takeover(config, resolved_unix,
    tcp_port)`, implementing the graceful-RPC → SIGTERM → SIGKILL → wait
    sequence described above; call it from `_cmd_start` when `force=True`
    and a daemon is already up (`_probe_daemon` returns `True`), *before*
    spawning the new one.
  - Update `_cmd_restart` (lines 248-284) to call `_cmd_start(...,
    force=True)` unconditionally for the new instance, and repoint the
    wait-loop's `fcntl.flock` target from `config.daemon_pidfile` to
    `config.daemon_lock_path`.
  - Update `_cmd_status`'s docstring/help text per the Description above
    (best-effort local cache framing).
  - Add `-f`/`--force` to the `start` subparser registration loop (~line
    296-326); `restart` does not need its own `--force` flag since it
    always forces the new instance by definition, but confirm this
    matches the issue's intent ("Make `restart` effectively `start
    --force`") rather than exposing a redundant flag.
- `tests/test_daemon_server.py` / `tests/test_daemon_spawn_race.py`: add
  tests per Acceptance Criteria (c) and the exit-code/message assertions
  for `__main__.py`'s refusal path (likely via `_parse_args` +
  direct-call testing of `main()` with a mocked `DaemonServer`, matching
  whatever pattern the existing `__main__` argument-parsing tests already
  use — see the "`__main__` argument parsing" test section marker already
  present in `test_daemon_server.py`).

### Testing plan

- Unit test: `main()` with a `DaemonServer.run()` mocked to raise
  `DaemonAlreadyRunningError(existing_pid=1234)` → asserts stderr contains
  `"aprilcamd already running (pid 1234)"` and `sys.exit(3)` is called
  (via `pytest.raises(SystemExit)` and checking `.code == 3`).
  - [x] Repeat for `DaemonPortInUseError` and `DaemonBindError` with their
        respective messages.
- Integration-style test (in-process, fake/stub daemon per existing
  patterns): start a `DaemonServer` in a thread, then exercise
  `daemon_cli.py`'s takeover helper against it with `force=True`, asserting
  the original thread's server shuts down and a new one (or a re-run of the
  helper) can acquire the lock.
- Update/add a restart-wait-loop test confirming it polls
  `daemon_lock_path`, not `daemon_pidfile`.
- `uv run pytest tests/test_daemon_server.py tests/test_daemon_spawn_race.py`
  targeted, then full `uv run pytest`.

### Documentation updates

- `daemon/__main__.py`'s module docstring: add `--force` to the documented
  flags list (currently documents `--unix`/`--tcp`/`--tcp-port`/
  `--unix-path`).
- `cli/daemon_cli.py`: update any inline comments describing `_cmd_start`
  as "the ONE place that is explicitly allowed to spawn the daemon" to
  also mention the takeover helper's role when `force=True`.
