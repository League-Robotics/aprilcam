---
id: '001'
title: Config lock path and new daemon error types
status: done
use-cases:
- SUC-001
- SUC-002
depends-on: []
github-issue: ''
issue: enforce-one-aprilcamd-daemon-per-machine.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Config lock path and new daemon error types

## Description

Foundation ticket for the one-daemon-per-machine sprint. Adds the
configuration surface and the exception types that every later ticket in
this sprint depends on, with **no behavior change** to the running daemon —
the tree stays green after this ticket lands, and nothing yet reads or acts
on the new field or error types.

Two independent additions:

1. `config.py`: a `daemon_lock_path: Path` field, sourced from the
   `APRILCAM_DAEMON_LOCK` environment variable, defaulting to
   `Path("/tmp/aprilcamd.lock")`. This is the fixed, world-writable,
   machine-global path that ticket 002 will `flock()` to enforce the
   singleton guard (see `architecture-update.md` "Decision: fixed path
   `/tmp/aprilcamd.lock`"). `daemon_pidfile` is unchanged — it remains the
   local, per-`socket_dir` pid cache used by `status`/`stop`.
2. `errors.py`: three new exception classes — `DaemonAlreadyRunningError`,
   `DaemonPortInUseError`, `DaemonBindError` — that later tickets raise from
   `daemon/server.py` (002) and `daemon/grpc_server.py` (003), and that
   `daemon/__main__.py` (004) catches.

See `architecture-update.md` "What Changed" (config.py / errors.py bullets)
and "Design Rationale" for the fixed-path reasoning.

## Acceptance Criteria

- [x] `Config` gains a `daemon_lock_path: Path` field, defaulting to
      `Path("/tmp/aprilcamd.lock")` when `APRILCAM_DAEMON_LOCK` is unset.
- [x] `Config.load()` parses `APRILCAM_DAEMON_LOCK` following the same
      priority chain as the other `APRILCAM_*` variables (env wins over
      `.env`/dotfiles/system config), and the field appears in the
      `CONFIG_VARS` table (~line 200) with the same shape as the existing
      `APRILCAM_SOCKET_DIR` / `APRILCAM_DAEMON_HOST` entries (key, field
      name, description, default).
- [x] `daemon_pidfile` is unchanged (still derived in `__post_init__` from
      `socket_dir`); no existing `Config` field is removed or renamed.
- [x] `errors.py` gains `DaemonAlreadyRunningError` with an
      `existing_pid: int | None` attribute set from the constructor,
      following the docstring style of the existing `DaemonNotFoundError`
      (explain cause and remedy).
- [x] `errors.py` gains `DaemonPortInUseError` and `DaemonBindError` as
      distinct exception classes (not aliases of each other), each with a
      short docstring stating which transport (TCP vs. Unix socket) it
      corresponds to.
- [x] A new unit test confirms `Config.load()` resolves `daemon_lock_path`
      from `APRILCAM_DAEMON_LOCK` when set, and falls back to
      `/tmp/aprilcamd.lock` when unset.
- [x] No other module is modified in this ticket — `daemon/server.py`,
      `daemon/grpc_server.py`, `daemon/__main__.py`, and `cli/daemon_cli.py`
      are untouched (their changes are tickets 002-004). `uv run pytest`
      passes unchanged (no new failures, no behavior change in the running
      daemon).

## Implementation Plan

### Approach

Purely additive. Mirror the existing pattern each new piece extends:
`daemon_host`/`daemon_port` in `config.py` for the new field, and
`DaemonNotFoundError` in `errors.py` for the new exceptions' style and
docstring conventions.

### Files to create/modify

- `src/aprilcam/config.py`:
  - Add `daemon_lock_path: Path = field(default_factory=lambda:
    Path("/tmp/aprilcamd.lock"))` near the other daemon-related fields
    (after `daemon_port`, following the "Remote daemon connection (sprint
    014)" comment block style — add a "Machine-global singleton lock
    (sprint 018)" comment block).
  - In `Config.load()`, add resolution of `APRILCAM_DAEMON_LOCK` from
    `sources` (same pattern as `_daemon_host_raw` / `_daemon_port_raw`
    around lines 507-511), converting to `Path`.
  - Add an entry to the `CONFIG_VARS` list (~line 200) with `key:
    "APRILCAM_DAEMON_LOCK"`, matching the dict shape used by neighboring
    entries (e.g. `APRILCAM_SOCKET_DIR` at line 207).
- `src/aprilcam/errors.py`:
  - Add `DaemonAlreadyRunningError(RuntimeError)` with `__init__(self,
    existing_pid: int | None = None)`, storing `self.existing_pid`, and a
    docstring explaining it is raised when a second daemon start finds the
    machine-global lock held and `--force` was not given.
  - Add `DaemonPortInUseError(RuntimeError)` — raised when the TCP transport
    fails to bind because `SO_REUSEPORT` is disabled and the port is
    occupied.
  - Add `DaemonBindError(RuntimeError)` — raised when the Unix socket
    transport fails to bind.
- `tests/test_config.py` (or nearest existing config test file — locate via
  `grep -rl "daemon_host" tests/`): add a test exercising
  `APRILCAM_DAEMON_LOCK` override and default-path fallback, following the
  existing pattern used for `APRILCAM_DAEMON_HOST`/`APRILCAM_DAEMON_PORT`
  tests.

### Testing plan

- New unit test(s) in the config test file: env override resolves to the
  custom path; unset env falls back to `/tmp/aprilcamd.lock`.
- Run the full existing config test suite to confirm no regressions from
  the new field / `CONFIG_VARS` entry.
- `uv run pytest` — full suite green (no other module touched, so no other
  test should be affected).

### Documentation updates

- None required beyond the docstrings already specified above (`Config`
  class docstring's "Key directories" section could optionally mention
  `daemon_lock_path`, but this is not required for this ticket — ticket 002
  is where the field starts being consumed and is the more natural place to
  cross-reference it in the `DaemonServer` docstring).
