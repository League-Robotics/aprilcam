"""Tests for ``aprilcam.cli._common`` (ticket 023-007's own callout:
``format_camera_row`` is one of the two existing readers ``calibration_stale``
must actually light up for, once ``Calibration.stale`` stops being hardcoded
``False``).

Headless, pure-function: :func:`format_camera_row` takes a
:class:`~aprilcam.types.CameraState` and returns a string -- no daemon, no
discovery, nothing to fake.
"""

from __future__ import annotations

from aprilcam.cli._common import fan_out, format_camera_row
from aprilcam.client.interfaces import DaemonInfo
from aprilcam.errors import ConnectionFailed
from aprilcam.types import CameraState


def _state(
    *,
    calibration_stale: bool = False,
    frame_dark: bool = False,
    flatfield_present: bool = False,
    flatfield_stale: bool = False,
    lens_model: str = "absent",
    lens_pending: bool = False,
    present: bool = True,
    usable: bool = True,
    closed: bool = False,
) -> CameraState:
    return CameraState(
        name="arducam-ov9782-usb-camera",
        present=present,
        usable=usable,
        closed=closed,
        playfield="main-playfield",
        calibrated=True,
        calibration_stale=calibration_stale,
        frame_dark=frame_dark,
        flatfield_present=flatfield_present,
        flatfield_stale=flatfield_stale,
        lens_model=lens_model,
        lens_pending=lens_pending,
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


# ---------------------------------------------------------------------------
# Lens provenance (sprint 044 ticket 010)
# ---------------------------------------------------------------------------


def test_format_camera_row_shows_the_lens_word_and_keeps_the_prefix():
    plain = format_camera_row(_state())
    for model in ("measured", "estimated", "legacy", "absent"):
        row = format_camera_row(_state(lens_model=model))
        assert f"lens={model}" in row
        assert "lens-pending" not in row
        # Everything up to the flat-field word is unchanged.
        assert row.split("flatfield=")[0] == plain.split("flatfield=")[0]
        # The lens word sits before the trailing flags.
        assert row.index(f"lens={model}") > row.index("flatfield=")


def test_format_camera_row_flags_lens_pending_in_the_trailing_flags():
    row = format_camera_row(_state(lens_model="measured", lens_pending=True, calibration_stale=True))
    assert row.endswith("(stale, lens-pending)")
    assert row.index("lens=measured") < row.index("(stale")


def test_format_camera_row_shows_pose_method_and_confidence():
    state = _state()
    from dataclasses import replace

    row = format_camera_row(replace(state, calibration_method="pose", calibration_confidence="full"))
    assert "calibrated (pose, full)" in row
    row = format_camera_row(replace(state, calibration_method="centre-point", calibration_confidence=""))
    assert "calibrated (centre-point)" in row
    assert row.startswith("[3]")
    assert "calibrated  flatfield" in format_camera_row(state)


def test_format_camera_row_shows_closed_distinct_from_absent():
    closed = format_camera_row(_state(present=True, closed=True))
    absent = format_camera_row(_state(present=False, closed=False, usable=False))
    assert " closed " in closed and " absent " not in closed
    assert " absent " in absent and " closed " not in absent


# ---------------------------------------------------------------------------
# fan_out() labels -- sprint 048 ticket 001: host names, not addresses
# ---------------------------------------------------------------------------


class _FakeDiscovery:
    def __init__(self, infos, dead=()):
        self._infos = infos
        self._dead = set(dead)

    def find_all(self):
        return self._infos

    def connect(self, host=None):
        if host in self._dead:
            raise ConnectionFailed(f"cannot reach {host}")
        return object()


def _fan(infos, dead=()):
    labels = []
    code = fan_out(_FakeDiscovery(infos, dead), lambda d: 1, lambda label, r: labels.append(label))
    return code, labels


def test_fan_out_labels_use_host_names(capsys):
    infos = (
        DaemonInfo(host="localhost", port=0, is_local=True, reachable=True, name="gala"),
        DaemonInfo(host="10.0.0.2:5280", port=5280, is_local=False, reachable=True, name="buzzkill"),
        DaemonInfo(host="10.0.0.3:5280", port=5280, is_local=False, reachable=True, name="donald"),
    )

    code, labels = _fan(infos)

    assert code == 0
    assert labels == ["gala (local)", "buzzkill", "donald"]


def test_fan_out_nameless_daemon_falls_back_to_address_label():
    infos = (
        DaemonInfo(host="localhost", port=0, is_local=True, reachable=True),
        DaemonInfo(host="10.0.0.2:5280", port=5280, is_local=False, reachable=True),
    )

    _, labels = _fan(infos)

    assert labels == ["localhost (local)", "10.0.0.2:5280"]


def test_fan_out_unreachable_line_uses_the_host_name(capsys):
    infos = (
        DaemonInfo(host="10.0.0.2:5280", port=5280, is_local=False, reachable=False, name="buzzkill"),
    )

    code, labels = _fan(infos, dead=["10.0.0.2:5280"])

    assert code == 1
    assert labels == []
    assert capsys.readouterr().err.startswith("buzzkill: unreachable")
