"""``Renderer``: rasterizes a frame's embedded tag overlay and
annotations to an image, client-side, cv2-free (tag half: ticket
022-005, sprint.md Decision 5, reworked to v1-parity by ticket 032-001;
annotation half: ticket 024-003).

Implements the full ``client.interfaces.Renderer.render()`` contract:
a per-tag overlay from the frame's embedded ``TagFrame`` plus one draw
routine per ``AnnotationShape`` (``Marker``/``PathShape``/``Polygon``/
``Circle``/``Text``) for the frame's embedded ``AnnotationSet`` and any
caller-supplied ``extra``. Draws with Pillow (``PIL.Image``/
``PIL.ImageDraw``/``PIL.ImageFont``), never ``cv2`` -- the same
discipline ``view/viewer.py`` already established for decoding served
frames (``tests/test_no_cv2_in_client_modules.py`` sweeps this whole
package; adding this module needs no change to that test).

**The tag overlay, mined from v1** (ticket 032-001;
``aprilcamv1/ui/display.py:162-278``, sprint 032's issue/sprint.md).
Per tag, in order:

* An open red (``#FF0000``) three-sided boundary --
  ``front_right``->``back_right``->``back_left``->``front_left``. The
  front edge (``front_left``->``front_right``, ``TagRecord``'s own
  corner-order contract) is *never* drawn red; it is the one side the
  hat closes instead.
* A green (``#00FF00``) peaked "hat" over that open edge: two lines
  from ``front_left`` and ``front_right`` to a shared apex at
  ``mid(front_left, front_right) + top_dir * clamp(0.35 * |front_right
  - front_left|, 6px, 80px)``, where ``top_dir`` is the unit vector
  from the boundary quad's own centroid through that midpoint -- v1's
  formula, re-derived in Pillow rather than ported (v1's own drawing is
  cv2-baked, and this project's client side is deliberately not).
* The tag's own id number (``record.tag.number``), centred on the same
  centroid the hat direction is computed from -- red fill, black
  outline, via Pillow's ``stroke_width``/``stroke_fill`` (both
  available since Pillow 8.0, well under this project's >=10.0 floor)
  so the label reads against a light or dark background alike.
* A yellow (``#FFFF00``) crosshair at ``record.world`` -- the
  parallax/mount-corrected position the daemon already computes
  (sprint 029), converted through the exact same
  ``world_to_deskewed_pixel`` the boundary uses -- with arm length the
  pixel length of 0.5 world-cm at *that frame's* scale, floored at 4px
  so it stays visible at a very small ``px_per_cm``. ``world_corners``
  are deliberately *not* mount-corrected (``types.TagRecord``'s own
  docstring), so the boundary/hat outline the tag as physically seen
  while the crosshair marks the corrected position a robot program
  would actually use -- drawing both, at potentially different points,
  is the entire reason the crosshair exists. It draws only when
  ``record.world is not None`` *and* the frame is deskewed with its own
  ``px_per_cm``/``world_extent`` published -- a raw frame has no
  world->pixel mapping at all, and an uncalibrated observation has no
  corrected position to mark; both are silent skips, mirroring
  ``_frame_can_place_annotations``'s discipline below, never a raised
  error.

**Raw frames draw the boundary/hat/id from ``pixel_corners`` instead**,
in the served raw stream's own pixel space -- no world metadata exists
to convert with on a raw frame (``types.TagRecord``'s docstring). The
crosshair never draws on a raw frame regardless of what ``record.world``
holds, since there is no pixel mapping to convert it through. A record
whose boundary cannot be drawn at all in the frame kind at hand (no
``world_corners`` on a deskewed frame, no ``pixel_corners`` on a raw
one) is skipped entirely -- crosshair included, since there is nothing
about that record this frame can place anywhere.

**No calibration/homography dependency for placing a tag.** A deskewed
frame's ``TagRecord.world_corners``/``world`` are converted to pixels
using nothing but *that frame's own published* ``px_per_cm``/
``world_extent`` metadata, via
``calibration.geometry.world_to_deskewed_pixel`` -- the same
anchor-invariant function ``calibration/deskew.py`` uses to build the
warp's destination quad (see that module's docstring), imported here
rather than re-derived so both sides of "does the warp really put
things where the renderer expects" share one formula. That function is
pure math with no ``cv2`` import of its own (verified by
``tests/test_no_cv2_in_client_modules.py``'s AST scan, which checks
every file's own literal imports, not what it transitively pulls in) --
consistent with ``client/models.py`` already importing
``calibration.calibration``/``calibration.interfaces`` for wire
marshalling. This module never touches a ``Calibration`` object or a
homography: "a client that rescales the image owns the conversion from
then on" (``client.interfaces.Renderer``'s own docstring) means exactly
this -- the daemon publishes metadata, not pixel positions, and a
client with only that metadata (never the daemon-only homography) must
be able to do the whole job.

**Annotations share the tag half's exact world->pixel formula**
(ticket 024-003). ``AnnotationShape`` geometry (``types.py``'s
``Marker``/``PathShape``/``Polygon``/``Circle``/``Text``, always
``WorldPoint``s or a world-cm radius) is converted through the same
``world_to_deskewed_pixel`` the tag half already imports and uses --
one formula, shared, so the two halves cannot disagree about where the
anchor sits or which way y flips (``types.ImageFrame``'s anchor
invariant: "world->pixel flips y exactly once, inside the published
mapping"). Annotations therefore only draw on a **deskewed** frame with
its ``px_per_cm``/``world_extent`` metadata present; a raw frame -- or
a deskewed frame missing that metadata -- draws none, the same
"skip rather than raise" discipline ``_tag_points`` already applies
below. ``frame.annotations`` embeds the daemon's current
``AnnotationSet`` for deskewed frames (``None`` on raw frames, by
contract); ``layers`` filters which of its layers get drawn (``None`` =
all -- ``view/viewer.py``'s default). ``extra`` is drawn identically,
alongside whatever ``frame.annotations`` contributes, whether that is
``None``, empty, or populated -- it is the caller's own local, ad-hoc
overlay (never sent to the daemon) and is never filtered by ``layers``,
since a caller that explicitly passed a shape plainly wants it drawn.
Each shape's ``Style`` supplies presentation hints only
(``types.Style``'s own docstring: geometry is world-cm, style is
presentation, and a renderer may ignore any hint); an absent
``color``/``line_width`` falls back to this module's own documented
default rather than crashing or silently skipping the geometry --
see ``_DEFAULT_ANNOTATION_COLOR``/``_DEFAULT_LINE_WIDTH_PX`` below.
"""

