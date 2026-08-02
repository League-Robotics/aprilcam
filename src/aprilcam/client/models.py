"""The client-side half of the proto boundary (ticket 020-005; the
tag-detection converters below are ticket 021-005; the annotation-store
converters below, and ``image_frame_from_proto``'s ``annotations``
field, are ticket 024-002).

``docs/design/design.md`` §6 is explicit: "generated proto types cross
into Python only via ``from_proto`` constructors in ``client/models.py``."
This module is that door, for the wire-to-Python direction. The reverse
direction -- Python dataclasses to proto messages, for a *server*
answering an RPC -- lives in ``daemon/rpc.py`` (``_camera_state_to_proto``
and friends); nothing here goes the other way, and nothing outside this
module reads a proto response message's fields directly. Only
``client/daemon_client.py`` calls into this module -- it builds request
messages itself (the direction *into* the wire, which is not what the
boundary rule restricts) and hands every response straight here before
touching a single field of it.

Every function is a pure, one-way conversion: proto message in,
``aprilcam.types`` (or a small local) dataclass out. ``optional`` scalar
proto fields (``playfield``, ``number``, ``TagRecord.heading_rad``/
``.speed``/``.yaw_rad``) and singular message fields with implicit
presence (``TagRecord.world``/``.world_velocity``/``.pixel``) round-trip
through ``HasField()`` exactly the way ``daemon/rpc.py``'s server-side
counterparts set them -- ``None`` on this side iff ``HasField()`` is
``False`` on the wire, never coerced to ``0.0``/empty, matching
``types.py``'s Optional-vs-None discipline field for field.
``world_corners``/``pixel_corners`` are ``repeated`` rather than
``optional`` (proto3 disallows ``optional repeated``); an empty
``repeated`` is the unambiguous "absent" encoding there because a valid
corner quad is never empty (unlike ``ImageFrame.tags``, where an empty
``TagFrame`` is itself a valid, distinct answer -- that field uses real
presence instead, see :func:`image_frame_from_proto`).
"""

from __future__ import annotations

import json
from typing import Optional, Tuple

from aprilcam.calibration.calibration import Calibration
from aprilcam.calibration.interfaces import CalibrationResult
from aprilcam.config.interfaces import CameraConfig
from aprilcam.config.loader import PlayfieldDefinition, PlayfieldFeatureEntry
from aprilcam.daemon.interfaces import DaemonStatus
from aprilcam.playfield.playfield import Playfield
from aprilcam.proto import aprilcam_v2_pb2
from aprilcam.types import (
    Annotation,
    AnnotationSet,
    AnnotationShape,
    CameraState,
    Circle,
    ImageFrame,
    Marker,
    PathShape,
    PixelPoint,
    Polygon,
    ProbeReport,
    ProbeSkip,
    Style,
    TagFamily,
    TagFrame,
    TagId,
    TagRecord,
    Text,
    WorldPoint,
)

__all__ = [
    "annotation_from_proto",
    "annotation_set_from_proto",
    "calibration_from_proto",
    "calibration_result_from_proto",
    "camera_config_from_proto",
    "camera_state_from_proto",
    "camera_states_from_proto",
    "daemon_status_from_proto",
    "image_frame_from_proto",
    "playfield_definition_from_proto",
    "playfield_from_proto",
    "playfields_from_proto",
    "probe_report_from_proto",
    "style_from_proto",
    "tag_frame_from_proto",
    "tag_id_from_proto",
    "tag_record_from_get_tag_response",
    "tag_record_from_proto",
]


def camera_state_from_proto(msg: aprilcam_v2_pb2.CameraState) -> CameraState:
    """The client-side mirror of ``daemon/rpc.py``'s
    ``_camera_state_to_proto`` -- field for field, in reverse."""
    return CameraState(
        name=msg.name,
        present=msg.present,
        usable=msg.usable,
        playfield=msg.playfield if msg.HasField("playfield") else None,
        calibrated=msg.calibrated,
        calibration_stale=msg.calibration_stale,
        frame_dark=msg.frame_dark,
        number=msg.number if msg.HasField("number") else None,
    )


