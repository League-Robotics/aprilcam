"""Process entry point: wires ``daemon/lifecycle.py`` + ``daemon/
service.py`` + ``daemon/rpc.py`` together into a running daemon
(ticket 020-004).

Run directly as ``python -m aprilcam.daemon`` -- this is exactly what
``python -m aprilcam.daemon`` invokes (package ``__main__`` module
convention), and exactly what ticket 020-005's client-side
``client/lifecycle.py`` is expected to ``subprocess.Popen`` when the
CLI runs ``aprilcam daemon start`` (see ``daemon/lifecycle.py``'s
module docstring for the client-side/server-side ``DaemonLifecycle``
split this depends on).

:func:`build` is the single place "what a real daemon process wires
together" is defined -- both this module's :func:`main` and this
ticket's ``@pytest.mark.camera`` integration tests call it, so a test
exercises the exact same assembly a real ``python -m aprilcam.daemon``
run does, just against a ``tmp_path`` instead of the real system
directories.
"""

from __future__ import annotations

import functools
import logging
import os
import signal
import sys
import threading
from pathlib import Path
from typing import Dict, List, Optional

from aprilcam.camera.camera import Camera
from aprilcam.camera.discovery import CameraDiscovery
from aprilcam.camera.presence import LinuxSysfsPresenceSource
from aprilcam.camera.registry import CameraRegistry
from aprilcam.config.directories import DEFAULT_DAEMON_PORT, Directories
from aprilcam.config.loader import ConfigLoader
from aprilcam.daemon.lifecycle import SOCKET_FILENAME, DaemonLifecycle
from aprilcam.daemon.mdns import MdnsAdvertiser
from aprilcam.daemon.rpc import build_server, build_stream_transport
from aprilcam.daemon.service import DaemonService

__all__ = ["build", "main"]

logger = logging.getLogger(__name__)


