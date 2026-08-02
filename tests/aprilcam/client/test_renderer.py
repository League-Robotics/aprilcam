"""Tests for ``aprilcam.client.renderer`` (ticket 022-005).

Headless, cv2-free throughout (this test module itself never imports
``cv2`` either -- ``tests/test_no_cv2_in_client_modules.py`` sweeps
``client/`` for the *source* modules, not tests, but there is no reason
to touch OpenCV here: a fixture ``ImageFrame`` with a known
``world_corners`` position is enough to predict, by hand, exactly which
pixel neighbourhood should come out drawn.
"""

from __future__ import annotations

import io
from typing import Tuple

import numpy as np
import pytest
from PIL import Image

from aprilcam.calibration.geometry import world_to_deskewed_pixel
from aprilcam.client.renderer import Renderer
from aprilcam.types import (
    Annotation,
    AnnotationSet,
    Circle,
    ImageFrame,
    Marker,
    PathShape,
    PixelPoint,
    Polygon,
    Style,
    TagFamily,
    TagFrame,
    TagId,
    TagRecord,
    Text,
    WorldPoint,
)

_BACKGROUND = (200, 200, 200)
_WIDTH_PX, _HEIGHT_PX = 500, 400
_WIDTH_CM, _HEIGHT_CM = 100.0, 80.0
_PX_PER_CM = _WIDTH_PX / _WIDTH_CM  # 5.0 -- deliberately simple for hand-checking


def _solid_png(width: int, height: int, color: Tuple[int, int, int] = _BACKGROUND) -> bytes:
    image = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _mean_color_near(image: np.ndarray, x: float, y: float, *, radius: int = 4) -> np.ndarray:
    height, width = image.shape[:2]
    x0, x1 = max(0, int(x) - radius), min(width, int(x) + radius + 1)
    y0, y1 = max(0, int(y) - radius), min(height, int(y) + radius + 1)
    patch = image[y0:y1, x0:x1]
    assert patch.size > 0
    return patch.reshape(-1, patch.shape[-1]).mean(axis=0)


def _decode(png_bytes: bytes) -> np.ndarray:
    return np.asarray(Image.open(io.BytesIO(png_bytes)).convert("RGB"))


#: A tag whose square boundary is (5, 15)-(15, 25) in world cm --
#: (front-left, front-right, back-right, back-left) order, front edge
#: on the north side. Known, hand-computable pixel positions via
#: world_to_deskewed_pixel with this module's own _PX_PER_CM/extent.
_FRONT_LEFT = WorldPoint(x=5.0, y=25.0)
_FRONT_RIGHT = WorldPoint(x=15.0, y=25.0)
_BACK_RIGHT = WorldPoint(x=15.0, y=15.0)
_BACK_LEFT = WorldPoint(x=5.0, y=15.0)


def _tag_record(**overrides) -> TagRecord:
    fields = dict(
        tag=TagId(TagFamily.APRILTAG, 7),
        world=WorldPoint(x=10.0, y=20.0),
        heading_rad=None,
        world_velocity=None,
        speed=None,
        timestamp=0.0,
        pixel=None,
        calibrated=True,
        yaw_rad=0.0,
        world_corners=(_FRONT_LEFT, _FRONT_RIGHT, _BACK_RIGHT, _BACK_LEFT),
        pixel_corners=None,
    )
    fields.update(overrides)
    return TagRecord(**fields)


def _deskewed_frame(*, tags=None, annotations=None) -> ImageFrame:
    return ImageFrame(
        data=_solid_png(_WIDTH_PX, _HEIGHT_PX),
        width=_WIDTH_PX,
        height=_HEIGHT_PX,
        encoding="png",
        timestamp=0.0,
        deskewed=True,
        px_per_cm=_PX_PER_CM,
        world_extent=(_WIDTH_CM, _HEIGHT_CM),
        tags=tags,
        annotations=annotations,
    )