def camera_states_from_proto(
    msgs,
) -> Tuple[CameraState, ...]:
    return tuple(camera_state_from_proto(m) for m in msgs)


def probe_report_from_proto(msg: aprilcam_v2_pb2.ProbeCamerasResponse) -> ProbeReport:
    """See ``daemon.interfaces.Daemon.probe_cameras`` -- ``skipped``
    empty means the probe really was complete, exactly as it did before
    crossing the wire (UC-25.S-7)."""
    return ProbeReport(
        cameras=camera_states_from_proto(msg.cameras),
        skipped=tuple(ProbeSkip(backend=s.backend, reason=s.reason) for s in msg.skipped),
    )


def image_frame_from_proto(msg: aprilcam_v2_pb2.GetFrameResponse) -> ImageFrame:
    """See ``daemon.interfaces.Daemon.get_frame``. ``tags`` (ticket
    021-005), ``px_per_cm``/``world_extent`` (ticket 022-005), and
    ``annotations`` (ticket 024-002) all round-trip via ``HasField()``
    -- ``None`` on this side iff absent on the wire, never coerced to
    ``0.0``/empty, matching ``types.py``'s Optional-vs-None discipline
    exactly. ``annotations`` is present only for a deskewed frame --
    ``daemon/rpc.py``'s ``_image_frame_to_proto`` never sets it for a
    raw one -- and, unlike ``tags``, is *always* set on that branch,
    even for an empty ``AnnotationSet`` (revision 0): that presence
    survives here unchanged, never coerced to ``None``."""
    return ImageFrame(
        data=msg.data,
        width=msg.width,
        height=msg.height,
        encoding=msg.encoding,
        timestamp=msg.timestamp,
        deskewed=msg.deskewed,
        calibration_stale=msg.calibration_stale,
        frame_dark=msg.frame_dark,
        tags=tag_frame_from_proto(msg.tags) if msg.HasField("tags") else None,
        px_per_cm=msg.px_per_cm if msg.HasField("px_per_cm") else None,
        world_extent=(
            (msg.world_extent.width_cm, msg.world_extent.height_cm)
            if msg.HasField("world_extent")
            else None
        ),
        annotations=(
            annotation_set_from_proto(msg.annotations) if msg.HasField("annotations") else None
        ),
    )


#: TagFamily <-> the generated enum's int values, both directions.
#: Duplicated (not shared) with ``daemon/rpc.py``'s identical-shaped
#: map -- see this module's docstring: each side of the proto boundary
#: owns its own small conversion rather than importing across it.
_TAG_FAMILY_FROM_PROTO = {
    aprilcam_v2_pb2.APRILTAG: TagFamily.APRILTAG,
    aprilcam_v2_pb2.ARUCO: TagFamily.ARUCO,
}


def _world_point_from_proto(msg: aprilcam_v2_pb2.WorldPoint) -> WorldPoint:
    return WorldPoint(x=msg.x, y=msg.y)


def _pixel_point_from_proto(msg: aprilcam_v2_pb2.PixelPoint) -> PixelPoint:
    return PixelPoint(x=msg.x, y=msg.y)


def _world_corners_from_proto(
    msgs,
) -> Optional[Tuple[WorldPoint, WorldPoint, WorldPoint, WorldPoint]]:
    """Empty ``repeated`` is the unambiguous "absent" encoding here (see
    the module docstring) -- a valid corner quad is never length 0.
    Converts to an explicit fixed 4-tuple (not a bare ``tuple(...)``
    call) to satisfy ``TagRecord.world_corners``'s fixed-length type,
    the same pattern ``daemon/detection.py``'s ``_pixel_corners``
    uses server-side."""
    if not msgs:
        return None
    pts = [_world_point_from_proto(m) for m in msgs]
    return (pts[0], pts[1], pts[2], pts[3])


