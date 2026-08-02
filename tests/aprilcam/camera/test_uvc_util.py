"""Tests for ``aprilcam.camera.backends.uvc_util`` (ticket 023-001).

Fully headless: ``subprocess.run`` is mocked throughout -- no real
``uvc-util`` invocation. What matters here is (1) the binary is resolved
via ``PATH`` and construction fails loudly, never silently, when it is
not found; (2) every invocation is an argument list with ``shell=False``,
never a shell string; (3) ``set_property`` never trusts its own
"succeeded" signal -- it reads the control back and only reports success
if the hardware actually reports the requested value, exactly the
UC-03/``UnsupportedSetting`` discipline applied at this backend's
boundary. ``@pytest.mark.camera`` coverage against the real ``uvc-util``
binary on camera 3 lives in ``test_camera.py``'s real-hardware section
(exercised through ``Camera.apply_config``/``DaemonService.
set_camera_config``, not this backend in isolation).
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from aprilcam.camera.backends.uvc_util import UvcUtilBackend


def _completed(*, returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# construction -- fail loudly, never silently, when uvc-util is missing
# ---------------------------------------------------------------------------


def test_constructor_raises_when_binary_not_found():
    with patch("aprilcam.camera.backends.uvc_util.shutil.which", return_value=None):
        with pytest.raises(RuntimeError, match="uvc-util not found"):
            UvcUtilBackend("Arducam OV9782 USB Camera")


def test_constructor_resolves_binary_via_path_when_not_given():
    with patch(
        "aprilcam.camera.backends.uvc_util.shutil.which", return_value="/usr/local/bin/uvc-util"
    ) as which:
        backend = UvcUtilBackend("Arducam OV9782 USB Camera")
    which.assert_called_once_with("uvc-util")
    assert backend._binary == "/usr/local/bin/uvc-util"


def test_constructor_accepts_an_explicit_binary_without_calling_which():
    with patch("aprilcam.camera.backends.uvc_util.shutil.which") as which:
        backend = UvcUtilBackend("Arducam OV9782 USB Camera", binary="/opt/bin/uvc-util")
    which.assert_not_called()
    assert backend._binary == "/opt/bin/uvc-util"


# ---------------------------------------------------------------------------
# settable_names -- the five controls validated live (ticket 023-001)
# ---------------------------------------------------------------------------


def test_settable_names_is_the_five_validated_controls():
    backend = UvcUtilBackend("cam", binary="/bin/uvc-util")
    assert backend.settable_names() == {
        "auto_exposure",
        "exposure",
        "gain",
        "auto_white_balance",
        "white_balance_temperature",
    }


# ---------------------------------------------------------------------------
# get_property -- parses "current-value: X", argument list, no shell
# ---------------------------------------------------------------------------


def test_get_property_parses_numeric_current_value():
    backend = UvcUtilBackend("Arducam OV9782 USB Camera", binary="/bin/uvc-util")
    stdout = "exposure-time-abs {\n  current-value: 6\n}\n"
    with patch(
        "aprilcam.camera.backends.uvc_util.subprocess.run", return_value=_completed(stdout=stdout)
    ) as run:
        value = backend.get_property("exposure")
    assert value == 6.0
    args, kwargs = run.call_args
    assert args[0] == ["/bin/uvc-util", "-N", "Arducam OV9782 USB Camera", "-S", "exposure-time-abs"]
    assert kwargs["shell"] is False


def test_get_property_parses_boolean_current_value():
    backend = UvcUtilBackend("cam", binary="/bin/uvc-util")
    stdout = "auto-white-balance-temp {\n  current-value: false\n}\n"
    with patch("aprilcam.camera.backends.uvc_util.subprocess.run", return_value=_completed(stdout=stdout)):
        assert backend.get_property("auto_white_balance") == 0.0

    stdout_true = "auto-white-balance-temp {\n  current-value: true\n}\n"
    with patch(
        "aprilcam.camera.backends.uvc_util.subprocess.run", return_value=_completed(stdout=stdout_true)
    ):
        assert backend.get_property("auto_white_balance") == 1.0


def test_get_property_unknown_key_returns_none_without_invoking_subprocess():
    backend = UvcUtilBackend("cam", binary="/bin/uvc-util")
    with patch("aprilcam.camera.backends.uvc_util.subprocess.run") as run:
        assert backend.get_property("not_a_real_key") is None
    run.assert_not_called()


def test_get_property_nonzero_returncode_returns_none():
    backend = UvcUtilBackend("cam", binary="/bin/uvc-util")
    with patch(
        "aprilcam.camera.backends.uvc_util.subprocess.run",
        return_value=_completed(returncode=1, stderr="no such device"),
    ):
        assert backend.get_property("gain") is None


def test_get_property_subprocess_timeout_returns_none():
    backend = UvcUtilBackend("cam", binary="/bin/uvc-util")
    with patch(
        "aprilcam.camera.backends.uvc_util.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="uvc-util", timeout=5.0),
    ):
        assert backend.get_property("gain") is None


# ---------------------------------------------------------------------------
# set_property -- argument list, no shell, never trusts its own write
# ---------------------------------------------------------------------------


def test_set_property_invokes_argument_list_never_a_shell_string():
    """Call sequence for a successful set: snapshot the pre-call value
    (``-S``), write it (``-s``), read it back (``-S``) -- three calls,
    all argument lists, ``shell=False``."""
    backend = UvcUtilBackend("Arducam OV9782 USB Camera", binary="/bin/uvc-util")
    original = _completed(stdout="exposure-time-abs {\n  current-value: 157\n}\n")
    readback = _completed(stdout="exposure-time-abs {\n  current-value: 6\n}\n")
    with patch(
        "aprilcam.camera.backends.uvc_util.subprocess.run",
        side_effect=[original, _completed(), readback],
    ) as run:
        ok = backend.set_property("exposure", 6)
    assert ok is True
    set_call = run.call_args_list[1]
    assert set_call.args[0] == [
        "/bin/uvc-util",
        "-N",
        "Arducam OV9782 USB Camera",
        "-s",
        "exposure-time-abs=6",
    ]
    for call in run.call_args_list:
        assert call.kwargs["shell"] is False


def test_set_property_boolean_control_formats_true_false():
    backend = UvcUtilBackend("cam", binary="/bin/uvc-util")
    original = _completed(stdout="auto-white-balance-temp {\n  current-value: true\n}\n")
    readback = _completed(stdout="auto-white-balance-temp {\n  current-value: false\n}\n")
    with patch(
        "aprilcam.camera.backends.uvc_util.subprocess.run",
        side_effect=[original, _completed(), readback],
    ) as run:
        ok = backend.set_property("auto_white_balance", 0)
    assert ok is True
    assert run.call_args_list[1].args[0][-1] == "auto-white-balance-temp=false"


def test_set_property_returns_false_when_the_set_call_itself_fails():
    backend = UvcUtilBackend("cam", binary="/bin/uvc-util")
    with patch(
        "aprilcam.camera.backends.uvc_util.subprocess.run",
        return_value=_completed(returncode=1, stderr="invalid value"),
    ):
        assert backend.set_property("exposure", 6) is False


def test_set_property_never_trusts_its_own_write_readback_mismatch():
    """UC-03/``UnsupportedSetting``'s "must never report success for a
    setting that did not take effect", enforced here, not only by the
    caller: the ``-s`` call itself reports success (returncode 0) but
    the readback disagrees -- ``set_property`` must still return
    ``False``."""
    backend = UvcUtilBackend("cam", binary="/bin/uvc-util")
    original = _completed(stdout="exposure-time-abs {\n  current-value: 157\n}\n")
    readback_unchanged = _completed(stdout="exposure-time-abs {\n  current-value: 157\n}\n")
    with patch(
        "aprilcam.camera.backends.uvc_util.subprocess.run",
        side_effect=[original, _completed(returncode=0), readback_unchanged, _completed()],
    ):
        assert backend.set_property("exposure", 6) is False


def test_set_property_mismatch_repairs_the_hardware_back_to_its_prior_value():
    """Real-hardware finding (ticket 023-001, live against camera 3):
    ``uvc-util`` can report success while silently *clamping* an
    out-of-range request (e.g. ``exposure-time-abs=99999``, max 5000,
    lands at ``5000``) instead of rejecting it -- leaving the hardware
    changed even though this method must report failure. A bare
    verify-and-reject would leave the camera at the clamped value, not
    its pre-push value -- exactly the half-configured state UC-03
    forbids. This backend must issue a **fourth** call, writing the
    pre-call value straight back, before returning ``False``."""
    backend = UvcUtilBackend("Arducam OV9782 USB Camera", binary="/bin/uvc-util")
    original = _completed(stdout="exposure-time-abs {\n  current-value: 6\n}\n")
    clamped_readback = _completed(stdout="exposure-time-abs {\n  current-value: 5000\n}\n")
    with patch(
        "aprilcam.camera.backends.uvc_util.subprocess.run",
        side_effect=[original, _completed(), clamped_readback, _completed()],
    ) as run:
        ok = backend.set_property("exposure", 99999)
    assert ok is False
    assert len(run.call_args_list) == 4
    repair_call = run.call_args_list[3]
    assert repair_call.args[0] == [
        "/bin/uvc-util",
        "-N",
        "Arducam OV9782 USB Camera",
        "-s",
        "exposure-time-abs=6",
    ]
    assert repair_call.kwargs["shell"] is False


def test_set_property_readback_within_tolerance_still_succeeds():
    """``uvc-util`` may snap a requested value to the nearest valid
    step -- a readback close to (not exactly) the requested value is
    still accepted."""
    backend = UvcUtilBackend("cam", binary="/bin/uvc-util")
    original = _completed(stdout="gain {\n  current-value: 0\n}\n")
    readback_close = _completed(stdout="gain {\n  current-value: 11\n}\n")
    with patch(
        "aprilcam.camera.backends.uvc_util.subprocess.run",
        side_effect=[original, _completed(returncode=0), readback_close],
    ):
        assert backend.set_property("gain", 10) is True


def test_set_property_unknown_key_returns_false_without_invoking_subprocess():
    backend = UvcUtilBackend("cam", binary="/bin/uvc-util")
    with patch("aprilcam.camera.backends.uvc_util.subprocess.run") as run:
        assert backend.set_property("not_a_real_key", 1) is False
    run.assert_not_called()


def test_set_property_subprocess_timeout_returns_false():
    backend = UvcUtilBackend("cam", binary="/bin/uvc-util")
    with patch(
        "aprilcam.camera.backends.uvc_util.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="uvc-util", timeout=5.0),
    ):
        assert backend.set_property("gain", 5) is False
