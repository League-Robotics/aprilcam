"""Tests for daemon idle-gating: on-demand capture_frame encode, pull tracking,
and the producer has_subscribers() signal.

These exercise the pure pieces of the idle-gating change without opening a
camera or starting the capture thread.
"""
import numpy as np

from aprilcam.config import Config
from aprilcam.daemon.camera_pipeline import CameraPipeline
from aprilcam.daemon.stream import ImageStreamProducer, TagStreamProducer


def _pipeline() -> CameraPipeline:
    # State-only construction — does not open the camera or start the thread.
    return CameraPipeline("test-cam", 0, Config.load())


def test_capture_frame_none_before_any_frame():
    assert _pipeline().capture_frame() is None


def test_capture_frame_encodes_on_demand():
    p = _pipeline()
    # Simulate a captured raw frame with NO pre-encoded jpeg (no image subscriber).
    p._latest_raw_frame = np.zeros((16, 16, 3), dtype=np.uint8)
    p._latest_frame_id = 5
    jpeg = p.capture_frame()
    assert isinstance(jpeg, bytes)
    assert jpeg[:2] == b"\xff\xd8"  # JPEG start-of-image marker
    # The on-demand encode is cached for that frame id.
    assert p._latest_jpeg_frame_id == 5
    assert p._latest_raw_jpeg == jpeg


def test_capture_frame_reuses_cached_jpeg_for_same_frame():
    p = _pipeline()
    p._latest_raw_frame = np.zeros((8, 8, 3), dtype=np.uint8)
    p._latest_frame_id = 2
    p._latest_raw_jpeg = b"cached-bytes"
    p._latest_jpeg_frame_id = 2  # matches latest frame -> reuse, no re-encode
    assert p.capture_frame() == b"cached-bytes"


def test_capture_frame_records_pull():
    p = _pipeline()
    p._latest_raw_frame = np.zeros((8, 8, 3), dtype=np.uint8)
    p._latest_frame_id = 1
    before = p._last_pull_mono
    p.capture_frame()
    assert p._last_pull_mono > before


def test_get_current_tags_records_pull():
    p = _pipeline()
    before = p._last_pull_mono
    p.get_current_tags()  # empty ring -> empty response, but still a pull
    assert p._last_pull_mono > before


def test_producer_has_subscribers():
    import queue

    prod = ImageStreamProducer(
        "test-cam", Config.load(), enable_unix=False, enable_tcp=False
    )
    assert prod.has_subscribers() is False
    prod._senders[object()] = queue.Queue()  # inject a fake connected sender
    assert prod.has_subscribers() is True


def test_tag_producer_has_subscribers_initially_false():
    prod = TagStreamProducer(
        "test-cam", Config.load(), enable_unix=False, enable_tcp=False
    )
    assert prod.has_subscribers() is False