from __future__ import annotations

import io
import math
from typing import List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

from aprilcam.calibration.geometry import world_to_deskewed_pixel
from aprilcam.client.interfaces import Renderer as _RendererBase
from aprilcam.types import (
    Annotation,
    Circle,
    ImageFrame,
    Marker,
    PathShape,
    Polygon,
    Style,
    TagFamily,
    TagRecord,
    Text,
    WorldPoint,
)

__all__ = ["Renderer"]

#: The boundary quad's three drawn sides (never the front edge) -- v1's
#: own colour for this exact shape (``aprilcamv1/ui/display.py:162-278``,
#: mined by ticket 032-001). A literal hex string, not a Pillow named
#: colour, so the exact RGB triplet the acceptance criteria pin
#: (``#FF0000``) is unambiguous from the source alone.
_BOUNDARY_COLOR = "#FF0000"

#: The peaked "hat" that closes the boundary's open front edge --
#: deliberately the opposite colour from the boundary so the facing
#: direction reads at a glance against the U (v1's own convention,
#: mined the same way as ``_BOUNDARY_COLOR``).
_HAT_COLOR = "#00FF00"

#: How far the hat's apex extends beyond the front edge's own midpoint,
#: as a fraction of the front edge's own (pixel) length -- v1's
#: ``top_dir * clamp(0.35 * |p1 - p0|, 6, 80)`` formula, mined
#: byte-for-byte from ``aprilcamv1/ui/display.py``. Purely a
#: presentation choice, same footing as the old ``_HAT_LENGTH_RATIO``
#: this replaces.
_HAT_APEX_RATIO = 0.35

#: Minimum hat height in pixels -- v1's own floor, keeping the peak
#: visible even on a tag that renders very small (far from the camera,
#: or lightly zoomed).
_HAT_APEX_MIN_PX = 6.0

