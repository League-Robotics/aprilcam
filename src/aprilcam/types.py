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
    * ``image_yaw_deg`` (sprint 039) is a **separate, diagnostic-only**
      field, unrelated to ``yaw_rad`` and never a substitute for it. It
      is derived from the same ``corners[0]→corners[1]`` front edge, but
      in raw pixel space rather than world space, and with a different
      sign convention: **0° = the tag's front edge toward the top of the
      image, clockwise-positive as seen on screen** (image y grows
      downward, so this is the opposite rotational sense from
      ``yaw_rad``'s CCW-positive world convention). It is populated
      whenever ``pixel_corners`` is available — calibrated or not —
      specifically so an uncalibrated camera, which has no world frame
      to report ``yaw_rad`` in at all, still reports *some* orientation.
      It is never computed from world corners and never overrides or is
      overridden by ``yaw_rad``.
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
    image_yaw_deg: Optional[float] = None


@dataclass(frozen=True)
class TagFrame:
    """Every tag detected in a single frame, with when it was captured.

    ``calibration_stale`` is the in-band staleness carrier (UC-11.S-3,
    UC-18): when calibration goes stale mid-stream, the subscriber is
    told *in the payload* rather than silently receiving plausible
    wrong positions. Streams have no side channel — if the flag is not
    in the frame, the promise cannot be kept.
    """

    tags: tuple[TagRecord, ...]
    timestamp: float
    frame_index: int
    calibration_stale: bool = False


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

    **The anchor invariant** (what makes the conversion exact): a
    deskewed frame spans *exactly* the field rectangle — image ``(0,
    0)`` is the field's north-west corner, world ``(-width/2,
    +height/2)`` with ``world_extent = (width, height)``; north is up,
    east is right, and world→pixel flips y exactly once, inside this
    mapping. Scale plus this anchor is the whole conversion; without
    the anchor stated, ``px_per_cm`` alone would leave the origin to
    be guessed.

    ``frame_dark`` carries the darkness advisory in-band, tied to
    *this* frame — the ``CameraState`` flag says the camera has been
    producing dark frames; this says the frame in hand is one of them.

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
    calibration_stale: bool = False
    frame_dark: bool = False


@dataclass(frozen=True)
class MountParameters:
    """How a tag sits on its robot (UC-12).

    Turns an observed tag pose into the robot's pose. ``mount_z`` drives
    the parallax projection down onto the field plane; ``mount_x``/
    ``mount_y`` offset from the robot's reference point in the robot's
    own frame; ``mount_yaw`` rotates so reported heading is the robot's.

    These are **registrations, not calibration data** — calibration
    stays robot-agnostic (spec §2.4). A registration is written through
    to ``<state_dir>/mounts/registry.json`` by ``daemon/service.py``'s
    ``register_tag`` and reloaded at daemon startup (ticket 036-007),
    the same file-backed treatment ``calibration.json`` already gets —
    v1's persisted ``tags.json`` (dropped 2026-07-31) is effectively
    revived, in a new on-disk shape. A client registers a tag's mount
    once; it stays registered — and survives a daemon restart — until
    explicitly unregistered (``unregister_tag``). This differs from
    annotations (UC-28.S-4), which remain in-memory-only, per-session
    state.

    **Reference point: client-defined, not daemon-mandated** (resolved
    2026-08-03, sprint 025). The daemon has no way to inspect a robot's
    physical geometry, so it does not — and cannot — mandate a specific
    point (geometric centre, drive-axle centre, or anything else). It
    applies whatever ``mount_x``/``mount_y``/``mount_z``/``mount_yaw_rad``
    it is given, purely mechanically; what physical point on the robot
    those offsets are measured *from* is entirely the client's choice and
    responsibility. The packaged guide (UC-27) recommends centre of
    rotation as a sensible default for teams with no reason to choose
    otherwise, but that recommendation lives in the guide's prose, never
    enforced or even checked in code.

    **Frame convention: carried forward from v1, unchanged** (resolved
    2026-08-03, sprint 025) — the robot's own frame, ``+x`` forward,
    ``+y`` left, rotating with ``mount_yaw_rad``. Offsets are in this
    frame, not the world frame, because that is what makes yaw compose
    correctly (UC-12): as the robot turns in place, ``mount_x``/
    ``mount_y`` turn with it. This matches v1's own ``x_mm`` forward /
    ``y_mm`` left convention (``archive/src/aprilcam/daemon/
    mobile_tags.py``) exactly — no new sign convention was invented for
    v2. See ``calibration.geometry.apply_mount_offset`` for the composed
    math and a worked example of the seam between this frame and the
    world frame's own ``+x`` east / ``+y`` north / CCW-positive-yaw
    convention.
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
    flatfield_present: bool = False
    """Whether this camera currently has a flat-field gain-map correction
    applied (sprint 037). Overlaid by ``DaemonService.list_cameras`` from
    ``DaemonService.get_flatfield``, the same way ``calibrated`` is
    overlaid from ``get_calibration`` -- ``Camera`` itself has no notion
    of this daemon-level state."""
    flatfield_stale: bool = False
    """Whether the applied flat-field map may no longer match this
    camera's current mounting/lighting/resolution (sprint 037) --
    advisory only, mirroring ``calibration_stale``: a stale map is still
    applied, never withheld."""
    number: Optional[int] = None
    """Stable registry enumeration (UC-14.S-1).

    Assigned once by the registry and **never reshuffled** when cameras
    come and go — the number a developer scribbles on a sticky note must
    still mean the same camera next week. ``None`` only for a camera not
    yet registered.
    """


@dataclass(frozen=True)
class ProbeSkip:
    """One backend a probe could not interrogate, and why (UC-25.S-7)."""

    backend: str
    reason: str


@dataclass(frozen=True)
class ProbeReport:
    """What a probe found, and what it honestly could not check.

    ``CameraDiscovery.probe``'s contract — skip a backend rather than
    disturb live subscribers, time out a wedged subprocess into a
    partial result — is only keepable if the result can *say so*. A
    bare camera list would present a partial probe as a complete one
    (UC-25.S-7). ``skipped`` empty means the probe really was complete.
    """

    cameras: tuple[CameraState, ...]
    skipped: tuple[ProbeSkip, ...] = ()
