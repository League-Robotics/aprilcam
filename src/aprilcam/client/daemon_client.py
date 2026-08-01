"""``GrpcDaemon`` -- the ``Daemon`` base class, client side (ticket
020-005; ``get_tags``/``get_tag``/``get_tag_history`` added by ticket
021-005; ``set_playfield``/``get_playfield``/``list_playfields``/
``set_camera_config``/``get_camera_config`` added by ticket 022-001;
``calibrate``/``get_calibration`` added by ticket 022-003): a typed
wrapper over the generated gRPC stub for
``list_cameras``, ``probe_cameras``, ``get_frame``, ``status``,
``get_tags``, ``get_tag``, ``get_tag_history``, ``set_playfield``,
``get_playfield``, ``list_playfields``, ``set_camera_config``,
``get_camera_config``, ``calibrate``, ``get_calibration``.

**Only these fourteen methods are implemented**, mirroring
``daemon.service.DaemonService``'s own stance: ``daemon.interfaces.
Daemon`` is a much larger base class (streams, annotations, tag
parameters, guides, ``reset_camera_config``, ``find_feature``), and
those are inherited as-is here rather than overridden -- calling one
raises ``NotImplementedError`` naming itself instead of failing with a
bare ``AttributeError``. ``isinstance(client,
Daemon)`` is ``True``: this really is a ``Daemon``, an incomplete one
this sprint, honestly so via the inherited stubs rather than by a
Protocol's structural guess.

**Request marshalling lives here; response marshalling does not.** This
module builds ``aprilcam_v2_pb2`` request messages directly (nothing
else may -- it is the one client module that talks the wire's request
side), but every response crosses back into Python exclusively through
``client/models.py``'s ``from_proto`` constructors
(``docs/design/design.md`` §6's proto boundary) -- this module never
reads a response field itself.

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
from typing import Optional, Tuple

import grpc

from aprilcam.calibration.calibration import Calibration
from aprilcam.calibration.interfaces import CalibrationResult
from aprilcam.client.models import (
    calibration_from_proto,
    calibration_result_from_proto,
    camera_config_from_proto,
    camera_states_from_proto,
    daemon_status_from_proto,
    image_frame_from_proto,
    playfield_from_proto,
    playfields_from_proto,
    probe_report_from_proto,
    tag_frame_from_proto,
    tag_record_from_get_tag_response,
)
from aprilcam.config.interfaces import CameraConfig
from aprilcam.config.interfaces import PlayfieldDefinition as PlayfieldDefinitionIface
from aprilcam.config.loader import PlayfieldDefinition
from aprilcam.daemon.interfaces import Daemon, DaemonStatus
from aprilcam.errors import (
    AlreadyRunning,
    CameraNotPresent,
    ConnectionFailed,
    NoPlayfieldLinked,
    NotCalibrated,
    UnknownCamera,
    UnknownPlayfield,
)
from aprilcam.playfield.interfaces import Playfield
from aprilcam.proto import aprilcam_v2_pb2, aprilcam_v2_pb2_grpc
from aprilcam.types import CameraState, ImageFrame, ProbeReport, TagFamily, TagFrame, TagId, TagRecord

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
    msg = aprilcam_v2_pb2.PlayfieldFeatureEntry(slug=entry.slug, x=entry.x, y=entry.y)
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


def _camera_config_to_proto(config: CameraConfig) -> aprilcam_v2_pb2.CameraConfig:
    """Request-building direction only, for an outgoing
    ``SetCameraConfigRequest``. ``settings`` crosses as JSON text (see
    ``aprilcam_v2.proto``'s header comment)."""
    msg = aprilcam_v2_pb2.CameraConfig(name=config.name, settings_json=json.dumps(config.settings))
    if config.playfield is not None:
        msg.playfield = config.playfield
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
    return ConnectionFailed(target, reason=exc.details() or str(exc.code()))


class GrpcDaemon(Daemon):
    """Concrete ``daemon.interfaces.Daemon`` (this sprint's slice only),
    driven by an already-connected gRPC stub.

    Derives from ``Daemon`` (see that class's module docstring for the
    base-class-vs-``Protocol`` design rule), mirroring
    ``daemon.service.DaemonService``'s own stance: the methods this
    sprint does not implement are inherited as-is and raise
    ``NotImplementedError`` naming themselves if called, rather than
    disappearing into a bare ``AttributeError``.

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

    # -- daemon.interfaces.Daemon (this sprint's slice) --------------------

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

    def set_camera_config(self, config: CameraConfig) -> None:
        request = aprilcam_v2_pb2.SetCameraConfigRequest(config=_camera_config_to_proto(config))
        self._call(self._stub.SetCameraConfig, request)

    def get_camera_config(self, camera: str) -> CameraConfig:
        request = aprilcam_v2_pb2.GetCameraConfigRequest(camera=camera)
        response = self._call(self._stub.GetCameraConfig, request)
        return camera_config_from_proto(response.config)

    def calibrate(self, camera: str) -> CalibrationResult:
        request = aprilcam_v2_pb2.CalibrateRequest(camera=camera)
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

    # -- internals -----------------------------------------------------

    def _call(self, method, request):
        try:
            return method(request)
        except grpc.RpcError as exc:
            raise _reconstruct_error(exc, self._target) from exc
