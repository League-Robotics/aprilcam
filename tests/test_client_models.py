"""Unit tests for aprilcam.client.models — Pydantic domain models."""

from __future__ import annotations

import pytest

from aprilcam.client.models import (
    CameraInfo,
    ImageFrame,
    PathRecord,
    StreamEndpoint,
    TagFrame,
    TagRecord,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_tag_record(**overrides) -> TagRecord:
    defaults = dict(
        id=1,
        center_px=(100.0, 200.0),
        corners_px=[(90.0, 190.0), (110.0, 190.0), (110.0, 210.0), (90.0, 210.0)],
        yaw=0.5,
        world_xy=(15.0, 20.0),
        in_playfield=True,
        vel_px=(1.0, -0.5),
        speed_px=1.118,
        vel_world=(0.1, -0.05),
        speed_world=0.112,
        heading_rad=0.3,
        age=0.0,
    )
    defaults.update(overrides)
    return TagRecord(**defaults)


def make_tag_frame(**overrides) -> TagFrame:
    defaults = dict(
        frame_id=42,
        ts_mono_ns=1_000_000_000,
        ts_wall_ms=1_700_000_000_000,
        tags=[make_tag_record(id=1), make_tag_record(id=2)],
        homography=[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
        playfield_corners=[(0.0, 0.0), (100.0, 0.0), (100.0, 80.0), (0.0, 80.0)],
        fps=30.0,
    )
    defaults.update(overrides)
    return TagFrame(**defaults)


# ---------------------------------------------------------------------------
# TagRecord
# ---------------------------------------------------------------------------


class TestTagRecord:
    def test_construct_all_fields(self):
        tag = make_tag_record()
        assert tag.id == 1
        assert tag.center_px == (100.0, 200.0)
        assert len(tag.corners_px) == 4
        assert isinstance(tag.yaw, float)
        assert tag.world_xy == (15.0, 20.0)
        assert tag.in_playfield is True
        assert tag.vel_px == (1.0, -0.5)
        assert tag.speed_px == pytest.approx(1.118)
        assert tag.vel_world == (0.1, -0.05)
        assert tag.speed_world == pytest.approx(0.112)
        assert tag.heading_rad == pytest.approx(0.3)
        assert tag.age == 0.0

    def test_world_xy_none(self):
        tag = make_tag_record(world_xy=None)
        assert tag.world_xy is None

    def test_optional_vel_fields_none(self):
        tag = make_tag_record(
            vel_px=None,
            speed_px=None,
            vel_world=None,
            speed_world=None,
            heading_rad=None,
        )
        assert tag.vel_px is None
        assert tag.speed_px is None
        assert tag.vel_world is None
        assert tag.speed_world is None
        assert tag.heading_rad is None

    def test_missing_required_field_raises(self):
        with pytest.raises(Exception):
            TagRecord(
                # missing id and other required fields
                center_px=(1.0, 2.0),
                yaw=0.0,
                in_playfield=False,
                age=0.0,
            )

    def test_center_px_type(self):
        tag = make_tag_record()
        cx, cy = tag.center_px
        assert isinstance(cx, float)
        assert isinstance(cy, float)


# ---------------------------------------------------------------------------
# TagFrame
# ---------------------------------------------------------------------------


class TestTagFrame:
    def test_construct_two_tags(self):
        frame = make_tag_frame()
        assert frame.frame_id == 42
        assert len(frame.tags) == 2
        assert frame.tags[0].id == 1
        assert frame.tags[1].id == 2

    def test_homography_none(self):
        frame = make_tag_frame(homography=None)
        assert frame.homography is None

    def test_homography_3x3(self):
        frame = make_tag_frame()
        assert frame.homography is not None
        assert len(frame.homography) == 3
        for row in frame.homography:
            assert len(row) == 3

    def test_playfield_corners_four_points(self):
        frame = make_tag_frame()
        assert len(frame.playfield_corners) == 4

    def test_fps_field(self):
        frame = make_tag_frame(fps=29.97)
        assert frame.fps == pytest.approx(29.97)

    def test_empty_tags(self):
        frame = make_tag_frame(tags=[])
        assert frame.tags == []

    def test_by_id_found(self):
        frame = make_tag_frame()
        tag = frame.by_id(2)
        assert tag is not None and tag.id == 2

    def test_by_id_missing_returns_none(self):
        frame = make_tag_frame()
        assert frame.by_id(99) is None

    def test_by_id_empty_frame_returns_none(self):
        assert make_tag_frame(tags=[]).by_id(1) is None

    def test_by_id_returns_first_on_duplicate(self):
        a = make_tag_record(id=1, center_px=(10.0, 10.0))
        b = make_tag_record(id=1, center_px=(20.0, 20.0))
        frame = make_tag_frame(tags=[a, b])
        assert frame.by_id(1).center_px == (10.0, 10.0)


# ---------------------------------------------------------------------------
# ImageFrame
# ---------------------------------------------------------------------------


class TestImageFrame:
    def test_construct(self):
        img = ImageFrame(
            cam_name="cam0",
            frame_id=1,
            ts_mono_ns=500_000_000,
            ts_wall_ms=1_700_000_000_000,
            jpeg=b"\xff\xd8\xff\xe0",
            width=640,
            height=480,
        )
        assert img.cam_name == "cam0"
        assert img.width == 640
        assert img.height == 480
        assert isinstance(img.jpeg, bytes)

    def test_missing_field_raises(self):
        with pytest.raises(Exception):
            ImageFrame(cam_name="cam0", frame_id=1)


# ---------------------------------------------------------------------------
# CameraInfo
# ---------------------------------------------------------------------------


class TestCameraInfo:
    def test_construct(self):
        info = CameraInfo(
            cam_name="cam0",
            calibrated=True,
            frame_size=(1280, 720),
            fps=30.0,
        )
        assert info.cam_name == "cam0"
        assert info.calibrated is True
        assert info.frame_size == (1280, 720)
        assert info.fps == 30.0

    def test_frame_size_tuple_of_ints(self):
        info = CameraInfo(
            cam_name="cam0",
            calibrated=False,
            frame_size=(640, 480),
            fps=15.0,
        )
        w, h = info.frame_size
        assert isinstance(w, int)
        assert isinstance(h, int)

    def test_uncalibrated(self):
        info = CameraInfo(
            cam_name="cam0",
            calibrated=False,
            frame_size=(320, 240),
            fps=10.0,
        )
        assert info.calibrated is False

    def test_missing_field_raises(self):
        with pytest.raises(Exception):
            CameraInfo(cam_name="cam0")


# ---------------------------------------------------------------------------
# PathRecord
# ---------------------------------------------------------------------------


class TestPathRecord:
    def test_construct(self):
        path = PathRecord(
            points=[(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)],
            color=(0, 255, 0),
            thickness=2,
            closed=True,
        )
        assert len(path.points) == 3
        assert path.color == (0, 255, 0)
        assert path.thickness == 2
        assert path.closed is True

    def test_open_path(self):
        path = PathRecord(
            points=[(1.0, 2.0), (3.0, 4.0)],
            color=(255, 0, 0),
            thickness=1,
            closed=False,
        )
        assert path.closed is False


# ---------------------------------------------------------------------------
# StreamEndpoint
# ---------------------------------------------------------------------------


class TestStreamEndpoint:
    def test_unix_socket(self):
        ep = StreamEndpoint(socket_path="/tmp/aprilcam/cam0/tags-abc.sock")
        assert ep.socket_path == "/tmp/aprilcam/cam0/tags-abc.sock"
        assert ep.tcp_port is None

    def test_tcp(self):
        ep = StreamEndpoint(tcp_port=5281)
        assert ep.tcp_port == 5281
        assert ep.socket_path is None

    def test_both(self):
        ep = StreamEndpoint(
            socket_path="/tmp/aprilcam/cam0/tags-abc.sock",
            tcp_port=5281,
        )
        assert ep.socket_path is not None
        assert ep.tcp_port == 5281

    def test_empty(self):
        ep = StreamEndpoint()
        assert ep.socket_path is None
        assert ep.tcp_port is None


# ---------------------------------------------------------------------------
# Import test
# ---------------------------------------------------------------------------


def test_importable_from_package():
    from aprilcam.client.models import TagFrame as TF  # noqa: F401

    assert TF is TagFrame


# ---------------------------------------------------------------------------
# from_proto() classmethods (require proto stubs)
# ---------------------------------------------------------------------------


class TestFromProto:
    """Tests for the from_proto() adapters.

    These tests import the generated proto stubs and construct proto messages
    programmatically, then verify the Pydantic adapter produces correct output.
    """

    @pytest.fixture(autouse=True)
    def import_pb2(self):
        try:
            from aprilcam.proto import aprilcam_pb2

            self.pb2 = aprilcam_pb2
        except ImportError as exc:  # pragma: no cover
            pytest.skip(f"Proto stubs not available: {exc}")

    def test_tag_record_from_proto(self):
        msg = self.pb2.TagMsg(
            id=7,
            cx_px=50.0,
            cy_px=60.0,
            corners_px=[45.0, 55.0, 55.0, 55.0, 55.0, 65.0, 45.0, 65.0],
            yaw=1.2,
            wx=10.0,
            wy=20.0,
            in_playfield=True,
            vx_px=2.0,
            vy_px=-1.0,
            speed_px=2.236,
            vx_world=0.2,
            vy_world=-0.1,
            speed_world=0.224,
            heading_rad=0.7,
            age=0.033,
        )
        tag = TagRecord.from_proto(msg)
        assert tag.id == 7
        assert tag.center_px == (50.0, 60.0)
        assert len(tag.corners_px) == 4
        assert tag.yaw == pytest.approx(1.2)
        assert tag.world_xy == (10.0, 20.0)
        assert tag.in_playfield is True
        assert tag.vel_px == (2.0, -1.0)
        assert tag.speed_px == pytest.approx(2.236)
        assert tag.vel_world[0] == pytest.approx(0.2, abs=1e-5)
        assert tag.vel_world[1] == pytest.approx(-0.1, abs=1e-5)
        assert tag.heading_rad == pytest.approx(0.7, abs=1e-5)
        assert tag.age == pytest.approx(0.033)

    def test_tag_record_from_proto_uncalibrated(self):
        msg = self.pb2.TagMsg(id=3, cx_px=0.0, cy_px=0.0, wx=0.0, wy=0.0)
        tag = TagRecord.from_proto(msg)
        # wx=0, wy=0 → world_xy is None (treat as uncalibrated)
        assert tag.world_xy is None

    def test_tag_frame_from_proto(self):
        tag_msg = self.pb2.TagMsg(id=5, cx_px=10.0, cy_px=20.0)
        msg = self.pb2.TagFrame(
            frame_id=99,
            ts_mono_ns=2_000_000_000,
            ts_wall_ms=1_700_000_000_001,
            tags=[tag_msg],
            homography=[1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
            playfield_corners=[0.0, 0.0, 100.0, 0.0, 100.0, 80.0, 0.0, 80.0],
            fps=25.0,
        )
        frame = TagFrame.from_proto(msg)
        assert frame.frame_id == 99
        assert frame.ts_mono_ns == 2_000_000_000
        assert len(frame.tags) == 1
        assert frame.tags[0].id == 5
        assert frame.homography is not None
        assert len(frame.homography) == 3
        assert len(frame.playfield_corners) == 4
        assert frame.fps == pytest.approx(25.0)

    def test_tag_frame_from_proto_no_homography(self):
        msg = self.pb2.TagFrame(frame_id=1)
        frame = TagFrame.from_proto(msg)
        assert frame.homography is None

    def test_image_frame_from_proto(self):
        msg = self.pb2.ImageFrame(
            frame_id=10,
            ts_mono_ns=3_000_000_000,
            jpeg=b"\xff\xd8\xff\xe0",
            width=1280,
            height=720,
        )
        img = ImageFrame.from_proto(msg, cam_name="cam0")
        assert img.cam_name == "cam0"
        assert img.frame_id == 10
        assert img.width == 1280
        assert img.height == 720
        assert img.jpeg == b"\xff\xd8\xff\xe0"

    def test_camera_info_from_proto(self):
        msg = self.pb2.CameraInfoResponse(
            cam_name="cam0",
            calibrated=True,
            frame_w=1920,
            frame_h=1080,
            fps=60.0,
        )
        info = CameraInfo.from_proto(msg)
        assert info.cam_name == "cam0"
        assert info.calibrated is True
        assert info.frame_size == (1920, 1080)
        assert info.fps == pytest.approx(60.0)

    def test_stream_endpoint_from_proto(self):
        msg = self.pb2.StreamEndpoint(
            socket_path="/tmp/aprilcam/cam0/tags-xyz.sock",
            tcp_port=5281,
        )
        ep = StreamEndpoint.from_proto(msg)
        assert ep.socket_path == "/tmp/aprilcam/cam0/tags-xyz.sock"
        assert ep.tcp_port == 5281
