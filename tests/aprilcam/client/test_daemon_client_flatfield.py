"""Tests for ``GrpcDaemon.capture_flat_field``/``.get_flatfield``/
``.clear_flat_field`` and ``client/models.py``'s
``flatfield_result_from_proto``/``flatfield_info_from_proto`` (sprint
037 ticket 004).

Fake-backend only, reusing ticket 003's own
``tests/aprilcam/daemon/test_flatfield_rpc.py::_FakeFlatFieldDaemon``
(a minimal ``Daemon`` double, plain base class derived from, that
answers with canned values) bound to a real ``grpc.Server`` via
``test_service.py``'s ``_bind_stub`` -- the identical "fake gRPC stub"
shape ticket 003's own RPC tests use. The difference here is what
drives the calls: ``client.daemon_client.GrpcDaemon`` (this ticket's
client wrapper), not the bare generated stub -- so these tests exercise
the full client-side proto boundary (``client/models.py``'s
``from_proto`` conversions returning genuine ``aprilcam.types``/
``aprilcam.calibration.interfaces`` dataclasses) and typed-error
reconstruction from gRPC trailing metadata, on top of the wire format
``test_flatfield_rpc.py`` already covers. No real camera, no vision --
``DaemonService`` is never constructed.
"""

from __future__ import annotations

import numpy as np
import pytest

from aprilcam.calibration.interfaces import FlatFieldInfo, FlatFieldResult
from aprilcam.client.daemon_client import GrpcDaemon
from aprilcam.errors import NoFieldMask

from tests.aprilcam.daemon.test_flatfield_rpc import _FakeFlatFieldDaemon
from tests.aprilcam.daemon.test_service import _bind_stub


# ---------------------------------------------------------------------------
# capture_flat_field
# ---------------------------------------------------------------------------


def test_capture_flat_field_success_round_trip_returns_flatfield_result():
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
        client = GrpcDaemon(stub, target="localhost")
        got = client.capture_flat_field("cam1", exposure=-4.0, frames=12, force=False, dry_run=False)

    assert isinstance(got, FlatFieldResult)
    assert got.succeeded is True
    assert got.spread_before == pytest.approx((1.55, 1.40, 1.33))
    assert got.spread_after == pytest.approx((1.03, 1.02, 1.01))
    assert got.reason is None
    assert got.forced_checks == ()
    # gain never crosses the wire -- always None client-side, even
    # though the fake daemon's canned result carried a real array.
    assert got.gain is None

    assert fake.capture_calls == [
        dict(camera="cam1", exposure=-4.0, frames=12, force=False, dry_run=False)
    ]


def test_capture_flat_field_defaults_leave_exposure_and_frames_absent_on_the_wire():
    fake = _FakeFlatFieldDaemon(
        result=FlatFieldResult(succeeded=True, spread_before=(1.1,), spread_after=(1.0,))
    )

    with _bind_stub(fake) as stub:
        client = GrpcDaemon(stub, target="localhost")
        client.capture_flat_field("cam1")

    assert fake.capture_calls == [
        dict(camera="cam1", exposure=None, frames=None, force=False, dry_run=False)
    ]


def test_capture_flat_field_refusal_round_trip_reports_reason_and_measured_spread():
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
        client = GrpcDaemon(stub, target="localhost")
        got = client.capture_flat_field("cam1")

    assert got.succeeded is False
    assert got.reason == "max_spread_after exceeded: 1.90 > 1.20"
    assert got.spread_before == pytest.approx((2.10,))
    assert got.spread_after == pytest.approx((1.90,))


def test_capture_flat_field_force_true_round_trips_forced_checks():
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
        client = GrpcDaemon(stub, target="localhost")
        got = client.capture_flat_field("cam1", force=True)

    assert got.succeeded is True
    assert got.forced_checks == ("max_spread_after",)
    assert fake.capture_calls[0]["force"] is True


def test_capture_flat_field_dry_run_true_is_marshalled_through():
    result = FlatFieldResult(
        succeeded=True,
        spread_before=(1.1,),
        spread_after=(1.02,),
        gain=np.ones((2, 2), dtype=np.float32),
    )
    fake = _FakeFlatFieldDaemon(result=result)

    with _bind_stub(fake) as stub:
        client = GrpcDaemon(stub, target="localhost")
        client.capture_flat_field("cam1", dry_run=True)

    assert fake.capture_calls[0]["dry_run"] is True


def test_capture_flat_field_no_field_mask_reconstructs_the_typed_exception():
    fake = _FakeFlatFieldDaemon(raises=NoFieldMask("cam1"))

    with _bind_stub(fake) as stub:
        client = GrpcDaemon(stub, target="localhost")
        with pytest.raises(NoFieldMask) as excinfo:
            client.capture_flat_field("cam1")

    assert excinfo.value.camera == "cam1"


# ---------------------------------------------------------------------------
# get_flatfield
# ---------------------------------------------------------------------------


def test_get_flatfield_absent_when_never_captured_returns_none():
    fake = _FakeFlatFieldDaemon(info=None)

    with _bind_stub(fake) as stub:
        client = GrpcDaemon(stub, target="localhost")
        got = client.get_flatfield("cam1")

    assert got is None


def test_get_flatfield_present_round_trip_returns_flatfield_info():
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
        client = GrpcDaemon(stub, target="localhost")
        got = client.get_flatfield("cam1")

    assert isinstance(got, FlatFieldInfo)
    assert got.present is True
    assert got.captured_at == pytest.approx(1234.5)
    assert got.shape == (480, 640, 3)
    assert got.gain_min == pytest.approx(0.72)
    assert got.gain_max == pytest.approx(1.31)
    assert got.stale is True


def test_get_flatfield_present_with_no_sidecar_never_fabricates_a_timestamp():
    """A pre-sprint-037 ``flatfield.npy`` with no ``flatfield.json``
    sidecar reports ``present=True, captured_at=None`` -- never a
    fabricated timestamp (``Daemon.get_flatfield``'s own docstring)."""
    info = FlatFieldInfo(present=True, captured_at=None, shape=None, stale=False)
    fake = _FakeFlatFieldDaemon(info=info)

    with _bind_stub(fake) as stub:
        client = GrpcDaemon(stub, target="localhost")
        got = client.get_flatfield("cam1")

    assert got.present is True
    assert got.captured_at is None
    assert got.shape is None


# ---------------------------------------------------------------------------
# clear_flat_field
# ---------------------------------------------------------------------------


def test_clear_flat_field_round_trip_reaches_the_daemon():
    fake = _FakeFlatFieldDaemon()

    with _bind_stub(fake) as stub:
        client = GrpcDaemon(stub, target="localhost")
        result = client.clear_flat_field("cam1")

    assert result is None
    assert fake.cleared == ["cam1"]