def _predicted(world: WorldPoint) -> PixelPoint:
    return world_to_deskewed_pixel(
        world, world_extent=(_WIDTH_CM, _HEIGHT_CM), px_per_cm=_PX_PER_CM
    )


# ---------------------------------------------------------------------------
# Tag rendering -- pixel-level checks against the frame's own published
# metadata, no live window needed.
# ---------------------------------------------------------------------------


def test_render_draws_the_boundary_at_the_position_the_metadata_predicts():
    record = _tag_record()
    frame = _deskewed_frame(tags=TagFrame(tags=(record,), timestamp=0.0, frame_index=0))

    png_bytes = Renderer().render(frame)

    image = _decode(png_bytes)
    front_edge_midpoint = _predicted(WorldPoint(x=10.0, y=25.0))  # midpoint of front-left/front-right
    patch = _mean_color_near(image, front_edge_midpoint.x, front_edge_midpoint.y, radius=3)
    background = np.array(_BACKGROUND, dtype=np.float64)
    assert np.linalg.norm(patch.astype(np.float64) - background) > 30


def test_render_draws_a_hat_beyond_the_front_edge_pointing_away_from_the_tag_centre():
    """The front "hat" marker sits *outside* the boundary quad, beyond
    the front edge, away from the tag's own centre -- checked here by
    sampling a point further out along that same line than the front
    edge itself and confirming *something* was drawn there too (not
    just the boundary line)."""
    record = _tag_record()
    frame = _deskewed_frame(tags=TagFrame(tags=(record,), timestamp=0.0, frame_index=0))

    png_bytes = Renderer().render(frame)

    image = _decode(png_bytes)
    front_edge_midpoint = _predicted(WorldPoint(x=10.0, y=25.0))
    # A point a few pixels further north (smaller pixel-y) than the
    # front edge itself -- beyond the boundary quad, inside the hat
    # marker's own footprint (the hat extends outward from the front
    # edge, away from the tag centre, per _draw_tag's docstring).
    beyond_x, beyond_y = front_edge_midpoint.x, front_edge_midpoint.y - 5
    patch = _mean_color_near(image, beyond_x, beyond_y, radius=3)
    background = np.array(_BACKGROUND, dtype=np.float64)
    assert np.linalg.norm(patch.astype(np.float64) - background) > 30


def test_render_leaves_the_rest_of_the_image_as_plain_background():
    """Nothing outside the tag's own footprint is touched -- a corner
    of the image far from the tag stays exactly the solid background
    colour, confirming there is no stray drawing anywhere else."""
    record = _tag_record()
    frame = _deskewed_frame(tags=TagFrame(tags=(record,), timestamp=0.0, frame_index=0))

    png_bytes = Renderer().render(frame)

    image = _decode(png_bytes)
    corner = image[5:15, 5:15].reshape(-1, 3)
    assert (corner == np.array(_BACKGROUND)).all()


def test_render_with_no_tag_frame_produces_a_plain_undrawn_image():
    frame = _deskewed_frame(tags=None)

    png_bytes = Renderer().render(frame)

    image = _decode(png_bytes)
    assert (image == np.array(_BACKGROUND)).all()


def test_render_with_an_empty_tag_frame_draws_nothing():
    frame = _deskewed_frame(tags=TagFrame(tags=(), timestamp=0.0, frame_index=0))

    png_bytes = Renderer().render(frame)

    image = _decode(png_bytes)
    assert (image == np.array(_BACKGROUND)).all()


def test_render_skips_a_record_with_no_world_corners_on_a_deskewed_frame():
    """A record that is somehow calibrated=False (or otherwise carries
    no world_corners) on a deskewed frame is skipped rather than raising
    -- there is nothing to convert."""
    record = _tag_record(calibrated=False, world_corners=None, yaw_rad=None)
    frame = _deskewed_frame(tags=TagFrame(tags=(record,), timestamp=0.0, frame_index=0))

    png_bytes = Renderer().render(frame)

    image = _decode(png_bytes)
    assert (image == np.array(_BACKGROUND)).all()


