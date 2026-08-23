"""Unit tests for Tag class."""

import time
import pytest
from unittest.mock import MagicMock

pytest.importorskip("cv2", reason="requires aprilcam[imaging]")

from aprilcam.core.tag import Tag
from aprilcam.core.detection import TagRecord, FrameRecord, RingBuffer


def _make_tag_record(tag_id=1, cx=100.0, cy=200.0, ts=1000.0, age=0.0,
                     vel_px=(10.0, 5.0), world_xy=None):
    return TagRecord(
        id=tag_id, center_px=(cx, cy),
        corners_px=[[cx-5, cy-5], [cx+5, cy-5], [cx+5, cy+5], [cx-5, cy+5]],
        orientation_yaw=1.57, world_xy=world_xy, in_playfield=True,
        vel_px=vel_px, speed_px=11.18, vel_world=None, speed_world=None,
        heading_rad=None, timestamp=ts, frame_index=0, age=age,
    )


def _make_pipeline_with_record(tr):
    """Create a mock pipeline with a ring buffer containing the given record."""
    rb = RingBuffer(maxlen=10)
    rb.append(FrameRecord(timestamp=tr.timestamp, frame_index=0, tags=[tr]))
    pipeline = MagicMock()
    pipeline.ring_buffer = rb
    return pipeline


class TestTag:

    def test_initial_state(self):
        pipeline = MagicMock()
        pipeline.ring_buffer = RingBuffer()
        tag = Tag(42, pipeline)
        assert tag.id == 42
        assert tag.cx == 0.0
        assert tag.cy == 0.0
        assert tag.wx is None
        assert not tag.is_visible

    def test_update_pulls_snapshot(self):
        tr = _make_tag_record(tag_id=1, cx=150.0, cy=250.0)
        pipeline = _make_pipeline_with_record(tr)
        tag = Tag(1, pipeline)
        tag.update()
        assert tag.cx == pytest.approx(150.0)
        assert tag.cy == pytest.approx(250.0)
        assert tag.is_visible

    def test_update_returns_self(self):
        tr = _make_tag_record()
        pipeline = _make_pipeline_with_record(tr)
        tag = Tag(1, pipeline)
        result = tag.update()
        assert result is tag

    def test_velocity_and_speed(self):
        tr = _make_tag_record(vel_px=(10.0, 5.0))
        pipeline = _make_pipeline_with_record(tr)
        tag = Tag(1, pipeline)
        tag.update()
        assert tag.velocity == (10.0, 5.0)
        assert tag.speed == pytest.approx(11.18, abs=0.1)

    def test_position_at_extrapolates(self):
        tr = _make_tag_record(cx=100.0, cy=200.0, vel_px=(10.0, 0.0), ts=1000.0)
        pipeline = _make_pipeline_with_record(tr)
        tag = Tag(1, pipeline)
        tag.update()
        pos = tag.position_at(1000.1)
        assert pos[0] == pytest.approx(101.0, abs=0.01)
        assert pos[1] == pytest.approx(200.0, abs=0.01)

    def test_to_dict(self):
        tr = _make_tag_record()
        pipeline = _make_pipeline_with_record(tr)
        tag = Tag(1, pipeline)
        tag.update()
        d = tag.to_dict()
        assert d["id"] == 1
        assert "cx" in d
        assert "cy" in d
        assert "is_visible" in d

    def test_stale_tag_not_visible(self):
        tr = _make_tag_record(age=0.5)
        pipeline = _make_pipeline_with_record(tr)
        tag = Tag(1, pipeline)
        tag.update()
        assert not tag.is_visible

    def test_world_position(self):
        tr = _make_tag_record(world_xy=(50.0, 30.0))
        pipeline = _make_pipeline_with_record(tr)
        tag = Tag(1, pipeline)
        tag.update()
        assert tag.wx == pytest.approx(50.0)
        assert tag.wy == pytest.approx(30.0)
