"""Path data model and registry for agent-drawn paths on the live playfield view.

A *path* is an ordered list of waypoints submitted by an agent in world
coordinates (cm).  The server renders the path on the live deskewed playfield
display each frame using OpenCV primitives.

Symbol set (exactly 8 values)
------------------------------
``"square"``         — hollow square outline
``"filled_square"``  — solid square
``"circle"``         — hollow circle outline
``"filled_circle"``  — solid circle
``"triangle"``       — hollow triangle outline
``"filled_triangle"``— solid triangle
``"x"``              — two crossing lines (intrinsically outlined; no fill variant)
``"none"``           — no symbol drawn at the waypoint; the waypoint is still
                       a vertex that connecting lines pass through, but it is
                       invisible itself (``symbol_color`` is unused)

Colors are stored as RGB triples throughout the data model and registry.
The RGB→BGR conversion required by OpenCV happens at the draw site only.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, asdict
from typing import Dict, List, Literal, Optional, Tuple

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

Symbol = Literal[
    "square",
    "filled_square",
    "circle",
    "filled_circle",
    "triangle",
    "filled_triangle",
    "x",
    "none",
]

RGB = Tuple[int, int, int]  # 0..255 per channel, agent-facing (not BGR)

#: The set of all valid symbol strings.  Exported for use by validation logic
#: in the MCP tool layer (T002).
VALID_SYMBOLS: frozenset[str] = frozenset(
    {
        "square",
        "filled_square",
        "circle",
        "filled_circle",
        "triangle",
        "filled_triangle",
        "x",
        "none",
    }
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Waypoint:
    """A single stop along a path, in world coordinates.

    Parameters
    ----------
    x:
        World X coordinate in cm (origin upper-left of the playfield).
    y:
        World Y coordinate in cm.
    size_cm:
        Physical extent of the symbol, in cm.  Must be > 0.
    symbol:
        Which symbol to draw at this waypoint.
    symbol_color:
        RGB color of the symbol (unused when ``symbol`` is ``"none"``).
    line_color:
        RGB color of the line segment drawn *from* this waypoint to the next
        one.  Ignored for the last waypoint in the path.
    """

    x: float
    y: float
    size_cm: float
    symbol: Symbol
    symbol_color: RGB
    line_color: RGB


@dataclass
class Path:
    """A named sequence of waypoints attached to a playfield.

    Parameters
    ----------
    path_id:
        Monotonic server-assigned identifier (``"path_000"``, …).
    playfield_id:
        The playfield this path belongs to.
    waypoints:
        Ordered list of :class:`Waypoint` objects.
    name:
        Optional human-readable display label. Defaults to ``""``; the viewer
        falls back to ``path_id`` when blank.
    """

    path_id: str
    playfield_id: str
    waypoints: List[Waypoint]
    name: str = ""

    def to_dict(self) -> dict:
        """Return a plain dict that serialises cleanly with :func:`json.dumps`.

        Colors (``symbol_color``, ``line_color``) are emitted as lists of
        three ints.  Waypoints are emitted as nested dicts.  No NumPy types
        or non-JSON-serialisable objects are included.
        """
        return {
            "path_id": self.path_id,
            "playfield_id": self.playfield_id,
            "name": self.name,
            "waypoints": [
                {
                    "x": wp.x,
                    "y": wp.y,
                    "size_cm": wp.size_cm,
                    "symbol": wp.symbol,
                    "symbol_color": list(wp.symbol_color),
                    "line_color": list(wp.line_color),
                }
                for wp in self.waypoints
            ],
        }


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class PathRegistry:
    """Thread-safe, dict-backed registry for :class:`Path` objects.

    IDs are monotonically increasing: ``path_000``, ``path_001``, …
    Zero-padded to three digits; naturally grows beyond three digits when
    the counter exceeds 999 (``f"path_{n:03d}"``).

    There is no capacity limit and no eviction — paths accumulate until
    explicitly deleted via :meth:`delete` or :meth:`clear_for`.
    """

    def __init__(self) -> None:
        self._paths: Dict[str, Path] = {}
        self._counter: int = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create(self, playfield_id: str, waypoints: List[Waypoint], name: str = "") -> Path:
        """Create a new path and store it in the registry.

        Parameters
        ----------
        playfield_id:
            The playfield this path is attached to.
        waypoints:
            Ordered list of waypoints for the path.
        name:
            Optional human-readable display label.  Defaults to ``""``.

        Returns
        -------
        Path
            The newly created path with a monotonic ``path_id``.
        """
        with self._lock:
            path_id = f"path_{self._counter:03d}"
            self._counter += 1
            path = Path(path_id=path_id, playfield_id=playfield_id, waypoints=list(waypoints), name=name)
            self._paths[path_id] = path
            return path

    def delete(self, path_id: str) -> Optional[Path]:
        """Remove a path by ID.

        Parameters
        ----------
        path_id:
            The ID of the path to remove.

        Returns
        -------
        Path or None
            The removed :class:`Path`, or ``None`` if *path_id* was not found.
        """
        with self._lock:
            return self._paths.pop(path_id, None)

    def get(self, path_id: str) -> Optional[Path]:
        """Look up a path by ID.

        Parameters
        ----------
        path_id:
            The ID to look up.

        Returns
        -------
        Path or None
            The :class:`Path`, or ``None`` if not found.
        """
        with self._lock:
            return self._paths.get(path_id)

    def list_for(self, playfield_id: str) -> List[Path]:
        """Return all paths for a given playfield, in creation order.

        Parameters
        ----------
        playfield_id:
            The playfield whose paths should be returned.

        Returns
        -------
        list of Path
            Paths attached to *playfield_id*, sorted by ``path_id``.
        """
        with self._lock:
            return [p for p in self._paths.values() if p.playfield_id == playfield_id]

    def clear_for(self, playfield_id: str) -> List[str]:
        """Remove all paths for a given playfield.

        Parameters
        ----------
        playfield_id:
            The playfield whose paths should be cleared.

        Returns
        -------
        list of str
            The ``path_id`` values that were deleted.
        """
        with self._lock:
            to_delete = [
                path_id
                for path_id, path in self._paths.items()
                if path.playfield_id == playfield_id
            ]
            for path_id in to_delete:
                del self._paths[path_id]
            return to_delete

    def __len__(self) -> int:
        with self._lock:
            return len(self._paths)
