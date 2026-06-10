"""Pydantic domain models for application-level AprilCam data.

These models represent what application code (CLI, MCP tools, tests) works
with after deserialization.  Proto-generated ``*_pb2`` types are used only
inside adapter code; all consumer-facing surfaces expose Pydantic models.

Each model that has a protobuf counterpart provides a ``from_proto()``
classmethod that converts the proto message into the Pydantic model.  That
thin mapping is the only place proto types cross the boundary.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    # Avoid importing proto stubs at module load — they are only needed
    # inside from_proto() classmethods which are called at runtime.
    from aprilcam.proto import aprilcam_pb2  # noqa: F401


# ---------------------------------------------------------------------------
# TagRecord
# ---------------------------------------------------------------------------


class TagRecord(BaseModel):
    """One detected AprilTag / ArUco marker within a single camera frame."""

    id: int
    center_px: tuple[float, float]
    corners_px: list[tuple[float, float]]  # 4 corner points
    yaw: float
    world_xy: tuple[float, float] | None  # None when uncalibrated
    in_playfield: bool
    vel_px: tuple[float, float] | None
    speed_px: float | None
    vel_world: tuple[float, float] | None
    speed_world: float | None
    heading_rad: float | None
    age: float

    @classmethod
    def from_proto(cls, msg: "aprilcam_pb2.TagMsg") -> "TagRecord":
        """Construct a TagRecord from a protobuf TagMsg.

        World coordinates are stored as ``(wx, wy)`` in the proto.  When both
        are zero the tag is considered uncalibrated (proto cannot encode None).
        We preserve the zero pair rather than converting to None so that a
        genuinely calibrated tag at world origin is not lost; callers that need
        to distinguish must check the ``CameraInfo.calibrated`` flag.
        """
        corners_raw: list[float] = list(msg.corners_px)
        corners: list[tuple[float, float]] = [
            (corners_raw[i], corners_raw[i + 1])
            for i in range(0, len(corners_raw), 2)
        ]

        world_xy: tuple[float, float] | None = (
            (float(msg.wx), float(msg.wy))
            if (msg.in_playfield or msg.wx != 0.0 or msg.wy != 0.0)
            else None
        )

        vel_px: tuple[float, float] | None = (
            (float(msg.vx_px), float(msg.vy_px))
            if (msg.vx_px != 0.0 or msg.vy_px != 0.0)
            else None
        )

        vel_world: tuple[float, float] | None = (
            (float(msg.vx_world), float(msg.vy_world))
            if (msg.vx_world != 0.0 or msg.vy_world != 0.0)
            else None
        )

        return cls(
            id=int(msg.id),
            center_px=(float(msg.cx_px), float(msg.cy_px)),
            corners_px=corners,
            yaw=float(msg.yaw),
            world_xy=world_xy,
            in_playfield=bool(msg.in_playfield),
            vel_px=vel_px,
            speed_px=float(msg.speed_px) if msg.speed_px != 0.0 else None,
            vel_world=vel_world,
            speed_world=float(msg.speed_world) if msg.speed_world != 0.0 else None,
            heading_rad=float(msg.heading_rad) if msg.heading_rad != 0.0 else None,
            age=float(msg.age),
        )


# ---------------------------------------------------------------------------
# TagFrame
# ---------------------------------------------------------------------------


class TagFrame(BaseModel):
    """One tag-stream message — all tags visible in a single camera frame."""

    frame_id: int
    ts_mono_ns: int
    ts_wall_ms: int
    tags: list[TagRecord]
    homography: list[list[float]] | None  # 3×3 row-major; None if uncalibrated
    playfield_corners: list[tuple[float, float]]  # 4 corner points (UL/UR/LR/LL)
    fps: float
    field_width_cm: float = 0.0
    field_height_cm: float = 0.0

    def by_id(self, tag_id: int) -> "TagRecord | None":
        """Return the tag with marker id *tag_id*, or ``None`` if not present.

        Convenience accessor so callers don't have to scan ``tags`` by hand.
        If the same id appears more than once (e.g. an AprilTag and an ArUco
        marker sharing a number), the first match in frame order is returned.
        """
        return next((t for t in self.tags if t.id == tag_id), None)

    @classmethod
    def from_proto(cls, msg: "aprilcam_pb2.TagFrame") -> "TagFrame":
        """Construct a TagFrame from a protobuf TagFrame message."""
        homo_flat: list[float] = list(msg.homography)
        homography: list[list[float]] | None = None
        if len(homo_flat) == 9:
            homography = [
                homo_flat[0:3],
                homo_flat[3:6],
                homo_flat[6:9],
            ]

        corners_flat: list[float] = list(msg.playfield_corners)
        playfield_corners: list[tuple[float, float]] = [
            (corners_flat[i], corners_flat[i + 1])
            for i in range(0, len(corners_flat), 2)
        ]

        return cls(
            frame_id=int(msg.frame_id),
            ts_mono_ns=int(msg.ts_mono_ns),
            ts_wall_ms=int(msg.ts_wall_ms),
            tags=[TagRecord.from_proto(t) for t in msg.tags],
            homography=homography,
            playfield_corners=playfield_corners,
            fps=float(msg.fps),
            field_width_cm=float(msg.field_width_cm),
            field_height_cm=float(msg.field_height_cm),
        )


# ---------------------------------------------------------------------------
# ImageFrame
# ---------------------------------------------------------------------------


class ImageFrame(BaseModel):
    """One image-stream message — a single JPEG frame from a camera."""

    cam_name: str
    frame_id: int
    ts_mono_ns: int
    ts_wall_ms: int
    jpeg: bytes
    width: int
    height: int

    model_config = {"arbitrary_types_allowed": True}

    @classmethod
    def from_proto(cls, msg: "aprilcam_pb2.ImageFrame", *, cam_name: str = "") -> "ImageFrame":
        """Construct an ImageFrame from a protobuf ImageFrame message.

        ``cam_name`` is not part of the wire message; pass it from the stream
        context (e.g., the endpoint's ``cam_name`` field).
        """
        return cls(
            cam_name=cam_name,
            frame_id=int(msg.frame_id),
            ts_mono_ns=int(msg.ts_mono_ns),
            ts_wall_ms=0,  # ImageFrame proto has no ts_wall_ms; default to 0
            jpeg=bytes(msg.jpeg),
            width=int(msg.width),
            height=int(msg.height),
        )


# ---------------------------------------------------------------------------
# CameraInfo
# ---------------------------------------------------------------------------


class CameraInfo(BaseModel):
    """Camera metadata returned by GetCameraInfo / open_camera."""

    cam_name: str
    calibrated: bool
    frame_size: tuple[int, int]  # (width, height)
    fps: float

    @classmethod
    def from_proto(cls, msg: "aprilcam_pb2.CameraInfoResponse") -> "CameraInfo":
        """Construct a CameraInfo from a protobuf CameraInfoResponse message."""
        return cls(
            cam_name=str(msg.cam_name),
            calibrated=bool(msg.calibrated),
            frame_size=(int(msg.frame_w), int(msg.frame_h)),
            fps=float(msg.fps),
        )


# ---------------------------------------------------------------------------
# PathRecord
# ---------------------------------------------------------------------------


class PathRecord(BaseModel):
    """A drawn path overlay on the playfield (world coordinates)."""

    points: list[tuple[float, float]]  # world coordinates in cm
    color: tuple[int, int, int]        # BGR
    thickness: int
    closed: bool


# ---------------------------------------------------------------------------
# StreamEndpoint
# ---------------------------------------------------------------------------


class StreamEndpoint(BaseModel):
    """Describes how to connect to an image or tag stream socket."""

    socket_path: str | None = None   # non-empty when using Unix sockets
    tcp_port: int | None = None       # non-zero when using TCP

    @classmethod
    def from_proto(cls, msg: "aprilcam_pb2.StreamEndpoint") -> "StreamEndpoint":
        """Construct a StreamEndpoint from a protobuf StreamEndpoint message."""
        return cls(
            socket_path=str(msg.socket_path) if msg.socket_path else None,
            tcp_port=int(msg.tcp_port) if msg.tcp_port else None,
        )
