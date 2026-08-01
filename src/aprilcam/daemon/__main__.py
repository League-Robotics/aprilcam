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
import threading
from pathlib import Path
from typing import Dict, Optional

from aprilcam.camera.camera import Camera
from aprilcam.camera.discovery import CameraDiscovery
from aprilcam.camera.registry import CameraRegistry
from aprilcam.config.directories import Directories
from aprilcam.daemon.lifecycle import SOCKET_FILENAME, DaemonLifecycle
from aprilcam.daemon.rpc import build_server
from aprilcam.daemon.service import DaemonService

__all__ = ["build", "main"]

logger = logging.getLogger(__name__)


def build(
    directories: Optional[Directories] = None,
    *,
    lock_path: Optional[Path] = None,
    socket_path: Optional[Path] = None,
    start_watch: bool = True,
) -> DaemonLifecycle:
    """Assemble the full daemon stack -- registry, discovery, service,
    the gRPC control-plane server, and the process-lifecycle wrapper
    around them -- and return the not-yet-started
    :class:`~aprilcam.daemon.lifecycle.DaemonLifecycle`.

    Real callers (``main()`` below) pass no arguments: a fresh
    :class:`Directories` resolves the real system/XDG roots, and
    ``lock_path``/``socket_path`` default to
    ``directories.socket_dir``. Tests override ``directories`` and/or
    ``lock_path``/``socket_path`` to a ``tmp_path`` so they never
    collide with a real running daemon on this machine (this ticket's
    Testing section requirement).

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
    # cache and CameraDiscovery's active_slugs callback -- see
    # daemon/service.py's module docstring for why this is what makes
    # probe()'s non-disturbance guarantee (UC-25.S-7) real for a
    # running daemon rather than just testable in isolation.
    held_cameras: Dict[str, Camera] = {}
    registry = CameraRegistry(directories.state_dir / "cameras")
    discovery = CameraDiscovery(
        registry, active_slugs=lambda: frozenset(held_cameras.keys())
    )
    service = DaemonService(
        registry=registry,
        discovery=discovery,
        directories=directories,
        cameras=held_cameras,
    )

    resolved_socket_path = (
        Path(socket_path) if socket_path is not None else directories.socket_dir / SOCKET_FILENAME
    )

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

    return DaemonLifecycle(
        directories=directories,
        build_listener=functools.partial(build_server, service, resolved_socket_path),
        status_source=service.status,
        watch=enumerate_then_watch if start_watch else None,
        stop_watch=discovery.stop_watch if start_watch else None,
        on_stop=service.close,
        lock_path=lock_path,
        socket_path=socket_path,
    )


def main() -> None:
    """Block until SIGTERM/SIGINT, mirroring v1's ``DaemonServer.run()``
    wait-for-shutdown loop. ``lifecycle.start()`` acquires the singleton
    lock and opens the control plane; starting with zero cameras and
    zero configuration succeeds (UC-01.S-2) -- ``AlreadyRunning``
    propagates to the caller (``client/lifecycle.py``, ticket 020-005)
    as an ordinary uncaught exception, since this process's exit status
    *is* the signal a spawning parent reads.
    """
    logging.basicConfig(level=os.environ.get("APRILCAM_LOG_LEVEL", "INFO"))

    lifecycle = build()
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