#: Maximum hat height in pixels -- v1's own ceiling, keeping the peak
#: proportionate rather than dwarfing a very large/close tag.
_HAT_APEX_MAX_PX = 80.0

#: The tag-id label's text colour -- the same red as the boundary (v1's
#: own choice: the label and the U read as one "this is the tag" set,
#: distinct from the hat's green).
_ID_LABEL_COLOR = "#FF0000"

#: The label's outline colour -- v1 draws the outline first at
#: thickness+2 under a thickness-2 fill; Pillow's ``stroke_width``/
#: ``stroke_fill`` achieve the same legible-on-any-background effect in
#: one call, so only the outline's own colour needs naming here.
_ID_LABEL_OUTLINE_COLOR = "black"

#: The label outline's stroke width in pixels -- matches v1's own
#: "+2 over the fill" outline weight closely enough by eye; a
#: presentation constant, not geometry.
_ID_LABEL_STROKE_WIDTH_PX = 2

#: The label's point size as a fraction of the tag's own front-edge
#: pixel length -- so the digits scale with the tag instead of staying
#: a fixed size regardless of how large the tag renders. Eyeballed
#: against v1's ``FONT_HERSHEY_SIMPLEX`` scale 0.8 (the ticket's own
#: framing: "a presentation constant, not geometry" -- there is no
#: principled conversion from an OpenCV font scale to a Pillow point
#: size, only a visual match).
_ID_LABEL_FONT_SIZE_RATIO = 0.6

#: Floor on the label's point size, so a tiny/far tag's id stays
#: readable rather than shrinking to nothing.
_ID_LABEL_FONT_SIZE_MIN_PX = 10.0

#: Ceiling on the label's point size, so a very close/large tag's id
#: does not balloon to dominate the frame.
_ID_LABEL_FONT_SIZE_MAX_PX = 60.0

#: The corrected-position crosshair's colour -- yellow, distinct from
#: both the red boundary/label and the green hat, matching v1's own
#: choice for this exact marker.
_CROSSHAIR_COLOR = "#FFFF00"

#: The world-cm distance the crosshair's arm should represent at
#: *frame*'s own scale -- v1: "the pixel length of 0.5cm at that spot".
_CROSSHAIR_ARM_CM = 0.5

#: Floor on the crosshair's arm length in pixels -- v1's own floor,
#: keeping the cross visible even at a very small ``px_per_cm``.
_CROSSHAIR_MIN_ARM_PX = 4.0

#: Shared stroke width for every tag-overlay line (boundary, hat,
#: crosshair) -- v1 draws all of these at thickness 2.
_TAG_LINE_WIDTH_PX = 2

_Point = Tuple[float, float]


def _tag_points(record: TagRecord, frame: ImageFrame) -> Optional[Tuple[_Point, _Point, _Point, _Point]]:
    """The four boundary-quad points to draw for *record*, in *frame*'s
    own pixel space -- ``None`` when this record carries nothing this
    frame kind can draw with (e.g. a raw record with no
    ``pixel_corners``, or a deskewed frame that -- inconsistently with
    its own ``deskewed=True`` -- carries no ``px_per_cm``/
    ``world_extent``; both are simply skipped rather than raising, the
    same "draw what is there" discipline the module docstring states
    for annotations).

    Order is preserved from ``TagRecord``'s own contract throughout:
    ``(front-left, front-right, back-right, back-left)`` -- point 0 to
    point 1 is always the front edge the hat sits on.
    """
    if frame.deskewed:
        if record.world_corners is None or frame.px_per_cm is None or frame.world_extent is None:
            return None
        pixels = tuple(
            world_to_deskewed_pixel(c, world_extent=frame.world_extent, px_per_cm=frame.px_per_cm)
            for c in record.world_corners
        )
        return (
            (pixels[0].x, pixels[0].y),
            (pixels[1].x, pixels[1].y),
            (pixels[2].x, pixels[2].y),
            (pixels[3].x, pixels[3].y),
        )

    if record.pixel_corners is None:
        return None
    corners = record.pixel_corners
    return (
        (corners[0].x, corners[0].y),
        (corners[1].x, corners[1].y),
        (corners[2].x, corners[2].y),
        (corners[3].x, corners[3].y),
    )


