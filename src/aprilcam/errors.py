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
