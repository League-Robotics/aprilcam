"""Camera subclass that reads frames from a video file."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import cv2 as cv
import numpy as np

from .camera import Camera


class VideoCamera(Camera):
    """Camera that reads frames from a video file instead of live hardware.

    Provides the same interface as Camera but reads sequentially from a
    video file. Useful for testing and replay.

    Usage:
        cam = VideoCamera("tests/movies/bright-gsc.mov")
        while True:
            ok, frame = cam.read()
            if not ok:
                break
            # process frame
        cam.close()
    """

    def __init__(self, path: str | Path, *, loop: bool = False):
        # Don't call super().__init__() with an index — we override everything
        self._path = Path(path)
        self._loop = loop
        self._cap: Optional[cv.VideoCapture] = None
        self._name: Optional[str] = None
        self._index = -1  # sentinel for "not a hardware camera"
        self._width: Optional[int] = None
        self._height: Optional[int] = None
        self._backend: Optional[int] = None

        if not self._path.exists():
            raise FileNotFoundError(f"Video file not found: {self._path}")

    @property
    def name(self) -> str:
        """The video filename stem."""
        return self._path.stem

    @property
    def path(self) -> Path:
        """The video file path."""
        return self._path

    def open(self) -> None:
        """Open the video file for reading.

        DEAD-CODE from MCP path: the MCP server never opens video files
        directly.  VideoCamera is used only for offline/test playback.
        """
        if self.is_open:
            return
        cap = cv.VideoCapture(str(self._path))
        if not cap.isOpened():
            raise FileNotFoundError(f"Cannot open video file: {self._path}")
        self._cap = cap

    def read(self) -> tuple[bool, Optional[np.ndarray]]:
        """Read the next frame from the video.

        Returns (False, None) at end of file (unless loop=True).
        """
        if not self.is_open:
            self.open()
        ok, frame = self._cap.read()
        if not ok and self._loop:
            self._cap.set(cv.CAP_PROP_POS_FRAMES, 0)
            ok, frame = self._cap.read()
        if not ok:
            return False, None
        return True, frame

    @property
    def frame_count(self) -> int:
        """Total number of frames in the video."""
        if not self.is_open:
            self.open()
        return int(self._cap.get(cv.CAP_PROP_FRAME_COUNT))

    @property
    def fps(self) -> float:
        """Video frame rate."""
        if not self.is_open:
            self.open()
        return float(self._cap.get(cv.CAP_PROP_FPS))

    def __repr__(self) -> str:
        status = "open" if self.is_open else "closed"
        return f"VideoCamera(path={self._path.name!r}, {status})"
