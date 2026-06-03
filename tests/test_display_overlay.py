import time
import numpy as np
import pytest
from unittest.mock import MagicMock

pytest.importorskip("cv2", reason="requires aprilcam[imaging]")

from aprilcam.ui.display import PlayfieldDisplay
from aprilcam.proto import aprilcam_pb2


def _make_display():
    """Construct a PlayfieldDisplay with only the attrs needed by draw_live_overlay."""
    d = PlayfieldDisplay.__new__(PlayfieldDisplay)
    # Attrs required by _map_points_to_display
    d._mode = "full"
    d._crop_xy = (0, 0)
    d._crop_wh = (600, 600)
    d.M_deskew = None
    return d


def _identity_homography():
    # Simple scaling homography: world cm -> display px at 3px/cm
    H = np.array([[3.0, 0, 0], [0, 3.0, 0], [0, 0, 1.0]])
    return H


def _frame():
    return np.zeros((600, 600, 3), dtype=np.uint8)


def _overlay(elements, ttl=10.0):
    return aprilcam_pb2.OverlayFrame(
        timestamp=time.time(),
        ttl=ttl,
        elements=elements,
    )


def test_no_op_when_homography_none():
    d = _make_display()
    frame = _frame()
    overlay = _overlay([])
    d.draw_live_overlay(frame, overlay, None)
    assert frame.sum() == 0  # nothing drawn


def test_no_op_when_expired():
    d = _make_display()
    frame = _frame()
    overlay = aprilcam_pb2.OverlayFrame(timestamp=time.time() - 10, ttl=0.1)
    d.draw_live_overlay(frame, overlay, _identity_homography())
    assert frame.sum() == 0


def test_arc_draws():
    d = _make_display()
    frame = _frame()
    elem = aprilcam_pb2.OverlayElement(
        type="arc", params=[50.0, 50.0, 10.0, 0.0, 180.0],
        color=[0, 255, 0], thickness=2
    )
    # origin_y=100 so raw_y = 100-50 = 50, which lands on the 600px frame
    d.draw_live_overlay(frame, _overlay([elem]), _identity_homography(), origin_y=100.0)
    assert frame.sum() > 0


def test_arrow_draws():
    d = _make_display()
    frame = _frame()
    elem = aprilcam_pb2.OverlayElement(
        type="arrow", params=[40.0, 40.0, 60.0, 60.0],
        color=[255, 0, 0], thickness=2
    )
    d.draw_live_overlay(frame, _overlay([elem]), _identity_homography(), origin_y=100.0)
    assert frame.sum() > 0


def test_point_draws():
    d = _make_display()
    frame = _frame()
    elem = aprilcam_pb2.OverlayElement(
        type="point", params=[50.0, 50.0, 3.0],
        color=[0, 0, 255], thickness=-1
    )
    d.draw_live_overlay(frame, _overlay([elem]), _identity_homography(), origin_y=100.0)
    assert frame.sum() > 0


def test_polyline_draws():
    d = _make_display()
    frame = _frame()
    elem = aprilcam_pb2.OverlayElement(
        type="polyline", params=[40.0, 40.0, 50.0, 60.0, 60.0, 40.0],
        color=[255, 255, 0], thickness=2
    )
    d.draw_live_overlay(frame, _overlay([elem]), _identity_homography(), origin_y=100.0)
    assert frame.sum() > 0


def test_unknown_type_skipped():
    d = _make_display()
    frame = _frame()
    elem = aprilcam_pb2.OverlayElement(type="bogus", params=[1.0, 2.0])
    d.draw_live_overlay(frame, _overlay([elem]), _identity_homography())
    # No exception raised; frame may or may not be modified (doesn't matter)


def test_text_draws():
    d = _make_display()
    frame = _frame()
    elem = aprilcam_pb2.OverlayElement(
        type="text", params=[50.0, 50.0],
        text="hello", color=[255, 255, 0], thickness=1,
    )
    d.draw_live_overlay(frame, _overlay([elem]), _identity_homography(), origin_y=100.0)
    assert frame.sum() > 0


def test_text_empty_string_no_raise():
    d = _make_display()
    frame = _frame()
    elem = aprilcam_pb2.OverlayElement(
        type="text", params=[50.0, 50.0], text="",
    )
    d.draw_live_overlay(frame, _overlay([elem]), _identity_homography(), origin_y=100.0)
    # empty string — no exception, frame may or may not be modified


def test_rect_draws():
    d = _make_display()
    frame = _frame()
    elem = aprilcam_pb2.OverlayElement(
        type="rect", params=[30.0, 30.0, 70.0, 60.0],
        color=[0, 255, 255], thickness=-1,
    )
    d.draw_live_overlay(frame, _overlay([elem]), _identity_homography(), origin_y=100.0)
    assert frame.sum() > 0


def test_polygon_draws():
    d = _make_display()
    frame = _frame()
    elem = aprilcam_pb2.OverlayElement(
        type="polygon", params=[50.0, 30.0, 70.0, 60.0, 30.0, 60.0],
        color=[255, 0, 200], thickness=-1,
    )
    d.draw_live_overlay(frame, _overlay([elem]), _identity_homography(), origin_y=100.0)
    assert frame.sum() > 0
