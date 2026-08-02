"""``Renderer``: rasterizes a frame's embedded tag boundaries/"hats" and
annotations to an image, client-side, cv2-free (tag half: ticket
022-005, sprint.md Decision 5; annotation half: ticket 024-003).

Implements the full ``client.interfaces.Renderer.render()`` contract:
tag boundaries/hats from the frame's embedded ``TagFrame`` (unchanged
since sprint 022) plus one draw routine per ``AnnotationShape``
(``Marker``/``PathShape``/``Polygon``/``Circle``/``Text``) for the
frame's embedded ``AnnotationSet`` and any caller-supplied ``extra``.
Draws with Pillow (``PIL.Image``/``PIL.ImageDraw``), never ``cv2`` --
the same discipline ``view/viewer.py`` already established for decoding
served frames (``tests/test_no_cv2_in_client_modules.py`` sweeps this
whole package; adding this module needs no change to that test).

**No calibration/homography dependency for placing a tag.** A deskewed
frame's ``TagRecord.world_corners`` are converted to pixels using
nothing but *that frame's own published* ``px_per_cm``/``world_extent``
metadata, via ``calibration.geometry.world_to_deskewed_pixel`` -- the
same anchor-invariant function ``calibration/deskew.py`` uses to build
the warp's destination quad (see that module's docstring), imported
here rather than re-derived so both sides of "does the warp really put
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

**Raw frames draw from ``pixel_corners`` instead**, in the served raw
stream's own pixel space -- no world metadata exists to convert with on
a raw frame (``types.TagRecord``'s docstring). Nothing in this sprint's
viewer wiring actually calls this path (``camera view`` shows raw bytes
directly, no overlays, per UC-16 -- ``view/viewer.py``'s module
docstring), but the interface itself supports both frame kinds, so this
implements both rather than leaving the raw branch aspirational.

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
from typing import List, Optional, Tuple

from PIL import Image, ImageDraw

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
    TagRecord,
    Text,
    WorldPoint,
)

__all__ = ["Renderer"]

#: Boundary quad outline colour -- an ordinary Pillow named colour, no
#: significance beyond "visible against a typical field photo".
_BOUNDARY_COLOR = "lime"

#: The front "hat" marker's colour -- deliberately distinct from the
#: boundary outline so the facing direction reads at a glance.
_HAT_COLOR = "red"

#: How far the hat marker extends beyond the tag's front-edge midpoint,
#: as a fraction of the distance from the tag's centre to that midpoint
#: -- i.e. the hat tip sits this fraction *further out* than the front
#: edge itself. Purely a presentation choice (``types.Style``'s "style
#: is presentation, geometry is world-cm" principle, applied here to a
#: fixed drawing constant instead of a per-annotation ``Style``, since
#: tags have no ``Style`` of their own).
_HAT_LENGTH_RATIO = 0.6

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


def _draw_tag(draw: ImageDraw.ImageDraw, points: Tuple[_Point, _Point, _Point, _Point]) -> None:
    """Draw one tag's boundary quad plus its front "hat", from *points*
    alone -- no ``yaw_rad`` needed: the front edge (``points[0]`` ->
    ``points[1]``) already carries the facing direction (``daemon/
    detection.py``'s ``_front_edge_yaw`` derives ``yaw_rad`` from this
    same edge, so re-deriving a direction from ``yaw_rad`` here would be
    a second, independently-signed computation of the same fact --
    exactly the kind of duplicated geometry this project's sign
    discipline warns against). The hat is a short line from the front
    edge's midpoint, extended further away from the tag's centre.
    """
    draw.polygon(list(points), outline=_BOUNDARY_COLOR)

    front_left, front_right = points[0], points[1]
    center = (
        sum(p[0] for p in points) / len(points),
        sum(p[1] for p in points) / len(points),
    )
    mid = ((front_left[0] + front_right[0]) / 2.0, (front_left[1] + front_right[1]) / 2.0)
    hat_tip = (
        mid[0] + (mid[0] - center[0]) * _HAT_LENGTH_RATIO,
        mid[1] + (mid[1] - center[1]) * _HAT_LENGTH_RATIO,
    )
    draw.line([mid, hat_tip], fill=_HAT_COLOR, width=2)


# ---------------------------------------------------------------------------
# Annotations (ticket 024-003): one draw routine per AnnotationShape, all
# sharing _to_pixel/_color/_line_width below so every shape agrees on the
# same world->pixel formula and the same Style-default behaviour.
# ---------------------------------------------------------------------------

#: Default annotation colour when a ``Style`` leaves ``color`` unset --
#: distinct from both the tag boundary (``_BOUNDARY_COLOR``, "lime") and
#: the hat marker (``_HAT_COLOR``, "red") so annotations read as their
#: own layer at a glance rather than being mistaken for tag overlays.
#: ``Style`` is presentation (``types.Style``'s own docstring); choosing
#: a default for an absent hint is squarely this renderer's job.
_DEFAULT_ANNOTATION_COLOR = "yellow"

#: Default stroke width in pixels when a ``Style`` leaves ``line_width``
#: unset -- matches the hat marker's own hard-coded width (``_draw_tag``,
#: above) for one consistent default stroke weight across both halves of
#: this renderer.
_DEFAULT_LINE_WIDTH_PX = 2

#: Fixed pixel radius for a ``Marker``'s point dot. ``Marker`` carries no
#: radius of its own (``types.Marker``: "a point of interest"), so this
#: is a presentation constant, not derived from any annotation field --
#: same footing as ``_HAT_LENGTH_RATIO`` above.
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
    (sprint 022) and the annotation half (ticket 024-003). See the
    module docstring for the shared world->pixel formula both use.
    """

    def render(
        self,
        frame: ImageFrame,
        *,
        layers: Optional[Tuple[str, ...]] = None,
        extra: Tuple[Annotation, ...] = (),
    ) -> bytes:
        """See ``client.interfaces.Renderer.render``.

        Tags draw exactly as before (sprint 022, unchanged). Annotations
        draw only on a deskewed frame carrying its own ``px_per_cm``/
        ``world_extent`` (``_frame_can_place_annotations``) -- a raw
        frame, or a deskewed frame missing that metadata, draws no
        annotations at all rather than raising, matching the tag half's
        own "skip what cannot be converted" discipline. ``frame.
        annotations`` filtered by *layers* (``None`` = every layer)
        contributes first, then *extra* -- unfiltered by *layers*, since
        it is the caller's own explicit, local request -- draws
        alongside it, even when ``frame.annotations`` is ``None`` or
        empty.
        """
        image = Image.open(io.BytesIO(frame.data)).convert("RGB")
        draw = ImageDraw.Draw(image)

        if frame.tags is not None:
            for record in frame.tags.tags:
                points = _tag_points(record, frame)
                if points is not None:
                    _draw_tag(draw, points)

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
