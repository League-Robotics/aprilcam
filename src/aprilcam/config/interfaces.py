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
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

from aprilcam.types import WorldPoint


@runtime_checkable
class ConfigDocument(Protocol):
    """A whole configuration document that knows its own target.

    Documents move **whole, in both directions**. There are no
    field-level edits: you push an entire document or none of it, and a
    rejected push leaves the previous state exactly as it was (UC-02.S-2,
    UC-03.S-3). Partial application is the failure mode this rule exists
    to prevent.
    """

    @property
    def name(self) -> str:
        """The camera slug or playfield slug this document configures.

        A document without a name is **rejected** (UC-23.S-4). The target
        is never inferred from the filename: a file copied to
        ``backup.json`` would otherwise push to a camera called
        ``backup``.
        """
        ...

    def to_dict(self) -> dict[str, Any]:
        """Serialize in exact on-disk format.

        The round trip that makes ephemeral config workable: dump →
        install → restart → identical config (UC-24.S-1). Output must be
        loadable verbatim, including ``name``.
        """
        ...


@runtime_checkable
class CameraConfig(ConfigDocument, Protocol):
    """Everything adjustable about one camera (UC-03).

    **Per-camera, never shared.** Two identical cameras side by side are
    configured independently; there is no class-wide or inherited config.
    The settable keys a camera reports are *that camera's* keys, since
    different hardware exposes different controls.
    """

    @property
    def playfield(self) -> Optional[str]:
        """Which playfield this camera observes, if any.

        A camera observes exactly one playfield; a playfield has zero or
        more cameras. Calibration requires this to be set — with no
        playfield there is no coordinate system to map to and nothing to
        calibrate (UC-15.S-2).
        """
        ...

    @property
    def settings(self) -> dict[str, Any]:
        """Hardware controls: exposure, gain, white balance, resolution.

        A fetched config reports **every settable key, blank when unset**
        (UC-03.S-1) — self-describing, so a client can discover what is
        adjustable without prior knowledge. This is the template every
        camera configuration starts from (UC-25).
        """
        ...


@runtime_checkable
class PlayfieldDefinition(ConfigDocument, Protocol):
    """The surveyed ground truth for one playfield (UC-02).

    Feature roles are **not** interchangeable:

    * ArUco and AprilTags are calibration ground truth.
    * Coloured rectangles and dots are *never* used for calibration;
      their surveyed positions are queryable only (UC-08).

    Colour *detection* is out of scope for v2, so a coloured feature's
    position is what the definition says it is — vision never confirms
    it is still physically there. That limitation should be understood
    rather than hidden.
    """

    @property
    def width_cm(self) -> float: ...

    @property
    def height_cm(self) -> float: ...

    def features(self) -> tuple["Feature", ...]:
        """Every surveyed feature: tags, rectangles, dots."""
        ...

    def required_aruco_ids(self) -> tuple[int, ...]:
        """The ArUco ids a camera must see for calibration to succeed.

        Calibration is **all-or-nothing against this set** (UC-04):
        seeing 9 of 10 is a failure, not a degraded success. An
        incomplete view means the camera cannot see the field it claims
        to observe, and a homography from it would be silently wrong
        somewhere.
        """
        ...


@runtime_checkable
class Feature(Protocol):
    """One surveyed thing on the playfield, at a known world position."""

    @property
    def slug(self) -> str: ...

    @property
    def kind(self) -> str:
        """``april_tag``, ``aruco_tag``, ``rectangle``, or ``dot``."""
        ...

    @property
    def position(self) -> WorldPoint: ...

    @property
    def colour(self) -> Optional[str]: ...


@runtime_checkable
class ConfigLoader(Protocol):
    """Turns files back into documents — the inverse of ``to_dict``.

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

    def load_camera_config(self, path: Path) -> CameraConfig: ...

    def load_playfield(self, path: Path) -> PlayfieldDefinition: ...


@runtime_checkable
class Directories(Protocol):
    """Where files live (``docs/design/configuration.md``).

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
    def config_dir(self) -> Path: ...

    @property
    def state_dir(self) -> Path: ...

    @property
    def socket_dir(self) -> Path:
        """Runtime plumbing. Not configurable — nobody needs to choose."""
        ...
