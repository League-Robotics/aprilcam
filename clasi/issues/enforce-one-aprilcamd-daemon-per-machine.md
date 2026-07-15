---
status: pending
---

# Enforce one aprilcamd daemon per machine

## Context

The AprilCam MCP-server/daemon debugging session that prompted this exposed
that **multiple `aprilcamd` daemons can run on one machine simultaneously** —
at one point 8 MCP servers were live against a confused mix of daemon
instances (some from different venvs), which made a hardware bug nearly
impossible to diagnose because code fixes appeared not to "take" (calls were
being served by stale daemons). The daemon is meant to be the single vision
authority and sole camera owner; duplicate daemons contend for the camera and
scatter state.

**Goal:** guarantee that only ONE `aprilcamd` runs per machine. A second start
must **refuse by default** (clear error, non-zero exit) and only take over the
existing daemon when given an explicit **`--force`**.

Two independent, mutually-reinforcing guards (both portable to macOS and
Linux/Raspberry Pi — the daemon runs on both):

1. A **machine-global `flock`** on a fixed path `/tmp/aprilcamd.lock`.
2. **Disabling gRPC `SO_REUSEPORT`** on the TCP port and detecting bind failure.

## Root cause (confirmed in code)

- **Singleton guard today is per-USER, not per-machine.** `server.py:108-141`
  takes an exclusive `fcntl.flock` on `config.daemon_pidfile`
  (`<socket_dir>/aprilcamd.pid`), but `socket_dir` is uid/runtime-namespaced
  (`config.py:_default_dirs()` 310-350: `/run/aprilcam` for root, else
  `$XDG_RUNTIME_DIR/aprilcam` / `/run/user/<uid>/aprilcam` /
  `$TMPDIR/aprilcam-<uid>`). Two daemons with different socket_dirs (e.g.
  systemd `/run/aprilcam` vs a user's `$TMPDIR/aprilcam-501`) both flock
  successfully and coexist.
- **TCP `SO_REUSEPORT` (primary cause).** `grpc_server.py:make_grpc_server`
  (1112-1147) calls `server.add_insecure_port(f"[::]:{tcp_port}")` bare, with
  no `grpc.server(..., options=...)`. gRPC C-core defaults to
  `GRPC_ARG_ALLOW_REUSEPORT=1`, so N daemons bind `[::]:5280` with zero error
  and the kernel load-balances between them. The `add_insecure_port` return
  (0 == bind failed) is also discarded (line 1145).

## Approach

Fixed lock path **`/tmp/aprilcamd.lock`** — the only portable, all-users-lockable
location (macOS has no `/run`; `/var/run` needs root; `$XDG_RUNTIME_DIR`/`$TMPDIR`
are per-user, which is the current bug). Created mode `0666` (via `os.fchmod`)
so any daemon-running user can rewrite the pid; `flock` is advisory and works
regardless of the pid text's owner, and the OS auto-releases it on process death
(preserving `kill -9` recovery). Overridable via `APRILCAM_DAEMON_LOCK` — the
sanctioned escape hatch for tests to run isolated daemons (paired with
`APRILCAM_DAEMON_PORT`).

## Ordered changes

**1. `config.py`** — add a `daemon_lock_path` field (env `APRILCAM_DAEMON_LOCK`,
default `/tmp/aprilcamd.lock`), parse in `load()`, register in the config-vars
table alongside the other `APRILCAM_*` entries. Keep `daemon_pidfile`
(`__post_init__` 428-430) for local `status`/`stop` pid discovery.

**2. `errors.py`** — add `DaemonAlreadyRunningError` (carries `existing_pid`),
`DaemonPortInUseError`, `DaemonBindError`.

**3. `daemon/server.py`** — replace the per-user pidfile guard (108-141) with a
`_acquire_global_lock()` on `daemon_lock_path`:
   - `os.open(O_RDWR|O_CREAT, 0o666)` + `os.fchmod` to 0666, then
     `flock(LOCK_EX|LOCK_NB)`.
   - On `BlockingIOError` (held): read the pid from the lock file; if `force`
     is set, run the takeover sequence (below) then retry; else raise
     `DaemonAlreadyRunningError(existing_pid)`.
   - Replace the racy "unlink + retry" stale branch with lock-first-then-rewrite:
     a stale file's advisory lock is already gone on the dead process, so a
     fresh `flock` simply succeeds; then `ftruncate`+write our pid. Do NOT
     unlink the lock file on shutdown (avoids a create/lock race) — keep the
     existing pidfile-release reasoning (`_release_pidfile` 257-267: `LOCK_UN`
     + close, no unlink).
   - Keep writing the local `daemon_pidfile` too (for `status`/`stop`).
   - Add `force: bool` to `DaemonServer.__init__`.
   - Change `run()` so a refused start **raises** (rather than the current
     silent `return`) so callers/systemd see failure.

**4. `daemon/grpc_server.py`** (`make_grpc_server` 1112-1147) — pass
`options=[("grpc.so_reuseport", 0)]` to `grpc.server(...)`; capture each
`server.add_insecure_port(addr)` return value; if a TCP bind returns 0, raise
`DaemonPortInUseError`. (Unix-socket bind failure → `DaemonBindError`; gRPC
handles unlinking a stale unix path itself.)

**5. `daemon/__main__.py`** — add `--force` to argparse; thread into
`DaemonServer(config, force=args.force)`; wrap `run()` to catch
`DaemonAlreadyRunningError`/`DaemonPortInUseError`/`DaemonBindError`, print a
clear "aprilcamd already running (pid N)" message, and `sys.exit(3)` (real
non-zero for systemd + CLI).

**6. `cli/daemon_cli.py`** —
   - Add `--force` to the `daemon start` subparser; thread through
     `_cmd_start` → `_spawn_daemon` (currently 35-55, passes no extra args) so
     the detached spawn gets `-m aprilcam.daemon --force`.
   - Make `restart` effectively `start --force`.
   - Point the restart wait-for-free loop (~256-284) at the global lock.
   - `status` reports the machine-global pid from the lock file.

**7. `--force` takeover sequence** (in `server.py` or a helper): prefer the
existing graceful **Shutdown RPC** (`DaemonControl.shutdown()`) → fall back to
`SIGTERM` on the lock-file pid → `SIGKILL` after a timeout → wait for the flock
to free (reuse the `_cmd_restart` wait pattern) → acquire.

**8. `deploy/aprilcamd.service`** — add `--force` to `ExecStart` (systemd should
win on boot) and a comment that **`PrivateTmp` must stay OFF** (a private `/tmp`
would defeat the shared lock).

## Tests (extend `tests/test_daemon_server.py`, no hardware)

Use unique tmp lock paths (`APRILCAM_DAEMON_LOCK`) and free ephemeral ports.

- **(a)** second daemon with a DIFFERENT `socket_dir` but the SAME lock path is
  refused — the gap that exists today.
- **(b)** `SO_REUSEPORT` disabled → second TCP bind on the same port raises
  `DaemonPortInUseError`.
- **(c)** `--force` takes over a running daemon (old one stops, new one holds
  the lock).
- **(d)** refuse-by-default raises `DaemonAlreadyRunningError` / exits code 3
  with "already running (pid N)". **Replaces** the existing
  `test_pidfile_lock_prevents_duplicate` (test_daemon_server.py:164), which
  only covers the shared-pidfile case.
- **(e)** stale lock file after `kill -9` is reclaimed without unlink.

## Risks / notes

- **Lock permissions across users:** create 0666 via `fchmod`; flock succeeds
  regardless of pid-text owner.
- **Behavior change:** port 5280 held by an unrelated process now causes a
  correct refusal — document it. Skipped when running `--unix`-only (no TCP).
- **systemd `PrivateTmp` must remain off**; CI must never touch the real
  `/tmp/aprilcamd.lock` or bind 5280 (tests override via env).
- **Legacy `daemon/client.py:ensure_running()`** also Popens the daemon but has
  no callers in `src/` — leave as-is; the new guard protects it anyway if
  re-wired.

## Verification (end-to-end)

1. `uv run pytest tests/test_daemon_server.py` — new singleton tests pass.
2. Manual: start a daemon (`aprilcam daemon start`); attempt a second
   `python -m aprilcam.daemon` → refused, exit 3, "already running (pid N)".
3. `python -m aprilcam.daemon --force` → takes over (old pid gone, new pid holds
   `/tmp/aprilcamd.lock`).
4. Confirm only ONE `pgrep -f 'aprilcam.daemon'` after each step, and one
   listener on `:5280` (`lsof -i :5280`).
5. `aprilcam daemon status` reports the single machine-global pid.
6. Full suite: `uv run pytest` green; `dotconfig version bump` at sprint close.

## Process

Full CLASI sprint (stakeholder-selected): create sprint → sprint-planner writes
architecture-update + usecases + tickets → stakeholder approval → execute →
review → close.