def _centroid(points: Tuple[_Point, _Point, _Point, _Point]) -> _Point:
    """The boundary quad's own centre -- the point both the hat's
    direction and the id label's position are computed from."""
    return (
        sum(p[0] for p in points) / len(points),
        sum(p[1] for p in points) / len(points),
    )


def _midpoint(a: _Point, b: _Point) -> _Point:
    return ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)


def _distance(a: _Point, b: _Point) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


def _normalized(vector: _Point) -> _Point:
    """*vector* scaled to unit length -- falls back to straight "up" in
    image space (``(0, -1)``) on the degenerate zero-length case (a
    front edge whose midpoint coincides exactly with the quad's own
    centroid, which real detection geometry never produces but a
    hand-built fixture could), rather than raising a divide-by-zero.
    """
    length = math.hypot(vector[0], vector[1])
    if length < 1e-9:
        return (0.0, -1.0)
    return (vector[0] / length, vector[1] / length)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _hat_apex(front_left: _Point, front_right: _Point, centroid: _Point) -> _Point:
    """v1's peaked-hat apex (module docstring): the front edge's own
    midpoint, pushed away from the quad's centre by a fraction of the
    front edge's own length, floored and ceilinged (``_HAT_APEX_MIN_PX``/
    ``_HAT_APEX_MAX_PX``) so the peak stays visible on a tiny tag and
    proportionate on a huge one.
    """
    mid = _midpoint(front_left, front_right)
    top_dir = _normalized((mid[0] - centroid[0], mid[1] - centroid[1]))
    height = _clamp(
        _HAT_APEX_RATIO * _distance(front_left, front_right), _HAT_APEX_MIN_PX, _HAT_APEX_MAX_PX
    )
    return (mid[0] + top_dir[0] * height, mid[1] + top_dir[1] * height)


def _draw_boundary_and_hat(draw: ImageDraw.ImageDraw, points: Tuple[_Point, _Point, _Point, _Point]) -> None:
    """Draw the open red "U" (the boundary quad's three non-front sides)
    plus the green peaked hat that closes it, from *points* alone -- no
    ``yaw_rad`` needed: the front edge (``points[0]`` -> ``points[1]``)
    already carries the facing direction (``daemon/detection.py``'s
    ``_front_edge_yaw`` derives ``yaw_rad`` from this same edge, so
    re-deriving a direction from ``yaw_rad`` here would be a second,
    independently-signed computation of the same fact -- exactly the
    kind of duplicated geometry this project's sign discipline warns
    against).
    """
    front_left, front_right, back_right, back_left = points
    draw.line([front_right, back_right], fill=_BOUNDARY_COLOR, width=_TAG_LINE_WIDTH_PX)
    draw.line([back_right, back_left], fill=_BOUNDARY_COLOR, width=_TAG_LINE_WIDTH_PX)
    draw.line([back_left, front_left], fill=_BOUNDARY_COLOR, width=_TAG_LINE_WIDTH_PX)

    apex = _hat_apex(front_left, front_right, _centroid(points))
    draw.line([front_left, apex], fill=_HAT_COLOR, width=_TAG_LINE_WIDTH_PX)
    draw.line([front_right, apex], fill=_HAT_COLOR, width=_TAG_LINE_WIDTH_PX)


def _label_font_size(points: Tuple[_Point, _Point, _Point, _Point]) -> float:
    """The id label's point size -- proportional to the tag's own
    front-edge pixel length (``_ID_LABEL_FONT_SIZE_RATIO``), clamped so
    it stays legible on a tiny tag and bounded on a huge one."""
    front_edge_px = _distance(points[0], points[1])
    return _clamp(
        front_edge_px * _ID_LABEL_FONT_SIZE_RATIO, _ID_LABEL_FONT_SIZE_MIN_PX, _ID_LABEL_FONT_SIZE_MAX_PX
    )