def build(
    directories: Optional[Directories] = None,
    *,
    lock_path: Optional[Path] = None,
    socket_path: Optional[Path] = None,
    start_watch: bool = True,
    tcp_port: Optional[int] = DEFAULT_DAEMON_PORT,
) -> DaemonLifecycle:
    """Assemble the full daemon stack -- registry, discovery, service,
    the gRPC control-plane server, mDNS self-advertisement, and the
    process-lifecycle wrapper around them -- and return the
    not-yet-started :class:`~aprilcam.daemon.lifecycle.DaemonLifecycle`.

    Real callers (``main()`` below) pass no arguments: a fresh
    :class:`Directories` resolves the real system/XDG roots, and
    ``lock_path``/``socket_path`` default to
    ``directories.socket_dir``. Tests override ``directories`` and/or
    ``lock_path``/``socket_path`` to a ``tmp_path`` so they never
    collide with a real running daemon on this machine (this ticket's
    Testing section requirement).

    ``tcp_port`` (ticket 028-003) defaults to
    ``config.directories.DEFAULT_DAEMON_PORT`` (5280, spec §4.1) --
    every real caller gets the documented control-plane port with no
    extra argument, mirroring the existing ``lock_path``/``socket_path``
    override pattern. Passed straight through to
    :func:`~aprilcam.daemon.rpc.build_server` via the
    ``functools.partial`` below; ``None`` disables the TCP bind
    entirely, preserving unix-socket-only behavior for a caller that
    wants it (e.g. a test that would otherwise collide with a real
    daemon's 5280, or a genuinely socket-only deployment). Binding
    failure is never fatal here either -- see ``build_server``'s own
    docstring -- so passing the default is always safe even when a
    real daemon (or this ticket's two-local-daemon test scenario) has
    already taken the port; :class:`~aprilcam.daemon.lifecycle.
    DaemonLifecycle` simply will not call ``advertise`` in that case.

    **Boot-time config load (ticket 023-003).** Immediately after
    *service* is constructed and before the control-plane listener is
    ever built, this reads *directories*.config_dir via
    :func:`_load_config_dir` and feeds every document found there into
    *service* through its already-existing ``set_playfield``/
    ``set_camera_config`` setters -- the fix for the restart trap sprint
    022 hit live (UC-01.S-4): a playfield and a camera config installed
    once in ``config_dir`` come back linked and ready on every
    subsequent start, with no RPC push required. A malformed individual
    file is skipped and logged at WARNING, never refused (sprint.md
    Design Rationale, Decision 1) -- see
    ``config.loader.ConfigLoader.load_config_dir``'s docstring.

    ``start_watch`` (default ``True``) controls both halves of UC-01's
    "enumerate, then keep watching" startup step -- the one-time active
    probe run synchronously inside ``start()`` (see
    ``enumerate_then_watch`` below) and the background passive-hotplug
    thread it hands off to. Tests that want a fully inert
    ``DaemonLifecycle`` with zero camera-subsystem activity (none in
    this ticket's suite need that) pass ``start_watch=False``.
    """
    directories = directories or Directories()

    # The same dict object backs both the service's held-open-camera
    # cache and CameraDiscovery's active_camera_state callback -- see
    # daemon/service.py's module docstring for why this is what makes
    # probe()'s non-disturbance guarantee (UC-25.S-7) real for a
    # running daemon rather than just testable in isolation, and
    # camera-DESIGN.md sprint 039's "Honest active-camera health" section
    # for why probe()/list_cameras() must read the same live Camera.state
    # instead of assuming a held-open camera is healthy.
    held_cameras: Dict[str, Camera] = {}
    registry = CameraRegistry(directories.state_dir / "cameras")
    # Sprint 047: on Linux, a fast /sys poll notices held cameras
    # vanishing/returning; the service drops/reopens them.
    presence_source = LinuxSysfsPresenceSource() if sys.platform.startswith("linux") else None
    service_ref: List[DaemonService] = []
    discovery = CameraDiscovery(
        registry,
        # "In use" means a live stream subscriber or an in-flight read,
        # not merely "cached" (sprint 047 ticket 003).
        active_camera_state=lambda slug: service_ref[0].active_camera_state(slug),
        presence_source=presence_source,
        on_presence=lambda slug, present: service_ref[0].on_hardware_presence(slug, present),
    )
    service = DaemonService(
        registry=registry,
        discovery=discovery,
        directories=directories,
        cameras=held_cameras,
    )

    service_ref.append(service)

    _load_config_dir(service, directories)

    resolved_socket_path = (
        Path(socket_path) if socket_path is not None else directories.socket_dir / SOCKET_FILENAME
    )

    # Ticket 027-005: the raw-socket stream transport (GetImageStream/
    # GetTagStream's endpoint allocation, plus the accept loops/sender
    # threads those endpoints point at). Constructing this touches no
    # socket, thread, or camera at all -- see StreamTransportManager's
    # own docstring -- it only starts binding listeners once a real
    # GetImageStream/GetTagStream RPC asks for one, matching every other
    # idle-gating guarantee this daemon makes.
    transport = build_stream_transport(service, directories.socket_dir)

    # Ticket 028-003: constructing this touches no socket or thread --
    # MdnsAdvertiser.start() is what actually opens the multicast
    # socket, and that only happens once DaemonLifecycle.start() calls
    # the advertise hook below, itself gated on a real bound TCP port
    # (see DaemonLifecycle.start()'s own docstring). No extra gating
    # logic belongs here beyond that -- duplicating the "was TCP
    # actually bound" check in this module would just be a second,
    # independently-maintained copy of the same decision.
    advertiser = MdnsAdvertiser()

    def _stop_service() -> None:
        """DaemonLifecycle's on_stop hook: tear down the stream
        transport (every listener, every accepted connection, every
        accept/sender thread) before the service itself closes its
        held cameras -- so no socket-driven subscriber outlives a clean
        shutdown (sprint.md Migration Concerns' "Clean daemon shutdown",
        extended through this transport layer by ticket 005)."""
        service.stop_control_watchdog()
        transport.stop()
        service.close()

    def enumerate_then_watch() -> None:
        """UC-01 main flow steps 3-4, in order: an initial **active**
        probe ("enumeration is an active interrogation of every
        backend", not just a device listing) so ``list_cameras()``
        answers correctly the instant ``start()`` returns, then begin
        passive hotplug watching for as long as the daemon runs.
        Without the synchronous probe here, a caller that queries
        ``status()``/``list_cameras()`` immediately after ``start()``
        would race ``watch()``'s own first background poll -- real
        camera enumeration (``system_profiler`` on macOS, subprocess
        probes elsewhere) is not instant.
        """
        discovery.probe()
        discovery.watch()
        # UVC-drift watchdog (2026-09-03): re-assert applied controls when
        # the hardware silently reverts them (USB re-enumeration etc.).
        service.start_control_watchdog()

    return DaemonLifecycle(
        directories=directories,
        build_listener=functools.partial(
            build_server, service, resolved_socket_path, transport=transport, tcp_port=tcp_port
        ),
        status_source=service.status,
        watch=enumerate_then_watch if start_watch else None,
        stop_watch=discovery.stop_watch if start_watch else None,
        advertise=advertiser.start,
        stop_advertise=advertiser.stop,
        on_stop=_stop_service,
        lock_path=lock_path,
        socket_path=socket_path,
    )


