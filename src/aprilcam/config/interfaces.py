"""Configuration documents and directory resolution.

Two ideas, both from ``docs/design/configuration.md`` and UC-01:

**Self-naming documents.** Every configuration document carries its own
``name``, so pushing one takes a file and nothing else — the document
says what it is. Playfield definitions already worked this way in v1;
camera configs did **not** (identity came from the containing directory),
and gaining a ``name`` field is a v2 schema change (UC-23).

**File-backed vs in-memory.** Disk is the source of record. A document
pushed over RPC is a live override that is discarded on restart. The
daemon never writes config: the config root is human-owned and not
writable by it, which makes the ephemeral rule structural rather than a
policy the daemon has to remember.

See ``aprilcam.daemon.interfaces``'s module docstring for the interface
design rule applied below: ``Directories`` is a behavioral interface
with an ordinary-class implementation (``config.directories.
Directories``), so it is a plain base class. ``ConfigDocument``,
``CameraConfig``, ``CameraPosition``, ``PlayfieldDefinition``, and
``Feature`` are value-shaped, so they are frozen ``@dataclass``\\ es
declaring fields.
``CameraConfig`` needed no separate "implementation" dataclass —
``camera.camera``'s former ``SimpleCameraConfig`` was exactly the same
shape, so it has been deleted in favour of constructing ``CameraConfig``
here directly (``camera/camera.py`` does exactly that). ``ConfigLoader``
is behavioral (a plain base class) — no concrete implementation exists
yet to check (out of this pass's scope), but converting it now means a
future implementation derives rather than duck-types.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from aprilcam.types import WorldPoint


@dataclass(frozen=True, kw_only=True)
class ConfigDocument:
    """A whole configuration document that knows its own target.

    Documents move **whole, in both directions**. There are no
    field-level edits: you push an entire document or none of it, and a
    rejected push leaves the previous state exactly as it was (UC-02.S-2,
    UC-03.S-3). Partial application is the failure mode this rule exists
    to prevent.
    """

    name: str
    """The camera slug or playfield slug this document configures.

    A document without a name is **rejected** (UC-23.S-4). The target
    is never inferred from the filename: a file copied to
    ``backup.json`` would otherwise push to a camera called
    ``backup``.
    """

    def to_dict(self) -> dict[str, Any]:
        """Serialize in exact on-disk format.

        The round trip that makes ephemeral config workable: dump →
        install → restart → identical config (UC-24.S-1). Output must be
        loadable verbatim, including ``name``.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement ConfigDocument.to_dict")


@dataclass(frozen=True, kw_only=True)
class CameraPosition:
    """The ground point directly below a camera's optical centre
    (nadir) and its optical height above the field plane -- the two
    inputs ``calibration.geometry.project_tag_to_field_plane`` needs.

    Operator-asserted metadata, not a hardware control: it never
    touches ``Camera.apply_config`` (``CameraConfig.settings``' own
    docstring on that boundary) and has no bearing on what a device's
    control surface reports as settable. Set by hand (a hand-authored
    ``camera_position`` block) or solved (``aprilcam camera locate``,
    sprint 029 ticket 003) -- either way, this is the value
    ``daemon.service.DaemonService._camera_geometry_for`` reads
    directly once present.

    ``height_cm`` is an *effective optical centre*, not a tape-measured
    distance to the sensor or lens -- see this sprint's own measured
    fixture (sprint.md Problem section) for why a solve, not a ruler,
    is the only thing that produces a usable value.
    """

    nadir: WorldPoint
    height_cm: float


@dataclass(frozen=True, kw_only=True)
class CameraConfig(ConfigDocument):
    """Everything adjustable about one camera (UC-03).

    **Per-camera, never shared.** Two identical cameras side by side are
    configured independently; there is no class-wide or inherited config.
    The settable keys a camera reports are *that camera's* keys, since
    different hardware exposes different controls.

    Concrete and directly constructed (see the module docstring) — there
    is no separate implementation class the way there is for
    ``PlayfieldDefinition``.
    """

    playfield: Optional[str]
    """Which playfield this camera observes, if any.

    A camera observes exactly one playfield; a playfield has zero or
    more cameras. Calibration requires this to be set — with no
    playfield there is no coordinate system to map to and nothing to
    calibrate (UC-15.S-2).
    """
    settings: dict[str, Any] = field(default_factory=dict)
    """Hardware controls: exposure, gain, white balance, resolution.

    A fetched config reports **every settable key, blank when unset**
    (UC-03.S-1) — self-describing, so a client can discover what is
    adjustable without prior knowledge. This is the template every
    camera configuration starts from (UC-25).
    """
    camera_position: Optional[CameraPosition] = None
    """This camera's physical ground nadir and optical height, if
    configured (sprint 029). ``None`` means "not configured" — the
    exact pre-029 state every existing consumer of this class already
    behaves as if it were, since a camera nobody has run ``aprilcam
    camera locate`` against (or been hand-authored a ``camera_position``
    block for) has never had this field at all.

    Deliberately **not** inside :attr:`settings`: this is metadata an
    operator asserts, never a hardware control pushed through
    ``Camera.apply_config`` or validated against a device's control
    surface (see :class:`CameraPosition`'s own docstring, and
    sprint.md's Design Rationale for the full reasoning).
    """

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "playfield": self.playfield,
            "settings": dict(self.settings),
        }
        if self.camera_position is not None:
            data["camera_position"] = {
                "nadir_x_cm": self.camera_position.nadir.x,
                "nadir_y_cm": self.camera_position.nadir.y,
                "height_cm": self.camera_position.height_cm,
            }
        return data


