"""Unit tests for RingBuffer and TagRecord."""

import time
import pytest

pytest.importorskip("cv2", reason="requires aprilcam[imaging]")

from aprilcam.core import RingBuffer, TagRecord
from aprilcam.core.detection import FrameRecord


def _make_tag_record(tag_id=1, cx=100.0, cy=200.0, ts=1000.0):
    return TagRecord(
        id=tag_id,
        center_px=(cx, cy),
        corners_px=[[cx-5, cy-5], [cx+5, cy-5], [cx+5, cy+5], [cx-5, cy+5]],
        orientation_yaw=0.0,
        world_xy=None,
        in_playfield=True,
        vel_px=(10.0, 0.0),
        speed_px=10.0,
        vel_world=None,
        speed_world=None,
        heading_rad=None,
        timestamp=ts,
        frame_index=0,
    )


class TestRingBuffer:

    def test_empty(self):
        rb = RingBuffer(maxlen=5)
        assert len(rb) == 0
        assert rb.get_latest() is None
        assert rb.get_last_n(3) == []

    def test_append_and_get_latest(self):
        rb = RingBuffer(maxlen=5)
        fr = FrameRecord(timestamp=1.0, frame_index=0, tags=[_make_tag_record()])
        rb.append(fr)
        assert len(rb) == 1
        assert rb.get_latest() is fr

    def test_maxlen_eviction(self):
        rb = RingBuffer(maxlen=3)
        for i in range(5):
            rb.append(FrameRecord(timestamp=float(i), frame_index=i, tags=[]))
        assert len(rb) == 3
        assert rb.get_latest().frame_index == 4

    def test_get_last_n(self):
        rb = RingBuffer(maxlen=10)
        for i in range(5):
            rb.append(FrameRecord(timestamp=float(i), frame_index=i, tags=[]))
        last2 = rb.get_last_n(2)
        assert len(last2) == 2
        assert last2[0].frame_index == 3
        assert last2[1].frame_index == 4

    def test_clear(self):
        rb = RingBuffer()
        rb.append(FrameRecord(timestamp=1.0, frame_index=0, tags=[]))
        rb.clear()
        assert len(rb) == 0


class TestTagRecord:

    def test_to_dict(self):
        tr = _make_tag_record()
        d = tr.to_dict()
        assert d["id"] == 1
        assert d["center_px"] == [100.0, 200.0]

    def test_estimate_shifts_position(self):
        tr = _make_tag_record(ts=1000.0)
        est = tr.estimate(1000.1)
        assert est.center_px[0] == pytest.approx(101.0, abs=0.01)
        assert est.timestamp == 1000.1

    def test_estimate_no_velocity(self):
        tr = TagRecord(
            id=1, center_px=(100.0, 200.0),
            corners_px=[[95, 195], [105, 195], [105, 205], [95, 205]],
            orientation_yaw=0.0, world_xy=None, in_playfield=True,
            vel_px=None, speed_px=None,
            vel_world=None, speed_world=None, heading_rad=None,
            timestamp=1000.0, frame_index=0,
        )
        est = tr.estimate(1000.5)
        assert est.center_px == tr.center_px
