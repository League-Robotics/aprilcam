"""Tests for ``aprilcam.cli._common`` (ticket 023-007's own callout:
``format_camera_row`` is one of the two existing readers ``calibration_stale``
must actually light up for, once ``Calibration.stale`` stops being hardcoded
``False``).

Headless, pure-function: :func:`format_camera_row` takes a
:class:`~aprilcam.types.CameraState` and returns a string -- no daemon, no
discovery, nothing to fake.
"""

from __future__ import annotations

from aprilcam.cli._common import format_camera_row
from aprilcam.types import CameraState


def _state(*, calibration_stale: bool = False, frame_dark: bool = False) -> CameraState:
    return CameraState(
        name="arducam-ov9782-usb-camera",
        present=True,
        usable=True,
        playfield="main-playfield",
        calibrated=True,
        calibration_stale=calibration_stale,
        frame_dark=frame_dark,
        number=3,
    )


def test_format_camera_row_flags_a_stale_calibration():
    row = format_camera_row(_state(calibration_stale=True))

    assert "(stale)" in row


def test_format_camera_row_omits_the_stale_flag_when_not_stale():
    row = format_camera_row(_state(calibration_stale=False))

    assert "stale" not in row


def test_format_camera_row_reports_both_stale_and_dark_together():
    """Both advisory flags are independent -- a camera can be both
    ``calibration_stale`` and ``frame_dark`` at once, and both must show,
    comma-separated, in the same parenthetical."""
    row = format_camera_row(_state(calibration_stale=True, frame_dark=True))

    assert "(stale, dark)" in row
