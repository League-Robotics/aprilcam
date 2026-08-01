"""Thin gRPC wrapper over ``daemon/service.py`` (ticket 020-004).

Translates between the sprint-020-scoped ``proto/aprilcam_v2.proto``
service (four RPCs: ``ListCameras``, ``ProbeCameras``, ``GetFrame``,
``GetStatus``) and ``daemon.service.DaemonService`` method calls.
Everything here is (de)serialization and error-code mapping; no
business logic, no camera access, no registry/discovery calls -- see
the sprint architecture's module table and this ticket's explicit
anti-pattern warning: v1's ``grpc_server.py`` (1193 lines) mixed both
in one file, which is exactly what splitting ``rpc.py`` from
``service.py`` exists to avoid.

**Proto boundary** (``docs/design/design.md`` §6: "generated proto
types cross into Python only via ``from_proto`` constructors in
``client/models.py``"). This module owns the *server-side* half of that
boundary -- Python dataclasses to proto messages -- so nothing outside
it imports ``aprilcam.proto.aprilcam_v2_pb2`` directly. The reverse
direction (wire -> Python, client-side) is ticket 020-005's
``client/models.py``.

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
than duplicate the list.
"""

from __future__ import annotations

import logging
from concurrent import futures
from pathlib import Path
from typing import Iterable

import grpc

from aprilcam.daemon.interfaces import Daemon, DaemonStatus
from aprilcam.daemon.lifecycle import ControlPlaneListener
from aprilcam.errors import AlreadyRunning, AprilCamError, NotCalibrated, UnknownCamera
from aprilcam.proto import aprilcam_v2_pb2, aprilcam_v2_pb2_grpc
from aprilcam.types import CameraState, ImageFrame, ProbeReport

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
    return aprilcam_v2_pb2.GetFrameResponse(
        data=frame.data,
        width=frame.width,
        height=frame.height,
        encoding=frame.encoding,
        timestamp=frame.timestamp,
        deskewed=frame.deskewed,
        calibration_stale=frame.calibration_stale,
        frame_dark=frame.frame_dark,
    )


def _status_to_proto(status: DaemonStatus) -> aprilcam_v2_pb2.GetStatusResponse:
    return aprilcam_v2_pb2.GetStatusResponse(
        running=status.running,
        cameras=_camera_states_to_proto(status.cameras),
        config_dir=status.config_dir,
        state_dir=status.state_dir,
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
