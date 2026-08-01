"""Thin gRPC wrapper over ``daemon/service.py`` (ticket 020-004; the
tag-detection RPCs below are ticket 021-005).

Translates between ``proto/aprilcam_v2.proto``'s service (sprint 020's
four RPCs -- ``ListCameras``, ``ProbeCameras``, ``GetFrame``,
``GetStatus`` -- plus sprint 021's ``GetTags``/``GetTag``/
``GetTagHistory`` and the ``GetFrame`` tag embed) and
``daemon.service.DaemonService`` method calls. Everything here is
(de)serialization and error-code mapping; no business logic, no camera
access, no registry/discovery/vision calls -- see the sprint
architecture's module table and this ticket's explicit anti-pattern
warning: v1's ``grpc_server.py`` (1193 lines) mixed both in one file,
which is exactly what splitting ``rpc.py`` from ``service.py`` exists
to avoid.

**Proto boundary** (``docs/design/design.md`` §6: "generated proto
types cross into Python only via ``from_proto`` constructors in
``client/models.py``"). This module owns the *server-side* half of that
boundary -- Python dataclasses to proto messages -- so nothing outside
it imports ``aprilcam.proto.aprilcam_v2_pb2`` directly. The reverse
direction (wire -> Python, client-side) is ticket 020-005's
``client/models.py``. The one exception is reading this module's *own*
incoming requests (``GetFrameRequest.camera``/``.deskewed``/
``.encoding`` already did this for scalars; ``GetTag``/
``GetTagHistory``'s ``tag`` field extends it to a message via
:func:`_tag_id_from_proto`) -- a server parsing what it was just sent
is not the "proto types crossing into other Python code" the rule
guards against.

**Error mapping.** ``daemon.service`` raises ``aprilcam.errors`` typed
exceptions (never bare strings, per this ticket's Definition of Done);
this module never invents its own error types. Each ``AprilCamError``
is mapped to a gRPC status code plus trailing metadata carrying the
exception's class name and its typed fields (``AlreadyRunning.holder``;
``UnknownCamera.requested``/``.available``) so ticket 020-005's client
can reconstruct the *same* typed exception client-side rather than
pattern-matching status text -- ``errors.py``'s stated purpose ("the
client library raises something it can dispatch on") only survives the
wire if this boundary preserves it. Metadata keys are documented next
to :func:`_abort` below; ticket 005 should read them from there rather
than duplicate the list. ``UnknownCamera``/``CameraNotPresent`` need no
new mapping here for the tag-detection RPCs -- they flow through the
same ``_camera_for`` path ``GetFrame`` already uses.
"""

from __future__ import annotations

import logging
from concurrent import futures
from pathlib import Path
from typing import Iterable

import grpc

from aprilcam.daemon.interfaces import Daemon, DaemonStatus
from aprilcam.daemon.lifecycle import ControlPlaneListener
from aprilcam.errors import (
    AlreadyRunning,
    AprilCamError,
    CameraNotPresent,
    NotCalibrated,
    UnknownCamera,
)
from aprilcam.proto import aprilcam_v2_pb2, aprilcam_v2_pb2_grpc
from aprilcam.types import (
    CameraState,
    ImageFrame,
    PixelPoint,
    ProbeReport,
    TagFamily,
    TagFrame,
    TagId,
    TagRecord,
    WorldPoint,
)

__all__ = ["build_server"]

logger = logging.getLogger(__name__)

#: exception type -> gRPC status code. Anything not listed (including
#: an ``AprilCamError`` subtype added by a later sprint before this map
#: is updated) falls back to ``INTERNAL`` in :func:`_abort` -- a
#: conservative default that never claims a more specific code than is
#: actually known to be correct.
_STATUS_CODES = {
    AlreadyRunning: grpc.StatusCode.ALREADY_EXISTS,
    UnknownCamera: grpc.StatusCode.NOT_FOUND,
    NotCalibrated: grpc.StatusCode.FAILED_PRECONDITION,
    # Same "the operation cannot succeed given current state" semantics
    # as NotCalibrated -- the camera is known, just not present right
    # now (ticket 021-002).
    CameraNotPresent: grpc.StatusCode.FAILED_PRECONDITION,
}


