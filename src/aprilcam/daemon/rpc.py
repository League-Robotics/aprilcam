"""Thin gRPC wrapper over ``daemon/service.py`` (ticket 020-004; the
tag-detection RPCs below are ticket 021-005; ``SetPlayfield``/
``GetPlayfield``/``ListPlayfields``/``SetCameraConfig``/
``GetCameraConfig`` are ticket 022-001; ``Calibrate``/``GetCalibration``
are ticket 022-003; ``ResetCameraConfig`` is ticket 023-004;
``PutAnnotation``/``RemoveAnnotation``/``ReplaceLayer``/
``ClearAnnotations``/``GetAnnotations`` and the ``GetFrame`` annotation
embed are ticket 024-001; ``FindFeature`` is ticket 025-002;
``RegisterTag``/``UnregisterTag``/``ListTagParameters`` are ticket
025-004; ``GetGuide`` is ticket 025-007; ``CaptureFlatField``/
``GetFlatField``/``ClearFlatField`` are ticket 037-003, this sprint's
latest).

Translates between ``proto/aprilcam_v2.proto``'s service (sprint 020's
four RPCs -- ``ListCameras``, ``ProbeCameras``, ``GetFrame``,
``GetStatus`` -- plus sprint 021's ``GetTags``/``GetTag``/
``GetTagHistory`` and the ``GetFrame`` tag embed, plus sprint 022's
playfield/camera-config RPCs and ``Calibrate``/``GetCalibration``, plus
sprint 023's ``ResetCameraConfig``, plus sprint 024's annotation-store
RPCs and the ``GetFrame`` annotation embed above, plus sprint 025's
``FindFeature``, plus sprint 037's ``CaptureFlatField``/``GetFlatField``/
``ClearFlatField`` and the ``CameraState`` flat-field flags) and
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

**``GetImageStream``/``GetTagStream`` (ticket 027-005).** Unlike every
other RPC above, these two never call into ``daemon.service`` directly
-- they delegate endpoint allocation to
``daemon.stream_transport.StreamTransportManager`` (built by
:func:`build_stream_transport` below, the one place this module hands
that transport-only module the two proto-serializing callables
(``_image_frame_bytes_for_stream``/``_tag_frame_bytes_for_stream``) it
needs without ever letting it import ``aprilcam.proto`` itself -- see
``daemon/stream_transport.py``'s own module docstring for why that
boundary matters). Marshalling still happens here (``StreamEndpoint``
-> ``aprilcam_v2_pb2.StreamEndpoint``); the socket accept loop and
per-connection sender threads that endpoint points at are entirely
``daemon/stream_transport.py``'s concern, never this module's.
"""

from __future__ import annotations

import json
import logging
from concurrent import futures
from pathlib import Path
from typing import Iterable, Optional

import grpc

from aprilcam.calibration.calibration import Calibration
from aprilcam.calibration.interfaces import CalibrationResult, FlatFieldInfo, FlatFieldResult
from aprilcam.intrinsics.interfaces import (
    IntrinsicsError,
    IntrinsicsFinishResult,
    IntrinsicsSessionActive,
    IntrinsicsStatus,
    NoIntrinsicsSession,
)
from aprilcam.config.interfaces import CalibrationTarget, CameraConfig, CameraPosition, Feature
from aprilcam.config.loader import PlayfieldDefinition, PlayfieldFeatureEntry
from aprilcam.daemon.interfaces import Daemon, DaemonStatus
from aprilcam.daemon.lifecycle import ControlPlaneListener
from aprilcam.daemon.stream_transport import StreamEndpoint, StreamTransportManager
from aprilcam.errors import (
    AlreadyRunning,
    AnnotationLimitExceeded,
    AprilCamError,
    CameraInUse,
    CameraNotPresent,
    NoFieldMask,
    NoPlayfieldLinked,
    NotCalibrated,
    UnknownCamera,
    UnknownGuide,
    UnknownPlayfield,
    UnsupportedSetting,
)
from aprilcam.playfield.interfaces import Playfield
from aprilcam.proto import aprilcam_v2_pb2, aprilcam_v2_pb2_grpc
from aprilcam.types import (
    Annotation,
    AnnotationSet,
    AnnotationShape,
    CameraState,
    Circle,
    ImageFrame,
    Marker,
    MountParameters,
    PathShape,
    PixelPoint,
    Polygon,
    ProbeReport,
    Style,
    TagFamily,
    TagFrame,
    TagId,
    TagRecord,
    Text,
    WorldPoint,
)

__all__ = ["build_server", "build_stream_transport", "PROTOCOL_VERSION"]

logger = logging.getLogger(__name__)

#: This codebase's wire protocol version (ticket 031-001) -- echoed on
#: every ``GetStatusResponse`` so a v2 client can positively confirm it
#: reached a compatible daemon on the first RPC any fresh connection
#: makes (``GetStatus``, via ``client/lifecycle.py``'s own ``status()``).
#: Deliberately independent of ``aprilcam.__version__``: the package
#: version bumps once per sprint close regardless of whether the wire
#: protocol changed at all, so tying this to it would make an unrelated
#: release look like a protocol change. Bump only when
#: ``aprilcam_v2.proto``'s existing fields are renumbered or removed --
#: purely additive growth (the only kind this file's evolution rule
#: allows, per its own module docstring) never requires a bump, since an
#: older client already reads an additive field's absence correctly.
PROTOCOL_VERSION = 1

#: exception type -> gRPC status code. Anything not listed (including
#: an ``AprilCamError`` subtype added by a later sprint before this map
#: is updated) falls back to ``INTERNAL`` in :func:`_abort` -- a
#: conservative default that never claims a more specific code than is
#: actually known to be correct.
_STATUS_CODES = {
    AlreadyRunning: grpc.StatusCode.ALREADY_EXISTS,
    UnknownCamera: grpc.StatusCode.NOT_FOUND,
    UnknownPlayfield: grpc.StatusCode.NOT_FOUND,
    # An unknown guide name (UC-27.S-3, ticket 025-007) -- same
    # "requested a name that does not exist" semantics as
    # UnknownCamera/UnknownPlayfield above, both also UnknownTarget
    # subtypes.
    UnknownGuide: grpc.StatusCode.NOT_FOUND,
    NotCalibrated: grpc.StatusCode.FAILED_PRECONDITION,
    # Same "the operation cannot succeed given current state" semantics
    # as NotCalibrated -- the camera is known, just not present right
    # now (ticket 021-002).
    CameraNotPresent: grpc.StatusCode.FAILED_PRECONDITION,
    # close_camera refused while the camera is in use (sprint 047 ticket
    # 007) -- state doesn't allow it right now; retry with force.
    CameraInUse: grpc.StatusCode.FAILED_PRECONDITION,
    # A calibrate request for a camera with no playfield link -- a
    # precondition failure, not a calibration failure (UC-15.S-2). Same
    # code family as NotCalibrated/CameraNotPresent: "cannot succeed
    # given current state," distinct from the explicit-but-failed
    # CalibrationResult path, which is not an error at all.
    NoPlayfieldLinked: grpc.StatusCode.FAILED_PRECONDITION,
    # A capture_flat_field request for a camera with neither a stored
    # calibration nor a live-detectable playfield corner set (sprint
    # 037 ticket 003) -- same "cannot succeed given current state"
    # family as NoPlayfieldLinked, raised before any capture burst is
    # taken.
    NoFieldMask: grpc.StatusCode.FAILED_PRECONDITION,
    # Intrinsics sessions (sprint 044 ticket 006). Order matters: the
    # specific subtypes precede the IntrinsicsError base.
    IntrinsicsSessionActive: grpc.StatusCode.ALREADY_EXISTS,
    NoIntrinsicsSession: grpc.StatusCode.NOT_FOUND,
    IntrinsicsError: grpc.StatusCode.FAILED_PRECONDITION,
    # A put_annotation/replace_layer request exceeding the fixed
    # per-layer count or per-shape point bound (ticket 031-010) --
    # RESOURCE_EXHAUSTED matches gRPC's own "quota exceeded" status
    # convention, distinct from INVALID_ARGUMENT (the request is not
    # malformed, it is simply larger than this daemon's fixed capacity).
    AnnotationLimitExceeded: grpc.StatusCode.RESOURCE_EXHAUSTED,
    # A set_camera_config push (or an apply-on-open of a stored/on-disk
    # document) naming a key the camera's backend(s) do not support, or
    # a value the hardware rejected (out of range, still gated by an
    # auto-mode control, or -- the 2026-08-22 hotfix's own trigger -- a
    # non-numeric settings value) -- INVALID_ARGUMENT matches gRPC's own
    # "the request itself is malformed/unacceptable" convention, distinct
    # from FAILED_PRECONDITION's "state doesn't allow it right now" (the
    # request would still be wrong on a retry with no state change).
    # Previously unmapped (fell through to the generic INTERNAL default
    # below with no metadata a client could reconstruct from -- every
    # UnsupportedSetting crossed the RPC boundary as an opaque
    # ConnectionFailed("did not answer: ...") until this entry existed).
    UnsupportedSetting: grpc.StatusCode.INVALID_ARGUMENT,
}