def _pixel_corners_from_proto(
    msgs,
) -> Optional[Tuple[PixelPoint, PixelPoint, PixelPoint, PixelPoint]]:
    if not msgs:
        return None
    pts = [_pixel_point_from_proto(m) for m in msgs]
    return (pts[0], pts[1], pts[2], pts[3])


def tag_id_from_proto(msg: aprilcam_v2_pb2.TagId) -> TagId:
    """The client-side mirror of ``daemon/rpc.py``'s
    ``_tag_id_to_proto``. The v1 ``-(N+1)`` internal ArUco encoding
    never appears on the wire, so ``msg.number`` is always the
    family's own non-negative id -- nothing to undo here."""
    return TagId(family=_TAG_FAMILY_FROM_PROTO[msg.family], number=msg.number)


def tag_record_from_proto(msg: aprilcam_v2_pb2.TagRecord) -> TagRecord:
    """The client-side mirror of ``daemon/rpc.py``'s
    ``_tag_record_to_proto`` -- field for field, in reverse. Every
    optional field round-trips through ``HasField()``, never coerced to
    ``0.0``/empty: an uncalibrated record's ``world``/``heading_rad``/
    ``world_velocity``/``speed``/``yaw_rad``/``world_corners`` all come
    back as ``None``, exactly as they went in (this sprint's binding
    contract -- no calibration subsystem exists yet)."""
    return TagRecord(
        tag=tag_id_from_proto(msg.tag),
        world=_world_point_from_proto(msg.world) if msg.HasField("world") else None,
        heading_rad=msg.heading_rad if msg.HasField("heading_rad") else None,
        world_velocity=(
            _world_point_from_proto(msg.world_velocity)
            if msg.HasField("world_velocity")
            else None
        ),
        speed=msg.speed if msg.HasField("speed") else None,
        timestamp=msg.timestamp,
        pixel=_pixel_point_from_proto(msg.pixel) if msg.HasField("pixel") else None,
        calibrated=msg.calibrated,
        yaw_rad=msg.yaw_rad if msg.HasField("yaw_rad") else None,
        world_corners=_world_corners_from_proto(msg.world_corners),
        pixel_corners=_pixel_corners_from_proto(msg.pixel_corners),
    )


def tag_frame_from_proto(msg: aprilcam_v2_pb2.TagFrame) -> TagFrame:
    """The client-side mirror of ``daemon/rpc.py``'s
    ``_tag_frame_to_proto`` -- field for field, in reverse."""
    return TagFrame(
        tags=tuple(tag_record_from_proto(r) for r in msg.tags),
        timestamp=msg.timestamp,
        frame_index=msg.frame_index,
        calibration_stale=msg.calibration_stale,
    )


def tag_record_from_get_tag_response(
    msg: aprilcam_v2_pb2.GetTagResponse,
) -> Optional[TagRecord]:
    """See ``daemon.interfaces.Daemon.get_tag``. Takes the whole
    ``GetTagResponse`` (like :func:`image_frame_from_proto` takes the
    whole ``GetFrameResponse``) so the ``HasField("record")`` presence
    check stays inside this module rather than at
    ``client/daemon_client.py``'s call site -- that module's docstring
    is explicit that response marshalling, presence checks included,
    does not happen there. Absent means the tag was not detected this
    burst -- never a zero-valued :class:`~aprilcam.types.TagRecord`
    (UC-09.S-4)."""
    return tag_record_from_proto(msg.record) if msg.HasField("record") else None


def daemon_status_from_proto(msg: aprilcam_v2_pb2.GetStatusResponse) -> DaemonStatus:
    """Assemble a ``daemon.interfaces.DaemonStatus`` client-side from a
    ``GetStatusResponse`` -- the wire-answered counterpart to
    ``daemon.service.DaemonService.status()``'s same-process
    construction of the same class."""
    return DaemonStatus(
        running=msg.running,
        cameras=camera_states_from_proto(msg.cameras),
        config_dir=msg.config_dir,
        state_dir=msg.state_dir,
    )


