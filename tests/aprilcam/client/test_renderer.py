"""Tests for ``aprilcam.client.renderer`` (ticket 022-005; tag overlay
reworked to v1-parity red-U/green-hat/id-label/crosshair scheme by
ticket 032-001).

Headless, cv2-free throughout (this test module itself never imports
``cv2`` either -- ``tests/test_no_cv2_in_client_modules.py`` sweeps
``client/`` for the *source* modules, not tests, but there is no reason
to touch OpenCV here: a fixture ``ImageFrame`` with a known
``world_corners`` position is enough to predict, by hand, exactly which
pixel neighbourhood should come out drawn.
"""

from __future__ import annotations

import io
import math
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


def _apex_from_pixels(
    front_left: Tuple[float, float],
    front_right: Tuple[float, float],
    back_right: Tuple[float, float],
    back_left: Tuple[float, float],
) -> Tuple[float, float]:
    """The green hat's apex, computed independently of
    ``renderer.py``'s own implementation, straight from the documented
    formula (that module's docstring / ticket 032-001):
    ``mid(front_left, front_right) + top_dir * clamp(0.35 * |front_right
    - front_left|, 6, 80)``, where ``top_dir`` is the unit vector from
    the quad's own centroid through that midpoint. Takes already-pixel
    points -- used directly by raw-frame tests, and via ``_expected_apex``
    below for deskewed (world-point) fixtures.
    """
    centroid_x = (front_left[0] + front_right[0] + back_right[0] + back_left[0]) / 4.0
    centroid_y = (front_left[1] + front_right[1] + back_right[1] + back_left[1]) / 4.0
    mid_x, mid_y = (front_left[0] + front_right[0]) / 2.0, (front_left[1] + front_right[1]) / 2.0
    dir_x, dir_y = mid_x - centroid_x, mid_y - centroid_y
    length = math.hypot(dir_x, dir_y)
    if length < 1e-9:
        dir_x, dir_y = 0.0, -1.0
    else:
        dir_x, dir_y = dir_x / length, dir_y / length
    front_edge_px = math.hypot(front_right[0] - front_left[0], front_right[1] - front_left[1])
    height = min(80.0, max(6.0, 0.35 * front_edge_px))
    return (mid_x + dir_x * height, mid_y + dir_y * height)


def _expected_apex(
    front_left: WorldPoint, front_right: WorldPoint, back_right: WorldPoint, back_left: WorldPoint
) -> PixelPoint:
    """``_apex_from_pixels`` for a deskewed (world-point) fixture --
    each corner converted through ``_predicted``, the same world->pixel
    formula the renderer itself uses, so this exercises the real
    conversion too, not just the apex arithmetic in isolation.
    """
    fl, fr, br, bl = (_predicted(p) for p in (front_left, front_right, back_right, back_left))
    x, y = _apex_from_pixels((fl.x, fl.y), (fr.x, fr.y), (br.x, br.y), (bl.x, bl.y))
    return PixelPoint(x=x, y=y)


def _has_color_near(image: np.ndarray, color: Tuple[int, int, int], *, tolerance: float = 40.0) -> bool:
    """Whether any pixel in *image* is within *tolerance* (Euclidean,
    RGB) of *color* -- used to check for the crosshair's own unique
    yellow, since nothing else this renderer draws (red boundary/label,
    green hat, black outline) is anywhere near yellow.
    """
    target = np.array(color, dtype=np.float64)
    distance = np.sqrt(((image.astype(np.float64) - target) ** 2).sum(axis=-1))
    return bool((distance < tolerance).any())


# ---------------------------------------------------------------------------
# Tag rendering -- pixel-level checks against the frame's own published
# metadata, no live window needed.
# ---------------------------------------------------------------------------


def test_render_draws_the_red_u_at_the_position_the_metadata_predicts():
    """The boundary's three red sides draw where the metadata predicts
    -- sampled at the midpoint of ``back_right``->``back_left`` (one of
    the three drawn sides), not the front edge, which the new scheme
    never draws red (see ``test_render_never_draws_the_front_edge_red``
    below for that half of the contract)."""
    record = _tag_record()
    frame = _deskewed_frame(tags=TagFrame(tags=(record,), timestamp=0.0, frame_index=0))

    png_bytes = Renderer().render(frame)

    image = _decode(png_bytes)
    back_edge_midpoint = _predicted(WorldPoint(x=10.0, y=15.0))  # midpoint of back-right/back-left
    patch = _mean_color_near(image, back_edge_midpoint.x, back_edge_midpoint.y, radius=3)
    background = np.array(_BACKGROUND, dtype=np.float64)
    assert np.linalg.norm(patch.astype(np.float64) - background) > 30