# ---------------------------------------------------------------------------
# Raw frames: pixel_corners, in the served raw stream's own pixel space --
# the interface's declared other branch, not exercised by this sprint's
# viewer wiring but implemented per the full Renderer contract.
# ---------------------------------------------------------------------------


def test_render_on_a_raw_frame_draws_from_pixel_corners_directly():
    pixel_corners = (
        PixelPoint(x=50.0, y=50.0),
        PixelPoint(x=90.0, y=50.0),
        PixelPoint(x=90.0, y=90.0),
        PixelPoint(x=50.0, y=90.0),
    )
    record = _tag_record(world_corners=None, pixel_corners=pixel_corners, calibrated=False)
    frame = ImageFrame(
        data=_solid_png(200, 150),
        width=200,
        height=150,
        encoding="png",
        timestamp=0.0,
        deskewed=False,
        tags=TagFrame(tags=(record,), timestamp=0.0, frame_index=0),
    )

    png_bytes = Renderer().render(frame)

    image = _decode(png_bytes)
    patch = _mean_color_near(image, 70.0, 50.0, radius=3)  # midpoint of the front edge
    background = np.array(_BACKGROUND, dtype=np.float64)
    assert np.linalg.norm(patch.astype(np.float64) - background) > 30


def test_render_skips_a_record_with_no_pixel_corners_on_a_raw_frame():
    record = _tag_record(world_corners=None, pixel_corners=None, calibrated=False)
    frame = ImageFrame(
        data=_solid_png(200, 150),
        width=200,
        height=150,
        encoding="png",
        timestamp=0.0,
        deskewed=False,
        tags=TagFrame(tags=(record,), timestamp=0.0, frame_index=0),
    )

    png_bytes = Renderer().render(frame)

    image = _decode(png_bytes)
    assert (image == np.array(_BACKGROUND)).all()


# ---------------------------------------------------------------------------
# Annotations (ticket 024-003): the previously-inert half, now real. Each
# shape gets its own drawn-at-the-predicted-pixel check, mirroring the tag
# assertions above; `_predicted` is the same shared world->pixel formula
# both halves of the renderer use, so "the renderer put it where the
# geometry helper predicts" is a real, non-tautological check.
# ---------------------------------------------------------------------------


def _annotation_frame(*annotations: Annotation, revision: int = 1) -> ImageFrame:
    return _deskewed_frame(
        annotations=AnnotationSet(annotations=tuple(annotations), revision=revision, timestamp=0.0)
    )


def test_render_with_annotations_none_draws_only_tags_and_never_raises():
    """Regression guard (acceptance criteria): a raw frame or a
    pre-annotation-store deskewed frame with ``annotations is None``
    behaves exactly as it did before this ticket -- tags still draw,
    nothing raises, nothing extra appears."""
    record = _tag_record()
    frame = _deskewed_frame(
        tags=TagFrame(tags=(record,), timestamp=0.0, frame_index=0), annotations=None
    )

    # Must not raise -- annotations=None is a genuine no-op, not an error.
    png_bytes = Renderer().render(frame, layers=("features",), extra=())

    image = _decode(png_bytes)
    front_edge_midpoint = _predicted(WorldPoint(x=10.0, y=25.0))
    patch = _mean_color_near(image, front_edge_midpoint.x, front_edge_midpoint.y, radius=3)
    background = np.array(_BACKGROUND, dtype=np.float64)
    # The tag itself is still drawn -- annotations=None did not suppress it.
    assert np.linalg.norm(patch.astype(np.float64) - background) > 30


def test_deskewed_frame_with_an_empty_annotation_set_renders_cleanly():
    frame = _annotation_frame(revision=0)

    png_bytes = Renderer().render(frame)

    image = _decode(png_bytes)
    assert (image == np.array(_BACKGROUND)).all()


