"""``GrpcDaemon`` -- the ``Daemon`` base class, client side (ticket
020-005): a typed wrapper over ticket 004's generated gRPC stub for
this sprint's four RPCs (``list_cameras``, ``probe_cameras``,
``get_frame``, ``status``).

**Only this sprint's four methods are implemented**, mirroring
``daemon.service.DaemonService``'s own stance: ``daemon.interfaces.
Daemon`` is a much larger base class (playfields, calibration, tags,
streams, annotations, tag parameters, guides), and those are inherited
as-is here rather than overridden -- calling one raises
``NotImplementedError`` naming itself instead of failing with a bare
``AttributeError``. ``isinstance(client, Daemon)`` is ``True``: this
really is a ``Daemon``, an incomplete one this sprint, honestly so via
the inherited stubs rather than by a Protocol's structural guess.

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

from typing import Tuple

import grpc

from aprilcam.client.models import (
    camera_states_from_proto,
    daemon_status_from_proto,
    image_frame_from_proto,
    probe_report_from_proto,
)
from aprilcam.daemon.interfaces import Daemon, DaemonStatus
from aprilcam.errors import (
    AlreadyRunning,
    CameraNotPresent,
    ConnectionFailed,
    NotCalibrated,
    UnknownCamera,
)
from aprilcam.proto import aprilcam_v2_pb2, aprilcam_v2_pb2_grpc
from aprilcam.types import CameraState, ImageFrame, ProbeReport

__all__ = ["GrpcDaemon"]


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

    # -- internals -----------------------------------------------------

    def _call(self, method, request):
        try:
            return method(request)
        except grpc.RpcError as exc:
            raise _reconstruct_error(exc, self._target) from exc
