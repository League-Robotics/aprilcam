"""Camera class wrapping cv.VideoCapture with device metadata."""

from __future__ import annotations

from typing import Optional

import cv2 as cv
import numpy as np

from .camutil import (
    list_cameras,
    get_device_name,
    select_camera_by_pattern,
    default_backends,
)
from ..errors import CameraNotFoundError, CameraError


class Camera:
    """Wraps cv.VideoCapture with lazy open and device metadata.

    The camera is not opened until the first call to :meth:`read` or an
    explicit call to :meth:`open`.  Use as a context manager to ensure
    :meth:`close` is called on exit.
    """

    def __init__(
        self,
        index: int,
        *,
        width: Optional[int] = None,
        height: Optional[int] = None,
        backend: Optional[int] = None,
    ) -> None:
        self._index = index
        self._width = width
        self._height = height
        self._backend = backend
        self._cap: Optional[cv.VideoCapture] = None
        self._name: Optional[str] = None  # lazily cached

    # ------------------------------------------------------------------
    # Class methods
    # ------------------------------------------------------------------

    @classmethod
    def list(cls) -> list[Camera]:
        """Enumerate available cameras and return Camera instances (not opened)."""
        cam_infos = list_cameras()
        return [cls(info.index) for info in cam_infos]

    @classmethod
    def find(cls, pattern: str) -> Camera:
        """Find a camera by name substring.

        Raises :class:`~aprilcam.errors.CameraNotFoundError` if no match is
        found.
        """
        cam_infos = list_cameras()
        index = select_camera_by_pattern(pattern, cam_infos)
        if index is None:
            raise CameraNotFoundError(
                f"No camera matching pattern {pattern!r}"
            )
        return cls(index)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def index(self) -> int:
        """The camera device index."""
        return self._index

    @property
    def name(self) -> str:
        """The OS-reported device name (lazily fetched and cached)."""
        if self._name is None:
            self._name = get_device_name(self._index)
        return self._name

    @property
    def is_open(self) -> bool:
        """True if the underlying VideoCapture is currently open."""
        return self._cap is not None and self._cap.isOpened()

    @property
    def resolution(self) -> tuple[int, int]:
        """Current capture resolution as (width, height).

        Only meaningful after the camera has been opened.

        Raises :class:`~aprilcam.errors.CameraError` if the camera is not
        open.
        """
        if not self.is_open:
            raise CameraError(
                f"Camera {self._index} is not open; call open() or read() first"
            )
        w = int(self._cap.get(cv.CAP_PROP_FRAME_WIDTH))
        h = int(self._cap.get(cv.CAP_PROP_FRAME_HEIGHT))
        return (w, h)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def open(self) -> None:
        """Open the VideoCapture device.

        Safe to call if already open (no-op).

        Raises :class:`~aprilcam.errors.CameraError` if the device cannot be
        opened.
        """
        if self.is_open:
            return

        backends = [self._backend] if self._backend is not None else default_backends()
        cap: Optional[cv.VideoCapture] = None
        for be in backends:
            # DAEMON-ONLY: Camera objects are only instantiated by the daemon's
            # CameraPipeline.  The MCP server never constructs Camera directly;
            # it talks to the daemon via gRPC instead.
            c = cv.VideoCapture(self._index, be)
            if c.isOpened():
                cap = c
                break
            c.release()

        if cap is None or not cap.isOpened():
            raise CameraError(
                f"Failed to open camera at index {self._index}"
            )

        if self._width is not None:
            cap.set(cv.CAP_PROP_FRAME_WIDTH, self._width)
        if self._height is not None:
            cap.set(cv.CAP_PROP_FRAME_HEIGHT, self._height)

        self._cap = cap

    def read(self) -> tuple[bool, Optional[np.ndarray]]:
        """Open the camera if needed, then capture and return a frame.

        Returns a ``(ok, frame)`` tuple matching the ``cv.VideoCapture.read``
        convention.  ``frame`` is ``None`` when ``ok`` is ``False``.
        """
        if not self.is_open:
            self.open()
        ok, frame = self._cap.read()
        if not ok:
            return False, None
        return True, frame

    def close(self) -> None:
        """Release the underlying VideoCapture.

        Safe to call if already closed (no-op).
        """
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> Camera:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Repr
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        status = "open" if self.is_open else "closed"
        return f"Camera(index={self._index}, name={self._name!r}, {status})"