def _playfield_feature_entry_from_proto(
    msg: aprilcam_v2_pb2.PlayfieldFeatureEntry,
) -> PlayfieldFeatureEntry:
    """The client-side mirror of ``daemon/rpc.py``'s
    ``_playfield_feature_entry_to_proto`` -- field for field, in
    reverse."""
    return PlayfieldFeatureEntry(
        slug=msg.slug,
        x=msg.x,
        y=msg.y,
        tag_id=msg.id if msg.HasField("id") else None,
        colour=msg.colour if msg.HasField("colour") else None,
    )


def playfield_definition_from_proto(msg: aprilcam_v2_pb2.PlayfieldDefinition) -> PlayfieldDefinition:
    """The client-side mirror of ``daemon/rpc.py``'s
    ``_playfield_definition_to_proto`` -- field for field, in reverse.
    Reconstructs the same ``config.loader.PlayfieldDefinition`` concrete
    type the server holds, so a push -> fetch round trip through a real
    daemon compares equal the same way a same-process
    ``load(x.to_dict()) == x`` check does (``config/loader.py``'s
    module docstring)."""
    return PlayfieldDefinition(
        name=msg.name,
        width_cm=msg.width_cm,
        height_cm=msg.height_cm,
        april_tags=tuple(_playfield_feature_entry_from_proto(e) for e in msg.april_tags),
        aruco_tags=tuple(_playfield_feature_entry_from_proto(e) for e in msg.aruco_tags),
        rectangles=tuple(_playfield_feature_entry_from_proto(e) for e in msg.rectangles),
        dots=tuple(_playfield_feature_entry_from_proto(e) for e in msg.dots),
    )


def playfield_from_proto(msg: aprilcam_v2_pb2.PlayfieldEntry) -> Playfield:
    """See ``daemon.interfaces.Daemon.get_playfield``. ``linked_cameras``
    crosses the wire **already derived** (``daemon/rpc.py``'s
    ``_playfield_entry_to_proto`` calls the server's own
    ``Playfield.linked_cameras()`` before serializing) -- the client has
    no camera-config store of its own to re-derive it from. Rather than
    giving the client-side ``Playfield`` a second, wire-only code path,
    this reconstructs a minimal synthetic ``camera_configs`` snapshot
    (one bare entry per linked camera, naming this playfield) so
    :meth:`~aprilcam.playfield.playfield.Playfield.linked_cameras`'s
    single real implementation reproduces exactly the list the wire
    carried, unchanged."""
    return Playfield(
        name=msg.name,
        definition=playfield_definition_from_proto(msg.definition),
        camera_configs=tuple(
            CameraConfig(name=name, playfield=msg.name, settings={}) for name in msg.linked_cameras
        ),
    )


def playfields_from_proto(msgs) -> Tuple[Playfield, ...]:
    return tuple(playfield_from_proto(m) for m in msgs)


def camera_config_from_proto(msg: aprilcam_v2_pb2.CameraConfig) -> CameraConfig:
    """The client-side mirror of ``daemon/rpc.py``'s
    ``_camera_config_to_proto``. ``settings`` crosses as JSON text (see
    ``aprilcam_v2.proto``'s header comment)."""
    settings = json.loads(msg.settings_json) if msg.settings_json else {}
    return CameraConfig(
        name=msg.name,
        playfield=msg.playfield if msg.HasField("playfield") else None,
        settings=settings,
    )


def calibration_result_from_proto(msg: aprilcam_v2_pb2.CalibrationResult) -> CalibrationResult:
    """The client-side mirror of ``daemon/rpc.py``'s
    ``_calibration_result_to_proto`` -- field for field, in reverse.
    ``missing_ids`` round-trips as the exact ids, never a count
    (UC-04.S-2)."""
    return CalibrationResult(
        succeeded=msg.succeeded,
        matched_ids=tuple(msg.matched_ids),
        missing_ids=tuple(msg.missing_ids),
        reprojection_error_cm=(
            msg.reprojection_error_cm if msg.HasField("reprojection_error_cm") else None
        ),
        reason=msg.reason if msg.HasField("reason") else None,
    )