@dataclass(frozen=True, kw_only=True)
class PlayfieldDefinition(ConfigDocument):
    """The surveyed ground truth for one playfield (UC-02).

    Feature roles are **not** interchangeable:

    * ArUco and AprilTags are calibration ground truth.
    * Coloured rectangles and dots are *never* used for calibration;
      their surveyed positions are queryable only (UC-08).

    Colour *detection* is out of scope for v2, so a coloured feature's
    position is what the definition says it is — vision never confirms
    it is still physically there. That limitation should be understood
    rather than hidden.

    No concrete implementation exists yet (out of this pass's scope);
    ``features``/``required_aruco_ids`` raise ``NotImplementedError``
    until one derives from this and overrides them.
    """

    width_cm: float
    height_cm: float

    def features(self) -> tuple["Feature", ...]:
        """Every surveyed feature: tags, rectangles, dots."""
        raise NotImplementedError(
            f"{type(self).__name__} does not implement PlayfieldDefinition.features"
        )

    def required_aruco_ids(self) -> tuple[int, ...]:
        """The ArUco ids a camera must see for calibration to succeed.

        Calibration is **all-or-nothing against this set** (UC-04):
        seeing 9 of 10 is a failure, not a degraded success. An
        incomplete view means the camera cannot see the field it claims
        to observe, and a homography from it would be silently wrong
        somewhere.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement PlayfieldDefinition.required_aruco_ids"
        )


@dataclass(frozen=True, kw_only=True)
class Feature:
    """One surveyed thing on the playfield, at a known world position."""

    slug: str
    kind: str
    """``april_tag``, ``aruco_tag``, ``rectangle``, or ``dot``."""
    position: WorldPoint
    colour: Optional[str] = None
    height_cm: float = 0.0
    """How far this feature sits above the field plane, in cm (sprint
    030 ticket 002) -- the same vocabulary ``CameraPosition.height_cm``
    and ``camera locate``'s ``HEIGHT_CM`` already use. ``0.0`` (the
    default) means "on the field plane," true for every feature ever
    surveyed before this ticket and for the ``rectangle``/``dot`` kinds
    today. A declared field, never a ``@property`` (CLAUDE.md's
    frozen-dataclass rule).

    Calibration ground truth only: ``Calibrator`` (``calibration/
    calibration.py``) uses a nonzero value on an ``april_tag``/
    ``aruco_tag`` feature to build a real 3D object point
    ``(x, y, height_cm)`` for intrinsics estimation, and to *exclude*
    that feature from the homography solve, which only ever maps the
    *field plane* (``height_cm == 0``) to pixels -- see that module's
    docstring for why feeding an elevated point into the homography
    solve would corrupt it exactly the way unmodelled parallax does.
    """


class ConfigLoader:
    """Turns files back into documents — the inverse of ``to_dict``.

    A plain base class, not ``Protocol`` — see ``daemon.interfaces``'s
    module docstring for the design rule.

    This is the client-side half of the push path (UC-23): the CLI and
    Python programs read a document from disk *here*, then hand it to
    ``Daemon.set_camera_config`` / ``set_playfield``. Validation happens
    **before any RPC**: unparseable JSON or a document with no ``name``
    raises ``MalformedDocument`` (``aprilcam.errors``) client-side — the
    daemon never sees a file the loader would reject (UC-23.S-4).

    The round-trip contract binds both directions:
    ``load(x.to_dict()) == x``. A dump that does not re-load verbatim is
    a bug in whichever side broke the symmetry (UC-24.S-1).
    """

    def load_camera_config(self, path: Path) -> CameraConfig:
        raise NotImplementedError(f"{type(self).__name__} does not implement ConfigLoader.load_camera_config")

    def load_playfield(self, path: Path) -> PlayfieldDefinition:
        raise NotImplementedError(f"{type(self).__name__} does not implement ConfigLoader.load_playfield")


class Directories:
    """Where files live (``docs/design/configuration.md``).

    A plain base class, not ``Protocol`` — see the module docstring.

    Two roots, split by **who writes them**:

    * ``config_dir`` — ``/etc/aprilcam``, human-owned, **daemon reads
      only**. Playfield definitions and camera configs.
    * ``state_dir`` — ``/var/lib/aprilcam``, daemon-owned, regenerable.
      Registry, calibrations, advisory info.

    Both fall back to the user's home when the system paths are
    unavailable, so an unprivileged developer can run a daemon without
    ceremony. The resolved roots are **reported at startup** — an
    unreported cascade is how a config mystery eats an afternoon.
    """

    @property
    def config_dir(self) -> Path:
        raise NotImplementedError(f"{type(self).__name__} does not implement Directories.config_dir")

    @property
    def state_dir(self) -> Path:
        raise NotImplementedError(f"{type(self).__name__} does not implement Directories.state_dir")

    @property
    def socket_dir(self) -> Path:
        """Runtime plumbing. Not configurable — nobody needs to choose."""
        raise NotImplementedError(f"{type(self).__name__} does not implement Directories.socket_dir")
