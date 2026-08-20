"""``GrpcDaemon`` -- the ``Daemon`` base class, client side (ticket
020-005; ``get_tags``/``get_tag``/``get_tag_history`` added by ticket
021-005; ``set_playfield``/``get_playfield``/``list_playfields``/
``set_camera_config``/``get_camera_config`` added by ticket 022-001;
``calibrate``/``get_calibration`` added by ticket 022-003;
``reset_camera_config`` added by ticket 023-004; ``put_annotation``/
``remove_annotation``/``replace_layer``/``clear_annotations``/
``get_annotations`` added by ticket 024-002; ``find_feature`` added by
ticket 025-002; ``register_tag``/``unregister_tag``/
``list_tag_parameters`` added by ticket 025-004; ``get_guide`` added by
ticket 025-007; ``stream_images``/``stream_tags`` added by ticket
027-006, completing the interface): a typed
wrapper over the generated gRPC stub for
``list_cameras``, ``probe_cameras``, ``get_frame``, ``status``,
``get_tags``, ``get_tag``, ``get_tag_history``, ``set_playfield``,
``get_playfield``, ``list_playfields``, ``set_camera_config``,
``get_camera_config``, ``calibrate``, ``get_calibration``,
``reset_camera_config``, ``put_annotation``, ``remove_annotation``,
``replace_layer``, ``clear_annotations``, ``get_annotations``,
``find_feature``, ``register_tag``, ``unregister_tag``,
``list_tag_parameters``, ``get_guide``, ``stream_images``,
``stream_tags``.

**All twenty-seven methods are implemented** -- ``daemon.interfaces.
Daemon`` in full, as of this ticket. Nothing is inherited as-is from the
base class any more; every method above overrides its
``NotImplementedError`` stub. ``isinstance(client, Daemon)`` was always
``True``; the difference this ticket makes is that it is now true of a
*complete* implementation, not an honestly-partial one.

**Request marshalling lives here; response marshalling does not.** This
module builds ``aprilcam_v2_pb2`` request messages directly (nothing
else may -- it is the one client module that talks the wire's request
side), but every response crosses back into Python exclusively through
``client/models.py``'s ``from_proto`` constructors
(``docs/design/design.md`` §6's proto boundary) -- this module never
reads a response field itself. ``stream_images``/``stream_tags`` follow
the identical one-hop shape for their own response sub-message
(``response.endpoint``), just handing it to ``client/streams.py``'s
``SocketImageStream.connect``/``SocketTagStream.connect`` instead of a
``client/models.py`` converter -- opening a socket, not building an
``aprilcam.types`` dataclass, is what happens next for that one field
(see ``client/streams.py``'s module docstring).

**Error mapping.** ``daemon/rpc.py``'s ``_abort`` docstring documents
the exact trailing-metadata contract this reconstructs from
(``aprilcam-error-type`` plus per-type extra fields). An ``RpcError``
whose metadata names a known ``aprilcam.errors`` type is rebuilt as
that exact type -- callers catch types, not strings, on this side of
the wire too. Anything else (no metadata at all, or a type this map
does not yet know) is :class:`~aprilcam.errors.ConnectionFailed`: "a
named daemon did not answer" is the most honest thing an unclassified
RPC failure can mean from here -- the daemon crashing mid-call, the
socket going away, or a status this client has no typed name for all
look the same from outside.
"""

from __future__ import annotations

import json
from typing import Dict, Optional, Tuple

import grpc

