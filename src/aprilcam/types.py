"""Shared value types.

The vocabulary every subsystem speaks. These are data, not behaviour —
the protocols in each subsystem's ``interfaces.py`` are defined in terms
of them.

Two conventions from ``docs/design/specification.md`` §7 are load-bearing
here and are never re-derived:

* **World frame** — A1-centred, ``+x`` east, ``+y`` north, units **cm**.
  The origin shift is a pure subtraction on both axes, never a y-flip.
* **Heading** — ``atan2(world_vy, world_vx)``; 0 = east, CCW positive.

Positions crossing a subsystem boundary are world coordinates. Pixel
coordinates are legitimate in exactly two places: inside the daemon's
detection internals before conversion, and at the final render step where
a client maps world → pixels for a specific image.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Union


class TagFamily(str, Enum):
    """Which fiducial family a tag id belongs to.

    Family is **part of a tag's identity**, never inferred from the id:
    AprilTag 1 and ArUco 1 are different tags, and ``main-playfield``
    genuinely contains both (UC-09.S-3).

    v1 encoded ArUco *N* internally as tid ``-(N+1)`` to avoid collision.
    That encoding is a daemon implementation detail and must never appear
    in an interface — no client sends or receives a negative tag id.
    """

    APRILTAG = "apriltag"
    ARUCO = "aruco"


@dataclass(frozen=True)
class TagId:
    """A tag's full identity: family plus number."""

    family: TagFamily
    number: int


@dataclass(frozen=True)
class WorldPoint:
    """A position on the playfield, in cm, A1-centred, +x east +y north."""

    x: float
    y: float


@dataclass(frozen=True)
class PixelPoint:
    """A position in an image, in pixels, origin top-left.

    Diagnostic or render-time only. A pixel position that escapes into
    stored state or a client contract is a bug, except as a calibration
    anchor (``corner_pixels``, ``static_markers``) where it *defines* the
    mapping.
    """

    x: float
    y: float


@dataclass(frozen=True)
class TagRecord:
    """One tag, as observed in one frame.

    **World-first contract** (spec §4.1). ``world`` is the answer;
    ``pixel`` is diagnostic. On an uncalibrated camera ``world`` is
    ``None`` and ``pixel`` is the only available representation — which
    consumers must be able to tell apart from a valid ``(0, 0)``. This
    is why ``world`` is Optional rather than defaulting to the origin.

    Mount corrections (``Camera``/registry, UC-12) are **already
    applied**: this reports where the *robot* is, not where its tag is.

    **Boundary and hat contract.** Every record carries enough to draw
    the tag without any vision processing: the boundary quad and the
    facing direction (the "hat" — the marker on the tag's front edge).

    * ``yaw_rad`` is the direction the tag's **front faces** (0 = east,
      CCW positive — same convention as heading). It is orientation;
      ``heading_rad`` is motion direction from velocity. A robot driving
      backwards has them 180° apart, which is signal, not error.
      ``mount_yaw_rad`` is already applied, so this is the *robot's*
      facing.
    * Corners are the **observed tag outline**, ordered
      ``(front-left, front-right, back-right, back-left)`` in the tag's
      own frame — the ``corners[0]→corners[1]`` edge is the front edge
      the hat sits on. Same physical order in world and pixel space.
      Unlike ``world``/``yaw_rad``, corners are **not mount-corrected**:
      they outline the tag as seen, because their purpose is drawing on
      the image, not locating the robot.
    * ``world_corners`` (calibrated only) is the contract for drawing on
      the deskewed stream, converted via the frame's published metadata.
      ``pixel_corners`` is in the **served raw stream's pixel space**
      and is the representation for drawing on raw frames — where no
      client-side world→pixel mapping exists — and the only one on an
      uncalibrated camera. This is principle 5's render-step carve-out,
      not a leak of the internal pipeline.
    """

    tag: TagId
    world: Optional[WorldPoint]
    heading_rad: Optional[float]
    world_velocity: Optional[WorldPoint]
    speed: Optional[float]
    timestamp: float
    pixel: Optional[PixelPoint] = None
    calibrated: bool = True
    yaw_rad: Optional[float] = None
    world_corners: Optional[
        tuple[WorldPoint, WorldPoint, WorldPoint, WorldPoint]
    ] = None
    pixel_corners: Optional[
        tuple[PixelPoint, PixelPoint, PixelPoint, PixelPoint]
    ] = None


@dataclass(frozen=True)
class TagFrame:
    """Every tag detected in a single frame, with when it was captured."""

    tags: tuple[TagRecord, ...]
    timestamp: float
    frame_index: int


@dataclass(frozen=True)
class Marker:
    """A point of interest, optionally labelled."""

    at: WorldPoint
    label: Optional[str] = None


@dataclass(frozen=True)
class PathShape:
    """An open polyline — a route, a trajectory, a planned motion."""

    points: tuple[WorldPoint, ...]


@dataclass(frozen=True)
class Polygon:
    """A closed region — a zone, a keep-out area."""

    points: tuple[WorldPoint, ...]


@dataclass(frozen=True)
class Circle:
    """A circular region. ``radius_cm`` is world units, like everything."""

    center: WorldPoint
    radius_cm: float


@dataclass(frozen=True)
class Text:
    """A free-standing label anchored at a world position."""

    at: WorldPoint
    text: str


AnnotationShape = Union[Marker, PathShape, Polygon, Circle, Text]
"""One shape per annotation, valid by construction.

A union rather than a kind-enum with optional fields for the same
reason ``TagRecord.world`` is Optional rather than zero-defaulted:
states that make no sense (a circle with path points) must be
unrepresentable, not merely discouraged.
"""


