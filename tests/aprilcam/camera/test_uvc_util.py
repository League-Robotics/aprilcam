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

from aprilcam.camera.backends.uvc_util import _MAX_SET_ATTEMPTS, UvcUtilBackend


def _completed(*, returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Ticket 030-005 (reopened): ``set_property`` now retries a raw CLI
    rejection with a brief backoff (``time.sleep``). This module is
    otherwise fully headless/instant (``subprocess.run`` mocked
    throughout, per the module's own stated convention) -- keep it that
    way rather than paying real wall-clock time whenever a test
    deliberately exercises the retry path."""
    monkeypatch.setattr("aprilcam.camera.backends.uvc_util.time.sleep", lambda seconds: None)


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


# ---------------------------------------------------------------------------
# set_property retry -- ticket 030-005 (reopened). A raw ``-s`` rejection
# (nonzero exit) is retried, bounded; a readback mismatch after an
# *accepted* write is not -- see the module docstring's "Bounded retry"
# section for why that split is deliberate.
# ---------------------------------------------------------------------------


def test_set_property_retries_a_raw_cli_rejection_and_succeeds():
    """Real-hardware reproductions of the actual bug this ticket
    investigates (``uvc-util -s ... exited 13``) succeeded on an
    immediate next attempt every time it was observed live -- this pins
    that exact recovery path headlessly."""
    backend = UvcUtilBackend("cam", binary="/bin/uvc-util")
    original = _completed(stdout="exposure-time-abs {\n  current-value: 157\n}\n")
    rejected = _completed(returncode=13, stderr="unable to write new value")
    accepted = _completed()
    readback = _completed(stdout="exposure-time-abs {\n  current-value: 6\n}\n")
    with patch(
        "aprilcam.camera.backends.uvc_util.subprocess.run",
        side_effect=[original, rejected, accepted, readback],
    ) as run:
        ok = backend.set_property("exposure", 6)
    assert ok is True
    # snapshot, rejected attempt, accepted attempt, readback -- one retry.
    assert run.call_count == 4


def test_set_property_gives_up_after_max_attempts_on_persistent_raw_rejection():
    """A control that is never going to accept this write (every raw
    attempt rejected) still fails, bounded -- exactly
    ``_MAX_SET_ATTEMPTS`` raw attempts, never an unbounded spin, and no
    readback call is ever reached since the raw write never succeeded."""
    backend = UvcUtilBackend("cam", binary="/bin/uvc-util")
    original = _completed(stdout="exposure-time-abs {\n  current-value: 157\n}\n")
    rejected = _completed(returncode=13, stderr="unable to write new value")
    with patch(
        "aprilcam.camera.backends.uvc_util.subprocess.run",
        side_effect=[original] + [rejected] * _MAX_SET_ATTEMPTS,
    ) as run:
        ok = backend.set_property("exposure", 6)
    assert ok is False
    assert run.call_count == 1 + _MAX_SET_ATTEMPTS  # snapshot + every raw attempt, no more


def test_set_property_does_not_retry_an_accepted_write_that_reads_back_wrong():
    """The mismatch-repair path (a write the CLI *accepted* but that
    then reads back wrong, e.g. a silently clamped out-of-range value)
    is a different, non-transient failure mode and must not be retried
    -- exactly 4 calls (snapshot, accepted ``-s``, readback, repair),
    never more, even though ``set_property`` now has retry logic for
    the raw-rejection path."""
    backend = UvcUtilBackend("cam", binary="/bin/uvc-util")
    original = _completed(stdout="exposure-time-abs {\n  current-value: 6\n}\n")
    clamped_readback = _completed(stdout="exposure-time-abs {\n  current-value: 5000\n}\n")
    with patch(
        "aprilcam.camera.backends.uvc_util.subprocess.run",
        side_effect=[original, _completed(), clamped_readback, _completed()],
    ) as run:
        ok = backend.set_property("exposure", 99999)
    assert ok is False
    assert run.call_count == 4


# ---------------------------------------------------------------------------
# describe_set_failure -- ticket 030-005's real-cause diagnostic. Real
# hardware investigation (both this project's real cameras) ruled out
# all four candidates the issue named for the actual reported failure
# (test-fixture gap, gate-value semantics, out-of-range value, control
# genuinely absent) and found it did not reproduce on repeated identical
# live attempts -- consistent with a transient hardware/driver
# condition. These tests pin the two causes this method *can* determine
# from live hardware (out of range; gate control not actually manual)
# plus its honest fallback when neither applies, headlessly (subprocess
# mocked throughout, matching this module's own stated convention).
# ---------------------------------------------------------------------------


def test_describe_set_failure_unknown_control_returns_none_without_invoking_subprocess():
    backend = UvcUtilBackend("cam", binary="/bin/uvc-util")
    with patch("aprilcam.camera.backends.uvc_util.subprocess.run") as run:
        assert backend.describe_set_failure("not_a_real_key", 1) is None
    run.assert_not_called()


def test_describe_set_failure_reports_out_of_range_value():
    backend = UvcUtilBackend("cam", binary="/bin/uvc-util")
    range_stdout = (
        "exposure-time-abs {\n"
        "  minimum: 1\n"
        "  maximum: 10000\n"
        "  current-value: 300\n"
        "}\n"
    )
    with patch(
        "aprilcam.camera.backends.uvc_util.subprocess.run",
        return_value=_completed(stdout=range_stdout),
    ) as run:
        reason = backend.describe_set_failure("exposure", 999999)
    assert reason is not None
    assert "999999" in reason
    assert "1..10000" in reason
    run.assert_called_once()  # short-circuits before the gate check


def test_describe_set_failure_reports_gate_control_not_manual():
    backend = UvcUtilBackend("cam", binary="/bin/uvc-util")
    range_stdout = (
        "exposure-time-abs {\n  minimum: 1\n  maximum: 10000\n  current-value: 300\n}\n"
    )
    gate_stdout = "auto-exposure-mode {\n  current-value: 8\n}\n"
    with patch(
        "aprilcam.camera.backends.uvc_util.subprocess.run",
        side_effect=[_completed(stdout=range_stdout), _completed(stdout=gate_stdout)],
    ) as run:
        reason = backend.describe_set_failure("exposure", 300)
    assert reason is not None
    assert "auto_exposure" in reason
    assert "8" in reason
    assert run.call_count == 2


def test_describe_set_failure_falls_back_to_transient_when_range_and_gate_are_both_fine():
    """The exact shape of ticket 030-005's own real finding: the value
    is in range and the gate control already reads back as manual, so
    neither determinable cause applies -- the method says so honestly
    rather than inventing a category or silently returning ``None``."""
    backend = UvcUtilBackend("cam", binary="/bin/uvc-util")
    range_stdout = (
        "exposure-time-abs {\n  minimum: 1\n  maximum: 10000\n  current-value: 300\n}\n"
    )
    gate_stdout = "auto-exposure-mode {\n  current-value: 1\n}\n"
    with patch(
        "aprilcam.camera.backends.uvc_util.subprocess.run",
        side_effect=[_completed(stdout=range_stdout), _completed(stdout=gate_stdout)],
    ):
        reason = backend.describe_set_failure("exposure", 300)
    assert reason is not None
    assert "transient" in reason
    assert "range" in reason


def test_describe_set_failure_ungated_control_in_range_skips_the_gate_check():
    """``gain`` has no gate in ``_GATE_FOR`` -- only the range call
    should ever be made."""
    backend = UvcUtilBackend("cam", binary="/bin/uvc-util")
    range_stdout = "gain {\n  minimum: 0\n  maximum: 255\n  current-value: 10\n}\n"
    with patch(
        "aprilcam.camera.backends.uvc_util.subprocess.run",
        return_value=_completed(stdout=range_stdout),
    ) as run:
        reason = backend.describe_set_failure("gain", 10)
    assert reason is not None
    assert "transient" in reason
    run.assert_called_once()
