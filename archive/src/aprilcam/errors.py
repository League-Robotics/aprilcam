"""Exception classes for camera and daemon errors."""

from __future__ import annotations


class CameraError(Exception):
    """Base exception for camera errors."""


class CameraNotFoundError(CameraError):
    """Camera index does not exist."""


class CameraInUseError(CameraError):
    """Camera is in use by another process."""

    def __init__(self, message: str, pid: int | None = None, process_name: str | None = None):
        super().__init__(message)
        self.pid = pid
        self.process_name = process_name


class CameraPermissionError(CameraError):
    """Insufficient permissions to access camera."""


class DaemonNotFoundError(RuntimeError):
    """No reachable AprilCam daemon was found.

    Raised by :func:`~aprilcam.client.control.DaemonControl.connect_default`
    and :func:`~aprilcam.client.discovery.resolve_daemon_target` when no
    daemon can be reached via Unix socket, TCP, or mDNS discovery.

    To resolve, start the daemon with::

        aprilcam daemon start

    or::

        systemctl start aprilcamd

    or set the ``APRILCAM_DAEMON_HOST`` environment variable to point to a
    remote daemon.
    """


class DaemonAlreadyRunningError(RuntimeError):
    """Another aprilcamd instance already holds the machine-global lock.

    Raised by :meth:`~aprilcam.daemon.server.DaemonServer._acquire_global_lock`
    (sprint 018) when a second daemon start finds ``config.daemon_lock_path``
    (default ``/tmp/aprilcamd.lock``) held by another process and ``--force``
    was not given. This guard is machine-global — it applies regardless of
    ``socket_dir``, so it catches the case of two daemons launched with
    different ``socket_dir`` values (e.g. a systemd instance and an ad hoc
    developer run) that would otherwise coexist undetected.

    To resolve, stop the existing daemon first::

        aprilcam daemon stop

    or restart with the lock's current holder replaced::

        aprilcam daemon start --force
    """

    def __init__(self, existing_pid: int | None = None):
        message = (
            f"aprilcamd already running (pid {existing_pid})"
            if existing_pid is not None
            else "aprilcamd already running"
        )
        super().__init__(message)
        self.existing_pid = existing_pid


class DaemonPortInUseError(RuntimeError):
    """The daemon's gRPC TCP transport failed to bind.

    Raised by :func:`~aprilcam.daemon.grpc_server.make_grpc_server` (sprint
    018) when ``add_insecure_port()`` returns 0 for a TCP address (e.g.
    ``[::]:5280``) with ``SO_REUSEPORT`` disabled. This means the TCP port is
    already occupied — either by another aprilcamd instance that bypassed the
    machine-global lock, or by an unrelated process. Check ``lsof -i
    :5280`` (or the configured ``APRILCAM_DAEMON_PORT``) to identify the
    occupant.
    """


class DaemonBindError(RuntimeError):
    """The daemon's gRPC Unix-domain-socket transport failed to bind.

    Raised by :func:`~aprilcam.daemon.grpc_server.make_grpc_server` (sprint
    018) when ``add_insecure_port()`` returns 0 for a Unix socket address
    (e.g. ``unix:///run/aprilcam/aprilcamd.sock``). Check that
    ``config.socket_dir`` is writable and that no stale socket file blocks
    the bind.
    """