def _camera_state_to_proto(state: CameraState) -> aprilcam_v2_pb2.CameraState:
    msg = aprilcam_v2_pb2.CameraState(
        name=state.name,
        present=state.present,
        usable=state.usable,
        calibrated=state.calibrated,
        calibration_stale=state.calibration_stale,
        frame_dark=state.frame_dark,
    )
    # optional fields: only set when not None, so HasField() on the wire
    # matches Optional[...] being None on this side (design.md §6).
    if state.playfield is not None:
        msg.playfield = state.playfield
    if state.number is not None:
        msg.number = state.number
    return msg


def _camera_states_to_proto(states: Iterable[CameraState]) -> list:
    return [_camera_state_to_proto(s) for s in states]


def _probe_report_to_proto(report: ProbeReport) -> aprilcam_v2_pb2.ProbeCamerasResponse:
    return aprilcam_v2_pb2.ProbeCamerasResponse(
        cameras=_camera_states_to_proto(report.cameras),
        skipped=[
            aprilcam_v2_pb2.ProbeSkip(backend=s.backend, reason=s.reason)
            for s in report.skipped
        ],
    )


def _image_frame_to_proto(frame: ImageFrame) -> aprilcam_v2_pb2.GetFrameResponse:
    msg = aprilcam_v2_pb2.GetFrameResponse(
        data=frame.data,
        width=frame.width,
        height=frame.height,
        encoding=frame.encoding,
        timestamp=frame.timestamp,
        deskewed=frame.deskewed,
        calibration_stale=frame.calibration_stale,
        frame_dark=frame.frame_dark,
    )
    # Presence, not an empty-vs-absent default: only set `tags` when
    # detection has actually run (frame.tags is not None). Leaving it
    # unset keeps HasField("tags") false, matching ImageFrame.tags's
    # three-way "absent means no detection has run" contract -- a
    # present-but-empty TagFrame (detection ran, saw nothing) is a
    # different, also-representable answer (ticket 021-005).
    if frame.tags is not None:
        msg.tags.CopyFrom(_tag_frame_to_proto(frame.tags))
    return msg


def _status_to_proto(status: DaemonStatus) -> aprilcam_v2_pb2.GetStatusResponse:
    return aprilcam_v2_pb2.GetStatusResponse(
        running=status.running,
        cameras=_camera_states_to_proto(status.cameras),
        config_dir=status.config_dir,
        state_dir=status.state_dir,
    )


#: TagFamily <-> the generated enum's int values, both directions.
#: Duplicated (not shared) with ``client/models.py``'s identical-shaped
#: map -- see this module's docstring: each side of the proto boundary
#: owns its own small conversion rather than importing across it.
_TAG_FAMILY_TO_PROTO = {
    TagFamily.APRILTAG: aprilcam_v2_pb2.APRILTAG,
    TagFamily.ARUCO: aprilcam_v2_pb2.ARUCO,
}
_TAG_FAMILY_FROM_PROTO = {v: k for k, v in _TAG_FAMILY_TO_PROTO.items()}


def _world_point_to_proto(point: WorldPoint) -> aprilcam_v2_pb2.WorldPoint:
    return aprilcam_v2_pb2.WorldPoint(x=point.x, y=point.y)


def _pixel_point_to_proto(point: PixelPoint) -> aprilcam_v2_pb2.PixelPoint:
    return aprilcam_v2_pb2.PixelPoint(x=point.x, y=point.y)


def _tag_id_to_proto(tag: TagId) -> aprilcam_v2_pb2.TagId:
    return aprilcam_v2_pb2.TagId(family=_TAG_FAMILY_TO_PROTO[tag.family], number=tag.number)


def _tag_id_from_proto(msg: aprilcam_v2_pb2.TagId) -> TagId:
    """The one wire -> Python conversion this module needs: an
    incoming ``GetTagRequest``/``GetTagHistoryRequest``'s ``tag`` field
    has to become a ``types.TagId`` before it can be passed to
    ``DaemonService``. This stays local to the *request* side of
    ``rpc.py`` -- the module docstring's boundary is about responses
    (Python -> proto, this module; proto -> Python, client-side
    ``client/models.py``); a server reading its own incoming request's
    fields is the same category of thing this module already does for
    ``GetFrameRequest.camera``/``.deskewed``/``.encoding``, just for a
    message-typed field instead of scalars."""
    return TagId(family=_TAG_FAMILY_FROM_PROTO[msg.family], number=msg.number)


