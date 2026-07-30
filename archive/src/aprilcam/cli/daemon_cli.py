"""CLI subcommand: aprilcam daemon — manage the AprilCam daemon process."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from typing import Optional


def _read_pid(config) -> Optional[int]:
    """Return the daemon PID from the pidfile, or None if unreadable."""
    try:
        return int(config.daemon_pidfile.read_text().strip())
    except Exception:
        return None


def _probe_daemon(resolved_unix: str) -> bool:
    """Return True when a daemon is already listening on the Unix socket."""
    from aprilcam.client.control import DaemonControl
    dc_probe = DaemonControl(unix_path=resolved_unix)
    dc_probe.connect()
    try:
        dc_probe.list_cameras()
        return True
    except Exception:
        return False
    finally:
        dc_probe.close()


def _spawn_daemon(config, log_level: Optional[str] = None, force: bool = False) -> None:
    """Spawn the daemon as a detached background process.

    This is the **only** place in the client code that calls
    ``subprocess.Popen`` to launch the daemon.  All other connection paths
    (``DaemonControl.connect_default``, ``connect_from_args``, etc.) must
    NOT spawn processes — they either connect to a running daemon or raise
    :class:`~aprilcam.errors.DaemonNotFoundError`.

    When ``force`` is True, ``--force`` is appended to the daemon's argv so
    it takes over the machine-global lock (sprint 018) instead of refusing
    to start (this is the defense-in-depth local fallback — the primary
    graceful-takeover sequence runs in :func:`_force_takeover` *before* this
    function is called).
    """
    config.log_dir.mkdir(parents=True, exist_ok=True)
    log_file = open(config.log_dir / "aprilcamd.log", "a")  # noqa: WPS515
    env = os.environ.copy()
    if log_level:
        env["APRILCAM_LOG_LEVEL"] = log_level
    argv = [sys.executable, "-m", "aprilcam.daemon"]
    if force:
        argv.append("--force")
    subprocess.Popen(
        argv,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=log_file,
        env=env,
    )


def _force_takeover(config, resolved_unix: str) -> None:
    """Take over the machine-global lock from a currently-running daemon.

    Sequence (SUC-002): graceful Shutdown RPC first, then SIGTERM, then
    SIGKILL after a bounded timeout, then wait for the global lock
    (``config.daemon_lock_path``) to become free. This is the CLI-level,
    graceful-first takeover — it runs *before* spawning a new daemon with
    ``--force``. ``daemon/server.py``'s own ``force=True`` handling (ticket
    002) is a narrower SIGTERM/SIGKILL-only local fallback for callers that
    construct ``DaemonServer`` directly without going through this CLI; it
    is not duplicated here.

    This function lives in ``daemon_cli.py`` (client side) rather than
    ``daemon/server.py`` because it needs ``DaemonControl`` from
    ``client/``, which ``daemon/server.py`` must not import (daemon stays a
    leaf module — see architecture-update.md's Dependency Graph).
    """
    import fcntl
    import signal

    # Step 1: attempt a graceful Shutdown RPC.
    from aprilcam.client.control import DaemonControl

    dc = DaemonControl(unix_path=resolved_unix)
    dc.connect()
    try:
        dc.list_cameras()  # confirm a daemon is actually there
        try:
            dc.shutdown()
        except Exception:
            pass  # daemon may drop the connection before replying
    except Exception:
        pass  # no daemon reachable via this socket — fall through to pid signals
    finally:
        dc.close()

    # Step 2/3: SIGTERM, then SIGKILL after a bounded grace window, targeting
    # the pid recorded in the local pidfile (best-effort local cache of the
    # machine-global pid).
    pid = _read_pid(config)
    if pid is not None:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pid = None
        except PermissionError:
            pass  # exists but owned by another user; try anyway

    if pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
        else:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.1)
            else:
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass

    # Step 4: wait for the global lock to become free.
    lock_path = config.daemon_lock_path
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if not lock_path.exists():
            break
        try:
            fd = os.open(str(lock_path), os.O_RDWR)
        except OSError:
            break  # lock file gone
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            break  # lock is free — old daemon has fully released it
        except BlockingIOError:
            time.sleep(0.1)
        finally:
            os.close(fd)


def _cmd_start(
    config,
    verbosity: int = 0,
    detach: bool = False,
    unix_path: Optional[str] = None,
    tcp_port: Optional[int] = None,
    force: bool = False,
) -> int:
    """Ensure the daemon is running, spawning it if needed.

    This is the ONE place that is explicitly allowed to spawn the daemon.
    All other client paths connect or raise DaemonNotFoundError.

    When ``force`` is True and a daemon is already up, the graceful-RPC ->
    SIGTERM -> SIGKILL -> wait-for-free takeover sequence
    (:func:`_force_takeover`) runs first, then the new daemon is spawned
    (or run in the foreground) with ``--force``/``force=True`` as a
    defense-in-depth fallback.
    """
    foreground = verbosity > 0 and not detach

    resolved_unix = unix_path or str(config.socket_dir / "control.sock")

    if force and _probe_daemon(resolved_unix):
        _force_takeover(config, resolved_unix)

    if foreground:
        import logging
        level = logging.DEBUG if verbosity >= 2 else logging.INFO
        logging.basicConfig(
            level=level,
            stream=sys.stdout,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        from aprilcam.daemon.server import DaemonServer
        DaemonServer(config, force=force).run()
        return 0

    from aprilcam.client.control import DaemonControl

    # Check if daemon is already up before spawning
    already_up = _probe_daemon(resolved_unix)

    if not already_up:
        log_level = "DEBUG" if verbosity >= 2 else "INFO" if verbosity == 1 else None
        _spawn_daemon(config, log_level=log_level, force=force)

        # Poll until the daemon is ready (OpenCV + gRPC can take 10+ s cold start)
        deadline = time.monotonic() + 20.0
        started = False
        while time.monotonic() < deadline:
            time.sleep(0.1)
            if _probe_daemon(resolved_unix):
                started = True
                break
        if not started:
            print("ERROR: aprilcamd did not start within 20 seconds.", file=sys.stderr)
            return 1

    pid = _read_pid(config)
    pid_str = f"  pid {pid}" if pid else ""

    if already_up:
        print(f"daemon already running{pid_str}")
    else:
        print(f"daemon started{pid_str}  (control socket: {resolved_unix})")

    # List open cameras
    dc = DaemonControl(unix_path=resolved_unix)
    dc.connect()
    try:
        cameras = dc.list_cameras()
        if cameras:
            print(f"open cameras: {', '.join(cameras)}")
        else:
            print("no cameras open")
    except Exception:
        pass
    finally:
        dc.close()

    return 0


def _cmd_status(
    config,
    unix_path: Optional[str] = None,
    tcp_port: Optional[int] = None,
) -> int:
    """Print daemon status: running/stopped, open cameras, data sockets.

    The pid shown is read from ``config.daemon_pidfile``, a ``socket_dir``-
    relative, best-effort local cache of the machine-global daemon's pid —
    it is written by the daemon immediately after it acquires the real
    singleton guard, ``config.daemon_lock_path`` (sprint 018). Under normal
    operation the two always agree; ``daemon_pidfile`` is not itself a
    locking mechanism and is not independently authoritative (see
    architecture-update.md Open Question 4).
    """
    from aprilcam.client.control import DaemonControl

    resolved_unix = unix_path or str(config.socket_dir / "control.sock")

    dc = DaemonControl(unix_path=resolved_unix)
    dc.connect()
    try:
        dc.list_cameras()
    except Exception:
        print("daemon: stopped")
        dc.close()
        return 1

    pid = _read_pid(config)
    pid_str = f"  (pid {pid})" if pid else ""
    print(f"daemon: running{pid_str}")
    print(f"control socket (unix): {resolved_unix}")
    if tcp_port is not None:
        print(f"control socket (tcp):  localhost:{tcp_port}")

    try:
        cameras = dc.list_cameras()
        if not cameras:
            print("cameras: none open")
        else:
            for cam in cameras:
                print(f"  camera: {cam}")
                try:
                    info = dc.get_camera_info(cam)
                    fw, fh = info.frame_size
                    print(f"    frame size  : {fw}x{fh}")
                    print(f"    calibrated  : {info.calibrated}")
                    print(f"    fps         : {info.fps:.1f}")
                except Exception:
                    pass
    except Exception as exc:
        print(f"warning: could not query cameras: {exc}")
    finally:
        dc.close()

    return 0


def _cmd_stop(
    config,
    unix_path: Optional[str] = None,
    tcp_port: Optional[int] = None,
) -> int:
    """Send a shutdown RPC to the running daemon; fall back to SIGTERM by PID."""
    import os
    import signal
    import time
    from aprilcam.client.control import DaemonControl

    resolved_unix = unix_path or str(config.socket_dir / "control.sock")

    # Try clean gRPC shutdown first.
    grpc_ok = False
    dc = DaemonControl(unix_path=resolved_unix)
    dc.connect()
    try:
        dc.list_cameras()
        grpc_ok = True
    except Exception:
        pass

    if grpc_ok:
        try:
            dc.shutdown()
        except Exception:
            pass  # daemon may drop the connection before replying
        finally:
            dc.close()
        print("daemon: shutdown requested")
        return 0

    dc.close()

    # gRPC failed — fall back to SIGTERM via pidfile.
    pid = _read_pid(config)
    if pid is None:
        print("daemon: not running")
        return 0

    try:
        os.kill(pid, 0)  # check process exists
    except ProcessLookupError:
        print("daemon: not running (stale pidfile)")
        return 0
    except PermissionError:
        pass  # process exists but owned by another user; try anyway

    try:
        os.kill(pid, signal.SIGTERM)
        # Wait up to 5 s for the process to exit.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            time.sleep(0.1)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
        print(f"daemon: stopped (pid {pid})")
    except Exception as exc:
        print(f"daemon: could not stop pid {pid}: {exc}")
        return 1

    return 0


def _cmd_restart(
    config,
    verbosity: int = 0,
    detach: bool = False,
    unix_path: Optional[str] = None,
    tcp_port: Optional[int] = None,
) -> int:
    """Stop the daemon if running, then start it.

    ``restart`` always performs a ``--force``-equivalent start for the new
    instance (SUC-002) — it stops the old daemon first via ``_cmd_stop``,
    then unconditionally passes ``force=True`` to ``_cmd_start`` so the new
    instance definitely wins the machine-global lock regardless of whether
    the old daemon stopped cleanly or needed escalation.
    """
    import fcntl
    import time
    _cmd_stop(config, unix_path=unix_path, tcp_port=tcp_port)
    # Wait until the machine-global lock is released — the socket disappears
    # early (gRPC stops accepting) but the old daemon keeps its flock on
    # config.daemon_lock_path while camera pipelines drain.  A new daemon
    # launched before the lock is released would otherwise immediately hit
    # "already running" (or need its own --force escalation).  The lock file
    # is never unlinked (see architecture-update.md), so "free" is
    # determined by successfully acquiring-then-releasing the flock, not by
    # the file's existence.
    lock_path = config.daemon_lock_path
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        time.sleep(0.1)
        if not lock_path.exists():
            break
        try:
            fd = os.open(str(lock_path), os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
                break  # lock is free — old daemon has fully exited
            except BlockingIOError:
                pass
            finally:
                os.close(fd)
        except OSError:
            break  # lock file gone
    return _cmd_start(
        config, verbosity=verbosity, detach=detach,
        unix_path=unix_path, tcp_port=tcp_port, force=True,
    )


def main(argv: Optional[list[str]] = None) -> int:
    from aprilcam.cli._daemon import add_daemon_args

    parser = argparse.ArgumentParser(
        prog="aprilcam daemon",
        description="Manage the AprilCam daemon process",
    )
    sub = parser.add_subparsers(dest="subcmd", metavar="<subcommand>")

    for name, help_text in [
        ("start",   "Start the daemon (no-op if already running)"),
        ("restart", "Stop then start the daemon"),
    ]:
        p = sub.add_parser(name, help=help_text)
        p.add_argument(
            "-v",
            dest="verbosity",
            action="count",
            default=0,
            help="INFO logging, stay in foreground (-vv for DEBUG)",
        )
        p.add_argument(
            "-d", "--detach",
            action="store_true",
            help="Detach even when -v/-vv given; logs go to aprilcamd.log",
        )
        p.add_argument(
            "--unix-path",
            default=None,
            metavar="PATH",
            help="Unix socket path for the daemon control socket",
        )
        p.add_argument(
            "--tcp-port",
            type=int,
            default=None,
            metavar="N",
            help="TCP port the daemon is/will be listening on",
        )
        if name == "start":
            # restart doesn't get its own --force flag: it always performs a
            # --force-equivalent start for the new instance by definition
            # (it already stops the old daemon first), so a redundant flag
            # here would be misleading.
            p.add_argument(
                "-f", "--force",
                action="store_true",
                default=False,
                help=(
                    "Take over the machine-global lock from a running "
                    "daemon instead of refusing to start"
                ),
            )
        add_daemon_args(p)

    for name, help_text in [
        ("status", "Show daemon status and open cameras"),
        ("stop",   "Stop the running daemon"),
    ]:
        p = sub.add_parser(name, help=help_text)
        p.add_argument(
            "--unix-path",
            default=None,
            metavar="PATH",
            help="Unix socket path for the daemon control socket",
        )
        p.add_argument(
            "--tcp-port",
            type=int,
            default=None,
            metavar="N",
            help="TCP port the daemon is listening on",
        )
        add_daemon_args(p)

    args = parser.parse_args(argv)

    if args.subcmd is None:
        parser.print_help()
        return 1

    from aprilcam.config import Config
    config = Config.load()

    unix_path = getattr(args, "unix_path", None)
    tcp_port = getattr(args, "tcp_port", None)

    if args.subcmd == "start":
        return _cmd_start(
            config, verbosity=args.verbosity, detach=args.detach,
            unix_path=unix_path, tcp_port=tcp_port,
            force=getattr(args, "force", False),
        )
    if args.subcmd == "status":
        return _cmd_status(config, unix_path=unix_path, tcp_port=tcp_port)
    if args.subcmd == "stop":
        return _cmd_stop(config, unix_path=unix_path, tcp_port=tcp_port)
    if args.subcmd == "restart":
        return _cmd_restart(
            config, verbosity=args.verbosity, detach=args.detach,
            unix_path=unix_path, tcp_port=tcp_port,
        )

    parser.print_help()
    return 1
