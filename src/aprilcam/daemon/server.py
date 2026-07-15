"""
aprilcam.daemon.server — DaemonServer: gRPC server, machine-global lock, camera registry.

The daemon owns:
  - An exclusive flock on ``config.daemon_lock_path`` (a fixed, machine-global
    path, default ``/tmp/aprilcamd.lock``) — this is the sole singleton guard
    that prevents duplicate daemons anywhere on the machine, regardless of
    ``socket_dir``.  See ``_acquire_global_lock()``.
  - A local pidfile at ``config.daemon_pidfile`` (``socket_dir``-relative), a
    best-effort convenience cache written immediately after the global lock
    is acquired, used by ``daemon status``/``stop`` for local pid discovery.
    It is not itself a locking mechanism.
  - A gRPC server bound to one or both of:
      * Unix domain socket at ``unix_path`` (default /tmp/aprilcam/control.sock)
      * TCP socket at ``0.0.0.0:tcp_port`` (default port 5280)
  - One CameraPipeline per opened camera.

The gRPC servicer (daemon.grpc_server.AprilCamServicer) implements all RPC
methods.  Stream endpoints are managed by ImageStreamProducer / TagStreamProducer
(see daemon.stream).
"""

from __future__ import annotations

import fcntl
import logging
import os
import signal
import threading
import time
from pathlib import Path
from typing import Dict, Optional

from ..config import Config, _has_config_files
from ..errors import DaemonAlreadyRunningError
from .camera_pipeline import CameraPipeline

log = logging.getLogger(__name__)

# Process-lifetime flag: set True the first time _warn_if_legacy_layout()
# fires a warning, so a second DaemonServer instance created in the same
# process (e.g. in tests) does not log it again. See run()'s call site and
# architecture-update.md "Daemon one-time legacy-layout warning".
_legacy_layout_warned = False

_DEFAULT_UNIX_PATH = "/tmp/aprilcam/control.sock"
_DEFAULT_TCP_PORT = 5280


