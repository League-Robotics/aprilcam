from __future__ import annotations

import time
from typing import Optional, Tuple

import cv2 as cv
import numpy as np

try:
    import mss  # type: ignore
except Exception:
    mss = None  # type: ignore


class ScreenCaptureMSS:
    """
    cv.VideoCapture-like wrapper that captures the desktop using mss.

    Args:
        monitor: mss monitor index (1 = primary). 0 is the virtual full area across monitors.
        fps: target capture fps (simple sleep to pace grabs).
        region: optional (left, top, width, height) to crop within the chosen monitor's coordinates.
    """

    def __init__(self, monitor: int = 1, fps: float = 30.0, region: Optional[Tuple[int, int, int, int]] = None):
        if mss is None:
            raise RuntimeError("mss is required. Install with: pip install mss")

        self._sct = mss.mss()
        mons = self._sct.monitors
        if not mons or monitor < 0 or monitor >= len(mons):
            raise ValueError(f"Invalid monitor index {monitor}. Available: 0..{len(mons)-1}")
        base = mons[monitor]
        self._mon = {
            "left": int(base["left"]),
            "top": int(base["top"]),
            "width": int(base["width"]),
            "height": int(base["height"]),
        }

        if region is None:
            self._bbox = dict(self._mon)
        else:
            l, t, w, h = map(int, region)
            self._bbox = {"left": int(base["left"] + l), "top": int(base["top"] + t), "width": int(w), "height": int(h)}

        self.width = int(self._bbox["width"])
        self.height = int(self._bbox["height"])
        self._period = 1.0 / max(1e-3, float(fps))
        self._next_t = time.monotonic()
        self._opened = True

    def isOpened(self) -> bool:
        return self._opened

    def read(self):
        if not self._opened:
            return False, np.zeros((self.height, self.width, 3), dtype=np.uint8)

        # Pace to target FPS
        now = time.monotonic()
        if now < self._next_t:
            time.sleep(max(0.0, self._next_t - now))
        self._next_t = time.monotonic() + self._period

        shot = self._sct.grab(self._bbox)  # BGRA
        frame = np.array(shot, dtype=np.uint8)  # HxWx4
        frame = cv.cvtColor(frame, cv.COLOR_BGRA2BGR)
        return True, frame

    def set(self, prop_id: int, value: float) -> bool:
        # Size is fixed by region; ignore
        return True

    def get(self, prop_id: int) -> float:
        if prop_id == getattr(cv, "CAP_PROP_FRAME_WIDTH", 3):
            return float(self.width)
        if prop_id == getattr(cv, "CAP_PROP_FRAME_HEIGHT", 4):
            return float(self.height)
        return 0.0

    def release(self):
        if not self._opened:
            return
        self._opened = False
        try:
            self._sct.close()
        except Exception:
            pass

    # Helper: expose capture bbox in global screen coords
    def get_bbox(self) -> Tuple[int, int, int, int]:
        """Return (left, top, width, height) of the captured region in global screen coordinates."""
        return (
            int(self._bbox["left"]),
            int(self._bbox["top"]),
            int(self._bbox["width"]),
            int(self._bbox["height"]),
        )

    def get_display_rect(self) -> Tuple[int, int, int, int]:
        """Return (left, top, width, height) of the underlying monitor used for capture."""
        return (
            int(self._mon["left"]),
            int(self._mon["top"]),
            int(self._mon["width"]),
            int(self._mon["height"]),
        )
