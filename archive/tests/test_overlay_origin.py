"""Regression tests for the A1-origin published on the tag stream.

Bug: the live view re-projected overlay world coordinates using the field
half-dimensions as the A1 origin, while the daemon centred world_xy on
AprilTag 1's actual world position.  When the homography is A1-centred those
disagree by ~half the field, throwing the yellow tag-1 cross to a corner.

Fix: the daemon publishes the origin it used (origin_x/origin_y) and the view
consumes it, so the two can never drift apart.
"""
import numpy as np
import pytest

from aprilcam.proto import aprilcam_pb2
from aprilcam.client.models import TagFrame as ModelTagFrame
from aprilcam.daemon.camera_pipeline import CameraPipeline


# ---------------------------------------------------------------------------
# Wire propagation: proto + client model
# ---------------------------------------------------------------------------

def test_proto_messages_carry_origin():
    for msg in (
        aprilcam_pb2.TagFrame(origin_x=3.0, origin_y=-4.0),
        aprilcam_pb2.TagFrameResponse(origin_x=3.0, origin_y=-4.0),
    ):
        rt = type(msg).FromString(msg.SerializeToString())
        assert rt.origin_x == pytest.approx(3.0)
        assert rt.origin_y == pytest.approx(-4.0)


def test_model_from_proto_carries_origin():
    proto = aprilcam_pb2.TagFrame(frame_id=1, origin_x=0.25, origin_y=-0.5)
    tf = ModelTagFrame.from_proto(proto)
    assert tf.origin_x == pytest.approx(0.25)
    assert tf.origin_y == pytest.approx(-0.5)


# ---------------------------------------------------------------------------
# Daemon origin selection
# ---------------------------------------------------------------------------

class _Cal:
    def __init__(self, w, h, static_markers=None):
        self.playfield_width_cm = w
        self.playfield_height_cm = h
        self.static_markers = static_markers


def _pipeline_with_cal(cal):
    p = CameraPipeline.__new__(CameraPipeline)
    p._calibration = cal
    return p


def test_a1_origin_prefers_apriltag1():
    cal = _Cal(134.3, 89.3, {"apriltag:1": {"world": [0.03, -0.21]}})
    assert _pipeline_with_cal(cal)._a1_origin() == pytest.approx((0.03, -0.21))


def test_a1_origin_falls_back_to_half_dims():
    cal = _Cal(134.3, 89.3, static_markers=None)
    assert _pipeline_with_cal(cal)._a1_origin() == pytest.approx((67.15, 44.65))


def test_a1_origin_zero_without_calibration():
    assert _pipeline_with_cal(None)._a1_origin() == (0.0, 0.0)


# ---------------------------------------------------------------------------
# End-to-end projection: the yellow cross lands on the tag with the published
# origin, and is displaced with the old half-dims origin (the bug).
# ---------------------------------------------------------------------------

pytest.importorskip("cv2", reason="requires aprilcam[imaging]")
from aprilcam.ui.display import PlayfieldDisplay  # noqa: E402
from aprilcam.core.models import AprilTag  # noqa: E402


def _display():
    d = PlayfieldDisplay.__new__(PlayfieldDisplay)
    d._mode = "full"
    d._crop_xy = (0, 0)
    d._crop_wh = (600, 600)
    d.M_deskew = None
    d.playfield = type("P", (), {"get_polygon": staticmethod(lambda: None)})()
    d.robot_tag_id = None
    d.gripper_offset_cm = 14.0
    return d


def _yellow_centroid(frame):
    """Centroid of pure-yellow (0,255,255 BGR) pixels, or None."""
    ys, xs = np.where(
        (frame[:, :, 0] == 0) & (frame[:, :, 1] == 255) & (frame[:, :, 2] == 255)
    )
    if len(xs) == 0:
        return None
    return (float(xs.mean()), float(ys.mean()))


def _make_tag(center, world_xy):
    cx, cy = center
    corners = np.array(
        [[cx - 10, cy - 10], [cx + 10, cy - 10], [cx + 10, cy + 10], [cx - 10, cy + 10]],
        dtype=np.float32,
    )
    t = AprilTag(
        id=1, family="36h11", corners_px=corners, center_px=(float(cx), float(cy)),
        top_dir_px=(0.0, -1.0), orientation_yaw=0.0, world_xy=world_xy, in_playfield=True,
    )
    t.vel_px = (0.0, 0.0)  # suppress the (also-yellow) velocity arrow
    return t


def test_yellow_cross_uses_published_origin():
    # Identity homography (pixel == H-world), so the math is transparent.
    H = np.eye(3)
    center = (300.0, 300.0)
    origin = (5.0, -7.0)                       # daemon origin (apriltag:1 world)

    # Reproduce the daemon's A1-centring of world_xy for this tag (+y = north,
    # pure origin shift — no flip).  H @ center = center; daemon: wx = Hx - ox,
    # wy = Hy - oy.
    world_xy = (center[0] - origin[0], center[1] - origin[1])

    # Correct: project with the published origin -> cross on the tag center.
    d = _display()
    frame = np.zeros((600, 600, 3), dtype=np.uint8)
    d.draw_overlays(frame, [_make_tag(center, world_xy)], homography=H,
                    origin_x=origin[0], origin_y=origin[1])
    good = _yellow_centroid(frame)
    assert good is not None
    assert good == pytest.approx(center, abs=2.0)

    # Buggy: project with field half-dims -> cross displaced by (half - origin).
    frame2 = np.zeros((600, 600, 3), dtype=np.uint8)
    half = (67.15, 44.65)
    d.draw_overlays(frame2, [_make_tag(center, world_xy)], homography=H,
                    origin_x=half[0], origin_y=half[1])
    bad = _yellow_centroid(frame2)
    assert bad is not None
    # displacement equals the origin disagreement in each axis
    assert abs(bad[0] - good[0]) == pytest.approx(half[0] - origin[0], abs=2.0)
    assert abs(bad[1] - good[1]) == pytest.approx(half[1] - origin[1], abs=2.0)
