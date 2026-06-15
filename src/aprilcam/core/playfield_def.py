"""aprilcam.core.playfield_def — PlayfieldDefinition and PlayfieldDefinitionRegistry.

A ``PlayfieldDefinition`` is a dataclass loaded from a named JSON file under
``data/aprilcam/playfields/<slug>.json``.  It exposes the playfield's geometry,
marker lists, and computed helpers for the four diagonal-cardinal corner ArUco
markers.

``PlayfieldDefinitionRegistry`` scans a directory for ``*.json`` files and
builds a ``{name: PlayfieldDefinition}`` map.  It is modelled on the existing
``PathRegistry`` / ``PlayfieldRegistry`` pattern: no I/O at attribute access;
loaded once at startup via ``load_all(playfields_dir)``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_log = logging.getLogger("aprilcam.playfield_def")

# The four cardinal directions that designate "corner" ArUco markers.
_CORNER_CARDINALS = frozenset({"northwest", "northeast", "southeast", "southwest"})


@dataclass
class PlayfieldDefinition:
    """Playfield geometry and marker layout loaded from a named JSON file.

    Attributes:
        name:         Filename stem (== the canonical reference id).
        display_name: Human-readable label; defaults to *name* when absent.
        width_cm:     Field width in centimetres.
        height_cm:    Field height in centimetres.
        origin:       Slug of the tag at the coordinate origin
                      (e.g. ``"apriltag-center-a1"``).
        april_tags:   Raw list of AprilTag dicts from the JSON.
        aruco_tags:   Raw list of ArUco tag dicts from the JSON.
        rectangles:   Raw list of rectangle dicts from the JSON.
        dots:         Raw list of dot dicts from the JSON.
    """

    name: str
    display_name: str
    width_cm: float
    height_cm: float
    origin: str
    april_tags: list[dict] = field(default_factory=list)
    aruco_tags: list[dict] = field(default_factory=list)
    rectangles: list[dict] = field(default_factory=list)
    dots: list[dict] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> "PlayfieldDefinition":
        """Parse a playfield JSON file and return a ``PlayfieldDefinition``.

        The file is expected to have the structure::

            {
              "playfield": {"width_cm": ..., "height_cm": ..., "origin": "..."},
              "april_tags": [...],
              "aruco_tags": [...],
              "rectangles": [...],
              "dots": [...]
            }

        The ``name`` attribute is taken from the filename stem (without the
        ``.json`` extension).

        Raises:
            FileNotFoundError: If *path* does not exist.
            ValueError: If the file is not valid JSON or is missing required
                geometry keys (``width_cm``, ``height_cm``).
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Playfield definition not found: {path}")

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Playfield definition is not valid JSON: {exc}") from exc

        pf_meta = data.get("playfield", {})
        try:
            width_cm = float(pf_meta["width_cm"])
            height_cm = float(pf_meta["height_cm"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"Playfield definition missing required geometry fields (width_cm, height_cm): {exc}"
            ) from exc

        origin = str(pf_meta.get("origin", ""))
        name = path.stem
        display_name = str(pf_meta.get("display_name", name))

        return cls(
            name=name,
            display_name=display_name,
            width_cm=width_cm,
            height_cm=height_cm,
            origin=origin,
            april_tags=list(data.get("april_tags", []) or []),
            aruco_tags=list(data.get("aruco_tags", []) or []),
            rectangles=list(data.get("rectangles", []) or []),
            dots=list(data.get("dots", []) or []),
        )

    # ------------------------------------------------------------------
    # Computed helpers
    # ------------------------------------------------------------------

    def corner_aruco_ids(self) -> list[int]:
        """Return the ArUco IDs of the four diagonal-cardinal corner markers.

        Filters ``aruco_tags`` for entries whose ``cardinal`` field is one of
        ``northwest``, ``northeast``, ``southeast``, ``southwest``.  Returns
        their ``id`` values in the order they appear in the JSON.

        For ``main-playfield.json`` this yields ``[1, 3, 5, 7]``
        (NW=1, NE=3, SE=5, SW=7).
        """
        ids: list[int] = []
        for tag in self.aruco_tags:
            if str(tag.get("cardinal", "")).lower() in _CORNER_CARDINALS:
                ids.append(int(tag["id"]))
        return ids

    def corner_world_coords(self) -> list[tuple[float, float]]:
        """Return the world (x, y) positions (cm) for each corner ArUco marker.

        Returns one ``(x, y)`` tuple for each entry in :meth:`corner_aruco_ids`,
        in the same order.

        For ``main-playfield.json`` this yields
        ``[(-67, 44.65), (67, 44.65), (67, -44.65), (-67, -44.65)]``
        (NW, NE, SE, SW).
        """
        coords: list[tuple[float, float]] = []
        for tag in self.aruco_tags:
            if str(tag.get("cardinal", "")).lower() in _CORNER_CARDINALS:
                coords.append((float(tag["x"]), float(tag["y"])))
        return coords

    def to_dict(self) -> dict:
        """Return the full definition as a JSON-serialisable dict.

        Shape mirrors the on-disk file plus the identity fields::

            {
              "name": ..., "display_name": ...,
              "playfield": {"width_cm": ..., "height_cm": ..., "origin": ...},
              "april_tags": [...], "aruco_tags": [...],
              "rectangles": [...], "dots": [...]
            }
        """
        return {
            "name": self.name,
            "display_name": self.display_name,
            "playfield": {
                "width_cm": self.width_cm,
                "height_cm": self.height_cm,
                "origin": self.origin,
            },
            "april_tags": self.april_tags,
            "aruco_tags": self.aruco_tags,
            "rectangles": self.rectangles,
            "dots": self.dots,
        }


class PlayfieldDefinitionRegistry:
    """Registry of named playfield definitions loaded from a directory.

    Usage::

        registry = PlayfieldDefinitionRegistry()
        registry.load_all(cfg.playfields_dir)
        defn = registry.get("main-playfield")

    The registry is populated once at startup; no I/O occurs at attribute
    access time.  If ``playfields_dir`` does not exist, the registry loads
    empty without raising.  Malformed JSON files are skipped with a log
    warning rather than raising.
    """

    def __init__(self) -> None:
        self._defs: dict[str, PlayfieldDefinition] = {}

    def load_all(self, playfields_dir: Path) -> None:
        """Scan *playfields_dir* for ``*.json`` files and load each one.

        Args:
            playfields_dir: Directory to scan.  If it does not exist, the
                method returns immediately leaving the registry empty.

        Files that cannot be parsed are skipped; a warning is logged for
        each failure.  Previously loaded definitions are replaced if the
        same name is encountered again (idempotent re-load).
        """
        playfields_dir = Path(playfields_dir)
        if not playfields_dir.exists():
            _log.debug("Playfields directory does not exist: %s — registry stays empty", playfields_dir)
            return

        for json_file in sorted(playfields_dir.glob("*.json")):
            try:
                defn = PlayfieldDefinition.load(json_file)
                self._defs[defn.name] = defn
                _log.debug("Loaded playfield definition: %s", defn.name)
            except Exception as exc:  # noqa: BLE001
                _log.warning("Skipping malformed playfield definition %s: %s", json_file, exc)

    def get(self, name: str) -> PlayfieldDefinition:
        """Return the ``PlayfieldDefinition`` for *name*.

        Raises:
            KeyError: If *name* is not in the registry.
        """
        return self._defs[name]

    def list(self) -> list[str]:
        """Return the sorted list of registered playfield names."""
        return sorted(self._defs.keys())

    def first(self) -> Optional[PlayfieldDefinition]:
        """Return the first (alphabetically) definition, or ``None`` if empty."""
        names = self.list()
        if not names:
            return None
        return self._defs[names[0]]