@dataclass(frozen=True)
class Style:
    """Presentation hints for an annotation.

    **Geometry is world-cm, always; style is presentation.** Style may
    be pixel-denominated (``line_width`` is pixels on the rendered
    image) and a renderer may ignore any hint. Do not "fix" line width
    to cm — it is not geometry.
    """

    color: Optional[str] = None
    line_width: Optional[float] = None


@dataclass(frozen=True)
class Annotation:
    """One drawing on a playfield, held by the daemon (UC-28).

    Keyed **(playfield, layer, id)** — ``id`` is client-chosen and
    unique *within its layer*, so two layers can both hold a ``goal``.
    There is **no ownership**: any client may overwrite or delete any
    annotation, and a colliding upsert is last-write-wins by contract
    (UC-28.S-6), not a race bug.

    Annotations are **runtime state, not configuration** — in-memory in
    the daemon, gone on restart, re-drawn by clients on reconnect, like
    ``MountParameters`` registrations. Nothing about them touches disk.

    Open (UC-28): ``ttl_seconds`` exists because ownerless annotations
    from a crashed client otherwise linger until restart — but whether
    upsert resets the clock, whether pruning is lazy at read/embed time,
    and whether expiry is observable are all undecided.
    """

    id: str
    layer: str
    shape: AnnotationShape
    style: Style = Style()
    ttl_seconds: Optional[float] = None


@dataclass(frozen=True)
class AnnotationSet:
    """A playfield's complete annotation state at one moment.

    ``revision`` is monotonic and bumps on every mutation. Frames embed
    this set at frame rate, so an unchanged revision is the renderer's
    licence to skip re-rasterizing the overlay — the same economy
    thinking as the change-driven tag stream.

    Open (UC-28): maximum annotations per playfield/layer and points
    per path are unbounded here; the daemon needs limits (memory and
    per-frame wire cost) and a defined behaviour at the limit.
    """

    annotations: tuple[Annotation, ...]
    revision: int
    timestamp: float


@dataclass(frozen=True)
class ImageFrame:
    """An encoded image plus what is needed to interpret it.

    ``world_extent`` and ``px_per_cm`` are present only for deskewed
    frames, where the mapping is uniform and exact by construction. They
    are **derived and reported, never chosen** — the field is scaled
    aspect-preserved into a bounding box and whatever px-per-cm falls out
    is what gets published (spec §4.1).

    A client that rescales the image owns the conversion from then on,
    which is why the daemon publishes world coordinates plus metadata
    rather than pre-baked pixel positions.

    **Frames carry tags.** When detection is running on the source
    camera, ``tags`` is the :class:`TagFrame` from the detection pass
    nearest this frame's capture (normally the same frame) — so one
    fetched frame is sufficient to render a fully annotated view:
    boundaries and hats from the records' corners and yaw, positions
    from world coordinates. The three-way distinction is deliberate:
    ``None`` means detection is off, an empty ``TagFrame`` means
    detection ran and saw nothing, and the embedded frame's own
    timestamp exposes any capture↔detection skew rather than hiding it.

    **Deskewed frames carry annotations** (UC-28, spec §9 #8).
    ``annotations`` is the linked playfield's current
    :class:`AnnotationSet`; with the embedded tags, one deskewed frame
    is everything a renderer needs. **Raw frames never carry them**:
    annotations are world-only and a raw frame publishes no
    world→pixel mapping to render them with — fetch the structured set
    from the daemon instead. Same three-way distinction as ``tags``:
    ``None`` = not carried on this frame kind, empty = nothing drawn.

    Open (UC-28): whether a deskewed frame embeds an *empty* set or
    omits it — the None/empty boundary above assumes it embeds.
    """

    data: bytes
    width: int
    height: int
    encoding: str
    timestamp: float
    deskewed: bool = False
    px_per_cm: Optional[float] = None
    world_extent: Optional[tuple[float, float]] = None
    tags: Optional[TagFrame] = None
    annotations: Optional[AnnotationSet] = None


@dataclass(frozen=True)
class MountParameters:
    """How a tag sits on its robot (UC-12).

    Turns an observed tag pose into the robot's pose. ``mount_z`` drives
    the parallax projection down onto the field plane; ``mount_x``/
    ``mount_y`` offset from the robot's reference point in the robot's
    own frame; ``mount_yaw`` rotates so reported heading is the robot's.

    These are **runtime registrations, not calibration data** —
    calibration stays robot-agnostic (spec §2.4). They are in-memory
    only and do not survive a daemon restart — v1's persisted
    ``tags.json`` is dropped (2026-07-31). A client that expects its
    tag to have a height or mount orientation **registers it when it
    connects, every session**, like re-drawing annotations
    (UC-28.S-4).

    Open (UC-12): the robot's "reference point" needs a stated convention
    — geometric centre, drive-axle centre, or client-defined. Offsets are
    meaningless without it.
    """

    size_cm: Optional[float] = None
    mount_x: float = 0.0
    mount_y: float = 0.0
    mount_z: float = 0.0
    mount_yaw_rad: float = 0.0


@dataclass(frozen=True)
class CameraState:
    """What is true about a camera right now (UC-14, UC-25).

    ``present`` false with a config still held is normal and not an
    error: a config outliving its hardware is expected when someone
    unplugs a camera (UC-25). ``usable`` false with ``present`` true is
    the "plugged in and broken" case, which must be distinguishable from
    "not plugged in" (UC-25.S-4).
    """

    name: str
    present: bool
    usable: bool
    playfield: Optional[str]
    calibrated: bool
    calibration_stale: bool = False
    frame_dark: bool = False
