"""Concrete ``ConfigLoader`` (ticket 022-001): JSON files on disk <->
typed ``CameraConfig``/``PlayfieldDefinition`` documents.

``CameraConfig`` needs no separate implementation class here (see
``config/interfaces.py``'s module docstring -- it is constructed
directly). ``PlayfieldDefinition`` does: the interface declares
``features()``/``required_aruco_ids()`` as behaviour with no data to
back it, so :class:`PlayfieldDefinition` below adds the fields a real
definition needs to answer both, following the same "derive, adding
fields the base does not have" rule ``config/directories.py`` already
uses for ``Directories`` (base imported under a private alias, concrete
class re-using the public name).

**Why entries are stored per-section, not just as ``Feature`` tuples.**
``config.interfaces.Feature`` deliberately carries no numeric id (only
``slug``/``kind``/``position``/``colour``) -- it exists for generic
surveyed-feature listing (``where is the blue dot``), not for
calibration matching. Calibration (ticket 003) needs the opposite: for
each *detected* ArUco id, the surveyed :class:`~aprilcam.types.WorldPoint`
to solve the homography against -- a lookup ``features()`` alone cannot
answer. Storing each raw entry (:class:`PlayfieldFeatureEntry`) instead
of only the derived ``Feature`` keeps both answers available from one
piece of state: :meth:`PlayfieldDefinition.features` derives the generic
view, :meth:`PlayfieldDefinition.aruco_position` derives the
calibration-matching view ticket 003 needs -- see the docstring there.
This is also what makes :meth:`PlayfieldDefinition.to_dict` lossless
for the fields this schema models (id/slug/x/y/colour); it does not
carry ``cardinal``/``type``/``display_name`` forward, since nothing in
the currently-declared interfaces (``FeatureQuery`` is unimplemented
this sprint) reads them.

**Whole-document validation.** ``load_playfield``/``load_camera_config``
either return a fully valid document or raise ``MalformedDocument``
(``aprilcam.errors``) -- never a partially-built one. This is the
client-side half of UC-23.S-4: a bad file never reaches the daemon,
because the loader rejects it before any RPC is even constructed.
Checked here: unreadable path, invalid JSON, a document with no
``name`` (the self-naming rule -- the target is never inferred from the
filename), missing/invalid field geometry, a feature entry missing
``slug``/``x``/``y`` (or ``id`` for a tag entry), a feature positioned
outside the declared field's half-extents ("field-size/corner
inconsistency"), an ArUco or AprilTag id repeated at two different
coordinates within its own family, and (sprint 029 ticket 001) a camera
config's optional ``camera_position.height_cm`` that is not strictly
positive -- see :func:`_parse_camera_position`.

**Dict-based entry points (ticket 026-003).** ``parse_camera_config``/
``parse_playfield`` run the exact same whole-document validation as
``load_camera_config``/``load_playfield`` above, just fed an
already-parsed ``dict`` instead of a file ``Path`` -- what
``mcp.tools.configuration``'s ``set_camera_config``/``set_playfield``
tools call, since an MCP client has no filesystem the daemon can reach
to hand over a path (spec principle 7). ``load_camera_config``/
``load_playfield`` are themselves now thin wrappers: read the file,
then delegate to the matching ``parse_*`` method, so every validation
rule (self-naming, geometry, per-section entry checks, duplicate-id
rejection) lives in exactly one place regardless of which entry point a
caller uses.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from aprilcam.config.interfaces import CameraConfig, CameraPosition, ConfigLoader as _ConfigLoaderBase
from aprilcam.config.interfaces import Directories
from aprilcam.config.interfaces import Feature
from aprilcam.config.interfaces import PlayfieldDefinition as _PlayfieldDefinitionBase
from aprilcam.errors import MalformedDocument
from aprilcam.types import WorldPoint

__all__ = [
    "ConfigDirError",
    "ConfigLoader",
    "PlayfieldDefinition",
    "PlayfieldFeatureEntry",
    "ScanResult",
]

#: A small absolute margin (cm) added to the field's declared
#: half-extents before rejecting a feature as out-of-bounds -- large
#: enough to absorb float round-off from JSON parsing, far too small to
#: mask a genuine data-entry mistake (a marker off by even 1 cm would
#: still fail). ``main-playfield.json``'s corner ArUcos sit exactly on
#: the boundary (e.g. y = 44.65 == height_cm/2), so a strict "<"
#: comparison with zero margin would wrongly reject the real fixture.
_BOUNDS_EPSILON_CM = 1e-6

#: JSON section name -> the ``Feature.kind`` every entry in it gets.
#: Assigned from the section an entry is *found in*, never trusted from
#: a per-entry ``"type"`` value -- more robust against a malformed or
#: missing ``"type"`` field, and matches how ``required_aruco_ids``
#: only ever looks at the ``aruco_tags`` section.
_KIND_BY_SECTION = {
    "april_tags": "april_tag",
    "aruco_tags": "aruco_tag",
    "rectangles": "rectangle",
    "dots": "dot",
}
#: Sections whose entries carry a numeric id and are calibration ground
#: truth (``config/interfaces.py``'s ``PlayfieldDefinition`` docstring).
_ID_SECTIONS = ("april_tags", "aruco_tags")
_ALL_SECTIONS = ("april_tags", "aruco_tags", "rectangles", "dots")


@dataclass(frozen=True, kw_only=True)
class PlayfieldFeatureEntry:
    """One raw entry from a playfield definition's ``april_tags``/
    ``aruco_tags``/``rectangles``/``dots`` array -- everything this
    loader models from the on-disk shape, no more. ``tag_id`` is set
    only for ``april_tags``/``aruco_tags`` entries; ``colour`` only for
    ``rectangles``/``dots`` entries (both ``None`` otherwise, matching
    :class:`~aprilcam.config.interfaces.Feature`'s own optionality).
    """

    slug: str
    x: float
    y: float
    tag_id: Optional[int] = None
    colour: Optional[str] = None


@dataclass(frozen=True, kw_only=True)
class PlayfieldDefinition(_PlayfieldDefinitionBase):
    """Concrete ``config.interfaces.PlayfieldDefinition``.

    The single canonical implementation -- constructed here by
    :meth:`ConfigLoader.load_playfield` (from a file), by
    ``daemon/rpc.py`` (reconstructing an incoming ``SetPlayfieldRequest``),
    and by ``client/models.py`` (parsing a ``GetPlayfieldResponse``), the
    same way ``config.interfaces.CameraConfig`` is directly constructed
    everywhere rather than re-declared per module (that module's
    docstring). Every field beyond ``name``/``width_cm``/``height_cm``
    (already declared by the base class) is a per-section tuple of
    :class:`PlayfieldFeatureEntry`.
    """

    april_tags: Tuple[PlayfieldFeatureEntry, ...] = ()
    aruco_tags: Tuple[PlayfieldFeatureEntry, ...] = ()
    rectangles: Tuple[PlayfieldFeatureEntry, ...] = ()
    dots: Tuple[PlayfieldFeatureEntry, ...] = ()

    def features(self) -> Tuple[Feature, ...]:
        """See ``PlayfieldDefinition.features``. Every surveyed entry,
        section order (april_tags, aruco_tags, rectangles, dots), each
        section's own document order -- deterministic, never
        re-sorted."""
        return tuple(
            Feature(
                slug=entry.slug,
                kind=_KIND_BY_SECTION[section],
                position=WorldPoint(x=entry.x, y=entry.y),
                colour=entry.colour,
            )
            for section in _ALL_SECTIONS
            for entry in getattr(self, section)
        )

    def required_aruco_ids(self) -> Tuple[int, ...]:
        """See ``PlayfieldDefinition.required_aruco_ids``. Sorted
        ascending -- a stable, predictable set for calibration's
        all-or-nothing acceptance gate to report against, independent
        of the document's own (arbitrary) array order."""
        return tuple(sorted(entry.tag_id for entry in self.aruco_tags if entry.tag_id is not None))

    def aruco_position(self, aruco_id: int) -> WorldPoint:
        """The surveyed position for ArUco *aruco_id* -- what
        calibration (ticket 003) matches a detected marker's id
        against to solve the homography. **Not part of the
        ``PlayfieldDefinition`` base class's declared contract**:
        ``features()`` returns ``Feature`` values, which carry no
        numeric id, so an id -> position lookup cannot be built from
        that method alone. This is an addition specific to this
        concrete implementation, the same way ``DaemonService.
        active_slugs()`` adds a capability ``Daemon`` itself does not
        declare. Raises ``KeyError`` for an id outside
        :meth:`required_aruco_ids`.
        """
        for entry in self.aruco_tags:
            if entry.tag_id == aruco_id:
                return WorldPoint(x=entry.x, y=entry.y)
        raise KeyError(aruco_id)

    def to_dict(self) -> Dict[str, Any]:
        """See ``ConfigDocument.to_dict``. Loadable verbatim by
        :meth:`ConfigLoader.load_playfield` -- the round-trip contract
        (``load(x.to_dict()) == x``) -- but not necessarily byte-equal
        to a hand-authored source file: fields this schema does not
        model (``cardinal``, per-entry ``"type"``, ``display_name``,
        rectangle/dot sizing) are not carried, since nothing in the
        currently-declared interfaces reads them back (see the module
        docstring)."""

        def _section(entries: Tuple[PlayfieldFeatureEntry, ...], *, with_id: bool) -> list:
            out = []
            for entry in entries:
                item: Dict[str, Any] = {"slug": entry.slug, "x": entry.x, "y": entry.y}
                if with_id:
                    item["id"] = entry.tag_id
                if entry.colour is not None:
                    item["color"] = entry.colour
                out.append(item)
            return out

        return {
            "name": self.name,
            "playfield": {"width_cm": self.width_cm, "height_cm": self.height_cm},
            "april_tags": _section(self.april_tags, with_id=True),
            "aruco_tags": _section(self.aruco_tags, with_id=True),
            "rectangles": _section(self.rectangles, with_id=False),
            "dots": _section(self.dots, with_id=False),
        }


@dataclass(frozen=True, kw_only=True)
class ConfigDirError:
    """One file :meth:`ConfigLoader.load_config_dir` could not load --
    named, so a caller (``daemon/__main__.py build()``) can log exactly
    which file was skipped and why, per Decision 1 (sprint 023 sprint.md):
    boot-time malformed config is skip-and-warn, never refuse-to-start."""

    path: Path
    message: str


@dataclass(frozen=True, kw_only=True)
class ScanResult:
    """Everything :meth:`ConfigLoader.load_config_dir` found under one
    ``config_dir``: every successfully-loaded document plus every file
    that failed to load, named. Never raised for one bad file among
    good ones -- see that method's docstring."""

    playfields: Tuple["PlayfieldDefinition", ...] = ()
    camera_configs: Tuple[CameraConfig, ...] = ()
    errors: Tuple[ConfigDirError, ...] = ()


class ConfigLoader(_ConfigLoaderBase):
    """Concrete ``config.interfaces.ConfigLoader``. See the module
    docstring for the validation this performs."""

    def load_config_dir(self, directories: Directories) -> ScanResult:
        """Scan *directories*.config_dir for every playfield definition
        (``playfields/*.json``) then every camera config
        (``cameras/<slug>/config.json``) -- ``configuration.md``'s
        two-directory layout -- loading each through
        :meth:`load_playfield`/:meth:`load_camera_config` in turn.

        This is ticket 023-003's boot-time-load half: `daemon.
        __main__.build()` calls this once, before the control plane
        opens, and feeds every document returned here into
        `DaemonService`'s already-existing `set_playfield`/
        `set_camera_config` setters (sprint.md Architecture, "Modules"
        point 1 -- `DaemonService` itself stays ignorant of disk).

        **Never raises for one malformed file** (sprint.md Design
        Rationale, Decision 1: skip-and-warn, not refuse-to-start,
        matching the existing precedent in
        `DaemonService._load_calibrations`). A `MalformedDocument` from
        either loader method for one file is caught here and recorded
        in the returned `ScanResult.errors`, naming the offending path
        and the message; scanning continues with every other file. This
        is what lets a daemon with five cameras start successfully when
        only one of their config files has a typo, rather than refusing
        to start over it.

        A missing `playfields/` or `cameras/` subdirectory is not an
        error -- an empty or partially-populated `config_dir` is the
        ordinary first-run case (UC-01.S-2). A `cameras/<slug>/`
        directory with no `config.json` inside it is likewise skipped
        silently, not reported as an error -- there is nothing there to
        have failed to load.

        Playfields are scanned (and appear in the returned tuple)
        before camera configs, in sorted-path order within each
        section, matching UC-01's stated main-flow order and giving
        deterministic output for a fixed directory tree.
        """
        playfields: list = []
        camera_configs: list = []
        errors: list = []

        playfields_dir = directories.config_dir / "playfields"
        if playfields_dir.is_dir():
            for path in sorted(playfields_dir.glob("*.json")):
                try:
                    playfields.append(self.load_playfield(path))
                except MalformedDocument as exc:
                    errors.append(ConfigDirError(path=path, message=str(exc)))

        cameras_dir = directories.config_dir / "cameras"
        if cameras_dir.is_dir():
            for camera_dir in sorted(cameras_dir.iterdir()):
                if not camera_dir.is_dir():
                    continue
                path = camera_dir / "config.json"
                if not path.exists():
                    continue
                try:
                    camera_configs.append(self.load_camera_config(path))
                except MalformedDocument as exc:
                    errors.append(ConfigDirError(path=path, message=str(exc)))

        return ScanResult(
            playfields=tuple(playfields),
            camera_configs=tuple(camera_configs),
            errors=tuple(errors),
        )

    def load_camera_config(self, path: Path) -> CameraConfig:
        data = _read_json_object(path, kind="camera config")
        return self.parse_camera_config(data, source=str(path))

    def load_playfield(self, path: Path) -> "PlayfieldDefinition":
        data = _read_json_object(path, kind="playfield definition")
        return self.parse_playfield(data, source=str(path))

    def parse_camera_config(
        self, data: Dict[str, Any], *, source: str = "the pushed document"
    ) -> CameraConfig:
        """Dict-based counterpart of :meth:`load_camera_config` (ticket
        026-003) -- see the module docstring's "Dict-based entry
        points" section. *source* only shapes error-message wording (a
        file path for the file-based caller above, a generic label for
        every other caller); it carries no other meaning.
        """
        if not isinstance(data, dict):
            raise MalformedDocument(f"camera config in {source} must be a JSON object")
        name = _require_name(data, kind="camera config", source=source)

        playfield = data.get("playfield")
        if playfield is not None and not isinstance(playfield, str):
            raise MalformedDocument(
                f"camera config in {source}: 'playfield' must be a string or null, got {playfield!r}"
            )

        settings = data.get("settings", {})
        if settings is None:
            settings = {}
        if not isinstance(settings, dict):
            raise MalformedDocument(f"camera config in {source}: 'settings' must be an object")

        camera_position = _parse_camera_position(data, source=source)

        return CameraConfig(
            name=name, playfield=playfield, settings=dict(settings), camera_position=camera_position
        )

    def parse_playfield(
        self, data: Dict[str, Any], *, source: str = "the pushed document"
    ) -> "PlayfieldDefinition":
        """Dict-based counterpart of :meth:`load_playfield` (ticket
        026-003) -- see the module docstring's "Dict-based entry
        points" section. *source* only shapes error-message wording;
        see :meth:`parse_camera_config`."""
        if not isinstance(data, dict):
            raise MalformedDocument(f"playfield definition in {source} must be a JSON object")
        name = _require_name(data, kind="playfield definition", source=source)
        width_cm, height_cm = _require_geometry(data, source=source)

        sections: Dict[str, Tuple[PlayfieldFeatureEntry, ...]] = {}
        for section in _ALL_SECTIONS:
            sections[section] = _parse_section(
                data, section, source=source, width_cm=width_cm, height_cm=height_cm
            )

        _reject_duplicate_ids(sections, source=source)

        return PlayfieldDefinition(
            name=name,
            width_cm=width_cm,
            height_cm=height_cm,
            april_tags=sections["april_tags"],
            aruco_tags=sections["aruco_tags"],
            rectangles=sections["rectangles"],
            dots=sections["dots"],
        )


# ---------------------------------------------------------------------------
# internals -- shared validation helpers
# ---------------------------------------------------------------------------


def _read_json_object(path: Path, *, kind: str) -> Dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MalformedDocument(f"cannot read {kind} at {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MalformedDocument(f"{kind} at {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise MalformedDocument(f"{kind} at {path} must be a JSON object")
    return data


def _require_name(data: Dict[str, Any], *, kind: str, source: str) -> str:
    """The self-naming rule: a document with no ``name`` is malformed --
    the target is always the document's own ``name`` field, never
    inferred from a path, filename, or any other context (UC-23.S-4).
    *source* only shapes the message (a file path or a generic label);
    shared by both :meth:`ConfigLoader.load_camera_config`/
    :meth:`~ConfigLoader.load_playfield` (file-based, *source* is
    ``str(path)``) and :meth:`ConfigLoader.parse_camera_config`/
    :meth:`~ConfigLoader.parse_playfield` (dict-based)."""
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise MalformedDocument(
            f"{kind} in {source} has no 'name' field -- the target is always the "
            "document's own 'name' field, never inferred from a path or filename"
        )
    return name


def _require_geometry(data: Dict[str, Any], *, source: str) -> Tuple[float, float]:
    pf = data.get("playfield")
    if not isinstance(pf, dict):
        raise MalformedDocument(
            f"playfield definition in {source} has no 'playfield' geometry block"
        )
    try:
        width_cm = float(pf["width_cm"])
        height_cm = float(pf["height_cm"])
    except (KeyError, TypeError, ValueError) as exc:
        raise MalformedDocument(
            f"playfield definition in {source} is missing/invalid width_cm/height_cm: {exc}"
        ) from exc
    if width_cm <= 0 or height_cm <= 0:
        raise MalformedDocument(
            f"playfield definition in {source}: width_cm/height_cm must be positive "
            f"(got {width_cm}x{height_cm})"
        )
    return width_cm, height_cm


def _parse_camera_position(data: Dict[str, Any], *, source: str) -> Optional[CameraPosition]:
    """Parse a camera config's optional ``camera_position`` block (sprint
    029 ticket 001) -- ``{"nadir_x_cm": ..., "nadir_y_cm": ...,
    "height_cm": ...}``, a fresh v2 shape (see ``config/interfaces.py``'s
    ``CameraPosition`` docstring and this sprint's sprint.md Design
    Rationale for why it diverges from v1's ``{height, x_offset,
    y_offset}``).

    Key absent -> ``None`` -- a pre-029 camera config parses unchanged,
    exactly like every other optional block this loader already treats
    this way. ``nadir_x_cm``/``nadir_y_cm`` are plain floats, no bounds
    check -- unlike a playfield feature, a camera's nadir has no reason
    to be constrained to the field's own rectangle. ``height_cm <= 0``
    is rejected whole-document, mirroring :func:`_require_geometry`'s
    identical positivity check for playfield ``width_cm``/``height_cm``:
    a non-positive height is ``project_tag_to_field_plane``'s own
    "nothing to correct" sentinel, so silently accepting one here would
    silently configure a camera that can never parallax-correct anything.
    """
    raw = data.get("camera_position")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise MalformedDocument(f"camera config in {source}: 'camera_position' must be an object")
    try:
        nadir_x_cm = float(raw["nadir_x_cm"])
        nadir_y_cm = float(raw["nadir_y_cm"])
        height_cm = float(raw["height_cm"])
    except (KeyError, TypeError, ValueError) as exc:
        raise MalformedDocument(
            f"camera config in {source}: 'camera_position' is missing/invalid "
            f"nadir_x_cm/nadir_y_cm/height_cm: {exc}"
        ) from exc
    if height_cm <= 0:
        raise MalformedDocument(
            f"camera config in {source}: 'camera_position.height_cm' must be positive "
            f"(got {height_cm})"
        )
    return CameraPosition(nadir=WorldPoint(x=nadir_x_cm, y=nadir_y_cm), height_cm=height_cm)


def _parse_section(
    data: Dict[str, Any], section: str, *, source: str, width_cm: float, height_cm: float
) -> Tuple[PlayfieldFeatureEntry, ...]:
    raw_list = data.get(section, []) or []
    if not isinstance(raw_list, list):
        raise MalformedDocument(f"playfield definition in {source}: {section!r} must be a list")

    half_w = width_cm / 2 + _BOUNDS_EPSILON_CM
    half_h = height_cm / 2 + _BOUNDS_EPSILON_CM
    with_id = section in _ID_SECTIONS

    entries = []
    for raw in raw_list:
        if not isinstance(raw, dict):
            raise MalformedDocument(
                f"playfield definition in {source}: an entry in {section!r} is not an object"
            )
        slug = raw.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            raise MalformedDocument(
                f"playfield definition in {source}: an entry in {section!r} has no 'slug'"
            )
        try:
            x = float(raw["x"])
            y = float(raw["y"])
        except (KeyError, TypeError, ValueError) as exc:
            raise MalformedDocument(
                f"playfield definition in {source}: {slug!r} has invalid/missing x/y: {exc}"
            ) from exc

        tag_id: Optional[int] = None
        if with_id:
            try:
                tag_id = int(raw["id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise MalformedDocument(
                    f"playfield definition in {source}: {slug!r} in {section!r} has no valid 'id': {exc}"
                ) from exc

        if abs(x) > half_w or abs(y) > half_h:
            raise MalformedDocument(
                f"playfield definition in {source}: {slug!r} at ({x}, {y}) lies outside the "
                f"declared {width_cm}x{height_cm} cm field -- field-size/corner inconsistency"
            )

        colour = raw.get("color")
        entries.append(PlayfieldFeatureEntry(slug=slug, x=x, y=y, tag_id=tag_id, colour=colour))
    return tuple(entries)


def _reject_duplicate_ids(
    sections: Dict[str, Tuple[PlayfieldFeatureEntry, ...]], *, source: str
) -> None:
    """A duplicate id *within one family* (April vs ArUco are distinct
    identities, ``types.TagFamily``'s own docstring) at two different
    coordinates is rejected whole, naming the id (UC-02.S-2). The same
    id repeated at the *same* coordinates is redundant but not
    conflicting, and is allowed."""
    for section in _ID_SECTIONS:
        family = "AprilTag" if section == "april_tags" else "ArUco"
        seen: Dict[int, Tuple[float, float]] = {}
        for entry in sections[section]:
            assert entry.tag_id is not None  # guaranteed by _parse_section(with_id=True)
            pos = (entry.x, entry.y)
            prior = seen.get(entry.tag_id)
            if prior is not None and prior != pos:
                raise MalformedDocument(
                    f"playfield definition in {source}: duplicate {family} id {entry.tag_id} "
                    f"at different coordinates ({prior} vs {pos})"
                )
            seen[entry.tag_id] = pos