def _load_config_dir(service: DaemonService, directories: Directories) -> None:
    """UC-01 main flow steps 2-3, and ticket 023-003's whole point:
    read every document already installed under *directories*.config_dir
    and feed it into *service* via its already-existing
    ``set_playfield``/``set_camera_config`` setters -- **not** a new
    ``DaemonService`` method (sprint.md Architecture, "Modules" point 1:
    this assembly module owns "where documents come from"; ``DaemonService``
    stays ignorant of disk entirely).

    A camera config loaded here for a camera not yet held open does
    nothing to hardware immediately -- there is no open handle to push
    settings to (exactly ``set_camera_config``'s existing "camera not
    currently held open" branch). It still takes effect the moment that
    camera *is* first opened, via ``DaemonService._camera_for``'s own
    apply-on-open step (this same ticket) -- so a camera enumerated
    after this function returns, or never opened until a client asks
    for it, still ends up configured exactly as if the document had
    just been pushed by hand.

    Every per-file failure is caught inside
    ``ConfigLoader.load_config_dir`` itself (never raised here) and
    logged at WARNING, naming the file and the reason -- skip-and-warn,
    not refuse-to-start (Design Rationale, Decision 1), matching the
    existing precedent in ``DaemonService._load_calibrations``.
    """
    scan = ConfigLoader().load_config_dir(directories)
    for error in scan.errors:
        logger.warning(
            "config_dir: skipping malformed config file %s: %s", error.path, error.message
        )
    for playfield in scan.playfields:
        service.set_playfield(playfield)
    for camera_config in scan.camera_configs:
        service.set_camera_config(camera_config)


#: Daemon log file name under ``state_dir`` (2026-09-04, out of process):
#: ``aprilcam daemon start`` spawns the daemon with its stderr piped to a
#: parent that exits as soon as the socket is up, so until this file
#: existed every warning the daemon logged after startup was lost --
#: every camera-wedge investigation was done blind. Rotated at 5 MB x 3.
LOG_FILENAME = "aprilcamd.log"
_LOG_MAX_BYTES = 5 * 1024 * 1024
_LOG_BACKUPS = 3


def configure_logging(state_dir: Path) -> Path:
    """Root logging to stderr *and* ``<state_dir>/aprilcamd.log``
    (rotating), level from ``APRILCAM_LOG_LEVEL`` (default INFO).
    Idempotent: a second call on the same process adds no duplicate
    handlers. Returns the log file path. A state dir that cannot be
    written falls back to stderr only rather than refusing to start."""
    import logging.handlers

    level = os.environ.get("APRILCAM_LOG_LEVEL", "INFO")
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    root = logging.getLogger()
    root.setLevel(level)
    log_path = state_dir / LOG_FILENAME
    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler) for h in root.handlers):
        stream = logging.StreamHandler()
        stream.setFormatter(logging.Formatter(fmt))
        root.addHandler(stream)
    already = any(
        isinstance(h, logging.handlers.RotatingFileHandler)
        and Path(getattr(h, "baseFilename", "")) == log_path
        for h in root.handlers
    )
    if not already:
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                log_path, maxBytes=_LOG_MAX_BYTES, backupCount=_LOG_BACKUPS, encoding="utf-8"
            )
            fh.setFormatter(logging.Formatter(fmt))
            root.addHandler(fh)
        except OSError as exc:
            root.warning("cannot open daemon log file %s: %s -- logging to stderr only", log_path, exc)
    return log_path


def main() -> None:
    """Block until SIGTERM/SIGINT, mirroring v1's ``DaemonServer.run()``
    wait-for-shutdown loop. ``lifecycle.start()`` acquires the singleton
    lock and opens the control plane; starting with zero cameras and
    zero configuration succeeds (UC-01.S-2) -- ``AlreadyRunning``
    propagates to the caller (``client/lifecycle.py``, ticket 020-005)
    as an ordinary uncaught exception, since this process's exit status
    *is* the signal a spawning parent reads.
    """
    log_path = configure_logging(Directories().state_dir)

    lifecycle = build()
    logger.info("aprilcamd logging to %s", log_path)
    lifecycle.start()
    logger.info(
        "aprilcamd ready (pid=%d): socket=%s", os.getpid(), lifecycle.socket_path
    )

    shutdown = threading.Event()

    def _handle_signal(signum: int, frame: object) -> None:  # noqa: ARG001
        logger.info("aprilcamd: received signal %d, shutting down", signum)
        shutdown.set()

    try:
        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)
    except ValueError:
        # Not the main thread (e.g. embedded in a test) -- skip signal
        # setup; the caller drives shutdown by calling lifecycle.stop()
        # directly instead.
        logger.debug("aprilcamd: signal handlers not installed (non-main thread)")

    shutdown.wait()
    lifecycle.stop()
    logger.info("aprilcamd: goodbye")


if __name__ == "__main__":
    main()