def test_render_on_a_raw_frame_draws_no_annotations_even_with_extra_but_does_not_raise():
    """A raw frame carries no world->pixel mapping, so annotations --
    embedded or `extra` -- have nothing to convert with. Skipped
    gracefully, per the ticket's own instruction, not a crash."""
    marker_extra = Annotation(id="ex", layer="debug", shape=Marker(at=WorldPoint(x=0.0, y=0.0)))
    frame = ImageFrame(
        data=_solid_png(200, 150),
        width=200,
        height=150,
        encoding="png",
        timestamp=0.0,
        deskewed=False,
    )

    png_bytes = Renderer().render(frame, extra=(marker_extra,))  # must not raise

    image = _decode(png_bytes)
    assert (image == np.array(_BACKGROUND)).all()


def test_render_draws_a_marker_at_the_position_the_metadata_predicts():
    ann = Annotation(id="goal", layer="planner", shape=Marker(at=WorldPoint(x=0.0, y=0.0), label="goal"))
    frame = _annotation_frame(ann)

    png_bytes = Renderer().render(frame)

    image = _decode(png_bytes)
    predicted = _predicted(WorldPoint(x=0.0, y=0.0))
    patch = _mean_color_near(image, predicted.x, predicted.y, radius=2)
    background = np.array(_BACKGROUND, dtype=np.float64)
    assert np.linalg.norm(patch.astype(np.float64) - background) > 30


def test_render_draws_a_path_shape_as_an_open_polyline():
    points = (WorldPoint(x=-20.0, y=0.0), WorldPoint(x=0.0, y=0.0), WorldPoint(x=20.0, y=0.0))
    ann = Annotation(id="route", layer="planner", shape=PathShape(points=points))
    frame = _annotation_frame(ann)

    png_bytes = Renderer().render(frame)

    image = _decode(png_bytes)
    midpoint = _predicted(WorldPoint(x=0.0, y=0.0))
    patch = _mean_color_near(image, midpoint.x, midpoint.y, radius=2)
    background = np.array(_BACKGROUND, dtype=np.float64)
    assert np.linalg.norm(patch.astype(np.float64) - background) > 30


def test_render_draws_a_polygon_as_a_closed_outline_not_filled():
    points = (
        WorldPoint(x=-10.0, y=10.0),
        WorldPoint(x=10.0, y=10.0),
        WorldPoint(x=10.0, y=-10.0),
        WorldPoint(x=-10.0, y=-10.0),
    )
    ann = Annotation(id="zone", layer="planner", shape=Polygon(points=points))
    frame = _annotation_frame(ann)

    png_bytes = Renderer().render(frame)

    image = _decode(png_bytes)
    background = np.array(_BACKGROUND, dtype=np.float64)

    edge_midpoint = _predicted(WorldPoint(x=0.0, y=10.0))
    edge_patch = _mean_color_near(image, edge_midpoint.x, edge_midpoint.y, radius=2)
    assert np.linalg.norm(edge_patch.astype(np.float64) - background) > 30

    # A Polygon is a closed *outline*, per its draw routine -- the
    # interior is left untouched, unlike a filled shape.
    center = _predicted(WorldPoint(x=0.0, y=0.0))
    center_patch = _mean_color_near(image, center.x, center.y, radius=1)
    assert (center_patch == np.array(_BACKGROUND)).all()


def test_render_draws_a_circle_whose_pixel_radius_matches_radius_cm_times_px_per_cm():
    radius_cm = 6.0
    center = WorldPoint(x=0.0, y=0.0)
    ann = Annotation(id="c1", layer="planner", shape=Circle(center=center, radius_cm=radius_cm))
    frame = _annotation_frame(ann)

    png_bytes = Renderer().render(frame)

    image = _decode(png_bytes)
    background = np.array(_BACKGROUND, dtype=np.float64)
    center_px = _predicted(center)
    expected_radius_px = radius_cm * _PX_PER_CM  # 30.0 with this module's fixture scale

    # Sample the outline at the expected radius in each cardinal
    # direction -- within a small pixel tolerance (the outline's own
    # stroke width), each should be drawn, not background.
    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        x = center_px.x + dx * expected_radius_px
        y = center_px.y + dy * expected_radius_px
        patch = _mean_color_near(image, x, y, radius=2)
        assert np.linalg.norm(patch.astype(np.float64) - background) > 30, (dx, dy)

    # A Circle draws only its outline -- the centre is untouched.
    center_patch = _mean_color_near(image, center_px.x, center_px.y, radius=1)
    assert (center_patch == np.array(_BACKGROUND)).all()