def _camera_state_to_proto(state: CameraState) -> aprilcam_v2_pb2.CameraState:
    msg = aprilcam_v2_pb2.CameraState(
        name=state.name,
        present=state.present,
        usable=state.usable,
        calibrated=state.calibrated,
        calibration_stale=state.calibration_stale,
        frame_dark=state.frame_dark,
        # Sprint 037 ticket 003: plain bool, not HasField-guarded --
        # same as calibrated/calibration_stale above.
        flatfield_present=state.flatfield_present,
        flatfield_stale=state.flatfield_stale,
        lens_model=state.lens_model,
        lens_pending=state.lens_pending,
        calibration_method=state.calibration_method,
        calibration_confidence=state.calibration_confidence,
        closed=state.closed,
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
        placeholder=frame.placeholder,
    )
    # Presence, not an empty-vs-absent default: only set `tags` when
    # detection has actually run (frame.tags is not None). Leaving it
    # unset keeps HasField("tags") false, matching ImageFrame.tags's
    # three-way "absent means no detection has run" contract -- a
    # present-but-empty TagFrame (detection ran, saw nothing) is a
    # different, also-representable answer (ticket 021-005).
    if frame.tags is not None:
        msg.tags.CopyFrom(_tag_frame_to_proto(frame.tags))
    # Ticket 022-005: present only for a deskewed frame, matching
    # ImageFrame's own "present only for deskewed frames" contract --
    # never set for a raw frame, never coerced to 0.0/absent-looking
    # defaults.
    if frame.px_per_cm is not None:
        msg.px_per_cm = frame.px_per_cm
    if frame.world_extent is not None:
        width_cm, height_cm = frame.world_extent
        msg.world_extent.CopyFrom(
            aprilcam_v2_pb2.WorldExtent(width_cm=width_cm, height_cm=height_cm)
        )
    # Ticket 024-001: present only on a deskewed frame, and -- unlike
    # `tags` above -- *always* set on that branch, even for an empty
    # AnnotationSet (frame.annotations is a real, non-None object
    # whenever DaemonService._get_deskewed_frame built this frame; see
    # that method's own docstring). Never set for a raw frame
    # (frame.annotations stays None by contract, UC-16).
    if frame.annotations is not None:
        msg.annotations.CopyFrom(_annotation_set_to_proto(frame.annotations))
    return msg