def _tag_record_to_proto(record: TagRecord) -> aprilcam_v2_pb2.TagRecord:
    msg = aprilcam_v2_pb2.TagRecord(
        tag=_tag_id_to_proto(record.tag),
        timestamp=record.timestamp,
        calibrated=record.calibrated,
    )
    # optional fields: only set when not None, so HasField() on the wire
    # matches Optional[...] being None on this side (design.md §6) --
    # never coerced to 0.0/empty, which would look like a real answer.
    if record.world is not None:
        msg.world.CopyFrom(_world_point_to_proto(record.world))
    if record.heading_rad is not None:
        msg.heading_rad = record.heading_rad
    if record.world_velocity is not None:
        msg.world_velocity.CopyFrom(_world_point_to_proto(record.world_velocity))
    if record.speed is not None:
        msg.speed = record.speed
    if record.pixel is not None:
        msg.pixel.CopyFrom(_pixel_point_to_proto(record.pixel))
    if record.yaw_rad is not None:
        msg.yaw_rad = record.yaw_rad
    if record.world_corners is not None:
        msg.world_corners.extend(_world_point_to_proto(p) for p in record.world_corners)
    if record.pixel_corners is not None:
        msg.pixel_corners.extend(_pixel_point_to_proto(p) for p in record.pixel_corners)
    return msg


def _tag_frame_to_proto(frame: TagFrame) -> aprilcam_v2_pb2.TagFrame:
    return aprilcam_v2_pb2.TagFrame(
        tags=[_tag_record_to_proto(r) for r in frame.tags],
        timestamp=frame.timestamp,
        frame_index=frame.frame_index,
        calibration_stale=frame.calibration_stale,
    )


def _abort(context: grpc.ServicerContext, exc: Exception) -> None:
    """Map *exc* to a gRPC status and end the RPC. Never returns --
    ``context.abort`` raises internally; the ``except`` blocks in
    :class:`_Servicer` below call this and then return an empty
    response only to satisfy the method's return type, since control
    never actually reaches that line.

    Trailing metadata keys, by exception type (the wire contract ticket
    020-005's client reads back into typed errors):

    * every ``AprilCamError``: ``aprilcam-error-type`` = the exception's
      class name.
    * :class:`AlreadyRunning`: + ``aprilcam-holder``.
    * :class:`UnknownCamera`: + ``aprilcam-requested``,
      ``aprilcam-available`` (comma-joined).
    * :class:`CameraNotPresent`: + ``aprilcam-slug``.
    """
    if isinstance(exc, AprilCamError):
        code = grpc.StatusCode.INTERNAL
        for exc_type, mapped_code in _STATUS_CODES.items():
            if isinstance(exc, exc_type):
                code = mapped_code
                break
        metadata = [("aprilcam-error-type", type(exc).__name__)]
        if isinstance(exc, AlreadyRunning):
            metadata.append(("aprilcam-holder", exc.holder))
        elif isinstance(exc, UnknownCamera):
            metadata.append(("aprilcam-requested", exc.requested))
            metadata.append(("aprilcam-available", ",".join(exc.available)))
        elif isinstance(exc, CameraNotPresent):
            metadata.append(("aprilcam-slug", exc.slug))
        context.set_trailing_metadata(tuple(metadata))
        context.abort(code, str(exc))
    else:
        logger.exception("unhandled error serving RPC")
        context.abort(grpc.StatusCode.INTERNAL, str(exc))