def test_render_draws_text_at_the_expected_position():
    ann = Annotation(id="label", layer="planner", shape=Text(at=WorldPoint(x=0.0, y=0.0), text="hi"))
    frame = _annotation_frame(ann)

    png_bytes = Renderer().render(frame)

    image = _decode(png_bytes)
    predicted = _predicted(WorldPoint(x=0.0, y=0.0))
    # Pillow anchors default text at its top-left corner and draws
    # glyphs to the right/below -- sample a small box covering the
    # glyphs rather than the exact anchor pixel, which may land between
    # strokes.
    x0, y0 = int(predicted.x), int(predicted.y)
    region = image[y0 : y0 + 12, x0 : x0 + 14]
    assert not (region == np.array(_BACKGROUND)).all()


def test_render_geometry_a_marker_lands_at_the_predicted_pixel_and_further_north_lands_further_up():
    """The ticket's own required geometry check: a known world position
    lands at the pixel the frame's metadata predicts, and a second
    annotation further north (larger world-y) lands further *up* on the
    image (smaller pixel-y) -- the anchor invariant's single y-flip,
    exercised through the renderer's actual output."""
    south = Annotation(id="south", layer="planner", shape=Marker(at=WorldPoint(x=0.0, y=0.0)))
    north = Annotation(id="north", layer="planner", shape=Marker(at=WorldPoint(x=0.0, y=20.0)))
    frame = _annotation_frame(south, north)

    png_bytes = Renderer().render(frame)

    image = _decode(png_bytes)
    background = np.array(_BACKGROUND, dtype=np.float64)
    south_px = _predicted(WorldPoint(x=0.0, y=0.0))
    north_px = _predicted(WorldPoint(x=0.0, y=20.0))

    south_patch = _mean_color_near(image, south_px.x, south_px.y, radius=2)
    north_patch = _mean_color_near(image, north_px.x, north_px.y, radius=2)
    assert np.linalg.norm(south_patch.astype(np.float64) - background) > 30
    assert np.linalg.norm(north_patch.astype(np.float64) - background) > 30

    assert north_px.y < south_px.y
    assert north_px.x == south_px.x


def test_layers_none_draws_every_layer_and_a_named_tuple_selects_only_that_layer():
    planner = Annotation(id="goal", layer="planner", shape=Marker(at=WorldPoint(x=-20.0, y=0.0)))
    features = Annotation(id="feat", layer="features", shape=Marker(at=WorldPoint(x=20.0, y=0.0)))
    frame = _annotation_frame(planner, features)
    background = np.array(_BACKGROUND, dtype=np.float64)
    planner_px = _predicted(WorldPoint(x=-20.0, y=0.0))
    features_px = _predicted(WorldPoint(x=20.0, y=0.0))

    both = _decode(Renderer().render(frame, layers=None))
    assert (
        np.linalg.norm(_mean_color_near(both, planner_px.x, planner_px.y, radius=2).astype(np.float64) - background)
        > 30
    )
    assert (
        np.linalg.norm(
            _mean_color_near(both, features_px.x, features_px.y, radius=2).astype(np.float64) - background
        )
        > 30
    )

    planner_only = _decode(Renderer().render(frame, layers=("planner",)))
    assert (
        np.linalg.norm(
            _mean_color_near(planner_only, planner_px.x, planner_px.y, radius=2).astype(np.float64) - background
        )
        > 30
    )
    assert (
        _mean_color_near(planner_only, features_px.x, features_px.y, radius=2) == np.array(_BACKGROUND)
    ).all()