def _draw_tag_id(draw: ImageDraw.ImageDraw, points: Tuple[_Point, _Point, _Point, _Point], number: int) -> None:
    """The tag's own id number, centred on the boundary quad's centroid
    -- red fill with a black outline (Pillow's ``stroke_width``/
    ``stroke_fill``, module docstring) so it reads against a light or
    dark background alike, matching v1's own outlined-text convention.
    """
    center = _centroid(points)
    font = ImageFont.load_default(size=_label_font_size(points))
    draw.text(
        center,
        str(number),
        anchor="mm",
        fill=_ID_LABEL_COLOR,
        font=font,
        stroke_width=_ID_LABEL_STROKE_WIDTH_PX,
        stroke_fill=_ID_LABEL_OUTLINE_COLOR,
    )


def _crosshair_pixel(record: TagRecord, frame: ImageFrame) -> Optional[_Point]:
    """*record*'s corrected world position (``record.world``), converted
    to *frame*'s own deskewed pixel space -- ``None`` when there is
    nothing to convert: an uncalibrated observation (``world is None``)
    or a frame with no world->pixel mapping at all (raw, or -- mirroring
    ``_frame_can_place_annotations``'s own guard for annotations below
    -- a deskewed frame missing its own ``px_per_cm``/``world_extent``
    metadata). This check is independent of ``_tag_points``: it knows
    nothing about corners, only about *this* record's world position and
    *this* frame's mapping -- the caller (``Renderer.render``) is what
    decides a record gets no drawing at all when neither corner
    representation is available.
    """
    if record.world is None:
        return None
    if not frame.deskewed or frame.px_per_cm is None or frame.world_extent is None:
        return None
    pixel = world_to_deskewed_pixel(record.world, world_extent=frame.world_extent, px_per_cm=frame.px_per_cm)
    return (pixel.x, pixel.y)


def _crosshair_arm_px(frame: ImageFrame) -> float:
    """Arm length in pixels: the pixel length of ``_CROSSHAIR_ARM_CM``
    at *frame*'s own scale, floored at ``_CROSSHAIR_MIN_ARM_PX``.
    Callers only reach this once ``_crosshair_pixel`` has already
    returned non-``None`` for the same frame, so ``frame.px_per_cm`` is
    always present here (the same calling discipline ``_to_pixel``,
    below, relies on for the annotation half).
    """
    assert frame.px_per_cm is not None
    return max(_CROSSHAIR_MIN_ARM_PX, _CROSSHAIR_ARM_CM * frame.px_per_cm)


def _draw_crosshair(draw: ImageDraw.ImageDraw, pixel: _Point, arm_px: float) -> None:
    """A yellow cross centred on *pixel*, each arm ``arm_px`` long."""
    x, y = pixel
    draw.line([(x - arm_px, y), (x + arm_px, y)], fill=_CROSSHAIR_COLOR, width=_TAG_LINE_WIDTH_PX)
    draw.line([(x, y - arm_px), (x, y + arm_px)], fill=_CROSSHAIR_COLOR, width=_TAG_LINE_WIDTH_PX)


# ---------------------------------------------------------------------------
# Annotations (ticket 024-003): one draw routine per AnnotationShape, all
# sharing _to_pixel/_color/_line_width below so every shape agrees on the
# same world->pixel formula and the same Style-default behaviour.
# ---------------------------------------------------------------------------

#: Default annotation colour when a ``Style`` leaves ``color`` unset --
#: distinct from the tag overlay's own palette (``_BOUNDARY_COLOR``/
#: ``_ID_LABEL_COLOR`` red, ``_HAT_COLOR`` green, ``_CROSSHAIR_COLOR``
#: also yellow but never on the same pixels -- annotations and tags
#: never share a frame region) so annotations read as their own layer
#: at a glance rather than being mistaken for tag overlays. ``Style`` is
#: presentation (``types.Style``'s own docstring); choosing a default
#: for an absent hint is squarely this renderer's job.
_DEFAULT_ANNOTATION_COLOR = "yellow"

#: Default stroke width in pixels when a ``Style`` leaves ``line_width``
#: unset -- matches the tag overlay's own hard-coded width
#: (``_TAG_LINE_WIDTH_PX``, above) for one consistent default stroke
#: weight across both halves of this renderer.
_DEFAULT_LINE_WIDTH_PX = 2

