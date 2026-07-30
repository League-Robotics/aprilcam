"""Frame registry with ring-buffer eviction for server-side image resources.

Provides :class:`FrameEntry` (a mutable dataclass representing a single
captured image with three promotion slots) and :class:`FrameRegistry`
(a thread-safe, dict-backed registry with deterministic IDs and automatic
oldest-first eviction).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np


@dataclass
class FrameEntry:
    """A server-side image resource with three promotion slots.

    Slot promotion logic:
        1. On create — ``original``, ``deskewed``, and ``processed`` all
           reference the same underlying array.  Zero copies.
        2. On deskew — ``deskewed`` is replaced with a new warped array;
           ``processed`` is updated to reference ``deskewed``.
           ``is_deskewed`` is set to ``True``.
        3. On pipeline ops — detection ops read from ``processed`` and
           store results in ``results`` without modifying the array.
           Transform ops write a new array to ``processed``.
    """

    frame_id: str
    source: str  # e.g. "cam_0", "file:/path/to/img.jpg"
    timestamp: float

    # Three image slots
    original: np.ndarray  # Slot 1: raw captured image, never modified
    deskewed: np.ndarray  # Slot 2: deskewed (or reference to original)
    processed: np.ndarray  # Slot 3: pipeline output (starts as ref to slot 2)

    # Metadata
    is_deskewed: bool = False
    operations_applied: List[str] = field(default_factory=list)
    results: Dict[str, Any] = field(default_factory=dict)

    # Detection results cached on the frame
    aruco_corners: Optional[Dict[int, Any]] = None
    apriltags: Optional[List[Any]] = None


class FrameRegistry:
    """Thread-safe dict-backed registry with ring-buffer eviction.

    Frames are stored in an internal dict keyed by deterministic IDs
    (``frm_000``, ``frm_001``, ...) with a :class:`~collections.deque`
    tracking insertion order for oldest-first eviction.

    Parameters
    ----------
    capacity:
        Maximum number of frames to retain.  When the registry is full
        the oldest frame is evicted automatically.  Defaults to 300.
    """

    def __init__(self, capacity: int = 300) -> None:
        if capacity < 1:
            raise ValueError(f"capacity must be >= 1, got {capacity}")
        self._capacity = capacity
        self._frames: dict[str, FrameEntry] = {}
        self._order: deque[str] = deque()
        self._counter: int = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add(
        self,
        raw: np.ndarray,
        source: str,
        timestamp: float | None = None,
    ) -> FrameEntry:
        """Create and store a new :class:`FrameEntry`.

        The three image slots (``original``, ``deskewed``, ``processed``)
        all start as the same object reference — zero copies.

        Parameters
        ----------
        raw:
            The captured image as a NumPy array.
        source:
            A human-readable source identifier (e.g. ``"cam_0"``).
        timestamp:
            Capture time as a Unix timestamp.  Defaults to
            :func:`time.time` if not supplied.

        Returns
        -------
        FrameEntry
            The newly created frame entry.
        """
        if timestamp is None:
            timestamp = time.time()

        with self._lock:
            frame_id = f"frm_{self._counter:03d}"
            self._counter += 1

            # Evict oldest if at capacity
            if len(self._frames) >= self._capacity:
                self._evict_oldest()

            entry = FrameEntry(
                frame_id=frame_id,
                source=source,
                timestamp=timestamp,
                original=raw,
                deskewed=raw,  # same reference
                processed=raw,  # same reference
            )
            self._frames[frame_id] = entry
            self._order.append(frame_id)
            return entry

    def get(self, frame_id: str) -> FrameEntry:
        """Return the frame entry for *frame_id*.

        Raises
        ------
        KeyError
            If *frame_id* is not in the registry (or was evicted).
        """
        with self._lock:
            try:
                return self._frames[frame_id]
            except KeyError:
                raise KeyError(
                    f"Frame '{frame_id}' not found (evicted or never created)"
                ) from None

    def release(self, frame_id: str) -> None:
        """Remove *frame_id* from the registry.

        Raises
        ------
        KeyError
            If *frame_id* is not in the registry.
        """
        with self._lock:
            if frame_id not in self._frames:
                raise KeyError(
                    f"Frame '{frame_id}' not found (evicted or never created)"
                )
            del self._frames[frame_id]
            self._order.remove(frame_id)

    def list_frames(self) -> list[dict]:
        """Return summary dicts for all frames in insertion order.

        Each dict contains: ``frame_id``, ``source``, ``timestamp``,
        ``operations_applied``, ``is_deskewed``.
        """
        with self._lock:
            summaries: list[dict] = []
            for fid in self._order:
                entry = self._frames[fid]
                summaries.append(
                    {
                        "frame_id": entry.frame_id,
                        "source": entry.source,
                        "timestamp": entry.timestamp,
                        "operations_applied": list(entry.operations_applied),
                        "is_deskewed": entry.is_deskewed,
                    }
                )
            return summaries

    def clear(self) -> None:
        """Remove all frames from the registry."""
        with self._lock:
            self._frames.clear()
            self._order.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._frames)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _evict_oldest(self) -> None:
        """Evict the oldest frame.  Caller must hold ``_lock``."""
        oldest_id = self._order.popleft()
        del self._frames[oldest_id]
