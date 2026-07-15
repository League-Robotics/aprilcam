---
status: draft
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 018 Use Cases

## SUC-001: Second daemon start is refused by default

Parent: UC (daemon lifecycle / camera ownership — see overview.md "Vision
Boundary" and "the daemon is the sole vision authority").

- **Actor**: An operator or an automated process (systemd, a developer
  shell, a stray leftover process from a prior session) attempting to start
  `aprilcamd` on a machine where an `aprilcamd` instance is already running.
- **Preconditions**:
  - An `aprilcamd` process is running and holds the machine-global lock
    (`/tmp/aprilcamd.lock`, or `$APRILCAM_DAEMON_LOCK` if overridden).
  - The second start attempt may use a *different* `socket_dir` (e.g. a
    different uid/runtime environment) than the first — this is the gap
    that exists today and that this sprint closes.
- **Main Flow**:
  1. The actor runs `python -m aprilcam.daemon` (directly, via `aprilcam
     daemon start`, or via a second systemd unit) without `--force`.
  2. The new process attempts to acquire an exclusive flock on the
     machine-global lock path.
  3. The lock is already held by the running daemon; the new process reads
     the existing pid from the lock file.
  4. The new process raises/reports a clear, actionable error identifying
     the existing daemon's pid, and exits non-zero (`sys.exit(3)`) without
     opening any camera, binding any additional port, or otherwise touching
     shared state.
  5. The original daemon is completely unaffected and continues serving.
- **Postconditions**: Exactly one `aprilcamd` process is running on the
  machine. The second invocation's process has exited. No camera device or
  TCP port 5280 has a second listener.
- **Acceptance Criteria**:
  - [ ] A second daemon start with a *different* `socket_dir` but the *same*
        lock path is refused (closes the confirmed gap in the current
        per-user pidfile guard).
  - [ ] The refusal error message includes the existing daemon's pid
        (e.g. "aprilcamd already running (pid N)").
  - [ ] The second process exits with code 3.
  - [ ] With `SO_REUSEPORT` disabled, a second process that somehow reaches
        the TCP bind step (e.g. lock bypassed via misconfiguration) fails
        the bind and raises a distinct, clear error rather than silently
        sharing port 5280 with the kernel load-balancing between the two.
  - [ ] `pgrep -f 'aprilcam.daemon'` shows exactly one process and `lsof -i
        :5280` shows exactly one listener after a refused second-start
        attempt.
  - [ ] A stale lock file left behind by a daemon that died via `kill -9` is
        reclaimed by the next start attempt without manual cleanup and
        without an unlink-based race.

## SUC-002: Operator takes over a running daemon with `--force`

Parent: UC (daemon lifecycle / camera ownership — see overview.md "Vision
Boundary" and "the daemon is the sole vision authority").

- **Actor**: An operator (or systemd on boot/restart) who explicitly wants
  their new daemon instance to replace whatever `aprilcamd` is currently
  running — e.g. after a code deploy, or systemd asserting authority over a
  developer's stray instance.
- **Preconditions**:
  - An `aprilcamd` process is running and holds the machine-global lock.
  - The actor invokes the daemon with the explicit `--force` flag (directly,
    via `aprilcam daemon start --force`, via `aprilcam daemon restart`, or
    via the systemd unit's `ExecStart`, which always passes `--force`).
- **Main Flow**:
  1. The new process attempts the machine-global lock and finds it held.
  2. Because `--force` was given, it reads the existing pid and attempts a
     graceful takeover: first the existing daemon's Shutdown RPC, falling
     back to `SIGTERM` if the RPC is unreachable or times out, escalating to
     `SIGKILL` after a bounded grace period if the process still hasn't
     exited.
  3. The new process waits for the lock to become free (the old process's
     flock is released when it exits, by the kernel, regardless of exit
     path).
  4. The new process acquires the lock, writes its pid, and proceeds to
     start its gRPC server (Unix and/or TCP transports) as normal.
- **Postconditions**: The old daemon process no longer exists. The new
  daemon process holds the machine-global lock and is the sole listener on
  its configured transports. Camera pipelines that the old daemon owned have
  been stopped (via its normal shutdown path) before the new daemon opens
  any camera.
- **Acceptance Criteria**:
  - [ ] `python -m aprilcam.daemon --force` (or `aprilcam daemon start
        --force` / `aprilcam daemon restart`) successfully takes over from a
        running daemon: the old pid is gone, the new pid holds the lock.
  - [ ] The takeover prefers the graceful Shutdown RPC before any signal.
  - [ ] The takeover completes even if the existing daemon is unresponsive
        to the RPC (falls back to SIGTERM, then SIGKILL after a timeout).
  - [ ] After takeover, exactly one `aprilcamd` process is running and one
        listener is bound to port 5280 (`pgrep`/`lsof` checks as in
        SUC-001).
  - [ ] `aprilcam daemon status` reports the new, machine-global pid after
        takeover.
  - [ ] The systemd unit (`deploy/aprilcamd.service`) always launches with
        `--force`, so a systemd (re)start always wins over any other
        instance.