def test_render_never_draws_the_front_edge_red():
    """The one shape difference from the old full 4-sided box: the front
    edge (``front_left``->``front_right``) is never drawn at all under
    the new scheme -- sampled at its own midpoint, which neither hat
    line crosses (they only meet at the apex, well above this point)
    nor the id label/crosshair reach (both centred lower, at the quad's
    own centroid)."""
    record = _tag_record()
    frame = _deskewed_frame(tags=TagFrame(tags=(record,), timestamp=0.0, frame_index=0))

    png_bytes = Renderer().render(frame)

    image = _decode(png_bytes)
    front_edge_midpoint = _predicted(WorldPoint(x=10.0, y=25.0))
    patch = _mean_color_near(image, front_edge_midpoint.x, front_edge_midpoint.y, radius=3)
    assert (patch == np.array(_BACKGROUND, dtype=np.float64)).all()


def test_render_draws_a_hat_apex_beyond_the_front_edge_pointing_away_from_the_tag_centre():
    """The green hat's apex sits *outside* the boundary quad, beyond the
    front edge, away from the tag's own centre -- computed here
    independently of the renderer's own implementation, per the
    documented formula (module docstring), and checked by sampling
    directly at that computed point."""
    record = _tag_record()
    frame = _deskewed_frame(tags=TagFrame(tags=(record,), timestamp=0.0, frame_index=0))

    png_bytes = Renderer().render(frame)

    image = _decode(png_bytes)
    apex = _expected_apex(_FRONT_LEFT, _FRONT_RIGHT, _BACK_RIGHT, _BACK_LEFT)
    patch = _mean_color_near(image, apex.x, apex.y, radius=3)
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
# Hat apex clamp bounds -- fixtures deliberately sized so
# 0.35 * |front edge| falls below the 6px floor and above the 80px
# ceiling, per the acceptance criteria.
# ---------------------------------------------------------------------------


def test_hat_apex_height_hits_the_6px_floor_for_a_small_front_edge():
    front_left = WorldPoint(x=9.0, y=25.0)
    front_right = WorldPoint(x=11.0, y=25.0)  # 2cm apart -> 10px; 0.35*10 = 3.5, below the 6px floor
    back_right = WorldPoint(x=11.0, y=23.0)
    back_left = WorldPoint(x=9.0, y=23.0)
    record = _tag_record(world_corners=(front_left, front_right, back_right, back_left))
    frame = _deskewed_frame(tags=TagFrame(tags=(record,), timestamp=0.0, frame_index=0))

    png_bytes = Renderer().render(frame)

    image = _decode(png_bytes)
    apex = _expected_apex(front_left, front_right, back_right, back_left)
    patch = _mean_color_near(image, apex.x, apex.y, radius=2)
    background = np.array(_BACKGROUND, dtype=np.float64)
    assert np.linalg.norm(patch.astype(np.float64) - background) > 30


def test_hat_apex_height_hits_the_80px_ceiling_for_a_large_front_edge():
    front_left = WorldPoint(x=-25.0, y=0.0)
    front_right = WorldPoint(x=25.0, y=0.0)  # 50cm apart -> 250px; 0.35*250 = 87.5, above the 80px ceiling
    back_right = WorldPoint(x=25.0, y=-10.0)
    back_left = WorldPoint(x=-25.0, y=-10.0)
    record = _tag_record(world_corners=(front_left, front_right, back_right, back_left))
    frame = _deskewed_frame(tags=TagFrame(tags=(record,), timestamp=0.0, frame_index=0))

    png_bytes = Renderer().render(frame)

    image = _decode(png_bytes)
    apex = _expected_apex(front_left, front_right, back_right, back_left)
    patch = _mean_color_near(image, apex.x, apex.y, radius=2)
    background = np.array(_BACKGROUND, dtype=np.float64)
    assert np.linalg.norm(patch.astype(np.float64) - background) > 30


# ---------------------------------------------------------------------------
# Tag id label.
# ---------------------------------------------------------------------------


def test_render_draws_a_tag_id_label_centred_on_the_tag():
    """The tag's own id number draws at the boundary quad's centroid.
    This fixture places ``record.world`` far away from that centroid
    (unlike the default fixture, where they coincide) specifically so a
    drawn patch at the centroid can only be the id label -- not the
    crosshair, and not the boundary/hat, neither of which reaches the
    quad's own interior centre point."""
    record = _tag_record(world=WorldPoint(x=40.0, y=-30.0))
    frame = _deskewed_frame(tags=TagFrame(tags=(record,), timestamp=0.0, frame_index=0))

    png_bytes = Renderer().render(frame)

    image = _decode(png_bytes)
    centroid = _predicted(WorldPoint(x=10.0, y=20.0))  # boundary quad's own centroid
    patch = _mean_color_near(image, centroid.x, centroid.y, radius=6)
    background = np.array(_BACKGROUND, dtype=np.float64)
    assert np.linalg.norm(patch.astype(np.float64) - background) > 30


