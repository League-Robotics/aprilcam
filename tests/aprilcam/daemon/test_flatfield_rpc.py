"""RPC round-trip tests for ``CaptureFlatField``/``GetFlatField``/
``ClearFlatField`` (sprint 037 ticket 003) -- the wire face of ticket
002's ``Daemon.capture_flat_field``/``.get_flatfield``/
``.clear_flat_field``.

Fake-backend only, mirroring ``test_service.py``'s ``_StaticTagsDaemon``
pattern: a minimal ``Daemon`` double (plain base class, derived from --
see ``daemon.interfaces``'s module docstring) that answers with canned
values and records the kwargs it was called with. These tests exercise
only ``daemon/rpc.py``'s marshalling and error mapping over a real
in-process gRPC server/servicer pair (``test_service.py``'s own
``_bind_stub``, reused here) -- not ``DaemonService``'s actual
capture/mask/persistence logic, which ticket 002's ``test_service.py``
section already covers.

The ``NoFieldMask`` case additionally proves the wire contract itself
(status code + trailing metadata) carries enough for a client to
reconstruct the typed exception. Reconstruction is done inline in that
test rather than via ``client/daemon_client.py``'s ``_reconstruct_error``
-- that shared client-side helper is ticket 004's ("client wrapper +
CLI") scope to extend, not this one's: this ticket touches only
``proto/``/``daemon/rpc.py``, both daemon-side.
"""

from __future__ import annotations

from typing import List, Optional

import grpc
import numpy as np
import pytest

from aprilcam.calibration.interfaces import FlatFieldInfo, FlatFieldResult
from aprilcam.daemon.interfaces import Daemon
from aprilcam.errors import NoFieldMask
from aprilcam.proto import aprilcam_v2_pb2
from aprilcam.types import CameraState

from tests.aprilcam.daemon.test_service import _bind_stub


class _FakeFlatFieldDaemon(Daemon):
    """Answers ``capture_flat_field``/``get_flatfield``/
    ``clear_flat_field``/``list_cameras`` with caller-supplied canned
    values, and records the kwargs each ``capture_flat_field`` call was
    made with so a test can assert the RPC handler marshalled every
    optional request field through untouched."""

    def __init__(
        self,
        *,
        result: Optional[FlatFieldResult] = None,
        raises: Optional[Exception] = None,
        info: Optional[FlatFieldInfo] = None,
        camera_state: Optional[CameraState] = None,
    ) -> None:
        self._result = result
        self._raises = raises
        self._info = info
        self._camera_state = camera_state
        self.capture_calls: List[dict] = []
        self.cleared: List[str] = []

    def capture_flat_field(
        self,
        camera: str,
        *,
        exposure: Optional[float] = None,
        frames: Optional[int] = None,
        force: bool = False,
        dry_run: bool = False,
    ) -> FlatFieldResult:
        self.capture_calls.append(
            dict(camera=camera, exposure=exposure, frames=frames, force=force, dry_run=dry_run)
        )
        if self._raises is not None:
            raise self._raises
        assert self._result is not None
        return self._result

    def get_flatfield(self, camera: str) -> Optional[FlatFieldInfo]:
        return self._info

    def clear_flat_field(self, camera: str) -> None:
        self.cleared.append(camera)

    def list_cameras(self):
        assert self._camera_state is not None
        return (self._camera_state,)


# ---------------------------------------------------------------------------
# CaptureFlatField
# ---------------------------------------------------------------------------


def test_capture_flat_field_round_trip_success():
    result = FlatFieldResult(
        succeeded=True,
        spread_before=(1.55, 1.40, 1.33),
        spread_after=(1.03, 1.02, 1.01),
        reason=None,
        forced_checks=(),
        gain=np.ones((4, 4, 3), dtype=np.float32),
    )
    fake = _FakeFlatFieldDaemon(result=result)

    with _bind_stub(fake) as stub:
        response = stub.CaptureFlatField(
            aprilcam_v2_pb2.CaptureFlatFieldRequest(
                camera="cam1", exposure=-4.0, frames=12, force=False, dry_run=False
            )
        )

    assert response.result.succeeded is True
    assert list(response.result.spread_before) == pytest.approx([1.55, 1.40, 1.33])
    assert list(response.result.spread_after) == pytest.approx([1.03, 1.02, 1.01])
    # reason absent on a clean pass -- never a fabricated empty string.
    assert response.result.HasField("reason") is False
    assert list(response.result.forced_checks) == []
    # gain never crosses the wire -- FlatFieldResult carries no such field.
    assert not hasattr(response.result, "gain")

    assert fake.capture_calls == [
        dict(camera="cam1", exposure=-4.0, frames=12, force=False, dry_run=False)
    ]


