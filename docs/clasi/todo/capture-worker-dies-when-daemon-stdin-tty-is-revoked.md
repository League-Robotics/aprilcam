---
status: pending
---

# Capture worker dies when the daemon's stdin TTY is revoked

## Description

Once the Terminal tab that ran `aprilcam daemon start` is closed, macOS
revokes the TTY and the daemon's fd 0 shows as `(revoked)` in `lsof`.
The daemon itself keeps running, but `camera/backends/worker_backend.py`
spawns every capture worker via `posix_spawn` with `close_fds=False`
and no `stdin=` argument, so the child inherits the revoked fd 0 and
CPython aborts in `init_sys_streams` with `OSError: [Errno 9] Bad file
descriptor` before any Python code runs. Every camera open then fails,
forever, including stall-watchdog respawns. Seen 2026-10-15 on a daemon
up since 2026-10-09.

Fix:

- `client/lifecycle.py`: launch the daemon with `stdin=subprocess.DEVNULL`.
- `camera/backends/worker_backend.py`: spawn workers with
  `stdin=subprocess.DEVNULL`.
- Optionally `dup2` `/dev/null` onto fd 0 in `daemon/__main__.py`.

Interim operator workaround: `aprilcam daemon start < /dev/null`.

See `docs/knowledge/2026-10-15-capture-worker-dies-when-daemon-stdin-tty-is-revoked.md`.

## Related: shutdown hangs and `daemon stop` misreports

With workers failing to spawn, SIGTERM shutdown hangs in an unbounded
thread join on the main thread and the lock is never released.
`aprilcam daemon stop` prints the 10 s warning and then "daemon
stopped" anyway, so the next `daemon start` fails with "already
running". `daemon stop` should escalate to SIGKILL after the grace
period and report truthfully; shutdown joins should carry a timeout.