# ---------------------------------------------------------------------------
# The corrected-position crosshair.
# ---------------------------------------------------------------------------

#: A world position well clear of the default fixture's own boundary
#: quad and id-label footprint, so crosshair-only checks below cannot
#: be confused with anything else this renderer draws.
_CROSSHAIR_WORLD = WorldPoint(x=40.0, y=0.0)


def test_crosshair_draws_at_the_world_position_not_the_raw_quad_centroid():
    """The crosshair sits at ``record.world`` -- deliberately far from
    the boundary quad's own centroid in this fixture, proving it is not
    derived from ``world_corners`` at all (module docstring: the two are
    intentionally allowed to differ)."""
    record = _tag_record(world=_CROSSHAIR_WORLD)
    frame = _deskewed_frame(tags=TagFrame(tags=(record,), timestamp=0.0, frame_index=0))

    png_bytes = Renderer().render(frame)

    image = _decode(png_bytes)
    center = _predicted(_CROSSHAIR_WORLD)
    patch = _mean_color_near(image, center.x, center.y, radius=2)
    background = np.array(_BACKGROUND, dtype=np.float64)
    assert np.linalg.norm(patch.astype(np.float64) - background) > 30
    # The raw quad's own centroid, where the id label sits instead, is a
    # different pixel neighbourhood entirely.
    quad_centroid = _predicted(WorldPoint(x=10.0, y=20.0))
    assert (center.x, center.y) != (quad_centroid.x, quad_centroid.y)


def test_crosshair_arm_length_hits_the_floor_when_px_per_cm_is_small():
    """Arm length = ``max(4px, 0.5*px_per_cm)``; this module's own
    ``px_per_cm`` (5.0) gives ``0.5*5.0 = 2.5``, below the 4px floor, so
    the floor applies. Checked in a region far from the tag's own
    quad/id label so nothing else could be mistaken for the crosshair."""
    record = _tag_record(world=_CROSSHAIR_WORLD)
    frame = _deskewed_frame(tags=TagFrame(tags=(record,), timestamp=0.0, frame_index=0))

    png_bytes = Renderer().render(frame)

    image = _decode(png_bytes)
    background = np.array(_BACKGROUND, dtype=np.float64)
    center = _predicted(_CROSSHAIR_WORLD)
    for dx, dy in ((4.0, 0.0), (-4.0, 0.0), (0.0, 4.0), (0.0, -4.0)):
        patch = _mean_color_near(image, center.x + dx, center.y + dy, radius=1)
        assert np.linalg.norm(patch.astype(np.float64) - background) > 30, (dx, dy)
    # Well beyond the 4px floor, nothing is drawn -- confirms the arm
    # actually stops there rather than continuing indefinitely.
    beyond = _mean_color_near(image, center.x + 12.0, center.y, radius=2)
    assert (beyond == np.array(_BACKGROUND)).all()


def test_crosshair_arm_length_does_not_floor_when_px_per_cm_is_large():
    """A frame published with a larger ``px_per_cm`` than this module's
    default lets ``0.5*px_per_cm`` exceed the 4px floor -- checked the
    same way as the floor case above, in an isolated region of a
    dedicated, higher-resolution frame."""
    px_per_cm = 20.0  # 0.5*20 = 10, above the 4px floor
    width_cm, height_cm = 25.0, 20.0
    crosshair_world = WorldPoint(x=5.0, y=0.0)
    # A small quad tucked in a far corner -- present so the record has
    # a drawable boundary too, well clear of the crosshair region below.
    quad_world = (
        WorldPoint(x=-10.0, y=8.0),
        WorldPoint(x=-8.0, y=8.0),
        WorldPoint(x=-8.0, y=6.0),
        WorldPoint(x=-10.0, y=6.0),
    )
    record = _tag_record(world=crosshair_world, world_corners=quad_world)
    frame = ImageFrame(
        data=_solid_png(500, 400),
        width=500,
        height=400,
        encoding="png",
        timestamp=0.0,
        deskewed=True,
        px_per_cm=px_per_cm,
        world_extent=(width_cm, height_cm),
        tags=TagFrame(tags=(record,), timestamp=0.0, frame_index=0),
    )

    png_bytes = Renderer().render(frame)

    image = _decode(png_bytes)
    background = np.array(_BACKGROUND, dtype=np.float64)
    center = world_to_deskewed_pixel(crosshair_world, world_extent=(width_cm, height_cm), px_per_cm=px_per_cm)
    for dx, dy in ((10.0, 0.0), (-10.0, 0.0), (0.0, 10.0), (0.0, -10.0)):
        patch = _mean_color_near(image, center.x + dx, center.y + dy, radius=1)
        assert np.linalg.norm(patch.astype(np.float64) - background) > 30, (dx, dy)
    beyond = _mean_color_near(image, center.x + 16.0, center.y, radius=2)
    assert (beyond == np.array(_BACKGROUND)).all()


