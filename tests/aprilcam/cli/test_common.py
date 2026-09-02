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


def _state(
    *,
    calibration_stale: bool = False,
    frame_dark: bool = False,
    flatfield_present: bool = False,
    flatfield_stale: bool = False,
) -> CameraState:
    return CameraState(
        name="arducam-ov9782-usb-camera",
        present=True,
        usable=True,
        playfield="main-playfield",
        calibrated=True,
        calibration_stale=calibration_stale,
        frame_dark=frame_dark,
        flatfield_present=flatfield_present,
        flatfield_stale=flatfield_stale,
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


def test_format_camera_row_shows_flatfield_present():
    row = format_camera_row(_state(flatfield_present=True))

    assert "flatfield=present" in row


def test_format_camera_row_shows_flatfield_absent_by_default():
    row = format_camera_row(_state(flatfield_present=False))

    assert "flatfield=absent" in row


def test_format_camera_row_flags_a_stale_flatfield():
    row = format_camera_row(_state(flatfield_present=True, flatfield_stale=True))

    assert "flatfield-stale" in row


def test_format_camera_row_omits_the_flatfield_stale_flag_when_not_stale():
    row = format_camera_row(_state(flatfield_present=True, flatfield_stale=False))

    assert "flatfield-stale" not in row


def test_format_camera_row_keeps_calibration_stale_and_flatfield_stale_distinct():
    """The two staleness signals are independent and must never be
    conflated into the same word -- calibration's own ``stale`` flag
    must not fire for a stale flat-field map, and vice versa."""
    row = format_camera_row(_state(calibration_stale=False, flatfield_present=True, flatfield_stale=True))

    assert "flatfield-stale" in row
    # the bare "stale" flag (calibration's own) must not have fired
    assert "(stale)" not in row


def test_format_camera_row_reports_calibration_stale_and_flatfield_stale_together():
    row = format_camera_row(
        _state(calibration_stale=True, flatfield_present=True, flatfield_stale=True)
    )

    assert "(stale, flatfield-stale)" in row
