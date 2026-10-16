"""Round-trip tests for the four intrinsics RPCs (sprint 044 ticket 006):
``GrpcDaemon`` -> real in-process gRPC server -> fake ``Daemon`` returning
canned statuses/finish results or raising service errors."""

from __future__ import annotations

from typing import List, Optional

import pytest

from aprilcam.client.daemon_client import GrpcDaemon
from aprilcam.daemon.interfaces import Daemon
from aprilcam.errors import CameraNotPresent, UnknownCamera
from aprilcam.intrinsics.interfaces import (
    BoardLayout,
    IntrinsicsFinishResult,
    IntrinsicsSessionActive,
    IntrinsicsStatus,
    NoIntrinsicsSession,
)

from tests.aprilcam.daemon.test_service import _bind_stub


def _full_status() -> IntrinsicsStatus:
    return IntrinsicsStatus(
        camera="cam1",
        state="collecting",
        board_spec="5x7:37:28:DICT_5X5_100",
        layout=BoardLayout.CURRENT,
        image_size=(1920, 1080),
        board_visible=True,
        view_count=12,
        tilted_count=5,
        cell_coverage={k: i for i, k in enumerate(("NW", "N", "NE", "W", "C", "E", "SW", "S", "SE"))},
        tilt_coverage={"N": 1, "E": 0, "S": 2, "W": 3},
        rms_px=0.41,
        residual_px=0.52,
        correction_px=3.5,
        fx=1234.5,
        hfov_deg=70.25,
        good=True,
        banner="good_enough",
        next_hint="tilt the board left",
        last_reject="blurry",
        errors=("e1", "e2"),
        started_at=1000.5,
        solved_at=1010.25,
    )


class _FakeIntrinsicsDaemon(Daemon):
    def __init__(self, *, status=None, finish=None, raises: Optional[Exception] = None) -> None:
        self._status = status
        self._finish = finish
        self._raises = raises
        self.calls: List[tuple] = []

    def _go(self, *call):
        self.calls.append(call)
        if self._raises is not None:
            raise self._raises
        return self._status

    def start_intrinsics(self, camera, board=None):
        return self._go("start", camera, board)

    def get_intrinsics_status(self, camera):
        return self._go("status", camera)

    def finish_intrinsics(self, camera, install, force=False):
        self._go("finish", camera, install, force)
        return self._finish

    def abort_intrinsics(self, camera):
        return self._go("abort", camera)


def test_full_status_round_trips_equal_through_every_command():
    status = _full_status()
    finish = IntrinsicsFinishResult(
        installed=True, reason=None, status=status,
        lens={"fx": 1.5, "dist_coeffs": [0.1, -0.2], "plausibility_error": None, "good": True},
    )
    fake = _FakeIntrinsicsDaemon(status=status, finish=finish)
    with _bind_stub(fake) as stub:
        c = GrpcDaemon(stub, target="localhost")
        assert c.start_intrinsics("cam1", "5x7:37:28:DICT_5X5_100") == status
        assert c.get_intrinsics_status("cam1") == status
        assert c.abort_intrinsics("cam1") == status
        assert c.finish_intrinsics("cam1", True, True) == finish
    assert fake.calls[0] == ("start", "cam1", "5x7:37:28:DICT_5X5_100")
    assert fake.calls[3] == ("finish", "cam1", True, True)


def test_nones_and_defaults_preserved():
    status = IntrinsicsStatus(camera="cam1")
    finish = IntrinsicsFinishResult(installed=False, reason="not good enough", status=status, lens=None)
    fake = _FakeIntrinsicsDaemon(status=status, finish=finish)
    with _bind_stub(fake) as stub:
        c = GrpcDaemon(stub, target="localhost")
        got = c.start_intrinsics("cam1")
        assert got == status
        assert got.image_size is None and got.rms_px is None and got.solved_at is None
        assert got.cell_coverage == {} and got.tilt_coverage == {} and got.errors == ()
        assert c.finish_intrinsics("cam1", False) == finish
    assert fake.calls[0] == ("start", "cam1", None)
    assert fake.calls[1] == ("finish", "cam1", False, False)


@pytest.mark.parametrize(
    "exc",
    [
        IntrinsicsSessionActive("cam1"),
        NoIntrinsicsSession("cam1"),
        UnknownCamera("nope", ("cam1",)),
        CameraNotPresent("cam1"),
    ],
)
@pytest.mark.parametrize("op", ["start", "status", "finish", "abort"])
def test_service_errors_reconstructed_as_typed_exceptions(exc, op):
    fake = _FakeIntrinsicsDaemon(raises=exc)
    with _bind_stub(fake) as stub:
        c = GrpcDaemon(stub, target="localhost")
        call = {
            "start": lambda: c.start_intrinsics("cam1"),
            "status": lambda: c.get_intrinsics_status("cam1"),
            "finish": lambda: c.finish_intrinsics("cam1", True),
            "abort": lambda: c.abort_intrinsics("cam1"),
        }[op]
        with pytest.raises(type(exc)) as info:
            call()
    if hasattr(exc, "camera"):
        assert info.value.camera == "cam1"


@pytest.mark.parametrize(
    "outline",
    [
        None,
        (((1.5, 2.0), (30.25, 4.0), (20.0, 40.75)),),
        (
            ((1.5, 2.0), (30.25, 4.0), (20.0, 40.75)),
            ((100.0, 100.0), (200.0, 100.0), (200.0, 200.0), (100.0, 200.0)),
        ),
    ],
)
def test_board_outline_round_trips(outline):
    status = IntrinsicsStatus(camera="cam1", board_visible=outline is not None,
                              board_outline=outline)
    fake = _FakeIntrinsicsDaemon(status=status)
    with _bind_stub(fake) as stub:
        got = GrpcDaemon(stub, target="localhost").get_intrinsics_status("cam1")
    assert got == status
    assert got.board_outline == outline