from aprilcam.calibration.interfaces import Calibration, CalibrationResult
from aprilcam.client.models import (
    annotation_set_from_proto,
    calibration_from_proto,
    calibration_result_from_proto,
    camera_config_from_proto,
    camera_states_from_proto,
    daemon_status_from_proto,
    features_from_proto,
    image_frame_from_proto,
    playfield_from_proto,
    playfields_from_proto,
    probe_report_from_proto,
    tag_frame_from_proto,
    tag_parameters_from_proto,
    tag_record_from_get_tag_response,
)
from aprilcam.client.streams import SocketImageStream, SocketTagStream
from aprilcam.config.interfaces import CameraConfig, CameraPosition, Feature
from aprilcam.config.interfaces import PlayfieldDefinition as PlayfieldDefinitionIface
from aprilcam.config.loader import PlayfieldDefinition
from aprilcam.daemon.interfaces import Daemon, DaemonStatus, ImageStream, TagStream
from aprilcam.errors import (
    AlreadyRunning,
    CameraNotPresent,
    ConnectionFailed,
    NoPlayfieldLinked,
    NotCalibrated,
    UnknownCamera,
    UnknownGuide,
    UnknownPlayfield,
)
from aprilcam.playfield.interfaces import Playfield
from aprilcam.proto import aprilcam_v2_pb2, aprilcam_v2_pb2_grpc
from aprilcam.types import (
    Annotation,
    AnnotationSet,
    CameraState,
    Circle,
    ImageFrame,
    Marker,
    MountParameters,
    PathShape,
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

__all__ = ["GrpcDaemon"]

#: TagFamily -> the generated enum's int values, request-building
#: direction only. Duplicated (not shared) with ``daemon/rpc.py``'s and
#: ``client/models.py``'s identical-shaped maps -- see this module's
#: docstring: request marshalling lives here, response marshalling
#: does not, so this module owns its own small conversion for the one
#: request field (``GetTagRequest.tag``/``GetTagHistoryRequest.tag``)
#: that is not a bare scalar.
_TAG_FAMILY_TO_PROTO = {
    TagFamily.APRILTAG: aprilcam_v2_pb2.APRILTAG,
    TagFamily.ARUCO: aprilcam_v2_pb2.ARUCO,
}


def _tag_id_to_proto(tag: TagId) -> aprilcam_v2_pb2.TagId:
    return aprilcam_v2_pb2.TagId(family=_TAG_FAMILY_TO_PROTO[tag.family], number=tag.number)


def _playfield_feature_entry_to_proto(entry) -> aprilcam_v2_pb2.PlayfieldFeatureEntry:
    msg = aprilcam_v2_pb2.PlayfieldFeatureEntry(
        slug=entry.slug, x=entry.x, y=entry.y, height_cm=entry.height_cm
    )
    if entry.tag_id is not None:
        msg.id = entry.tag_id
    if entry.colour is not None:
        msg.colour = entry.colour
    return msg


def _playfield_definition_to_proto(definition: PlayfieldDefinition) -> aprilcam_v2_pb2.PlayfieldDefinition:
    """Request-building direction only (Python -> proto, for an
    outgoing ``SetPlayfieldRequest``) -- duplicated, not shared, with
    ``daemon/rpc.py``'s identically-shaped converter, matching this
    module's own established pattern for ``_TAG_FAMILY_TO_PROTO``/
    ``_tag_id_to_proto`` above: each side of the proto boundary owns its
    own small conversion."""
    return aprilcam_v2_pb2.PlayfieldDefinition(
        name=definition.name,
        width_cm=definition.width_cm,
        height_cm=definition.height_cm,
        april_tags=[_playfield_feature_entry_to_proto(e) for e in definition.april_tags],
        aruco_tags=[_playfield_feature_entry_to_proto(e) for e in definition.aruco_tags],
        rectangles=[_playfield_feature_entry_to_proto(e) for e in definition.rectangles],
        dots=[_playfield_feature_entry_to_proto(e) for e in definition.dots],
    )


def _mount_parameters_to_proto(params: MountParameters) -> aprilcam_v2_pb2.MountParameters:
    """Request-building direction only (Python -> proto, for an
    outgoing ``RegisterTagRequest``, ticket 025-004) -- duplicated, not
    shared, with ``daemon/rpc.py``'s identically-shaped
    ``_mount_parameters_to_proto``, matching this module's own
    established pattern above."""
    msg = aprilcam_v2_pb2.MountParameters(
        mount_x=params.mount_x,
        mount_y=params.mount_y,
        mount_z=params.mount_z,
        mount_yaw_rad=params.mount_yaw_rad,
    )
    if params.size_cm is not None:
        msg.size_cm = params.size_cm
    return msg


def _camera_position_to_proto(position: CameraPosition) -> aprilcam_v2_pb2.CameraPosition:
    """Request-building direction only (sprint 029 ticket 001), for an
    outgoing ``SetCameraConfigRequest``'s optional ``camera_position``."""
    return aprilcam_v2_pb2.CameraPosition(
        nadir=_world_point_to_proto(position.nadir), height_cm=position.height_cm
    )


def _camera_config_to_proto(config: CameraConfig) -> aprilcam_v2_pb2.CameraConfig:
    """Request-building direction only, for an outgoing
    ``SetCameraConfigRequest``. ``settings`` crosses as JSON text (see
    ``aprilcam_v2.proto``'s header comment). ``camera_position`` is
    ``HasField``-guarded (sprint 029 ticket 001): absent means "not
    configured," never a fabricated zero-valued ``CameraPosition``."""
    msg = aprilcam_v2_pb2.CameraConfig(name=config.name, settings_json=json.dumps(config.settings))
    if config.playfield is not None:
        msg.playfield = config.playfield
    if config.camera_position is not None:
        msg.camera_position.CopyFrom(_camera_position_to_proto(config.camera_position))
    return msg


#: Annotations (ticket 024-002, UC-28, specification.md section 9
#: decision 8). Request-building direction only (Python -> proto, for
#: an outgoing ``PutAnnotationRequest``/``ReplaceLayerRequest``) --
#: duplicated, not shared, with ``daemon/rpc.py``'s identically-shaped
#: ``_annotation_to_proto`` and friends, matching this module's own
#: established pattern above: each side of the proto boundary owns its
#: own small conversion.


def _world_point_to_proto(point: WorldPoint) -> aprilcam_v2_pb2.WorldPoint:
    return aprilcam_v2_pb2.WorldPoint(x=point.x, y=point.y)


def _marker_to_proto(marker: Marker) -> aprilcam_v2_pb2.Marker:
    msg = aprilcam_v2_pb2.Marker(at=_world_point_to_proto(marker.at))
    if marker.label is not None:
        msg.label = marker.label
    return msg


def _path_shape_to_proto(path: PathShape) -> aprilcam_v2_pb2.PathShape:
    return aprilcam_v2_pb2.PathShape(points=[_world_point_to_proto(p) for p in path.points])


def _polygon_to_proto(polygon: Polygon) -> aprilcam_v2_pb2.Polygon:
    return aprilcam_v2_pb2.Polygon(points=[_world_point_to_proto(p) for p in polygon.points])


def _circle_to_proto(circle: Circle) -> aprilcam_v2_pb2.Circle:
    return aprilcam_v2_pb2.Circle(center=_world_point_to_proto(circle.center), radius_cm=circle.radius_cm)


def _text_to_proto(text: Text) -> aprilcam_v2_pb2.Text:
    return aprilcam_v2_pb2.Text(at=_world_point_to_proto(text.at), text=text.text)


def _style_to_proto(style: Style) -> aprilcam_v2_pb2.Style:
    msg = aprilcam_v2_pb2.Style()
    if style.color is not None:
        msg.color = style.color
    if style.line_width is not None:
        msg.line_width = style.line_width
    return msg


def _annotation_to_proto(annotation: Annotation) -> aprilcam_v2_pb2.Annotation:
    """Python -> proto, for an outgoing ``PutAnnotationRequest``/
    ``ReplaceLayerRequest``. See ``daemon/rpc.py``'s
    ``_annotation_to_proto`` -- exactly one ``oneof shape`` field is
    set, matching ``types.AnnotationShape``'s
    ``Union[Marker, PathShape, Polygon, Circle, Text]``; the ``else``
    branch is unreachable through normal use for the same reason as
    that function's."""
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


def _split_available(raw: str) -> Tuple[str, ...]:
    """Undo ``daemon/rpc.py``'s ``",".join(exc.available)`` -- an empty
    string means the original tuple was empty, not a one-element tuple
    holding ``""``."""
    return tuple(item for item in raw.split(",") if item)


def _reconstruct_error(exc: grpc.RpcError, target: str) -> Exception:
    """Rebuild the typed ``aprilcam.errors`` exception *exc* carries in
    its trailing metadata, per ``daemon/rpc.py``'s ``_abort`` contract.
    Falls back to :class:`ConnectionFailed` naming *target* for
    anything unrecognised."""
    metadata = dict(exc.trailing_metadata() or ())
    error_type = metadata.get("aprilcam-error-type")
    if error_type == "AlreadyRunning":
        return AlreadyRunning(metadata.get("aprilcam-holder", "unknown holder"))
    if error_type == "UnknownCamera":
        return UnknownCamera(
            metadata.get("aprilcam-requested", ""),
            _split_available(metadata.get("aprilcam-available", "")),
        )
    if error_type == "CameraNotPresent":
        return CameraNotPresent(metadata.get("aprilcam-slug", "unknown"))
    if error_type == "NotCalibrated":
        return NotCalibrated(exc.details() or "camera not calibrated")
    if error_type == "UnknownPlayfield":
        return UnknownPlayfield(
            metadata.get("aprilcam-requested", ""),
            _split_available(metadata.get("aprilcam-available", "")),
        )
    if error_type == "NoPlayfieldLinked":
        return NoPlayfieldLinked(metadata.get("aprilcam-camera", "unknown"))
    if error_type == "UnknownGuide":
        return UnknownGuide(
            metadata.get("aprilcam-requested", ""),
            _split_available(metadata.get("aprilcam-available", "")),
        )
    return ConnectionFailed(target, reason=exc.details() or str(exc.code()))


class GrpcDaemon(Daemon):
    """Concrete ``daemon.interfaces.Daemon``, driven by an
    already-connected gRPC stub -- complete as of ticket 027-006, which
    added the last two methods (``stream_images``/``stream_tags``).

    Derives from ``Daemon`` (see that class's module docstring for the
    base-class-vs-``Protocol`` design rule). Nothing is inherited as-is
    from the base class any more -- every method is overridden -- but
    the class still derives from ``Daemon`` rather than, say, standing
    alone, for the same reason ``daemon.service.DaemonService`` derives
    from it too: a future ``Daemon`` method added upstream is inherited
    here as an honest ``NotImplementedError`` stub naming itself, not a
    bare ``AttributeError``, until this class catches up.

    Parameters
    ----------
    stub:
        A ready ``AprilCamV2Stub`` -- connection establishment (unix
        socket for local, TCP for a named remote) is
        ``client/discovery.py``'s job, not this class's; by the time a
        caller has one of these, the channel is already up.
    target:
        The host this stub talks to (``"localhost"`` or the ``-d``
        name), carried purely so an unclassified RPC failure can be
        reported as :class:`~aprilcam.errors.ConnectionFailed` naming
        *which* daemon stopped answering.
    """

    def __init__(self, stub: aprilcam_v2_pb2_grpc.AprilCamV2Stub, *, target: str) -> None:
        self._stub = stub
        self._target = target

    # -- daemon.interfaces.Daemon (complete, ticket 027-006) ----------------

    def list_cameras(self) -> Tuple[CameraState, ...]:
        response = self._call(self._stub.ListCameras, aprilcam_v2_pb2.ListCamerasRequest())
        return camera_states_from_proto(response.cameras)

    def probe_cameras(self) -> ProbeReport:
        response = self._call(self._stub.ProbeCameras, aprilcam_v2_pb2.ProbeCamerasRequest())
        return probe_report_from_proto(response)

    def get_frame(
        self, camera: str, *, deskewed: bool = False, encoding: str = "jpeg"
    ) -> ImageFrame:
        request = aprilcam_v2_pb2.GetFrameRequest(
            camera=camera, deskewed=deskewed, encoding=encoding
        )
        response = self._call(self._stub.GetFrame, request)
        return image_frame_from_proto(response)

    def status(self) -> DaemonStatus:
        response = self._call(self._stub.GetStatus, aprilcam_v2_pb2.GetStatusRequest())
        return daemon_status_from_proto(response)

    def get_tags(self, camera: str) -> TagFrame:
        request = aprilcam_v2_pb2.GetTagsRequest(camera=camera)
        response = self._call(self._stub.GetTags, request)
        return tag_frame_from_proto(response.tags)

    def get_tag(self, camera: str, tag: TagId) -> Optional[TagRecord]:
        request = aprilcam_v2_pb2.GetTagRequest(camera=camera, tag=_tag_id_to_proto(tag))
        response = self._call(self._stub.GetTag, request)
        return tag_record_from_get_tag_response(response)

    def get_tag_history(
        self, camera: str, tag: Optional[TagId] = None, frames: Optional[int] = None
    ) -> Tuple[TagFrame, ...]:
        request = aprilcam_v2_pb2.GetTagHistoryRequest(camera=camera)
        if tag is not None:
            request.tag.CopyFrom(_tag_id_to_proto(tag))
        if frames is not None:
            request.frames = frames
        response = self._call(self._stub.GetTagHistory, request)
        return tuple(tag_frame_from_proto(f) for f in response.history)

    def stream_images(self, camera: str, *, deskewed: bool = False) -> ImageStream:
        """See ``daemon.interfaces.Daemon.stream_images`` (ticket
        027-006, UC-07). ``GetImageStream`` only allocates (or reuses)
        a socket endpoint -- it starts no producer
        (``daemon/stream_transport.py``'s own contract); it is the
        *connection* :meth:`~aprilcam.client.streams.SocketImageStream.
        connect` makes next that the daemon-side accept loop notices and
        turns into a real subscription. ``response.endpoint`` is handed
        straight across, unread, per this module's docstring."""
        request = aprilcam_v2_pb2.GetImageStreamRequest(camera=camera, deskewed=deskewed)
        response = self._call(self._stub.GetImageStream, request)
        return SocketImageStream.connect(response.endpoint, host=self._target)

    def stream_tags(self, camera: str) -> TagStream:
        """See ``daemon.interfaces.Daemon.stream_tags`` (ticket 027-006,
        UC-11). Same shape as :meth:`stream_images` above, for
        ``GetTagStream``."""
        request = aprilcam_v2_pb2.GetTagStreamRequest(camera=camera)
        response = self._call(self._stub.GetTagStream, request)
        return SocketTagStream.connect(response.endpoint, host=self._target)

    def set_playfield(self, definition: PlayfieldDefinitionIface) -> None:
        if not isinstance(definition, PlayfieldDefinition):
            # Every PlayfieldDefinition a caller has in hand came from
            # ConfigLoader.load_playfield -- the only place that
            # constructs one this sprint -- so this is always the
            # concrete type in practice. Guards against a future
            # PlayfieldDefinition subclass this module would not know
            # how to serialize, the same defensive check
            # ``daemon/rpc.py``'s ``_playfield_entry_to_proto`` makes.
            raise TypeError(
                f"cannot push playfield {definition.name!r}: got "
                f"{type(definition).__name__}, not aprilcam.config.loader.PlayfieldDefinition"
            )
        request = aprilcam_v2_pb2.SetPlayfieldRequest(
            definition=_playfield_definition_to_proto(definition)
        )
        self._call(self._stub.SetPlayfield, request)

    def get_playfield(self, name: str) -> Playfield:
        request = aprilcam_v2_pb2.GetPlayfieldRequest(name=name)
        response = self._call(self._stub.GetPlayfield, request)
        return playfield_from_proto(response.playfield)

    def list_playfields(self) -> Tuple[Playfield, ...]:
        response = self._call(self._stub.ListPlayfields, aprilcam_v2_pb2.ListPlayfieldsRequest())
        return playfields_from_proto(response.playfields)

    def find_feature(self, playfield: str, query: str) -> Tuple[Feature, ...]:
        """See ``daemon.interfaces.Daemon.find_feature`` (ticket
        025-002, UC-08). No camera opened, no calibration required --
        answered entirely from the named playfield's surveyed
        definition. Ambiguous queries return every match (UC-08.S-2);
        no match returns an empty tuple (UC-08.S-3), not an error."""
        request = aprilcam_v2_pb2.FindFeatureRequest(playfield=playfield, query=query)
        response = self._call(self._stub.FindFeature, request)
        return features_from_proto(response.features)

    def register_tag(self, tag: TagId, params: MountParameters) -> None:
        """See ``daemon.interfaces.Daemon.register_tag`` (ticket
        025-004, UC-12). Upserts by *tag* -- takes effect on the next
        detection burst for that tag; does not survive a daemon
        restart."""
        request = aprilcam_v2_pb2.RegisterTagRequest(
            tag=_tag_id_to_proto(tag), params=_mount_parameters_to_proto(params)
        )
        self._call(self._stub.RegisterTag, request)

    def unregister_tag(self, tag: TagId) -> None:
        """See ``daemon.interfaces.Daemon.unregister_tag``. An absent
        registration is a no-op, enforced daemon-side."""
        request = aprilcam_v2_pb2.UnregisterTagRequest(tag=_tag_id_to_proto(tag))
        self._call(self._stub.UnregisterTag, request)

    def list_tag_parameters(self) -> Dict[TagId, MountParameters]:
        """See ``daemon.interfaces.Daemon.list_tag_parameters``. A
        snapshot of every currently registered tag's mount parameters."""
        response = self._call(
            self._stub.ListTagParameters, aprilcam_v2_pb2.ListTagParametersRequest()
        )
        return tag_parameters_from_proto(response)

    def set_camera_config(self, config: CameraConfig) -> None:
        request = aprilcam_v2_pb2.SetCameraConfigRequest(config=_camera_config_to_proto(config))
        self._call(self._stub.SetCameraConfig, request)

    def reset_camera_config(self, camera: str) -> None:
        """Discard *camera*'s live override and re-apply
        `config_dir/cameras/<slug>/config.json` (ticket 023-004) --
        ``daemon.interfaces.Daemon.reset_camera_config``'s "RPC face of
        ``Camera.reset()``". Request carries only the camera token,
        mirroring ``CalibrateRequest``'s shape -- there is nothing else
        for a caller to supply, the daemon reads its own disk."""
        request = aprilcam_v2_pb2.ResetCameraConfigRequest(camera=camera)
        self._call(self._stub.ResetCameraConfig, request)

    def get_camera_config(self, camera: str) -> CameraConfig:
        request = aprilcam_v2_pb2.GetCameraConfigRequest(camera=camera)
        response = self._call(self._stub.GetCameraConfig, request)
        return camera_config_from_proto(response.config)

    def calibrate(self, camera: str, *, estimate_intrinsics: bool = False) -> CalibrationResult:
        request = aprilcam_v2_pb2.CalibrateRequest(
            camera=camera, estimate_intrinsics=estimate_intrinsics
        )
        response = self._call(self._stub.Calibrate, request)
        return calibration_result_from_proto(response.result)

    def get_calibration(self, camera: str) -> Optional[Calibration]:
        request = aprilcam_v2_pb2.GetCalibrationRequest(camera=camera)
        response = self._call(self._stub.GetCalibration, request)
        # Absent (never a fabricated/default Calibration) means never
        # successfully calibrated -- same presence discipline as
        # get_tag's Optional[TagRecord].
        if not response.HasField("calibration"):
            return None
        return calibration_from_proto(response.calibration)

    def put_annotation(self, playfield: str, annotation: Annotation) -> None:
        """Upsert one annotation on *playfield* (UC-28) -- the RPC face
        of ``Daemon.put_annotation``. Thin request-build-and-call, like
        every method above: no business logic here, that stays in the
        daemon (``adapters.md``)."""
        request = aprilcam_v2_pb2.PutAnnotationRequest(
            playfield=playfield, annotation=_annotation_to_proto(annotation)
        )
        self._call(self._stub.PutAnnotation, request)

    def remove_annotation(self, playfield: str, layer: str, id: str) -> None:
        """Delete one annotation by its ``(layer, id)`` key. Removing an
        absent key is a no-op, enforced daemon-side."""
        request = aprilcam_v2_pb2.RemoveAnnotationRequest(playfield=playfield, layer=layer, id=id)
        self._call(self._stub.RemoveAnnotation, request)

    def replace_layer(
        self, playfield: str, layer: str, annotations: Tuple[Annotation, ...]
    ) -> None:
        """Atomically swap *layer*'s entire contents (UC-28.S-2)."""
        request = aprilcam_v2_pb2.ReplaceLayerRequest(
            playfield=playfield,
            layer=layer,
            annotations=[_annotation_to_proto(a) for a in annotations],
        )
        self._call(self._stub.ReplaceLayer, request)

    def clear_annotations(self, playfield: str, layer: Optional[str] = None) -> None:
        """Clear one layer, or every layer on *playfield* when *layer*
        is ``None``. Clearing an already-empty (or nonexistent) layer is
        a no-op, enforced daemon-side."""
        request = aprilcam_v2_pb2.ClearAnnotationsRequest(playfield=playfield)
        if layer is not None:
            request.layer = layer
        self._call(self._stub.ClearAnnotations, request)

    def get_annotations(self, playfield: str, layer: Optional[str] = None) -> AnnotationSet:
        """The current annotation set for *playfield*, optionally
        filtered to one *layer* (UC-28.S-3). ``GetAnnotationsResponse.
        annotations`` is always present on the wire (``daemon/rpc.py``'s
        ``GetAnnotations`` handler), so no ``HasField()`` check is
        needed here -- unlike ``get_calibration``'s genuinely optional
        response."""
        request = aprilcam_v2_pb2.GetAnnotationsRequest(playfield=playfield)
        if layer is not None:
            request.layer = layer
        response = self._call(self._stub.GetAnnotations, request)
        return annotation_set_from_proto(response.annotations)

    def get_guide(self, name: Optional[str] = None) -> str:
        """See ``daemon.interfaces.Daemon.get_guide`` (ticket 025-007,
        UC-27). The RPC path for a caller that already has a daemon
        connection -- ``client.guides.Guides`` (ticket 025-006) is the
        primary no-daemon-needed path; this exists so an already-connected
        caller can fetch the identical text without a second, separate
        accessor. Serves byte-identical text to that no-daemon path by
        construction: both ultimately read ``aprilcam.guides.read_guide``,
        just via ``DaemonService.get_guide`` on this end instead of
        directly."""
        request = aprilcam_v2_pb2.GetGuideRequest()
        if name is not None:
            request.name = name
        response = self._call(self._stub.GetGuide, request)
        return response.text

    # -- internals -----------------------------------------------------

    def _call(self, method, request):
        try:
            return method(request)
        except grpc.RpcError as exc:
            raise _reconstruct_error(exc, self._target) from exc