class _Servicer(aprilcam_v2_pb2_grpc.AprilCamV2Servicer):
    """Implements the generated ``AprilCamV2Servicer`` base by
    delegating every method body to a :class:`~aprilcam.daemon.
    interfaces.Daemon`. See the module docstring -- no business logic
    lives here, only marshalling and error mapping."""

    def __init__(self, service: Daemon) -> None:
        self._service = service

    def ListCameras(self, request, context):  # noqa: N802 - gRPC-generated name
        try:
            cameras = self._service.list_cameras()
        except Exception as exc:  # noqa: BLE001 - boundary: map every error
            _abort(context, exc)
            return aprilcam_v2_pb2.ListCamerasResponse()
        return aprilcam_v2_pb2.ListCamerasResponse(cameras=_camera_states_to_proto(cameras))

    def ProbeCameras(self, request, context):  # noqa: N802
        try:
            report = self._service.probe_cameras()
        except Exception as exc:  # noqa: BLE001
            _abort(context, exc)
            return aprilcam_v2_pb2.ProbeCamerasResponse()
        return _probe_report_to_proto(report)

    def GetFrame(self, request, context):  # noqa: N802
        encoding = request.encoding or "jpeg"
        try:
            frame = self._service.get_frame(
                request.camera, deskewed=request.deskewed, encoding=encoding
            )
        except Exception as exc:  # noqa: BLE001
            _abort(context, exc)
            return aprilcam_v2_pb2.GetFrameResponse()
        return _image_frame_to_proto(frame)

    def GetStatus(self, request, context):  # noqa: N802
        try:
            status = self._service.status()
        except Exception as exc:  # noqa: BLE001
            _abort(context, exc)
            return aprilcam_v2_pb2.GetStatusResponse()
        return _status_to_proto(status)

    def GetTags(self, request, context):  # noqa: N802
        try:
            tag_frame = self._service.get_tags(request.camera)
        except Exception as exc:  # noqa: BLE001
            _abort(context, exc)
            return aprilcam_v2_pb2.GetTagsResponse()
        return aprilcam_v2_pb2.GetTagsResponse(tags=_tag_frame_to_proto(tag_frame))

    def GetTag(self, request, context):  # noqa: N802
        tag = _tag_id_from_proto(request.tag)
        try:
            record = self._service.get_tag(request.camera, tag)
        except Exception as exc:  # noqa: BLE001
            _abort(context, exc)
            return aprilcam_v2_pb2.GetTagResponse()
        response = aprilcam_v2_pb2.GetTagResponse()
        # Absent (never a zero-valued TagRecord) means not detected
        # this burst -- UC-09.S-4, and the same presence discipline as
        # every other optional field in this module.
        if record is not None:
            response.record.CopyFrom(_tag_record_to_proto(record))
        return response

    def GetTagHistory(self, request, context):  # noqa: N802
        tag = _tag_id_from_proto(request.tag) if request.HasField("tag") else None
        frames = request.frames if request.HasField("frames") else None
        try:
            history = self._service.get_tag_history(request.camera, tag=tag, frames=frames)
        except Exception as exc:  # noqa: BLE001
            _abort(context, exc)
            return aprilcam_v2_pb2.GetTagHistoryResponse()
        return aprilcam_v2_pb2.GetTagHistoryResponse(
            history=[_tag_frame_to_proto(f) for f in history]
        )


class _GrpcListener(ControlPlaneListener):
    """Adapts ``grpc.Server`` to ``daemon.lifecycle.ControlPlaneListener``
    (``start()`` / ``stop(grace)``), waiting for full shutdown inside
    ``stop()`` so the caller's next action -- e.g. a restart's fresh
    :func:`build_server` re-binding the same socket path -- never races
    the previous server's teardown.

    Derives from ``ControlPlaneListener`` (a plain base class -- see
    ``daemon.interfaces``'s module docstring for the design rule) so
    this is a mechanically real ``ControlPlaneListener``, not merely a
    structural match -- while ``daemon/lifecycle.py`` itself still
    imports no gRPC (this module imports *it*, not the other way
    around)."""

    def __init__(self, server: grpc.Server) -> None:
        self._server = server

    def start(self) -> None:
        self._server.start()

    def stop(self, grace: float) -> None:
        self._server.stop(grace).wait(timeout=grace + 1.0)


def build_server(service: Daemon, socket_path: Path, *, max_workers: int = 10) -> _GrpcListener:
    """Construct and bind, but do not start, a gRPC server serving
    *service* over the unix socket at *socket_path*.

    Deliberately unstarted -- this is the callable
    ``daemon/lifecycle.py``'s ``build_listener`` factory parameter
    expects (typically via ``functools.partial(build_server, service,
    socket_path)``), matching that module's "fresh server object per
    ``start()``" requirement: a stopped ``grpc.Server`` cannot be
    restarted, so ``daemon/__main__.py`` passes this function itself,
    not an already-built server.

    Removes a stale socket file left by a crashed prior run before
    binding -- safe here specifically because ``DaemonLifecycle.
    start()`` calls ``build_listener`` only *after* the singleton lock
    is already held, so no live daemon can own that path (see
    ``daemon/lifecycle.py``'s "lock before listener, always" ordering).
    """
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    if socket_path.exists():
        socket_path.unlink()
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=max_workers))
    aprilcam_v2_pb2_grpc.add_AprilCamV2Servicer_to_server(_Servicer(service), server)
    server.add_insecure_port(f"unix:{socket_path}")
    return _GrpcListener(server)