#: Fixed pixel radius for a ``Marker``'s point dot. ``Marker`` carries no
#: radius of its own (``types.Marker``: "a point of interest"), so this
#: is a presentation constant, not derived from any annotation field --
#: same footing as the tag overlay's own presentation constants
#: (``_HAT_APEX_RATIO`` and friends) above.
_MARKER_RADIUS_PX = 4.0


def _to_pixel(point: WorldPoint, frame: ImageFrame) -> _Point:
    """A ``WorldPoint`` converted to *frame*'s deskewed pixel space.
    Callers only reach this once ``_frame_can_place_annotations`` is
    true, so ``frame.px_per_cm``/``frame.world_extent`` are always
    present here.
    """
    assert frame.px_per_cm is not None and frame.world_extent is not None
    pixel = world_to_deskewed_pixel(point, world_extent=frame.world_extent, px_per_cm=frame.px_per_cm)
    return (pixel.x, pixel.y)


def _color(style: Style) -> str:
    return style.color if style.color is not None else _DEFAULT_ANNOTATION_COLOR


def _line_width(style: Style) -> int:
    if style.line_width is None:
        return _DEFAULT_LINE_WIDTH_PX
    return max(1, round(style.line_width))


def _draw_marker(draw: ImageDraw.ImageDraw, shape: Marker, style: Style, frame: ImageFrame) -> None:
    x, y = _to_pixel(shape.at, frame)
    color = _color(style)
    r = _MARKER_RADIUS_PX
    draw.ellipse([x - r, y - r, x + r, y + r], fill=color)
    if shape.label:
        draw.text((x + r + 2.0, y - r), shape.label, fill=color)


def _draw_path(draw: ImageDraw.ImageDraw, shape: PathShape, style: Style, frame: ImageFrame) -> None:
    """An open polyline (``ImageDraw.line`` with the full point list) --
    a route or trajectory, never closed back to its own start."""
    if len(shape.points) < 2:
        return  # Nothing to connect -- draw.line needs at least two points.
    points = [_to_pixel(p, frame) for p in shape.points]
    draw.line(points, fill=_color(style), width=_line_width(style))


def _draw_polygon(draw: ImageDraw.ImageDraw, shape: Polygon, style: Style, frame: ImageFrame) -> None:
    """A closed region -- ``ImageDraw.polygon`` closes the outline back
    to its first point automatically, unlike ``_draw_path``'s open line."""
    if len(shape.points) < 2:
        return
    points = [_to_pixel(p, frame) for p in shape.points]
    draw.polygon(points, outline=_color(style), width=_line_width(style))


def _draw_circle(draw: ImageDraw.ImageDraw, shape: Circle, style: Style, frame: ImageFrame) -> None:
    """``radius_cm`` (world units) scaled to pixels by *frame*'s own
    ``px_per_cm`` -- uniform scale by construction (``types.ImageFrame``'s
    docstring), so this one multiplication is exact with no distortion
    to correct for, per the ticket's own framing."""
    assert frame.px_per_cm is not None
    cx, cy = _to_pixel(shape.center, frame)
    radius_px = shape.radius_cm * frame.px_per_cm
    draw.ellipse(
        [cx - radius_px, cy - radius_px, cx + radius_px, cy + radius_px],
        outline=_color(style),
        width=_line_width(style),
    )


def _draw_text(draw: ImageDraw.ImageDraw, shape: Text, style: Style, frame: ImageFrame) -> None:
    x, y = _to_pixel(shape.at, frame)
    draw.text((x, y), shape.text, fill=_color(style))


def _draw_annotation(draw: ImageDraw.ImageDraw, annotation: Annotation, frame: ImageFrame) -> None:
    """Dispatch on *annotation*'s shape -- ``isinstance``, not a
    kind-enum, per ``AnnotationShape``'s own "valid by construction"
    design (``types.py``)."""
    shape = annotation.shape
    style = annotation.style
    if isinstance(shape, Marker):
        _draw_marker(draw, shape, style, frame)
    elif isinstance(shape, PathShape):
        _draw_path(draw, shape, style, frame)
    elif isinstance(shape, Polygon):
        _draw_polygon(draw, shape, style, frame)
    elif isinstance(shape, Circle):
        _draw_circle(draw, shape, style, frame)
    elif isinstance(shape, Text):
        _draw_text(draw, shape, style, frame)
    else:  # pragma: no cover -- AnnotationShape is a closed Union of the above five.
        raise AssertionError(f"unhandled annotation shape: {type(shape)!r}")


