---
date: 2026-10-15
tags: [daemon, worker-backend, macos, posix_spawn, stdin, tty]
---

# Every camera open fails after the Terminal that started the daemon goes away

## Symptom

Daemon up for days, `daemon status` shows every camera `present usable`,
but **every** `get_frame`/`get_tags`/view fails with:

```
capture worker did not connect to the control channel within 10.0s; the
worker process exited with code 1 (worker stderr: Fatal Python error:
init_sys_streams: can't initialize sys standard streams | ...
OSError: [Errno 9] Bad file descriptor)
```

The worker never reaches Python code at all -- it dies inside
interpreter start-up, before the sprint-038 "connect before importing
cv2" ordering can help.

## Cause

```
$ lsof -p <daemon pid> | awk '$4 ~ /^[012]/'
Python  63631 eric  0   (revoked)
Python  63631 eric  1u  CHR  3,2  /dev/null
Python  63631 eric  2   PIPE ...
```

`client/lifecycle.py` launches the daemon with `stdout=DEVNULL` and a
stderr pipe but **inherits stdin from the Terminal**. When that Terminal
tab/window is later closed, macOS revokes the TTY and the daemon's fd 0
becomes `(revoked)`. The daemon itself never reads stdin, so it keeps
running fine. But `camera/backends/worker_backend.py` spawns each
capture worker via `posix_spawn` with `close_fds=False` and no `stdin=`
argument, so the child inherits the revoked fd 0. CPython's
`init_sys_streams` does `fstat`/`isatty` on fd 0, gets `EBADF`, and
aborts with the fatal error above. Exit code 1, every spawn, forever --
including the stall-watchdog respawns.

This is almost certainly the same mechanism as the "overnight
2026-09-05" spawn failures in
`daemon-restart-launches-pipx-build-no-cameras.md` (child dying before
Python could start, after the host slept) -- at that time the worker's
stderr was not captured, so the reason was never seen.

## Recovery (operator, from Terminal -- never from an agent)

```sh
aprilcam daemon stop && aprilcam daemon start < /dev/null
```

The `< /dev/null` is the workaround until the code fix lands: with
stdin already `/dev/null` there is nothing to revoke. Without it, the
problem returns the next time the launching Terminal tab is closed.

## Durable fix (TODO filed)

- `client/lifecycle.py`: launch the daemon with `stdin=subprocess.DEVNULL`.
- `camera/backends/worker_backend.py`: spawn workers with
  `stdin=subprocess.DEVNULL` too, so the worker never depends on
  whatever fd 0 the daemon happens to hold.
- The daemon's own `__main__` could also `dup2` `/dev/null` onto fd 0
  at start-up for belt-and-braces.

## Tell-tales

- *All* cameras fail, the failure is instant per spawn (exit code 1,
  not a timeout), and `lsof` shows fd 0 `(revoked)`. Contrast the
  pipx-missing-extra case (all cameras `absent`) and the AVFoundation
  wedge (one camera, `no frame available`).

## Follow-on: `daemon stop` then reports "stopped" while the process is still alive

In this state SIGTERM does not finish. The log shows `received signal 15,
shutting down`, then the main thread sits in a `threading` join forever
(`sample <pid>` shows `PyThreadHandleObject_join` on the main thread and
every other thread parked in `pthread_cond_wait`), still holding
`daemon.lock`. `aprilcam daemon stop` waits 10 s, prints "did not release
... within 10.0s of SIGTERM" **and then "daemon stopped"**, which is
false; the immediately following `daemon start` refuses with "a daemon
is already running". Recovery:

```sh
kill -9 <pid> && aprilcam daemon start < /dev/null
```

Two things to fix here, tracked in the same TODO: `daemon stop` should
escalate to SIGKILL after the grace period (or at least not claim
success), and shutdown should not join a thread without a timeout.