def calibration_from_proto(msg: aprilcam_v2_pb2.Calibration) -> Calibration:
    """The client-side mirror of ``daemon/rpc.py``'s
    ``_calibration_to_proto`` -- field for field, in reverse.
    ``homography`` un-flattens the wire's 9 row-major doubles back into
    :class:`~aprilcam.calibration.calibration.Calibration`'s
    ``Tuple[Tuple[float, float, float], ...]`` shape."""
    h = list(msg.homography)
    homography = (
        (h[0], h[1], h[2]),
        (h[3], h[4], h[5]),
        (h[6], h[7], h[8]),
    )
    return Calibration(
        stale=msg.stale,
        calibrated_at=msg.calibrated_at,
        playfield=msg.playfield,
        homography=homography,
        matched_ids=tuple(msg.matched_ids),
        reprojection_error_cm=(
            msg.reprojection_error_cm if msg.HasField("reprojection_error_cm") else None
        ),
    )


#: Annotations (ticket 024-002, UC-28, specification.md section 9
#: decision 8). The client-side mirror of ``daemon/rpc.py``'s
#: ``_annotation_*_from_proto`` converters -- field for field, in
#: reverse, same ``HasField()`` discipline as everywhere else in this
#: module. Per-shape helpers stay private (like ``_world_point_from_proto``
#: above); ``annotation_from_proto``/``annotation_set_from_proto``/
#: ``style_from_proto`` are the public door, matching the granularity of
#: this module's other composite converters (``tag_record_from_proto``
#: and friends).


def _marker_from_proto(msg: aprilcam_v2_pb2.Marker) -> Marker:
    return Marker(at=_world_point_from_proto(msg.at), label=msg.label if msg.HasField("label") else None)


def _path_shape_from_proto(msg: aprilcam_v2_pb2.PathShape) -> PathShape:
    return PathShape(points=tuple(_world_point_from_proto(p) for p in msg.points))


def _polygon_from_proto(msg: aprilcam_v2_pb2.Polygon) -> Polygon:
    return Polygon(points=tuple(_world_point_from_proto(p) for p in msg.points))


def _circle_from_proto(msg: aprilcam_v2_pb2.Circle) -> Circle:
    return Circle(center=_world_point_from_proto(msg.center), radius_cm=msg.radius_cm)


def _text_from_proto(msg: aprilcam_v2_pb2.Text) -> Text:
    return Text(at=_world_point_from_proto(msg.at), text=msg.text)


def style_from_proto(msg: aprilcam_v2_pb2.Style) -> Style:
    """The client-side mirror of ``daemon/rpc.py``'s ``_style_to_proto``
    -- field for field, in reverse. An unset ``color``/``line_width``
    stays ``None``, never coerced to ``""``/``0.0``."""
    return Style(
        color=msg.color if msg.HasField("color") else None,
        line_width=msg.line_width if msg.HasField("line_width") else None,
    )


def annotation_from_proto(msg: aprilcam_v2_pb2.Annotation) -> Annotation:
    """The client-side mirror of ``daemon/rpc.py``'s
    ``_annotation_from_proto`` -- field for field, in reverse, including
    the same ``WhichOneof("shape")`` dispatch (see that function's
    docstring for why the ``else`` branch is unreachable through normal
    use)."""
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
        style=style_from_proto(msg.style),
        ttl_seconds=msg.ttl_seconds if msg.HasField("ttl_seconds") else None,
    )


def annotation_set_from_proto(msg: aprilcam_v2_pb2.AnnotationSet) -> AnnotationSet:
    """The client-side mirror of ``daemon/rpc.py``'s
    ``_annotation_set_to_proto`` -- field for field, in reverse."""
    return AnnotationSet(
        annotations=tuple(annotation_from_proto(a) for a in msg.annotations),
        revision=msg.revision,
        timestamp=msg.timestamp,
    )