def test_extra_annotations_draw_even_when_frame_annotations_is_none_or_empty():
    marker_extra = Annotation(id="ex", layer="debug", shape=Marker(at=WorldPoint(x=15.0, y=-10.0)))
    predicted = _predicted(WorldPoint(x=15.0, y=-10.0))
    background = np.array(_BACKGROUND, dtype=np.float64)

    image_none = _decode(Renderer().render(_deskewed_frame(annotations=None), extra=(marker_extra,)))
    assert (
        np.linalg.norm(_mean_color_near(image_none, predicted.x, predicted.y, radius=2).astype(np.float64) - background)
        > 30
    )

    image_empty = _decode(Renderer().render(_annotation_frame(), extra=(marker_extra,)))
    assert (
        np.linalg.norm(
            _mean_color_near(image_empty, predicted.x, predicted.y, radius=2).astype(np.float64) - background
        )
        > 30
    )


def test_extra_annotations_are_not_filtered_by_layers():
    """`extra` is the caller's own explicit, local drawing request --
    unlike `frame.annotations`, it is never subject to `layers`
    filtering (module docstring)."""
    marker_extra = Annotation(id="ex", layer="debug", shape=Marker(at=WorldPoint(x=15.0, y=-10.0)))
    predicted = _predicted(WorldPoint(x=15.0, y=-10.0))
    background = np.array(_BACKGROUND, dtype=np.float64)

    image = _decode(
        Renderer().render(_deskewed_frame(annotations=None), layers=("planner",), extra=(marker_extra,))
    )
    assert (
        np.linalg.norm(_mean_color_near(image, predicted.x, predicted.y, radius=2).astype(np.float64) - background)
        > 30
    )


def test_extra_uses_the_same_draw_routine_as_an_embedded_annotation_of_the_same_shape():
    """The same `Marker` drawn once embedded and once as `extra` (on two
    separate frames) produces the same footprint -- one draw routine,
    exercised both ways, not two independently-written code paths."""
    shape = Marker(at=WorldPoint(x=0.0, y=0.0), label="same")
    embedded = Annotation(id="m", layer="planner", shape=shape)

    embedded_image = _decode(Renderer().render(_annotation_frame(embedded)))
    extra_image = _decode(Renderer().render(_deskewed_frame(annotations=None), extra=(embedded,)))

    assert np.array_equal(embedded_image, extra_image)


def test_style_with_color_and_line_width_set_is_honored():
    ann = Annotation(
        id="p1",
        layer="planner",
        shape=PathShape(points=(WorldPoint(x=-10.0, y=0.0), WorldPoint(x=10.0, y=0.0))),
        style=Style(color="blue", line_width=5.0),
    )
    frame = _annotation_frame(ann)

    png_bytes = Renderer().render(frame)

    image = _decode(png_bytes)
    midpoint = _predicted(WorldPoint(x=0.0, y=0.0))
    patch = _mean_color_near(image, midpoint.x, midpoint.y, radius=1)
    # Pillow's "blue" is pure (0, 0, 255) -- more blue than red confirms
    # the honored hint, not the yellow-ish default.
    assert patch[2] > patch[0]


def test_style_with_unset_color_and_width_uses_a_documented_default_not_a_crash():
    """A `Style` with both fields unset (the `Annotation` dataclass's own
    default) must render with a visible, documented default -- not raise,
    and not silently draw nothing (acceptance criteria: "not silently-
    blank geometry")."""
    ann = Annotation(id="m1", layer="planner", shape=Marker(at=WorldPoint(x=0.0, y=0.0)))
    assert ann.style == Style()  # unset, exactly the case under test
    frame = _annotation_frame(ann)

    png_bytes = Renderer().render(frame)  # must not raise

    image = _decode(png_bytes)
    predicted = _predicted(WorldPoint(x=0.0, y=0.0))
    patch = _mean_color_near(image, predicted.x, predicted.y, radius=1)
    background = np.array(_BACKGROUND, dtype=np.float64)
    assert np.linalg.norm(patch.astype(np.float64) - background) > 30
