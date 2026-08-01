"""``Renderer``: rasterizes a frame's embedded tag boundaries and "hats"
to an image, client-side, cv2-free (ticket 022-005; sprint.md Decision
5).

Implements the **tag half** of ``client.interfaces.Renderer.render()`` --
the annotation half stays inert this sprint (see below), matching
Decision 5's stated scope exactly. Draws with Pillow
(``PIL.Image``/``PIL.ImageDraw``), never ``cv2`` -- the same discipline
``view/viewer.py`` already established for decoding served frames
(``tests/test_no_cv2_in_client_modules.py`` sweeps this whole package;
adding this module needs no change to that test).

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

**Annotations are inert, not stubbed** (Decision 5): ``frame.annotations``
is always ``None`` this sprint (the annotation store does not exist
yet), so the annotation-drawing step below is simply never reached --
the interface is implemented completely for the data that exists, with
nothing left to draw for the data that does not.
"""

from __future__ import annotations

import io
from typing import Optional, Tuple

from PIL import Image, ImageDraw

from aprilcam.calibration.geometry import world_to_deskewed_pixel
from aprilcam.client.interfaces import Renderer as _RendererBase
from aprilcam.types import Annotation, ImageFrame, TagRecord

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


class Renderer(_RendererBase):
    """Concrete ``client.interfaces.Renderer`` -- the tag-drawing half
    (sprint.md Decision 5). See the module docstring for what is, and
    is not, drawn this sprint.
    """

    def render(
        self,
        frame: ImageFrame,
        *,
        layers: Optional[Tuple[str, ...]] = None,
        extra: Tuple[Annotation, ...] = (),
    ) -> bytes:
        """See ``client.interfaces.Renderer.render``.

        ``layers``/``extra`` are accepted per the declared interface but
        have nothing to act on yet: annotation drawing is the inert half
        this sprint (module docstring) -- both parameters are currently
        unused, not silently misapplied.
        """
        image = Image.open(io.BytesIO(frame.data)).convert("RGB")
        draw = ImageDraw.Draw(image)

        if frame.tags is not None:
            for record in frame.tags.tags:
                points = _tag_points(record, frame)
                if points is not None:
                    _draw_tag(draw, points)

        # Annotations: inert this sprint (module docstring, Decision 5)
        # -- frame.annotations is always None, so there is nothing to
        # draw; `extra` is likewise unused until a real annotation
        # renderer exists. Neither raises.

        out = io.BytesIO()
        image.save(out, format="PNG")
        return out.getvalue()
