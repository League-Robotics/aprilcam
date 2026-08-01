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
    ImageFrame,
    PixelPoint,
    TagFamily,
    TagFrame,
    TagId,
    TagRecord,
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
# Annotations: inert this sprint (Decision 5) -- a genuine no-op, not a stub.
# ---------------------------------------------------------------------------


def test_render_with_annotations_none_draws_only_tags_and_never_raises():
    record = _tag_record()
    frame = _deskewed_frame(
        tags=TagFrame(tags=(record,), timestamp=0.0, frame_index=0), annotations=None
    )

    # Must not raise -- the inert path is a genuine no-op.
    png_bytes = Renderer().render(frame, layers=("features",), extra=())

    image = _decode(png_bytes)
    front_edge_midpoint = _predicted(WorldPoint(x=10.0, y=25.0))
    patch = _mean_color_near(image, front_edge_midpoint.x, front_edge_midpoint.y, radius=3)
    background = np.array(_BACKGROUND, dtype=np.float64)
    # The tag itself is still drawn -- annotations=None did not suppress it.
    assert np.linalg.norm(patch.astype(np.float64) - background) > 30