def test_capture_flat_field_round_trip_quality_gate_refusal():
    result = FlatFieldResult(
        succeeded=False,
        spread_before=(2.10,),
        spread_after=(1.90,),
        reason="max_spread_after exceeded: 1.90 > 1.20",
        forced_checks=(),
        gain=None,
    )
    fake = _FakeFlatFieldDaemon(result=result)

    with _bind_stub(fake) as stub:
        # No exposure/frames given -- absent on the wire, so the fake
        # daemon must see them as None, not a coerced 0/0.0.
        response = stub.CaptureFlatField(aprilcam_v2_pb2.CaptureFlatFieldRequest(camera="cam1"))

    assert response.result.succeeded is False
    assert response.result.HasField("reason") is True
    assert response.result.reason == "max_spread_after exceeded: 1.90 > 1.20"
    assert list(response.result.forced_checks) == []
    # The measured spread is still reported on a refused attempt.
    assert list(response.result.spread_before) == pytest.approx([2.10])
    assert list(response.result.spread_after) == pytest.approx([1.90])

    assert fake.capture_calls == [
        dict(camera="cam1", exposure=None, frames=None, force=False, dry_run=False)
    ]


def test_capture_flat_field_round_trip_force_true_reports_forced_checks():
    result = FlatFieldResult(
        succeeded=True,
        spread_before=(2.10,),
        spread_after=(1.90,),
        reason="max_spread_after exceeded: 1.90 > 1.20 (forced)",
        forced_checks=("max_spread_after",),
        gain=np.ones((2, 2), dtype=np.float32),
    )
    fake = _FakeFlatFieldDaemon(result=result)

    with _bind_stub(fake) as stub:
        response = stub.CaptureFlatField(
            aprilcam_v2_pb2.CaptureFlatFieldRequest(camera="cam1", force=True)
        )

    assert response.result.succeeded is True
    assert list(response.result.forced_checks) == ["max_spread_after"]
    assert response.result.HasField("reason") is True

    assert len(fake.capture_calls) == 1
    assert fake.capture_calls[0]["force"] is True
    assert fake.capture_calls[0]["dry_run"] is False


def test_capture_flat_field_round_trip_dry_run_true_is_marshalled_through():
    result = FlatFieldResult(
        succeeded=True,
        spread_before=(1.10,),
        spread_after=(1.02,),
        gain=np.ones((2, 2), dtype=np.float32),
    )
    fake = _FakeFlatFieldDaemon(result=result)

    with _bind_stub(fake) as stub:
        response = stub.CaptureFlatField(
            aprilcam_v2_pb2.CaptureFlatFieldRequest(camera="cam1", dry_run=True)
        )

    assert response.result.succeeded is True
    assert len(fake.capture_calls) == 1
    assert fake.capture_calls[0]["dry_run"] is True
    assert fake.capture_calls[0]["force"] is False


def test_capture_flat_field_no_field_mask_maps_to_failed_precondition_and_reconstructs():
    fake = _FakeFlatFieldDaemon(raises=NoFieldMask("cam1"))

    with _bind_stub(fake) as stub:
        with pytest.raises(grpc.RpcError) as exc_info:
            stub.CaptureFlatField(aprilcam_v2_pb2.CaptureFlatFieldRequest(camera="cam1"))

    exc = exc_info.value
    assert exc.code() == grpc.StatusCode.FAILED_PRECONDITION

    metadata = dict(exc.trailing_metadata() or ())
    assert metadata["aprilcam-error-type"] == "NoFieldMask"
    assert metadata["aprilcam-camera"] == "cam1"

    # The wire contract carries enough for a client to reconstruct the
    # typed exception -- proven here inline (ticket 004's own client
    # wrapper does this for real, via _reconstruct_error).
    reconstructed = NoFieldMask(metadata["aprilcam-camera"])
    assert isinstance(reconstructed, NoFieldMask)
    assert reconstructed.camera == "cam1"


# ---------------------------------------------------------------------------
# GetFlatField
# ---------------------------------------------------------------------------


def test_get_flat_field_round_trip_absent_when_never_captured():
    fake = _FakeFlatFieldDaemon(info=None)

    with _bind_stub(fake) as stub:
        response = stub.GetFlatField(aprilcam_v2_pb2.GetFlatFieldRequest(camera="cam1"))

    # Never a fabricated/default FlatFieldInfo -- mirrors
    # GetCalibrationResponse.calibration's presence discipline.
    assert response.HasField("flatfield") is False