def test_no_crosshair_when_world_is_none():
    """An uncalibrated observation (``world=None``) still gets its
    boundary/hat/id from ``world_corners``, but no crosshair -- checked
    by scanning the whole image for the crosshair's own unique yellow,
    since there is no world position at all to predict a "would-be"
    location from."""
    record = _tag_record(world=None)
    frame = _deskewed_frame(tags=TagFrame(tags=(record,), timestamp=0.0, frame_index=0))

    png_bytes = Renderer().render(frame)

    image = _decode(png_bytes)
    assert not _has_color_near(image, (255, 255, 0))
    # The boundary/hat/id still draw, from world_corners.
    back_edge_midpoint = _predicted(WorldPoint(x=10.0, y=15.0))
    patch = _mean_color_near(image, back_edge_midpoint.x, back_edge_midpoint.y, radius=3)
    background = np.array(_BACKGROUND, dtype=np.float64)
    assert np.linalg.norm(patch.astype(np.float64) - background) > 30


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
    # Midpoint of back_right->back_left (70, 90) -- one of the three red
    # sides. The front edge (50, 50)-(90, 50) is never drawn under the
    # new scheme, so it is not a valid "boundary was drawn" sample.
    patch = _mean_color_near(image, 70.0, 90.0, radius=3)
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


def test_render_on_a_raw_frame_draws_the_hat_and_id_label_too():
    """The raw-frame path draws the same U/hat/id trio as the deskewed
    path, straight from ``pixel_corners`` -- no world->pixel conversion
    involved anywhere. ``world`` is explicitly ``None`` here (a raw,
    uncalibrated observation), which also exercises "no crosshair on a
    raw frame" incidentally -- ``test_no_crosshair_on_a_raw_frame_even_
    when_world_is_set`` below covers the sharper case where ``world``
    *is* set but the frame is still raw."""
    pixel_corners = (
        PixelPoint(x=50.0, y=50.0),
        PixelPoint(x=90.0, y=50.0),
        PixelPoint(x=90.0, y=90.0),
        PixelPoint(x=50.0, y=90.0),
    )
    record = _tag_record(world_corners=None, pixel_corners=pixel_corners, calibrated=False, world=None)
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
    background = np.array(_BACKGROUND, dtype=np.float64)

    apex_x, apex_y = _apex_from_pixels((50.0, 50.0), (90.0, 50.0), (90.0, 90.0), (50.0, 90.0))
    hat_patch = _mean_color_near(image, apex_x, apex_y, radius=3)
    assert np.linalg.norm(hat_patch.astype(np.float64) - background) > 30

    centroid_x, centroid_y = (50.0 + 90.0 + 90.0 + 50.0) / 4.0, (50.0 + 50.0 + 90.0 + 90.0) / 4.0
    id_patch = _mean_color_near(image, centroid_x, centroid_y, radius=6)
    assert np.linalg.norm(id_patch.astype(np.float64) - background) > 30


def test_no_crosshair_on_a_raw_frame_even_when_world_is_set():
    """A raw frame has no world->pixel mapping at all, so the crosshair
    never draws there regardless of what ``record.world`` holds -- even
    when it is set, unlike the companion test above."""
    pixel_corners = (
        PixelPoint(x=50.0, y=50.0),
        PixelPoint(x=90.0, y=50.0),
        PixelPoint(x=90.0, y=90.0),
        PixelPoint(x=50.0, y=90.0),
    )
    record = _tag_record(
        world_corners=None, pixel_corners=pixel_corners, calibrated=False, world=WorldPoint(x=10.0, y=20.0)
    )
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
    assert not _has_color_near(image, (255, 255, 0))
    # The boundary/hat/id still draw, from pixel_corners.
    patch = _mean_color_near(image, 70.0, 90.0, radius=3)
    background = np.array(_BACKGROUND, dtype=np.float64)
    assert np.linalg.norm(patch.astype(np.float64) - background) > 30


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
    # The tag's own centroid (10, 20) -- also its `world` position, so
    # both the id label and the crosshair land here -- guaranteed to
    # differ from background regardless of the front edge's own
    # never-drawn status under the new scheme.
    centroid = _predicted(WorldPoint(x=10.0, y=20.0))
    patch = _mean_color_near(image, centroid.x, centroid.y, radius=3)
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