class DaemonServer:
    """Single-process AprilCam daemon.

    Lifecycle::

        server = DaemonServer(config)
        server.run()   # blocks until SIGTERM / SIGINT / Shutdown RPC

    Transport configuration
    -----------------------
    ``unix_enabled``
        When True, the gRPC server binds ``unix://<unix_path>``.
    ``tcp_enabled``
        When True, the gRPC server binds ``[::]:<tcp_port>``.
    ``unix_path``
        Filesystem path for the Unix domain socket
        (default ``/tmp/aprilcam/control.sock``).
    ``tcp_port``
        TCP port number (default 5280).

    At least one of *unix_enabled* or *tcp_enabled* must be True; the
    constructor raises ``ValueError`` if both are False.
    """

    def __init__(
        self,
        config: Config,
        *,
        unix_enabled: bool = True,
        tcp_enabled: bool = True,
        unix_path: str = _DEFAULT_UNIX_PATH,
        tcp_port: int = _DEFAULT_TCP_PORT,
        force: bool = False,
    ) -> None:
        if not unix_enabled and not tcp_enabled:
            raise ValueError(
                "DaemonServer: at least one transport must be enabled "
                "(--unix or --tcp).  Both are currently disabled."
            )

        self._config = config
        self._unix_enabled = unix_enabled
        self._tcp_enabled = tcp_enabled
        self._unix_path = unix_path
        self._tcp_port = tcp_port
        self._force = force

        # Cameras: cam_name → CameraPipeline
        self._cameras: Dict[str, CameraPipeline] = {}
        self._cam_lock = threading.Lock()

        # Shutdown coordination
        self._shutdown_event = threading.Event()

        # Machine-global lock file descriptor (kept open for the lifetime of
        # the process so the flock is held continuously) — the singleton
        # guard.  Distinct from the local pidfile fd below.
        self._lock_fd: Optional[int] = None

        # Local pidfile file descriptor — a convenience cache for
        # status/stop pid discovery, not itself a locking mechanism.
        self._pidfile_fd: Optional[int] = None

        # gRPC server (set in run())
        self._grpc_server = None

        # gRPC servicer (set in run(), kept for shutdown)
        self._servicer = None

        # mDNS advertiser (set in run() when tcp_enabled=True)
        self._mdns_advertiser = None

        # Set once the gRPC server has started — useful for tests / readiness checks
        self.started_event = threading.Event()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Block until shutdown.  Raises ``DaemonAlreadyRunningError`` if a
        daemon already holds the machine-global lock and ``force`` was not
        set at construction time.
        """
        # 0. Warn (at most once per process) if relying on the legacy
        #    config/state single-tree fallback -------------------------------
        self._warn_if_legacy_layout()

        # 1. Acquire the machine-global singleton lock ---------------------
        self._acquire_global_lock()

        # 1b. Write the local pidfile (convenience cache for status/stop) --
        pidfile = self._config.daemon_pidfile
        pidfile.parent.mkdir(parents=True, exist_ok=True)
        pidfile_fd = os.open(str(pidfile), os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o644)
        os.write(pidfile_fd, f"{os.getpid()}\n".encode())
        self._pidfile_fd = pidfile_fd

        # 2. Build gRPC server --------------------------------------------
        from .grpc_server import AprilCamServicer, make_grpc_server

        self._servicer = AprilCamServicer(
            cameras=self._cameras,
            cam_lock=self._cam_lock,
            config=self._config,
            shutdown_event=self._shutdown_event,
        )

        transports = []
        if self._unix_enabled:
            unix_sock_path = Path(self._unix_path)
            unix_sock_path.parent.mkdir(parents=True, exist_ok=True)
            transports.append(f"unix:{self._unix_path}")
        if self._tcp_enabled:
            transports.append(f"[::]:{self._tcp_port}")

        self._grpc_server = make_grpc_server(transports, self._servicer)

        # 3. Install signal handlers (only works from the main thread) ------
        try:
            signal.signal(signal.SIGTERM, self._handle_signal)
            signal.signal(signal.SIGINT, self._handle_signal)
        except ValueError:
            # Called from a non-main thread (e.g. in tests) — skip signal setup.
            log.debug("aprilcamd: signal handlers not installed (non-main thread)")

        # 4. Start gRPC server --------------------------------------------
        self._grpc_server.start()

        addrs = []
        if self._unix_enabled:
            addrs.append(f"unix:{self._unix_path}")
        if self._tcp_enabled:
            addrs.append(f"tcp://[::]:{self._tcp_port}")
        log.info(
            "aprilcamd started (pid=%d, transports=%s)",
            os.getpid(),
            ", ".join(addrs),
        )

        # Signal that the server is ready (for tests and readiness checks)
        self.started_event.set()

        # 5. Start mDNS advertisement (TCP transport only) -----------------
        if self._tcp_enabled:
            from .mdns import MDNSAdvertiser

            self._mdns_advertiser = MDNSAdvertiser()
            self._mdns_advertiser.start(tcp_port=self._tcp_port)

        # 6. Wait for shutdown event --------------------------------------
        # Block until SIGTERM/SIGINT or the Shutdown RPC sets the event.
        self._shutdown_event.wait()

        # Clean shutdown
        self._shutdown()

    # ------------------------------------------------------------------
    # Internal: legacy config/state layout warning
    # ------------------------------------------------------------------

    def _warn_if_legacy_layout(self) -> None:
        """Log one WARNING if this daemon is relying on the legacy config
        fallback (config files co-located under ``data_dir``, none yet under
        the true ``config_dir``).

        Detection-only — performs zero file operations and never moves
        anything (see architecture-update.md "No automatic migration —
        explicit CLI only"). Fires at most once per process, guarded by the
        module-level ``_legacy_layout_warned`` flag, so repeated
        ``DaemonServer`` construction within one process (tests, restarts of
        the object without a new interpreter) does not repeat the log line.
        """
        global _legacy_layout_warned
        if _legacy_layout_warned:
            return

        config = self._config
        if config.config_dir == config.data_dir:
            # Both roots pinned to the same path (e.g. env-forced) — the
            # config/state split doesn't apply; nothing to warn about.
            return

        # Only warn when the operator has NOT explicitly set
        # APRILCAM_CONFIG_DIR — an explicit split-root install (even an
        # empty config_dir, e.g. fresh install with no cameras configured
        # yet) is not "legacy" and should stay silent.
        if config._config_dir_explicit:
            return

        config_has_files = _has_config_files(config.config_dir)
        data_has_files = _has_config_files(config.data_dir)

        if not config_has_files and data_has_files:
            log.warning(
                "legacy config layout detected under %s; run "
                "'aprilcam migrate-dirs' to relocate config files — see docs",
                config.data_dir,
            )
            _legacy_layout_warned = True

    # ------------------------------------------------------------------
    # Internal: machine-global lock
    # ------------------------------------------------------------------

    def _acquire_global_lock(self) -> None:
        """Acquire an exclusive flock on ``config.daemon_lock_path``.

        This is the sole singleton guard: it is machine-global (a fixed
        path, default ``/tmp/aprilcamd.lock``), not ``socket_dir``-relative,
        so it catches duplicate daemons regardless of what ``socket_dir``
        each was launched with.

        Stale-lock recovery is lock-first-then-rewrite: a dead holder's
        flock is already released by the kernel, so a plain
        ``flock(LOCK_EX | LOCK_NB)`` on the *existing* (never recreated)
        file simply succeeds; only then do we ftruncate + write our pid.
        The lock file is never unlinked.

        On contention (``BlockingIOError``): if ``self._force`` is False,
        raise ``DaemonAlreadyRunningError`` carrying the pid read from the
        lock file. If ``self._force`` is True, perform a local
        SIGTERM→SIGKILL takeover of the existing holder and retry once.
        """
        lock_path = self._config.daemon_lock_path
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o666)
        os.fchmod(fd, 0o666)

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            existing_pid = self._read_lock_pid(fd)
            if not self._force:
                os.close(fd)
                log.error(
                    "already running (pid %s), lock=%s",
                    existing_pid,
                    lock_path,
                )
                raise DaemonAlreadyRunningError(existing_pid=existing_pid)

            os.close(fd)
            log.warning(
                "aprilcamd: --force given, taking over lock from pid %s",
                existing_pid,
            )
            self._force_takeover(existing_pid)

            # Retry once, non-forced this time — if it still fails, the
            # takeover did not succeed and we surface the error normally.
            fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o666)
            os.fchmod(fd, 0o666)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                retry_pid = self._read_lock_pid(fd)
                os.close(fd)
                log.error(
                    "aprilcamd: --force takeover failed, lock still held "
                    "(pid %s)",
                    retry_pid,
                )
                raise DaemonAlreadyRunningError(existing_pid=retry_pid)

        # Lock acquired — rewrite the pid.
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._lock_fd = fd

    @staticmethod
    def _read_lock_pid(fd: int) -> Optional[int]:
        """Best-effort read of the pid recorded in an open lock-file fd."""
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            contents = os.read(fd, 64).decode().strip()
            return int(contents) if contents else None
        except (OSError, ValueError):
            return None

    @staticmethod
    def _force_takeover(existing_pid: Optional[int]) -> None:
        """Local half of the --force takeover: SIGTERM, wait, SIGKILL.

        This is the pure ``os``/``signal`` fallback only — it does not send
        the graceful Shutdown RPC first.  That step is layered on top at the
        CLI level (ticket 018-004's ``daemon_cli.py`` takeover helper calls
        ``DaemonControl.shutdown()`` before ever constructing a
        ``force=True`` DaemonServer), so ``daemon/server.py`` stays free of
        a ``client/`` import and remains a leaf module.
        """
        if existing_pid is None:
            return

        try:
            os.kill(existing_pid, signal.SIGTERM)
        except ProcessLookupError:
            return  # already gone
        except OSError as exc:
            log.warning("--force: SIGTERM to pid %d failed: %s", existing_pid, exc)

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                os.kill(existing_pid, 0)
            except ProcessLookupError:
                return  # process exited
            time.sleep(0.1)

        # Still alive after the grace window — escalate.
        try:
            os.kill(existing_pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except OSError as exc:
            log.warning("--force: SIGKILL to pid %d failed: %s", existing_pid, exc)

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                os.kill(existing_pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)

    # ------------------------------------------------------------------
    # Internal: signal handling
    # ------------------------------------------------------------------

    def _handle_signal(self, signum: int, frame: object) -> None:  # noqa: ARG002
        """SIGTERM / SIGINT → request graceful shutdown."""
        log.info("aprilcamd: received signal %d, shutting down", signum)
        self._shutdown_event.set()

    # ------------------------------------------------------------------
    # Internal: shutdown
    # ------------------------------------------------------------------

    def _shutdown(self) -> None:
        """Stop all pipelines, stop gRPC server, release pidfile, exit."""
        log.info("aprilcamd: shutting down")

        # Stop gRPC server (grace=5 s for in-flight RPCs)
        if self._grpc_server is not None:
            try:
                self._grpc_server.stop(grace=5)
            except Exception:
                log.exception("Error stopping gRPC server")
            self._grpc_server = None

        # Stop stream producers (if servicer tracks them)
        if self._servicer is not None:
            try:
                self._servicer.stop_all_producers()
            except Exception:
                log.exception("Error stopping stream producers")
            self._servicer = None

        # Stop camera pipelines
        with self._cam_lock:
            for pipeline in self._cameras.values():
                try:
                    pipeline.stop()
                except Exception:
                    log.exception("Error stopping pipeline %s", pipeline.cam_name)
            self._cameras.clear()

        # Stop mDNS advertisement
        if self._mdns_advertiser is not None:
            try:
                self._mdns_advertiser.stop()
            except Exception:
                log.exception("Error stopping mDNS advertiser")
            self._mdns_advertiser = None

        # Release the global lock and local pidfile
        self._release_pidfile()
        self._release_global_lock()

        log.info("aprilcamd: goodbye")

    def _release_pidfile(self) -> None:
        """Close the local pidfile fd.  Never unlinks the file."""
        if self._pidfile_fd is not None:
            try:
                os.close(self._pidfile_fd)
            except OSError:
                pass
            self._pidfile_fd = None

    def _release_global_lock(self) -> None:
        """Release the machine-global lock: ``LOCK_UN`` + close, no unlink.

        The lock file is intentionally never removed — see
        architecture-update.md "Decision: never unlink the lock file".
        Removing it would reopen a create/lock race window between the
        unlink and the next daemon's open+flock.
        """
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(self._lock_fd)
            except OSError:
                pass
            self._lock_fd = None