def test_get_flat_field_round_trip_present():
    info = FlatFieldInfo(
        present=True,
        captured_at=1234.5,
        shape=(480, 640, 3),
        gain_min=0.72,
        gain_max=1.31,
        stale=True,
    )
    fake = _FakeFlatFieldDaemon(info=info)

    with _bind_stub(fake) as stub:
        response = stub.GetFlatField(aprilcam_v2_pb2.GetFlatFieldRequest(camera="cam1"))

    assert response.HasField("flatfield") is True
    assert response.flatfield.present is True
    assert response.flatfield.HasField("captured_at") is True
    assert response.flatfield.captured_at == pytest.approx(1234.5)
    assert list(response.flatfield.shape) == [480, 640, 3]
    assert response.flatfield.HasField("gain_min") is True
    assert response.flatfield.gain_min == pytest.approx(0.72)
    assert response.flatfield.HasField("gain_max") is True
    assert response.flatfield.gain_max == pytest.approx(1.31)
    assert response.flatfield.stale is True


def test_get_flat_field_round_trip_present_with_no_sidecar_is_absent_not_fabricated():
    """A pre-sprint-037 ``flatfield.npy`` with no ``flatfield.json``
    sidecar reports ``present=True`` with everything else absent --
    never a fabricated timestamp/shape (``FlatFieldInfo``'s own
    docstring)."""
    info = FlatFieldInfo(present=True)
    fake = _FakeFlatFieldDaemon(info=info)

    with _bind_stub(fake) as stub:
        response = stub.GetFlatField(aprilcam_v2_pb2.GetFlatFieldRequest(camera="cam1"))

    assert response.flatfield.present is True
    assert response.flatfield.HasField("captured_at") is False
    assert list(response.flatfield.shape) == []
    assert response.flatfield.HasField("gain_min") is False
    assert response.flatfield.HasField("gain_max") is False
    assert response.flatfield.stale is False


# ---------------------------------------------------------------------------
# ClearFlatField
# ---------------------------------------------------------------------------


def test_clear_flat_field_round_trip():
    fake = _FakeFlatFieldDaemon()

    with _bind_stub(fake) as stub:
        response = stub.ClearFlatField(aprilcam_v2_pb2.ClearFlatFieldRequest(camera="cam1"))

    assert response == aprilcam_v2_pb2.ClearFlatFieldResponse()
    assert fake.cleared == ["cam1"]


def test_clear_flat_field_round_trip_is_idempotent_no_op():
    """Clearing an already-clear camera is a no-op, never an error --
    the fake daemon simply records every call, matching
    ``Daemon.clear_flat_field``'s own no-op-on-already-clear contract."""
    fake = _FakeFlatFieldDaemon()

    with _bind_stub(fake) as stub:
        stub.ClearFlatField(aprilcam_v2_pb2.ClearFlatFieldRequest(camera="cam1"))
        stub.ClearFlatField(aprilcam_v2_pb2.ClearFlatFieldRequest(camera="cam1"))

    assert fake.cleared == ["cam1", "cam1"]


# ---------------------------------------------------------------------------
# CameraState.flatfield_present / .flatfield_stale (proto extension, AC 1)
# ---------------------------------------------------------------------------


def test_list_cameras_round_trip_carries_flatfield_flags():
    state = CameraState(
        name="cam1",
        present=True,
        usable=True,
        playfield=None,
        calibrated=False,
        flatfield_present=True,
        flatfield_stale=True,
    )
    fake = _FakeFlatFieldDaemon(camera_state=state)

    with _bind_stub(fake) as stub:
        response = stub.ListCameras(aprilcam_v2_pb2.ListCamerasRequest())

    assert len(response.cameras) == 1
    assert response.cameras[0].flatfield_present is True
    assert response.cameras[0].flatfield_stale is True


def test_list_cameras_round_trip_flatfield_flags_default_false():
    state = CameraState(
        name="cam1", present=True, usable=True, playfield=None, calibrated=False
    )
    fake = _FakeFlatFieldDaemon(camera_state=state)

    with _bind_stub(fake) as stub:
        response = stub.ListCameras(aprilcam_v2_pb2.ListCamerasRequest())

    assert response.cameras[0].flatfield_present is False
    assert response.cameras[0].flatfield_stale is False