def _status_to_proto(status: DaemonStatus) -> aprilcam_v2_pb2.GetStatusResponse:
    return aprilcam_v2_pb2.GetStatusResponse(
        running=status.running,
        cameras=_camera_states_to_proto(status.cameras),
        config_dir=status.config_dir,
        state_dir=status.state_dir,
        # Ticket 031-001: always set, not `DaemonStatus`-derived -- this
        # codebase's protocol version is a constant of the running code,
        # not a property of any particular daemon's runtime state.
        protocol_version=PROTOCOL_VERSION,
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


def _world_point_from_proto(msg: aprilcam_v2_pb2.WorldPoint) -> WorldPoint:
    """Proto -> Python. Needed server-side only for the annotation RPCs
    (ticket 024-001): a ``PutAnnotation``/``ReplaceLayer`` request's
    shape geometry has to become ``types.WorldPoint`` before it can be
    passed to ``DaemonService`` -- the same "reading this module's own
    incoming requests" exception :func:`_tag_id_from_proto` and
    :func:`_playfield_definition_from_proto` already document."""
    return WorldPoint(x=msg.x, y=msg.y)


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
    if record.image_yaw_deg is not None:
        msg.image_yaw_deg = record.image_yaw_deg
    return msg


def _tag_frame_to_proto(frame: TagFrame) -> aprilcam_v2_pb2.TagFrame:
    return aprilcam_v2_pb2.TagFrame(
        tags=[_tag_record_to_proto(r) for r in frame.tags],
        timestamp=frame.timestamp,
        frame_index=frame.frame_index,
        calibration_stale=frame.calibration_stale,
        camera_absent=frame.camera_absent,
    )


def _image_frame_bytes_for_stream(frame: ImageFrame) -> bytes:
    """The exact bytes ``daemon/stream_transport.py``'s image-stream
    sender thread writes per published tick (ticket 027-005). Reuses
    :func:`_image_frame_to_proto` -- the identical ``GetFrameResponse``-
    shaped message ``GetFrame``'s one-shot path already produces
    (``design.md`` §5: "one deskewed frame carries everything needed to
    render a fully annotated view") -- rather than a second, separately-
    maintained image-wire-message conversion. A stream tick and a
    one-shot ``GetFrame`` response for the same ``ImageFrame`` are
    therefore byte-for-byte identical on the wire.
    """
    return _image_frame_to_proto(frame).SerializeToString()


def _tag_frame_bytes_for_stream(frame: TagFrame) -> bytes:
    """Same idea as :func:`_image_frame_bytes_for_stream`, for tag
    streams: reuses :func:`_tag_frame_to_proto`, the identical
    ``TagFrame`` conversion ``GetTagsResponse.tags`` already uses."""
    return _tag_frame_to_proto(frame).SerializeToString()


def _stream_endpoint_to_proto(endpoint: StreamEndpoint) -> aprilcam_v2_pb2.StreamEndpoint:
    """``daemon.stream_transport.StreamEndpoint`` -> the wire message of
    the same name (``proto/aprilcam_v2.proto``). Presence discipline
    matches every other optional field in this module: a ``None`` half
    stays unset (``HasField(...) is False``), never coerced to an
    empty string / zero port that could be mistaken for a real value.
    """
    msg = aprilcam_v2_pb2.StreamEndpoint()
    if endpoint.socket_path is not None:
        msg.socket_path = endpoint.socket_path
    if endpoint.tcp_port is not None:
        msg.tcp_port = endpoint.tcp_port
    return msg


def _playfield_feature_entry_to_proto(
    entry: PlayfieldFeatureEntry,
) -> aprilcam_v2_pb2.PlayfieldFeatureEntry:
    msg = aprilcam_v2_pb2.PlayfieldFeatureEntry(
        slug=entry.slug, x=entry.x, y=entry.y, height_cm=entry.height_cm
    )
    if entry.tag_id is not None:
        msg.id = entry.tag_id
    if entry.size_mm is not None:
        msg.size_mm = entry.size_mm
    if entry.policy is not None:
        msg.policy = entry.policy
    if entry.colour is not None:
        msg.colour = entry.colour
    return msg


def _playfield_feature_entry_from_proto(
    msg: aprilcam_v2_pb2.PlayfieldFeatureEntry,
) -> PlayfieldFeatureEntry:
    return PlayfieldFeatureEntry(
        slug=msg.slug,
        x=msg.x,
        y=msg.y,
        tag_id=msg.id if msg.HasField("id") else None,
        colour=msg.colour if msg.HasField("colour") else None,
        height_cm=msg.height_cm,
        size_mm=msg.size_mm if msg.HasField("size_mm") else None,
        policy=msg.policy if msg.HasField("policy") else None,
    )


def _playfield_definition_to_proto(definition: PlayfieldDefinition) -> aprilcam_v2_pb2.PlayfieldDefinition:
    """Python -> proto, for a ``GetPlayfield``/``ListPlayfields``
    response. Mirrors ``config.loader.PlayfieldDefinition``'s stored
    per-section entries directly (see this module's proto-boundary
    docstring and ``aprilcam_v2.proto``'s header comment for why this
    is not built from the more abstract ``features()``/
    ``required_aruco_ids()`` view)."""
    msg = aprilcam_v2_pb2.PlayfieldDefinition(
        name=definition.name,
        width_cm=definition.width_cm,
        height_cm=definition.height_cm,
        april_tags=[_playfield_feature_entry_to_proto(e) for e in definition.april_tags],
        aruco_tags=[_playfield_feature_entry_to_proto(e) for e in definition.aruco_tags],
        rectangles=[_playfield_feature_entry_to_proto(e) for e in definition.rectangles],
        dots=[_playfield_feature_entry_to_proto(e) for e in definition.dots],
    )
    if definition.marker_size_mm_default is not None:
        msg.marker_size_mm = definition.marker_size_mm_default
    if definition.calibration_target is not None:
        t = definition.calibration_target
        msg.calibration_target.CopyFrom(
            aprilcam_v2_pb2.CalibrationTarget(
                kind=t.kind, board=t.board, centre_x_cm=t.centre_x_cm,
                centre_y_cm=t.centre_y_cm, rotation_deg=t.rotation_deg,
            )
        )
    return msg


def _calibration_target_from_proto(msg: aprilcam_v2_pb2.PlayfieldDefinition):
    if not msg.HasField("calibration_target"):
        return None
    t = msg.calibration_target
    return CalibrationTarget(
        kind=t.kind, board=t.board, centre_x_cm=t.centre_x_cm,
        centre_y_cm=t.centre_y_cm, rotation_deg=t.rotation_deg,
    )


def _playfield_definition_from_proto(msg: aprilcam_v2_pb2.PlayfieldDefinition) -> PlayfieldDefinition:
    """Proto -> Python, for an incoming ``SetPlayfieldRequest``. The
    module docstring's "reading this module's own incoming requests"
    exception applies here exactly as it already does for
    :func:`_tag_id_from_proto`."""
    return PlayfieldDefinition(
        name=msg.name,
        width_cm=msg.width_cm,
        height_cm=msg.height_cm,
        april_tags=tuple(_playfield_feature_entry_from_proto(e) for e in msg.april_tags),
        aruco_tags=tuple(_playfield_feature_entry_from_proto(e) for e in msg.aruco_tags),
        rectangles=tuple(_playfield_feature_entry_from_proto(e) for e in msg.rectangles),
        dots=tuple(_playfield_feature_entry_from_proto(e) for e in msg.dots),
        marker_size_mm_default=msg.marker_size_mm if msg.HasField("marker_size_mm") else None,
        calibration_target=_calibration_target_from_proto(msg),
    )


def _playfield_entry_to_proto(playfield: Playfield) -> aprilcam_v2_pb2.PlayfieldEntry:
    definition = playfield.definition
    if not isinstance(definition, PlayfieldDefinition):
        # Every definition DaemonService ever holds was built either by
        # ConfigLoader.load_playfield or by _playfield_definition_from_proto
        # below -- both always produce this concrete type. Not reachable
        # through normal use; guards against a future Daemon
        # implementation storing some other PlayfieldDefinition subclass
        # this module would not know how to serialize.
        raise TypeError(
            f"cannot serialize playfield {playfield.name!r}: definition is "
            f"{type(definition).__name__}, not aprilcam.config.loader.PlayfieldDefinition"
        )
    return aprilcam_v2_pb2.PlayfieldEntry(
        name=playfield.name,
        definition=_playfield_definition_to_proto(definition),
        linked_cameras=list(playfield.linked_cameras()),
    )


def _feature_match_to_proto(feature: Feature) -> aprilcam_v2_pb2.FeatureMatch:
    """Python -> proto, for a ``FindFeature`` response (ticket 025-002).
    Mirrors ``config.interfaces.Feature`` field-for-field -- not
    :func:`_playfield_feature_entry_to_proto`'s ``PlayfieldFeatureEntry``
    shape, which carries an optional numeric ``id`` and no ``kind``
    (this file's header comment)."""
    msg = aprilcam_v2_pb2.FeatureMatch(
        slug=feature.slug,
        kind=feature.kind,
        position=_world_point_to_proto(feature.position),
        height_cm=feature.height_cm,
    )
    if feature.colour is not None:
        msg.colour = feature.colour
    return msg


def _mount_parameters_to_proto(params: MountParameters) -> aprilcam_v2_pb2.MountParameters:
    """Python -> proto, for a ``ListTagParameters`` response (ticket
    025-004). Mirrors ``types.MountParameters`` field-for-field --
    ``mount_x``/``mount_y``/``mount_z``/``mount_yaw_rad`` are never
    absent (the dataclass itself defaults each to ``0.0``, never
    ``None``), only ``size_cm`` needs the presence check."""
    msg = aprilcam_v2_pb2.MountParameters(
        mount_x=params.mount_x,
        mount_y=params.mount_y,
        mount_z=params.mount_z,
        mount_yaw_rad=params.mount_yaw_rad,
    )
    if params.size_cm is not None:
        msg.size_cm = params.size_cm
    return msg


def _mount_parameters_from_proto(msg: aprilcam_v2_pb2.MountParameters) -> MountParameters:
    """Proto -> Python, for an incoming ``RegisterTagRequest``. The
    module docstring's "reading this module's own incoming requests"
    exception applies here exactly as it already does for
    :func:`_tag_id_from_proto`."""
    return MountParameters(
        size_cm=msg.size_cm if msg.HasField("size_cm") else None,
        mount_x=msg.mount_x,
        mount_y=msg.mount_y,
        mount_z=msg.mount_z,
        mount_yaw_rad=msg.mount_yaw_rad,
    )


def _camera_position_to_proto(position: CameraPosition) -> aprilcam_v2_pb2.CameraPosition:
    """Python -> proto, for a ``GetCameraConfig`` response's optional
    ``camera_position`` (sprint 029 ticket 001)."""
    return aprilcam_v2_pb2.CameraPosition(
        nadir=_world_point_to_proto(position.nadir), height_cm=position.height_cm
    )


def _camera_position_from_proto(msg: aprilcam_v2_pb2.CameraPosition) -> CameraPosition:
    """Proto -> Python, for an incoming ``SetCameraConfigRequest``'s
    optional ``camera_position``. The module docstring's "reading this
    module's own incoming requests" exception applies here exactly as it
    already does for :func:`_tag_id_from_proto`."""
    return CameraPosition(nadir=_world_point_from_proto(msg.nadir), height_cm=msg.height_cm)


def _camera_config_to_proto(config: CameraConfig) -> aprilcam_v2_pb2.CameraConfig:
    """Python -> proto, for a ``GetCameraConfig`` response. ``settings``
    crosses as JSON text -- see ``aprilcam_v2.proto``'s header comment.
    ``camera_position`` is ``HasField``-guarded (sprint 029 ticket 001):
    absent means "not configured," never a fabricated zero-valued
    ``CameraPosition``, mirroring ``GetCalibrationResponse.calibration``'s
    presence discipline."""
    msg = aprilcam_v2_pb2.CameraConfig(name=config.name, settings_json=json.dumps(config.settings))
    if config.playfield is not None:
        msg.playfield = config.playfield
    if config.camera_position is not None:
        msg.camera_position.CopyFrom(_camera_position_to_proto(config.camera_position))
    return msg


def _camera_config_from_proto(msg: aprilcam_v2_pb2.CameraConfig) -> CameraConfig:
    """Proto -> Python, for an incoming ``SetCameraConfigRequest``."""
    settings = json.loads(msg.settings_json) if msg.settings_json else {}
    return CameraConfig(
        name=msg.name,
        playfield=msg.playfield if msg.HasField("playfield") else None,
        settings=settings,
        camera_position=(
            _camera_position_from_proto(msg.camera_position)
            if msg.HasField("camera_position")
            else None
        ),
    )


#: Sprint 045: optional scalar pose-provenance fields, by shared name.
_RESULT_POSE_OPTIONALS = (
    "method", "method_reason", "confidence", "corner_count",
    "reprojection_error_px", "scale_agreement", "angular_spread_deg",
    "camera_height_cm", "camera_tilt_deg", "leave_one_out_worst_id",
    "leave_one_out_worst_cm", "layout_extent_ratio",
)
_CALIBRATION_POSE_OPTIONALS = (
    "method", "confidence", "camera_height_cm", "camera_nadir_x_cm",
    "camera_nadir_y_cm", "camera_tilt_deg", "reprojection_error_px",
    "corner_count", "scale_agreement",
)


def _calibration_result_to_proto(result: CalibrationResult) -> aprilcam_v2_pb2.CalibrationResult:
    """Python -> proto, for a ``Calibrate`` response. ``missing_ids``
    always carries the exact ids (never a count, UC-04.S-2). Optional
    fields are ``HasField``-guarded: ``None`` means "the concept did not
    apply this call," never a fabricated ``False``/empty-string/``0``."""
    msg = aprilcam_v2_pb2.CalibrationResult(
        succeeded=result.succeeded,
        matched_ids=list(result.matched_ids),
        missing_ids=list(result.missing_ids),
    )
    if result.reprojection_error_cm is not None:
        msg.reprojection_error_cm = result.reprojection_error_cm
    if result.reason is not None:
        msg.reason = result.reason
    if result.lens_model_source is not None:
        msg.lens_model_source = result.lens_model_source
    if result.lens_warning is not None:
        msg.lens_warning = result.lens_warning
    # Sprint 045 pose provenance (HasField-guarded; None stays absent).
    for name in _RESULT_POSE_OPTIONALS:
        value = getattr(result, name)
        if value is not None:
            setattr(msg, name, value)
    if result.camera_nadir is not None:
        msg.camera_nadir.CopyFrom(_world_point_to_proto(result.camera_nadir))
    msg.notes.extend(result.notes)
    msg.helpful_missing_ids.extend(result.helpful_missing_ids)
    msg.leave_one_out_ids.extend(i for i, _ in result.leave_one_out_cm)
    msg.leave_one_out_cm.extend(cm for _, cm in result.leave_one_out_cm)
    return msg


def _calibration_to_proto(calibration: Calibration) -> aprilcam_v2_pb2.Calibration:
    """Python -> proto, for a ``GetCalibration`` response. ``homography``
    flattens the 3x3 matrix into 9 row-major ``double``s -- see
    ``aprilcam_v2.proto``'s ``Calibration`` message comment.
    ``camera_matrix``/``dist_coeffs`` (sprint 030 ticket 002) are
    ``None``-guarded the same way -- an empty ``repeated double`` is
    the unambiguous "no lens model" encoding (a real 3x3 matrix is
    never empty), so nothing is set at all when either is ``None``."""
    msg = aprilcam_v2_pb2.Calibration(
        stale=calibration.stale,
        calibrated_at=calibration.calibrated_at,
        playfield=calibration.playfield,
        homography=[v for row in calibration.homography for v in row],
        matched_ids=list(calibration.matched_ids),
    )
    if calibration.reprojection_error_cm is not None:
        msg.reprojection_error_cm = calibration.reprojection_error_cm
    if calibration.camera_matrix is not None:
        msg.camera_matrix.extend(v for row in calibration.camera_matrix for v in row)
    if calibration.dist_coeffs is not None:
        msg.dist_coeffs.extend(calibration.dist_coeffs)
    # Sprint 044 ticket 010: provenance (HasField-guarded; None stays absent).
    if calibration.lens_source is not None:
        msg.lens_source = calibration.lens_source
    if calibration.lens_views is not None:
        msg.lens_views = calibration.lens_views
    if calibration.lens_rms_px is not None:
        msg.lens_rms_px = calibration.lens_rms_px
    if calibration.lens_residual_px is not None:
        msg.lens_residual_px = calibration.lens_residual_px
    if calibration.lens_installed_at is not None:
        msg.lens_installed_at = calibration.lens_installed_at
    for name in _CALIBRATION_POSE_OPTIONALS:
        value = getattr(calibration, name)
        if value is not None:
            setattr(msg, name, value)
    return msg


def _flatfield_result_to_proto(result: FlatFieldResult) -> aprilcam_v2_pb2.FlatFieldResult:
    """Python -> proto, for a ``CaptureFlatField`` response. ``gain`` is
    deliberately never sent -- see ``calibration/interfaces.py``'s
    ``FlatFieldResult`` docstring and ``aprilcam_v2.proto``'s header
    comment: mirrors ``Calibration``'s "report what happened, never the
    matrix" precedent."""
    msg = aprilcam_v2_pb2.FlatFieldResult(
        succeeded=result.succeeded,
        spread_before=list(result.spread_before),
        spread_after=list(result.spread_after),
        forced_checks=list(result.forced_checks),
    )
    if result.reason is not None:
        msg.reason = result.reason
    return msg


_CELL_ORDER = ("NW", "N", "NE", "W", "C", "E", "SW", "S", "SE")
_TILT_ORDER = ("N", "E", "S", "W")


def _intrinsics_status_to_proto(status: IntrinsicsStatus) -> aprilcam_v2_pb2.IntrinsicsStatus:
    """Python -> proto for every intrinsics RPC. ``None`` optionals stay
    unset; the coverage dicts flatten to fixed-order repeated fields
    (empty dict <-> empty list)."""
    msg = aprilcam_v2_pb2.IntrinsicsStatus(
        camera=status.camera,
        state=status.state,
        layout=getattr(status.layout, "value", str(status.layout)),
        board_visible=status.board_visible,
        view_count=status.view_count,
        tilted_count=status.tilted_count,
        good=status.good,
        banner=status.banner,
        errors=list(status.errors),
        started_at=status.started_at,
    )
    if status.image_size is not None:
        msg.image_size.extend(status.image_size)
    if status.cell_coverage:
        msg.cell_coverage.extend(status.cell_coverage.get(k, 0) for k in _CELL_ORDER)
    if status.tilt_coverage:
        msg.tilt_coverage.extend(status.tilt_coverage.get(k, 0) for k in _TILT_ORDER)
    for poly in status.board_outline or ():
        msg.board_outline.add().xy.extend(c for pt in poly for c in pt)
    for name in ("board_spec", "rms_px", "residual_px", "correction_px", "fx",
                 "hfov_deg", "next_hint", "last_reject", "solved_at", "good_now"):
        value = getattr(status, name)
        if value is not None:
            setattr(msg, name, value)
    return msg


def _intrinsics_finish_to_proto(
    result: IntrinsicsFinishResult,
) -> aprilcam_v2_pb2.FinishIntrinsicsResponse:
    msg = aprilcam_v2_pb2.FinishIntrinsicsResponse(
        installed=result.installed, status=_intrinsics_status_to_proto(result.status)
    )
    if result.reason is not None:
        msg.reason = result.reason
    if result.lens is not None:
        msg.lens_json = json.dumps(result.lens)
    return msg


def _flatfield_info_to_proto(info: FlatFieldInfo) -> aprilcam_v2_pb2.FlatFieldInfo:
    """Python -> proto, for a ``GetFlatField`` response. ``stale`` is
    plain (not ``HasField``-guarded), mirroring ``Calibration.stale``;
    ``captured_at``/``shape``/``gain_min``/``gain_max`` are ``None``-
    guarded the same way ``Calibration.reprojection_error_cm`` is --
    absent means "not known," never a fabricated zero."""
    msg = aprilcam_v2_pb2.FlatFieldInfo(present=info.present, stale=info.stale)
    if info.captured_at is not None:
        msg.captured_at = info.captured_at
    if info.shape is not None:
        msg.shape.extend(info.shape)
    if info.gain_min is not None:
        msg.gain_min = info.gain_min
    if info.gain_max is not None:
        msg.gain_max = info.gain_max
    return msg


#: Annotations (ticket 024-001, UC-28, specification.md section 9
#: decision 8). Every converter below mirrors the exact
#: ``types.py``/``aprilcam_v2.proto`` field-for-field shape -- see this
#: module's docstring and ``aprilcam_v2.proto``'s Annotations header
#: comment for the design notes. ``*_from_proto`` exists for these
#: (unlike most of this module's converters) because ``PutAnnotation``/
#: ``ReplaceLayer`` *receive* shapes on the wire -- the same "reading
#: this module's own incoming requests" exception :func:`_tag_id_from_proto`
#: already documents, just extended to a five-way ``oneof`` instead of a
#: single message.


def _marker_to_proto(marker: Marker) -> aprilcam_v2_pb2.Marker:
    msg = aprilcam_v2_pb2.Marker(at=_world_point_to_proto(marker.at))
    if marker.label is not None:
        msg.label = marker.label
    return msg


def _marker_from_proto(msg: aprilcam_v2_pb2.Marker) -> Marker:
    return Marker(at=_world_point_from_proto(msg.at), label=msg.label if msg.HasField("label") else None)


def _path_shape_to_proto(path: PathShape) -> aprilcam_v2_pb2.PathShape:
    return aprilcam_v2_pb2.PathShape(points=[_world_point_to_proto(p) for p in path.points])


def _path_shape_from_proto(msg: aprilcam_v2_pb2.PathShape) -> PathShape:
    return PathShape(points=tuple(_world_point_from_proto(p) for p in msg.points))


def _polygon_to_proto(polygon: Polygon) -> aprilcam_v2_pb2.Polygon:
    return aprilcam_v2_pb2.Polygon(points=[_world_point_to_proto(p) for p in polygon.points])


def _polygon_from_proto(msg: aprilcam_v2_pb2.Polygon) -> Polygon:
    return Polygon(points=tuple(_world_point_from_proto(p) for p in msg.points))


def _circle_to_proto(circle: Circle) -> aprilcam_v2_pb2.Circle:
    return aprilcam_v2_pb2.Circle(center=_world_point_to_proto(circle.center), radius_cm=circle.radius_cm)


def _circle_from_proto(msg: aprilcam_v2_pb2.Circle) -> Circle:
    return Circle(center=_world_point_from_proto(msg.center), radius_cm=msg.radius_cm)


def _text_to_proto(text: Text) -> aprilcam_v2_pb2.Text:
    return aprilcam_v2_pb2.Text(at=_world_point_to_proto(text.at), text=text.text)


def _text_from_proto(msg: aprilcam_v2_pb2.Text) -> Text:
    return Text(at=_world_point_from_proto(msg.at), text=msg.text)


def _style_to_proto(style: Style) -> aprilcam_v2_pb2.Style:
    msg = aprilcam_v2_pb2.Style()
    if style.color is not None:
        msg.color = style.color
    if style.line_width is not None:
        msg.line_width = style.line_width
    return msg


def _style_from_proto(msg: aprilcam_v2_pb2.Style) -> Style:
    return Style(
        color=msg.color if msg.HasField("color") else None,
        line_width=msg.line_width if msg.HasField("line_width") else None,
    )


def _annotation_to_proto(annotation: Annotation) -> aprilcam_v2_pb2.Annotation:
    """Python -> proto. Exactly one ``oneof shape`` field is set,
    matching ``types.AnnotationShape``'s ``Union[Marker, PathShape,
    Polygon, Circle, Text]`` -- a shape of a type not in that union
    cannot construct a valid ``Annotation`` in the first place, so the
    ``else`` branch below is unreachable through normal use; it exists
    to fail loudly rather than silently serialize an empty ``oneof`` if
    a future shape is ever added to the union without a matching wire
    case here."""
    msg = aprilcam_v2_pb2.Annotation(
        id=annotation.id, layer=annotation.layer, style=_style_to_proto(annotation.style)
    )
    shape = annotation.shape
    if isinstance(shape, Marker):
        msg.marker.CopyFrom(_marker_to_proto(shape))
    elif isinstance(shape, PathShape):
        msg.path.CopyFrom(_path_shape_to_proto(shape))
    elif isinstance(shape, Polygon):
        msg.polygon.CopyFrom(_polygon_to_proto(shape))
    elif isinstance(shape, Circle):
        msg.circle.CopyFrom(_circle_to_proto(shape))
    elif isinstance(shape, Text):
        msg.text.CopyFrom(_text_to_proto(shape))
    else:
        raise TypeError(
            f"cannot serialize annotation {annotation.id!r}: unknown shape type "
            f"{type(shape).__name__}"
        )
    if annotation.ttl_seconds is not None:
        msg.ttl_seconds = annotation.ttl_seconds
    return msg


def _annotation_from_proto(msg: aprilcam_v2_pb2.Annotation) -> Annotation:
    which = msg.WhichOneof("shape")
    shape: AnnotationShape
    if which == "marker":
        shape = _marker_from_proto(msg.marker)
    elif which == "path":
        shape = _path_shape_from_proto(msg.path)
    elif which == "polygon":
        shape = _polygon_from_proto(msg.polygon)
    elif which == "circle":
        shape = _circle_from_proto(msg.circle)
    elif which == "text":
        shape = _text_from_proto(msg.text)
    else:
        raise ValueError(f"annotation {msg.id!r} has no shape set")
    return Annotation(
        id=msg.id,
        layer=msg.layer,
        shape=shape,
        style=_style_from_proto(msg.style),
        ttl_seconds=msg.ttl_seconds if msg.HasField("ttl_seconds") else None,
    )


def _annotation_set_to_proto(annotation_set: AnnotationSet) -> aprilcam_v2_pb2.AnnotationSet:
    return aprilcam_v2_pb2.AnnotationSet(
        annotations=[_annotation_to_proto(a) for a in annotation_set.annotations],
        revision=annotation_set.revision,
        timestamp=annotation_set.timestamp,
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
    * :class:`~aprilcam.errors.UnknownPlayfield`: same two keys as
      :class:`UnknownCamera` -- both derive from ``UnknownTarget`` and
      share its ``requested``/``available`` shape.
    * :class:`~aprilcam.errors.UnknownGuide`: same two keys again --
      also an ``UnknownTarget`` subtype (UC-27.S-3, ticket 025-007).
    * :class:`CameraNotPresent`: + ``aprilcam-slug``.
    * :class:`~aprilcam.errors.NoPlayfieldLinked`: + ``aprilcam-camera``.
    * :class:`~aprilcam.errors.NoFieldMask` (sprint 037 ticket 003): +
      ``aprilcam-camera`` -- same single field, same key, as
      :class:`~aprilcam.errors.NoPlayfieldLinked` above.
    * :class:`~aprilcam.errors.AnnotationLimitExceeded` (ticket
      031-010): + ``aprilcam-kind``, ``aprilcam-bound``,
      ``aprilcam-actual``.
    * :class:`~aprilcam.errors.UnsupportedSetting` (2026-08-22 hotfix):
      + ``aprilcam-key``, and ``aprilcam-reason`` when ``.reason`` is
      set -- previously unmapped entirely (fell through to the generic
      ``AprilCamError`` case below with no extra metadata, so the
      client's ``_reconstruct_error`` had nothing to dispatch on and
      reported every ``UnsupportedSetting`` as an opaque
      ``ConnectionFailed``).
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
        elif isinstance(exc, (UnknownCamera, UnknownPlayfield, UnknownGuide)):
            metadata.append(("aprilcam-requested", exc.requested))
            metadata.append(("aprilcam-available", ",".join(exc.available)))
        elif isinstance(exc, (CameraNotPresent, CameraInUse)):
            metadata.append(("aprilcam-slug", exc.slug))
        elif isinstance(exc, NoPlayfieldLinked):
            metadata.append(("aprilcam-camera", exc.camera))
        elif isinstance(exc, NoFieldMask):
            metadata.append(("aprilcam-camera", exc.camera))
        elif isinstance(exc, (IntrinsicsSessionActive, NoIntrinsicsSession)):
            metadata.append(("aprilcam-camera", exc.camera))
        elif isinstance(exc, AnnotationLimitExceeded):
            metadata.append(("aprilcam-kind", exc.kind))
            metadata.append(("aprilcam-bound", str(exc.bound)))
            metadata.append(("aprilcam-actual", str(exc.actual)))
        elif isinstance(exc, UnsupportedSetting):
            metadata.append(("aprilcam-key", exc.key))
            if exc.reason:
                metadata.append(("aprilcam-reason", exc.reason))
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

    def __init__(self, service: Daemon, transport: Optional[StreamTransportManager] = None) -> None:
        self._service = service
        self._transport = transport

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

    def SetPlayfield(self, request, context):  # noqa: N802
        definition = _playfield_definition_from_proto(request.definition)
        try:
            self._service.set_playfield(definition)
        except Exception as exc:  # noqa: BLE001
            _abort(context, exc)
            return aprilcam_v2_pb2.SetPlayfieldResponse()
        return aprilcam_v2_pb2.SetPlayfieldResponse()

    def GetPlayfield(self, request, context):  # noqa: N802
        try:
            playfield = self._service.get_playfield(request.name)
        except Exception as exc:  # noqa: BLE001
            _abort(context, exc)
            return aprilcam_v2_pb2.GetPlayfieldResponse()
        return aprilcam_v2_pb2.GetPlayfieldResponse(playfield=_playfield_entry_to_proto(playfield))

    def ListPlayfields(self, request, context):  # noqa: N802
        try:
            playfields = self._service.list_playfields()
        except Exception as exc:  # noqa: BLE001
            _abort(context, exc)
            return aprilcam_v2_pb2.ListPlayfieldsResponse()
        return aprilcam_v2_pb2.ListPlayfieldsResponse(
            playfields=[_playfield_entry_to_proto(p) for p in playfields]
        )

    def SetCameraConfig(self, request, context):  # noqa: N802
        config = _camera_config_from_proto(request.config)
        try:
            self._service.set_camera_config(config)
        except Exception as exc:  # noqa: BLE001
            _abort(context, exc)
            return aprilcam_v2_pb2.SetCameraConfigResponse()
        return aprilcam_v2_pb2.SetCameraConfigResponse()

    def GetCameraConfig(self, request, context):  # noqa: N802
        try:
            config = self._service.get_camera_config(request.camera)
        except Exception as exc:  # noqa: BLE001
            _abort(context, exc)
            return aprilcam_v2_pb2.GetCameraConfigResponse()
        return aprilcam_v2_pb2.GetCameraConfigResponse(config=_camera_config_to_proto(config))

    def Calibrate(self, request, context):  # noqa: N802
        try:
            result = self._service.calibrate(request.camera)
        except Exception as exc:  # noqa: BLE001
            _abort(context, exc)
            return aprilcam_v2_pb2.CalibrateResponse()
        return aprilcam_v2_pb2.CalibrateResponse(result=_calibration_result_to_proto(result))

    def GetCalibration(self, request, context):  # noqa: N802
        try:
            calibration = self._service.get_calibration(request.camera)
        except Exception as exc:  # noqa: BLE001
            _abort(context, exc)
            return aprilcam_v2_pb2.GetCalibrationResponse()
        response = aprilcam_v2_pb2.GetCalibrationResponse()
        # Absent (never a fabricated/default Calibration) means never
        # successfully calibrated -- same presence discipline as
        # GetTagResponse.record.
        if calibration is not None:
            if not isinstance(calibration, Calibration):
                # Every Calibration a DaemonService ever holds was built
                # by calibration.calibration.Calibrator.calibrate -- the
                # only place that constructs one -- so this is always
                # the concrete type in practice. Guards against a future
                # Daemon implementation storing some other
                # calibration.interfaces.Calibration subclass this
                # module would not know how to serialize, the same
                # defensive check ``_playfield_entry_to_proto`` makes.
                raise TypeError(
                    f"cannot serialize calibration for {request.camera!r}: got "
                    f"{type(calibration).__name__}, not "
                    "aprilcam.calibration.calibration.Calibration"
                )
            response.calibration.CopyFrom(_calibration_to_proto(calibration))
        return response

    def ResetCameraConfig(self, request, context):  # noqa: N802
        try:
            self._service.reset_camera_config(request.camera)
        except Exception as exc:  # noqa: BLE001
            _abort(context, exc)
            return aprilcam_v2_pb2.ResetCameraConfigResponse()
        return aprilcam_v2_pb2.ResetCameraConfigResponse()

    def CloseCamera(self, request, context):  # noqa: N802
        try:
            self._service.close_camera(request.camera, force=request.force)
        except Exception as exc:  # noqa: BLE001
            _abort(context, exc)
            return aprilcam_v2_pb2.CloseCameraResponse()
        return aprilcam_v2_pb2.CloseCameraResponse()

    def PutAnnotation(self, request, context):  # noqa: N802
        annotation = _annotation_from_proto(request.annotation)
        try:
            self._service.put_annotation(request.playfield, annotation)
        except Exception as exc:  # noqa: BLE001
            _abort(context, exc)
            return aprilcam_v2_pb2.PutAnnotationResponse()
        return aprilcam_v2_pb2.PutAnnotationResponse()

    def RemoveAnnotation(self, request, context):  # noqa: N802
        try:
            self._service.remove_annotation(request.playfield, request.layer, request.id)
        except Exception as exc:  # noqa: BLE001
            _abort(context, exc)
            return aprilcam_v2_pb2.RemoveAnnotationResponse()
        return aprilcam_v2_pb2.RemoveAnnotationResponse()

    def ReplaceLayer(self, request, context):  # noqa: N802
        annotations = tuple(_annotation_from_proto(a) for a in request.annotations)
        try:
            self._service.replace_layer(request.playfield, request.layer, annotations)
        except Exception as exc:  # noqa: BLE001
            _abort(context, exc)
            return aprilcam_v2_pb2.ReplaceLayerResponse()
        return aprilcam_v2_pb2.ReplaceLayerResponse()

    def ClearAnnotations(self, request, context):  # noqa: N802
        layer = request.layer if request.HasField("layer") else None
        try:
            self._service.clear_annotations(request.playfield, layer)
        except Exception as exc:  # noqa: BLE001
            _abort(context, exc)
            return aprilcam_v2_pb2.ClearAnnotationsResponse()
        return aprilcam_v2_pb2.ClearAnnotationsResponse()

    def GetAnnotations(self, request, context):  # noqa: N802
        layer = request.layer if request.HasField("layer") else None
        try:
            annotations = self._service.get_annotations(request.playfield, layer)
        except Exception as exc:  # noqa: BLE001
            _abort(context, exc)
            return aprilcam_v2_pb2.GetAnnotationsResponse()
        return aprilcam_v2_pb2.GetAnnotationsResponse(
            annotations=_annotation_set_to_proto(annotations)
        )

    def FindFeature(self, request, context):  # noqa: N802
        try:
            features = self._service.find_feature(request.playfield, request.query)
        except Exception as exc:  # noqa: BLE001
            _abort(context, exc)
            return aprilcam_v2_pb2.FindFeatureResponse()
        return aprilcam_v2_pb2.FindFeatureResponse(
            features=[_feature_match_to_proto(f) for f in features]
        )

    def RegisterTag(self, request, context):  # noqa: N802
        tag = _tag_id_from_proto(request.tag)
        params = _mount_parameters_from_proto(request.params)
        try:
            warning = self._service.register_tag(tag, params)
        except Exception as exc:  # noqa: BLE001
            _abort(context, exc)
            return aprilcam_v2_pb2.RegisterTagResponse()
        response = aprilcam_v2_pb2.RegisterTagResponse()
        # Ticket 030-004, SUC-004: HasField-guarded exactly like every
        # other optional scalar in this module -- absent (never a
        # fabricated empty string) means either mount_z was zero or a
        # camera is already located, not "no opinion."
        if warning is not None:
            response.warning = warning
        return response

    def UnregisterTag(self, request, context):  # noqa: N802
        tag = _tag_id_from_proto(request.tag)
        try:
            self._service.unregister_tag(tag)
        except Exception as exc:  # noqa: BLE001
            _abort(context, exc)
            return aprilcam_v2_pb2.UnregisterTagResponse()
        return aprilcam_v2_pb2.UnregisterTagResponse()

    def ListTagParameters(self, request, context):  # noqa: N802
        try:
            registrations = self._service.list_tag_parameters()
        except Exception as exc:  # noqa: BLE001
            _abort(context, exc)
            return aprilcam_v2_pb2.ListTagParametersResponse()
        return aprilcam_v2_pb2.ListTagParametersResponse(
            entries=[
                aprilcam_v2_pb2.TagMountEntry(
                    tag=_tag_id_to_proto(tag),
                    params=_mount_parameters_to_proto(registration.params),
                    mount_z_applied=registration.mount_z_applied,
                )
                for tag, registration in registrations.items()
            ]
        )

    def GetGuide(self, request, context):  # noqa: N802
        """See ``daemon.interfaces.Daemon.get_guide`` (ticket 025-007,
        UC-27). ``request.name`` absent (``HasField`` false) means the
        default guide -- mirrors ``DaemonService.get_guide(name:
        Optional[str] = None)`` exactly, the same optional-scalar
        presence discipline every other optional request field in this
        module already uses."""
        name = request.name if request.HasField("name") else None
        try:
            text = self._service.get_guide(name)
        except Exception as exc:  # noqa: BLE001
            _abort(context, exc)
            return aprilcam_v2_pb2.GetGuideResponse()
        return aprilcam_v2_pb2.GetGuideResponse(text=text)

    def GetImageStream(self, request, context):  # noqa: N802
        """Endpoint allocation only (ticket 027-005, UC-07) -- never
        touches ``self._service`` here, and never starts a producer.
        See ``daemon.stream_transport.StreamTransportManager``'s module
        docstring for why: validation and producer start both happen
        later, inside the first accepted connection's own sender
        thread, not this RPC handler.
        """
        if self._transport is None:
            # No transport was wired into build_server (e.g. a test
            # exercising only the one-shot RPCs above) -- the same
            # UNIMPLEMENTED a caller would have seen before this ticket
            # existed, made explicit rather than raising an
            # AttributeError.
            context.abort(grpc.StatusCode.UNIMPLEMENTED, "stream transport not configured")
            return aprilcam_v2_pb2.GetImageStreamResponse()
        endpoint = self._transport.get_or_create_image_endpoint(request.camera, deskewed=request.deskewed)
        return aprilcam_v2_pb2.GetImageStreamResponse(endpoint=_stream_endpoint_to_proto(endpoint))

    def GetTagStream(self, request, context):  # noqa: N802
        """Same "endpoint allocation only" contract as
        :meth:`GetImageStream` above, for tag streams (UC-11)."""
        if self._transport is None:
            context.abort(grpc.StatusCode.UNIMPLEMENTED, "stream transport not configured")
            return aprilcam_v2_pb2.GetTagStreamResponse()
        endpoint = self._transport.get_or_create_tag_endpoint(request.camera)
        return aprilcam_v2_pb2.GetTagStreamResponse(endpoint=_stream_endpoint_to_proto(endpoint))

    def CaptureFlatField(self, request, context):  # noqa: N802
        exposure = request.exposure if request.HasField("exposure") else None
        frames = request.frames if request.HasField("frames") else None
        try:
            result = self._service.capture_flat_field(
                request.camera,
                exposure=exposure,
                frames=frames,
                force=request.force,
                dry_run=request.dry_run,
            )
        except Exception as exc:  # noqa: BLE001
            _abort(context, exc)
            return aprilcam_v2_pb2.CaptureFlatFieldResponse()
        return aprilcam_v2_pb2.CaptureFlatFieldResponse(result=_flatfield_result_to_proto(result))

    def GetFlatField(self, request, context):  # noqa: N802
        try:
            info = self._service.get_flatfield(request.camera)
        except Exception as exc:  # noqa: BLE001
            _abort(context, exc)
            return aprilcam_v2_pb2.GetFlatFieldResponse()
        response = aprilcam_v2_pb2.GetFlatFieldResponse()
        # Absent (never a fabricated/default FlatFieldInfo) means never
        # built, or cleared -- same presence discipline as
        # GetCalibrationResponse.calibration.
        if info is not None:
            response.flatfield.CopyFrom(_flatfield_info_to_proto(info))
        return response

    def ClearFlatField(self, request, context):  # noqa: N802
        try:
            self._service.clear_flat_field(request.camera)
        except Exception as exc:  # noqa: BLE001
            _abort(context, exc)
            return aprilcam_v2_pb2.ClearFlatFieldResponse()
        return aprilcam_v2_pb2.ClearFlatFieldResponse()

    def StartIntrinsics(self, request, context):  # noqa: N802
        board = request.board if request.HasField("board") else None
        try:
            status = self._service.start_intrinsics(request.camera, board)
        except Exception as exc:  # noqa: BLE001
            _abort(context, exc)
            return aprilcam_v2_pb2.StartIntrinsicsResponse()
        return aprilcam_v2_pb2.StartIntrinsicsResponse(status=_intrinsics_status_to_proto(status))

    def GetIntrinsicsStatus(self, request, context):  # noqa: N802
        try:
            status = self._service.get_intrinsics_status(request.camera)
        except Exception as exc:  # noqa: BLE001
            _abort(context, exc)
            return aprilcam_v2_pb2.GetIntrinsicsStatusResponse()
        return aprilcam_v2_pb2.GetIntrinsicsStatusResponse(status=_intrinsics_status_to_proto(status))

    def FinishIntrinsics(self, request, context):  # noqa: N802
        try:
            result = self._service.finish_intrinsics(request.camera, request.install, request.force)
        except Exception as exc:  # noqa: BLE001
            _abort(context, exc)
            return aprilcam_v2_pb2.FinishIntrinsicsResponse()
        return _intrinsics_finish_to_proto(result)

    def AbortIntrinsics(self, request, context):  # noqa: N802
        try:
            status = self._service.abort_intrinsics(request.camera)
        except Exception as exc:  # noqa: BLE001
            _abort(context, exc)
            return aprilcam_v2_pb2.AbortIntrinsicsResponse()
        return aprilcam_v2_pb2.AbortIntrinsicsResponse(status=_intrinsics_status_to_proto(status))


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
    around).

    *tcp_port* (ticket 028-003) is the TCP port :func:`build_server`
    already bound this listener's server to, or ``None`` if no TCP bind
    was requested or it failed -- captured at construction time (the
    bind itself happens in :func:`build_server`, before this object
    exists) and simply handed back by :meth:`start`, which is the
    signal ``daemon.lifecycle.DaemonLifecycle.start`` uses to decide
    whether (and with which port) to call its ``advertise`` hook.
    """

    def __init__(self, server: grpc.Server, *, tcp_port: Optional[int] = None) -> None:
        self._server = server
        self._tcp_port = tcp_port

    def start(self) -> Optional[int]:
        self._server.start()
        return self._tcp_port

    def stop(self, grace: float) -> None:
        self._server.stop(grace).wait(timeout=grace + 1.0)


def build_server(
    service: Daemon,
    socket_path: Path,
    *,
    max_workers: int = 10,
    transport: Optional[StreamTransportManager] = None,
    tcp_port: Optional[int] = None,
) -> _GrpcListener:
    """Construct and bind, but do not start, a gRPC server serving
    *service* over the unix socket at *socket_path* -- and, if
    *tcp_port* is given, also over TCP on all interfaces (ticket
    028-003; stakeholder decision recorded in that ticket: ``0.0.0.0``,
    unauthenticated, matching UC-01's documented end state).

    Deliberately unstarted -- this is the callable
    ``daemon/lifecycle.py``'s ``build_listener`` factory parameter
    expects (typically via ``functools.partial(build_server, service,
    socket_path, transport=transport, tcp_port=tcp_port)``), matching
    that module's "fresh server object per ``start()``" requirement: a
    stopped ``grpc.Server`` cannot be restarted, so
    ``daemon/__main__.py`` passes this function itself, not an
    already-built server.

    *transport* (``None`` by default -- every existing caller predating
    ticket 027-005 is unaffected) is handed straight to the
    :class:`_Servicer` this builds, so its ``GetImageStream``/
    ``GetTagStream`` handlers can allocate real endpoints; omitting it
    still builds a fully working server for every other RPC, with
    ``GetImageStream``/``GetTagStream`` answering ``UNIMPLEMENTED``.

    *tcp_port* (``None`` by default -- every existing caller predating
    ticket 028-003 is unaffected, and keeps today's unix-socket-only
    behavior exactly) requests a TCP bind on ``0.0.0.0:<tcp_port>``
    alongside the unix socket, via the same ``grpc.Server.
    add_insecure_port`` call the unix bind already makes -- one gRPC
    server, two transports, no change to :class:`_Servicer` or any
    RPC's request/response shape. **Binding TCP is never fatal**: this
    codebase's pinned ``grpc`` (1.80.0) raises ``RuntimeError`` from
    ``add_insecure_port`` on a genuine bind failure rather than
    returning ``0`` (``grpc._common.validate_port_binding_result``
    converts the C-core's ``0`` into that exception before it ever
    reaches a Python-level caller -- confirmed by direct testing while
    building this, contrary to a literal "returns 0" reading; both
    signals are handled below so this stays correct if some other
    ``grpc`` build ever surfaces the older return-based signal
    instead). Either way, a failure is caught, logged at WARNING naming
    the port, and treated as "no TCP" -- the returned
    :class:`_GrpcListener` still has a fully working unix socket, and
    its ``start()`` reports ``tcp_port=None`` so
    ``daemon.lifecycle.DaemonLifecycle`` knows not to advertise.

    The server disables ``grpc.so_reuseport``. ``grpc`` enables
    ``SO_REUSEPORT`` by default on its TCP listeners, which means two
    *independent* ``grpc.server()`` processes can both silently succeed
    binding the exact same port -- the kernel then load-balances
    incoming connections between two unrelated daemons, a far worse
    failure mode than the graceful unix-socket-only degradation this
    function otherwise promises (a stray second daemon on the same port
    must be *observable* as a bind failure, not invisible RPC
    misrouting between two processes). Disabling it has no effect on
    the unix-socket bind.

    Removes a stale socket file left by a crashed prior run before
    binding -- safe here specifically because ``DaemonLifecycle.
    start()`` calls ``build_listener`` only *after* the singleton lock
    is already held, so no live daemon can own that path (see
    ``daemon/lifecycle.py``'s "lock before listener, always" ordering).
    """
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    if socket_path.exists():
        socket_path.unlink()
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=max_workers),
        options=[("grpc.so_reuseport", 0)],
    )
    aprilcam_v2_pb2_grpc.add_AprilCamV2Servicer_to_server(_Servicer(service, transport), server)
    server.add_insecure_port(f"unix:{socket_path}")

    bound_tcp_port: Optional[int] = None
    if tcp_port is not None:
        try:
            bound = server.add_insecure_port(f"0.0.0.0:{tcp_port}")
        except RuntimeError as exc:
            logger.warning(
                "daemon: TCP control-plane bind failed on port %d (%s) -- "
                "continuing unix-socket-only",
                tcp_port,
                exc,
            )
        else:
            if bound == 0:
                # Defensive: see the docstring above -- this codebase's
                # pinned grpc raises instead, but a 0 return is handled
                # identically if a future/older build ever surfaces it.
                logger.warning(
                    "daemon: TCP control-plane bind failed on port %d -- "
                    "continuing unix-socket-only",
                    tcp_port,
                )
            else:
                bound_tcp_port = bound

    return _GrpcListener(server, tcp_port=bound_tcp_port)


def _stream_error_bytes(exc: Exception) -> bytes:
    """Serialize a failed stream subscribe's exception as a
    ``StreamError`` (the body of an error frame, sprint 043 ticket 003;
    ``stream_transport.py`` prepends ``framing.ERROR_FRAME_MARKER``).
    ``kind`` is the exception class name; ``subject``/``available``
    carry the structured fields ``UnknownCamera``/``CameraNotPresent``
    need to be rebuilt client-side."""
    msg = aprilcam_v2_pb2.StreamError(kind=type(exc).__name__, message=str(exc))
    subject = getattr(exc, "requested", None) or getattr(exc, "slug", None)
    if isinstance(subject, str):
        msg.subject = subject
    available = getattr(exc, "available", None)
    if isinstance(available, (tuple, list)):
        msg.available.extend(str(a) for a in available)
    return msg.SerializeToString()


def build_stream_transport(
    service: Daemon,
    socket_dir: Path,
    *,
    enable_unix: bool = True,
    enable_tcp: bool = True,
) -> StreamTransportManager:
    """Construct the :class:`~aprilcam.daemon.stream_transport.
    StreamTransportManager` a real daemon wires into :func:`build_server`
    (its *transport* parameter) and into ``daemon/__main__.py``'s
    shutdown hook (ticket 027-005).

    Lives here, not in ``daemon/stream_transport.py``, because the two
    callables it hands that module -- *image_encoder*/*tag_encoder* --
    are exactly this module's own :func:`_image_frame_bytes_for_stream`/
    :func:`_tag_frame_bytes_for_stream`: this is the one seam where the
    proto-serializing half of the boundary (owned here) meets the
    transport-only half (owned there), and this function is what wires
    them together without either module importing the other's private
    internals directly.
    """
    return StreamTransportManager(
        service,
        socket_dir,
        image_encoder=_image_frame_bytes_for_stream,
        tag_encoder=_tag_frame_bytes_for_stream,
        error_encoder=_stream_error_bytes,
        enable_unix=enable_unix,
        enable_tcp=enable_tcp,
    )
