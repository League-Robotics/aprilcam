"""The client-side half of the proto boundary (ticket 020-005).

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
proto fields (``playfield``, ``number``) round-trip through
``HasField()`` exactly the way ``daemon/rpc.py``'s server-side
counterparts set them -- ``None`` on this side iff ``HasField()`` is
``False`` on the wire, matching ``types.py``'s Optional-vs-None
discipline field for field.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from aprilcam.proto import aprilcam_v2_pb2
from aprilcam.types import CameraState, ImageFrame, ProbeReport, ProbeSkip

__all__ = [
    "RemoteDaemonStatus",
    "camera_state_from_proto",
    "camera_states_from_proto",
    "daemon_status_from_proto",
    "image_frame_from_proto",
    "probe_report_from_proto",
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
    """See ``daemon.interfaces.Daemon.get_frame``. The sprint-020 wire
    message carries none of ``ImageFrame``'s deskewed-only fields
    (``px_per_cm``, ``world_extent``, ``tags``, ``annotations`` -- see
    ``aprilcam_v2.proto``'s ``GetFrameResponse`` comment); they stay at
    their ``types.py`` defaults (``None``) here rather than being
    invented client-side."""
    return ImageFrame(
        data=msg.data,
        width=msg.width,
        height=msg.height,
        encoding=msg.encoding,
        timestamp=msg.timestamp,
        deskewed=msg.deskewed,
        calibration_stale=msg.calibration_stale,
        frame_dark=msg.frame_dark,
    )


@dataclass(frozen=True)
class RemoteDaemonStatus:
    """Concrete ``daemon.interfaces.DaemonStatus``, assembled client-side
    from a ``GetStatusResponse``. The wire-answered counterpart to
    ``daemon.service.SimpleDaemonStatus`` -- same shape, different
    origin (a real RPC round trip rather than a same-process call)."""

    running: bool
    cameras: Tuple[CameraState, ...]
    config_dir: str
    state_dir: str


def daemon_status_from_proto(msg: aprilcam_v2_pb2.GetStatusResponse) -> RemoteDaemonStatus:
    return RemoteDaemonStatus(
        running=msg.running,
        cameras=camera_states_from_proto(msg.cameras),
        config_dir=msg.config_dir,
        state_dir=msg.state_dir,
    )