def _frame_can_place_annotations(frame: ImageFrame) -> bool:
    """Whether *frame* carries what ``_to_pixel`` needs -- a deskewed
    frame with its ``px_per_cm``/``world_extent`` metadata present.
    Annotations are world-coordinate, so a raw frame (no such metadata,
    by contract -- ``types.ImageFrame``'s docstring) has nothing to
    convert with; this is checked once up front so every shape's draw
    routine can assume the metadata is there rather than re-checking."""
    return frame.deskewed and frame.px_per_cm is not None and frame.world_extent is not None


class Renderer(_RendererBase):
    """Concrete ``client.interfaces.Renderer`` -- both the tag half
    (sprint 022, reworked to v1-parity by ticket 032-001) and the
    annotation half (ticket 024-003). See the module docstring for the
    shared world->pixel formula both use.
    """

    def render(
        self,
        frame: ImageFrame,
        *,
        layers: Optional[Tuple[str, ...]] = None,
        extra: Tuple[Annotation, ...] = (),
    ) -> bytes:
        """See ``client.interfaces.Renderer.render``.

        AprilTag-family only (sprint 033 ticket 001): a record whose
        ``tag.family`` is not ``TagFamily.APRILTAG`` is skipped before
        any geometry is computed for it -- the field's ArUco 4x4
        calibration corner markers are detected (unaffected) but never
        boxed, hatted, ID-labeled, or crosshaired here. This check is
        independent of the equivalent filter in ``view/viewer.py``'s
        ``convert()`` (sprint 033 sprint.md Design Rationale: two
        inline checks, not a shared predicate), since this method reads
        the raw, unfiltered ``ImageFrame`` directly.

        Per tag (module docstring, ticket 032-001): the red 3-sided
        boundary, the green hat, and the id label draw together from
        ``_tag_points`` -- a record with no drawable corners in this
        frame kind (``points is None``) is skipped entirely, nothing
        drawn for it at all. The crosshair is then checked
        independently, via ``_crosshair_pixel`` -- present only when the
        record has a corrected world position *and* the frame is
        deskewed with its own ``px_per_cm``/``world_extent`` published;
        absent, nothing else about the record is affected, since the
        boundary/hat/id were already decided above.

        Annotations draw only on a deskewed frame carrying its own
        ``px_per_cm``/``world_extent`` (``_frame_can_place_annotations``)
        -- a raw frame, or a deskewed frame missing that metadata, draws
        no annotations at all rather than raising, matching the tag
        half's own "skip what cannot be converted" discipline.
        ``frame.annotations`` filtered by *layers* (``None`` = every
        layer) contributes first, then *extra* -- unfiltered by
        *layers*, since it is the caller's own explicit, local request
        -- draws alongside it, even when ``frame.annotations`` is
        ``None`` or empty.
        """
        image = Image.open(io.BytesIO(frame.data)).convert("RGB")
        draw = ImageDraw.Draw(image)

        if frame.tags is not None:
            for record in frame.tags.tags:
                if record.tag.family != TagFamily.APRILTAG:
                    continue
                points = _tag_points(record, frame)
                if points is None:
                    continue
                _draw_boundary_and_hat(draw, points)
                _draw_tag_id(draw, points, record.tag.number)
                crosshair_pixel = _crosshair_pixel(record, frame)
                if crosshair_pixel is not None:
                    _draw_crosshair(draw, crosshair_pixel, _crosshair_arm_px(frame))

        if _frame_can_place_annotations(frame):
            to_draw: List[Annotation] = []
            if frame.annotations is not None:
                for annotation in frame.annotations.annotations:
                    if layers is None or annotation.layer in layers:
                        to_draw.append(annotation)
            to_draw.extend(extra)
            for annotation in to_draw:
                _draw_annotation(draw, annotation, frame)

        out = io.BytesIO()
        image.save(out, format="PNG")
        return out.getvalue()
