"""Hardware verification for sprint 029's camera geometry / real parallax
correction (ticket 029-004, SUC-001, SUC-003, UC-29) -- against whichever
attached camera is genuinely configured and calibrated against the
physical ``main-playfield`` (discovered at run time via
``tests/aprilcam/hardware_camera.py``, never a hardcoded device name --
see that module's docstring for why), the same ground truth
``test_calibration_hardware.py`` and ``test_mobile_tags_hardware.py`` use.
Historically that camera has been named ``arducam-ov9782-usb-camera`` on
the stakeholder's Mac (see the real geometry constants below, which are
true of *that physical mount*, not of any device name), but this module
never assumes so.

**Rescope, recorded here for anyone reading this module cold.** This
ticket's own text (``clasi/sprints/029-.../tickets/004-...md``) assumes
AprilTags 14 and 15 are still staged on stands at the surveyed
``dot-northeast-orange``/``dot-southwest-orange`` positions so
``aprilcam camera locate`` can be re-run live during this ticket's
execution. The stakeholder removed both stands before this ticket ran
and confirmed they are not needed again. **Two specific checks this
module cannot perform as a result, and does not fake**:

1. A *fresh* end-to-end ``camera locate`` run against live-detected
   tags 14/15 (no elevated AprilTag reference is currently staged on the
   field). ``test_locate_reports_missing_references_or_reproduces_the_documented_fixture``
   below still genuinely attempts this against the real, currently-running
   production daemon (read-only, ``--dry-run``) and records whichever
   real outcome results -- a clean, honest skip naming the missing
   references if they are absent, or a real pass if they are somehow
   present.
2. Observing a genuinely, physically elevated tag's corrected position
   change live. The mechanism-proof test below
   (``test_mount_z_is_a_noop_until_camera_position_is_pushed_then_engages_with_no_restart``)
   demonstrates the same correction *code path* engaging live, on real
   detection, with no daemon restart -- but it registers a **synthetic**
   ``mount_z`` on one of the field's flat ArUco corner markers (always in
   frame), not a genuinely elevated tag, since none is currently staged.
   It proves the plumbing; it does not re-derive or re-check ground
   truth.

**The issue's own Verification bullet is not re-derived here.** The
original issue (since sprint 025) asked for: "register a mount with a
nonzero mount_z for a tag at a known height and known offset from the
camera's optical axis; the corrected world position matches a
hand-measured expectation." That check was performed for real, on this
exact camera/playfield, *before* the reference stands were removed:
tag 15 at ``dot-northeast-orange`` (surveyed (50, 30) cm, stand height
17.2 cm) read raw at (57.38, 34.90) cm -- 8.86 cm of real, measured
parallax error; tag 14 at ``dot-southwest-orange`` (surveyed (-50, -30)
cm, stand height 15.0 cm) read raw at (-57.15, -33.40) cm -- 7.92 cm of
error. ``calibration.geometry.solve_camera_geometry`` (ticket 002), run
on that real data, recovered nadir (3.057, -2.799) cm, height 128.230
cm; applying ``project_tag_to_field_plane`` with that solved geometry to
the same two raw readings brings both to within ~0.2 cm of their
surveyed ground truth ((50.093, 29.843) and (-50.107, -29.820) --
0.182 cm and 0.209 cm residual). That real result is permanently pinned
by ``tests/aprilcam/calibration/test_geometry.py::
test_solve_camera_geometry_real_fixture_correction_matches_documented_errors``
(headless -- the fixture *is* that real hardware data) and by the real
per-camera config this sprint's ``camera locate`` run actually wrote to
``~/.config/aprilcam/cameras/arducam-ov9782-usb-camera/config.json``
(``camera_position.height_cm=128.22990496814816``,
``nadir_x_cm=3.0570142285481654``, ``nadir_y_cm=-2.798700632324644`` --
read and confirmed live via ``aprilcam camera config
arducam-ov9782-usb-camera --dump`` as part of ticket 004's own
completion notes). This module adds no test that re-measures that
result, because doing so honestly requires the removed stands.

Every test is ``@pytest.mark.camera`` and skips cleanly (module-wide,
via ``_skip_if_arducam_not_attached``) on a machine without that camera
attached -- ``uv run pytest -m "not camera"`` must be unaffected.
Helper functions are duplicated locally rather than imported from
``test_mobile_tags_hardware.py``, matching every other
``@pytest.mark.camera`` module's own stated precedent of defining its
own copy.

**Ticket 030-005: a third guard, and a deliberate non-isolation
decision for the locate-reproduction test.**
``test_locate_reports_missing_references_or_reproduces_the_documented_fixture``
originally guarded two of its three real preconditions -- camera
attached, reference tags present -- and fell through to a hard
``no local daemon answered`` failure when nothing was listening on the
local control socket, instead of skipping like the other two gaps. It
now also calls ``_skip_if_no_local_daemon_reachable()``, an in-process
probe of the same shape as ``_skip_if_arducam_not_attached``.

Unlike ``test_mount_z_is_a_noop_until_camera_position_is_pushed_then_engages_with_no_restart``
below (which stands up its own isolated, self-cleaning daemon on a temp
``socket_dir`` via the ``cli_env`` fixture's ``_short_tmp_dir``
machinery), the locate-reproduction test deliberately does **not** get
that treatment. Its entire purpose is reproducing the documented
fixture against the *real* production daemon's real, currently-held
``camera_position``/calibration state (``REAL_NADIR_X_CM`` etc., sourced
from what ``camera locate`` actually wrote to
``~/.config/aprilcam/cameras/arducam-ov9782-usb-camera/config.json`` --
see above). An isolated daemon on a fresh temp ``socket_dir`` would
start with none of that state, so it could only ever hit this test's
"references not staged" skip path, never the real reproduction path
that recovers ``nadir``/``height``/``rms_residual_cm`` and checks them
against the documented tolerance. Self-containment here would trade a
real (if currently mostly-skipped) hardware check for a permanently
vacuous one -- the wrong trade for a test whose whole point is trusting
the production daemon's own state. This test therefore still requires a
real local daemon to already be running (read-only, ``--dry-run``,
never writes) -- the new guard makes that requirement skip cleanly
instead of failing hard when unmet, it does not remove it.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

import pytest

from aprilcam.client.discovery import probe_socket_reachable
from aprilcam.config.directories import Directories
from aprilcam.daemon.lifecycle import SOCKET_FILENAME
from tests.aprilcam.daemon.mount_z_math import (
    REAL_HEIGHT_CM,
    REAL_NADIR_X_CM,
    REAL_NADIR_Y_CM,
    expected_parallax_corrected_position,
)
from tests.aprilcam.hardware_camera import resolve_hardware_camera_slug

pytestmark = pytest.mark.camera

#: Resolved lazily, once per test, by :func:`_skip_if_arducam_not_attached`
#: against this machine's real, persisted system state (see
#: tests/aprilcam/hardware_camera.py) -- never hardcoded. This is the same
#: physical rig ticket 003/029-004 measured (the real geometry constants
#: below are true of *that mount*, not of any particular device name it
#: happens to enumerate under), discovered by "which attached camera is
#: configured+calibrated against main-playfield" instead of by a fixed
#: slug string, so a device-name change (this project has already seen
#: one) does not silently point this module at the wrong camera.
ARDUCAM_SLUG: str = ""

#: The real, checked-in playfield definition this module pushes -- the
#: same file ``test_calibration_hardware.py``/``test_mobile_tags_hardware.py``
#: name as ground truth.
MAIN_PLAYFIELD_JSON = (
    Path(__file__).resolve().parents[3] / "config" / "aprilcam-old" / "playfields" / "main-playfield.json"
)

#: The real fixture (sprint.md Problem section; ticket 002's own pinned
#: regression; the real value ``camera locate`` actually wrote to
#: ``~/.config/aprilcam`` before the reference stands were removed --
#: confirmed live, read-only, as part of this ticket's completion notes).
#: Sprint 035 ticket 011: moved to ``tests/aprilcam/daemon/
#: mount_z_math.py`` so this module and every other real-hardware
#: mount_z test import the exact same values rather than each
#: redeclaring them -- re-exported here (still ``REAL_NADIR_X_CM`` etc.
#: in this module's own namespace) so nothing below needed to change.


def _skip_if_arducam_not_attached() -> None:
    global ARDUCAM_SLUG
    ARDUCAM_SLUG = resolve_hardware_camera_slug()


def _skip_if_cli_camera_open_failed(result: subprocess.CompletedProcess) -> None:
    """``camera probe``/``camera config`` (above) only prove the camera
    is enumerated and configured -- not that this isolated daemon
    subprocess can actually open it right now (tests/aprilcam/
    hardware_camera.py's own "enumeration is not availability" contract,
    docs/knowledge/daemon-camera-grant-and-detection-priming.md). Only
    the specific, known "failed to open camera" signature is treated as
    environmental; any other non-zero exit still fails the calling
    assertion for real."""
    if result.returncode != 0 and b"failed to open camera" in result.stderr:
        pytest.skip(
            f"{ARDUCAM_SLUG!r} qualifies but the isolated daemon could not "
            f"open it just now -- {result.stderr.decode(errors='replace').strip()}. "
            f"Expected when this process has no camera TCC grant (e.g. an "
            f"agent session) or the device is genuinely busy elsewhere; "
            f"re-run from an operator Terminal session to exercise this "
            f"test for real."
        )


def _skip_if_no_local_daemon_reachable() -> None:
    """Third guard, same shape as :func:`_skip_if_arducam_not_attached`
    -- an in-process, sub-second probe of the well-known local control
    socket (:data:`~aprilcam.daemon.lifecycle.SOCKET_FILENAME` under
    ``Directories().socket_dir``), not a subprocess round trip. ``camera
    locate`` drives detection through the daemon's ``GetTag`` RPC (the
    daemon is the sole vision authority), so with no local daemon
    reachable the CLI would exit before it can ever reach the "not
    detected" path the other guard depends on -- the bug this guard
    exists to fix (issue: ``no local daemon answered`` surfacing as a
    hard ``assert 1 == 0`` instead of a clean skip)."""
    directories = Directories()
    socket_path = directories.socket_dir / SOCKET_FILENAME
    if not probe_socket_reachable(socket_path):
        pytest.skip(
            f"no local aprilcam daemon reachable at {socket_path} -- "
            "this test drives `camera locate` through the daemon's "
            "GetTag RPC and needs a real daemon already running "
            "(`aprilcam daemon start`)"
        )


def _short_tmp_dir(prefix: str) -> Path:
    """A short, top-level ``/tmp`` directory -- unix domain sockets have
    a ~103-character path limit on macOS/Linux, and pytest's
    ``tmp_path`` nests too deeply (matches every other
    ``@pytest.mark.camera`` module's identically-named workaround)."""
    return Path(tempfile.mkdtemp(prefix=prefix, dir="/tmp"))


def _aprilcam_bin() -> Path:
    candidate = Path(sys.executable).parent / "aprilcam"
    if not candidate.exists():  # pragma: no cover - environment-dependent
        pytest.skip(f"no aprilcam console script next to {sys.executable}")
    return candidate


def _run(bin_path: Path, *args: str, env: Dict[str, str], timeout: float = 30.0):
    return subprocess.run([str(bin_path), *args], capture_output=True, env=env, timeout=timeout)


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """Isolated environment for a real ``aprilcam`` subprocess -- its own
    private daemon, its own ``config``/``state`` dirs, so this module
    never touches the stakeholder's real ``~/.config/aprilcam`` or
    ``~/.local/share/aprilcam`` (matching ``test_mobile_tags_hardware.py``'s
    own precedent exactly)."""
    short_dir = _short_tmp_dir("ac-geom-cli-")
    try:
        monkeypatch.setenv("APRILCAM_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("APRILCAM_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(short_dir))

        directories = Directories()
        if directories.socket_dir != short_dir / "aprilcam":
            pytest.skip(
                "this sandbox's /run/aprilcam is available and writable, "
                "so socket_dir cannot be isolated down to a private tmp "
                "root -- cannot safely isolate a real-subprocess CLI test "
                "from a real daemon here"
            )
        yield dict(os.environ)
    finally:
        shutil.rmtree(short_dir, ignore_errors=True)


#: Matches any tag row printed by ``cli.camera._format_tag_row`` --
#: family, number, world x/y, yaw (or ``-`` when unpopulated).
_ANY_TAG_ROW_RE = re.compile(
    r"(apriltag|aruco) (\d+)\s+world=\(([-0-9.]+),\s*([-0-9.]+)\)\s+yaw=([-0-9.]+|-)"
)


def _parse_all_tags(stdout: bytes) -> Tuple[Tuple[str, int, float, float, Optional[float]], ...]:
    rows = []
    for match in _ANY_TAG_ROW_RE.finditer(stdout.decode()):
        family, number, x, y, yaw = match.groups()
        rows.append((family, int(number), float(x), float(y), (None if yaw == "-" else float(yaw))))
    return tuple(rows)


def _poll_for_tag(
    bin_path: Path, env: Dict[str, str], family: str, number: int, *, attempts: int = 20, delay: float = 0.5
) -> Tuple[float, float, Optional[float]]:
    last_stdout = b""
    for _ in range(attempts):
        result = _run(bin_path, "camera", "tags", ARDUCAM_SLUG, env=env)
        assert result.returncode == 0, result.stderr
        last_stdout = result.stdout
        for row_family, row_number, x, y, yaw in _parse_all_tags(result.stdout):
            if row_family == family and row_number == number:
                return x, y, yaw
        time.sleep(delay)
    pytest.skip(
        f"{family} {number} not detected across {attempts} attempts after "
        f"it was the target tag -- physical setup likely changed mid-test. "
        f"Last output:\n{last_stdout.decode()}"
    )
    raise AssertionError("unreachable")  # pragma: no cover - pytest.skip always raises


# ---------------------------------------------------------------------------
# Ticket 004 item 1 / UC-29.S-1 hardware reproduction -- against the real,
# already-running production daemon (read-only, --dry-run: never writes).
# ---------------------------------------------------------------------------


def test_locate_reports_missing_references_or_reproduces_the_documented_fixture(capsys):
    """Genuinely attempts a live ``camera locate`` reproduction of the
    documented fixture against camera 3 / main-playfield, using whatever
    production daemon is already running on this machine (no isolated
    sandbox -- this is read-only and ``--dry-run``, so it is safe to
    point at real state).

    If no local daemon is reachable at all, this test skips cleanly
    (``_skip_if_no_local_daemon_reachable``, ticket 030-005) rather than
    failing hard on ``camera locate``'s own ``no local daemon answered``
    -- see the module docstring's "deliberate non-isolation decision"
    for why this test does not stand up its own isolated daemon instead.

    If tags 14/15 are not physically staged (the expected, current
    state -- the stakeholder removed the stands), ``camera locate``
    itself reports "not detected" and this test skips cleanly, recording
    that real response rather than failing or silently passing. If the
    stands are ever restored, this test starts actually validating the
    reproduction against the documented tolerance below.
    """
    _skip_if_arducam_not_attached()
    _skip_if_no_local_daemon_reachable()
    bin_path = _aprilcam_bin()
    env = dict(os.environ)  # the real, already-running production daemon

    result = _run(
        bin_path,
        "camera",
        "locate",
        ARDUCAM_SLUG,
        "--reference",
        "15:dot-northeast-orange:17.2",
        "--reference",
        "14:dot-southwest-orange:15.0",
        "--dry-run",
        env=env,
    )
    stdout, stderr = result.stdout.decode(), result.stderr.decode()

    if result.returncode != 0 and "not detected" in stderr:
        pytest.skip(
            "reference AprilTags 14/15 are not physically staged on the "
            "field right now -- the stakeholder removed the elevated "
            "stands used for sprint 029's real-hardware measurement pass "
            "and confirmed they are not needed again (see ticket 004's "
            "completion notes for the real measurement this fixture was "
            f"captured from, before removal). camera locate's own "
            f"real-hardware response: {stderr.strip()}"
        )

    assert result.returncode == 0, stderr
    assert "solved:" in stdout, stdout
    match = re.search(
        r"nadir=\(([-0-9.]+), ([-0-9.]+)\) cm\s+height=([-0-9.]+) cm\s+rms_residual_cm=([-0-9.]+)",
        stdout,
    )
    assert match, stdout
    nadir_x, nadir_y, height_cm, rms_residual_cm = (float(g) for g in match.groups())

    # Loose, documented, live-hardware tolerance -- wider than ticket
    # 002's headless regression (built from a single recorded sample of
    # this same real data) since a fresh live detection burst carries its
    # own repeated-sample noise on top of that.
    assert nadir_x == pytest.approx(3.05, abs=0.2)
    assert nadir_y == pytest.approx(-2.75, abs=0.2)
    assert height_cm == pytest.approx(128.25, abs=0.5)
    assert rms_residual_cm == pytest.approx(0.16, abs=0.1)


# ---------------------------------------------------------------------------
# Ticket 004 items 2 and 5 combined / SUC-001 ACs 2 and 4 -- an isolated,
# self-cleaning daemon instance (its own config/state dirs -- never touches
# ~/.config/aprilcam or ~/.local/share/aprilcam) proves, live, in one
# uninterrupted daemon lifetime with no restart:
#   (5) an unconfigured camera's mount_z is a no-op (pre-029 behaviour), then
#   (2) pushing camera_position mid-lifetime makes the very next detection
#       burst reflect it -- no restart needed.
# A synthetic (not physically real) mount_z=20cm is registered against one
# of the field's flat, always-visible ArUco corner markers -- this proves the
# correction *mechanism* engages live; it does not re-derive ground truth
# (see module docstring for why that is not attempted here).
# ---------------------------------------------------------------------------


def test_mount_z_is_a_noop_until_camera_position_is_pushed_then_engages_with_no_restart(cli_env, tmp_path):
    _skip_if_arducam_not_attached()
    bin_path = _aprilcam_bin()
    env = cli_env

    unconfigured_path = tmp_path / "arducam-unconfigured.json"
    unconfigured_path.write_text(
        json.dumps({"name": ARDUCAM_SLUG, "playfield": "main-playfield"}), encoding="utf-8"
    )
    configured_path = tmp_path / "arducam-configured.json"
    configured_path.write_text(
        json.dumps(
            {
                "name": ARDUCAM_SLUG,
                "playfield": "main-playfield",
                "camera_position": {
                    "nadir_x_cm": REAL_NADIR_X_CM,
                    "nadir_y_cm": REAL_NADIR_Y_CM,
                    "height_cm": REAL_HEIGHT_CM,
                },
            }
        ),
        encoding="utf-8",
    )

    start = _run(bin_path, "daemon", "start", env=env)
    assert start.returncode == 0, start.stderr
    try:
        probe = _run(bin_path, "camera", "probe", env=env)
        assert probe.returncode == 0, probe.stderr

        pushed_pf = _run(bin_path, "playfield", "config", str(MAIN_PLAYFIELD_JSON), env=env)
        assert pushed_pf.returncode == 0, pushed_pf.stderr
        pushed_cfg = _run(bin_path, "camera", "config", str(unconfigured_path), env=env)
        assert pushed_cfg.returncode == 0, pushed_cfg.stderr

        calibrated = _run(bin_path, "camera", "calibrate", ARDUCAM_SLUG, env=env, timeout=90.0)
        _skip_if_cli_camera_open_failed(calibrated)
        assert calibrated.returncode == 0, calibrated.stderr
        assert b"succeeded" in calibrated.stdout, calibrated.stdout

        # -- item 5: unconfigured camera, mount_z registered -> no-op ------
        # Deliberately a *specific*, always-visible corner marker far from
        # the nadir (ArUco 1, surveyed at the field's NW corner) rather
        # than "whichever tag happens to be detected first"
        # (_poll_for_any_tag): a tag that happens to sit close to the
        # nadir (e.g. AprilTag 1, the field's centre marker, near
        # (0, 0) -- close to nadir (3.057, -2.799)) would show only a
        # few mm of expected displacement once corrected, indistinguishable
        # from real detection noise. ArUco 1 sits ~70 cm from the nadir,
        # giving a clean, reliably-above-noise signal every run.
        family, number = "aruco", 1
        before_x, before_y, _before_yaw = _poll_for_tag(bin_path, env, family, number)

        registered = _run(
            bin_path, "tags", "mount", "register", family, str(number), "--z", "20.0", env=env
        )
        assert registered.returncode == 0, registered.stderr
        assert f"registered {family} {number}" in registered.stdout.decode()

        unconfigured_x, unconfigured_y, _ = _poll_for_tag(bin_path, env, family, number)
        assert unconfigured_x == pytest.approx(before_x, abs=1.0), (
            "mount_z registered on a camera with no camera_position moved "
            f"the reported position: before={before_x}, "
            f"after={unconfigured_x} -- expected the documented pre-029 "
            "no-op"
        )
        assert unconfigured_y == pytest.approx(before_y, abs=1.0)

        # -- item 2: push camera_position live, mid-lifetime, no restart ---
        pushed_position = _run(bin_path, "camera", "config", str(configured_path), env=env)
        assert pushed_position.returncode == 0, pushed_position.stderr

        dumped = _run(bin_path, "camera", "config", ARDUCAM_SLUG, "--dump", env=env)
        assert dumped.returncode == 0, dumped.stderr
        dumped_data = json.loads(dumped.stdout)
        assert dumped_data["camera_position"]["height_cm"] == pytest.approx(REAL_HEIGHT_CM, abs=1e-6)
        assert dumped_data["camera_position"]["nadir_x_cm"] == pytest.approx(REAL_NADIR_X_CM, abs=1e-6)

        configured_x, configured_y, _ = _poll_for_tag(bin_path, env, family, number)
        expected_x, expected_y = expected_parallax_corrected_position(
            before_x, before_y, mount_z_cm=20.0
        )
        # The per-axis shift depends on how far *this particular* tag sits
        # from the nadir on that axis -- for a tag that happens to sit
        # close to the nadir's x (as the field's own corner markers can),
        # the x-only shift can be under a per-axis noise-sized threshold
        # even though the correction is genuinely engaged. Check the
        # combined 2D displacement instead of either axis alone.
        displacement_cm = math.hypot(configured_x - before_x, configured_y - before_y)
        assert displacement_cm > 2.0, (
            "pushing camera_position did not measurably change the "
            "mount_z-corrected position on the very next detection burst: "
            f"before=({before_x}, {before_y}), after=({configured_x}, {configured_y})"
        )
        assert configured_x == pytest.approx(expected_x, abs=2.0), (
            f"mount_z-corrected world.x {configured_x} not within tolerance "
            f"of expected {expected_x:.2f} (raw before={before_x})"
        )
        assert configured_y == pytest.approx(expected_y, abs=2.0)

        cleared = _run(bin_path, "tags", "mount", "clear", family, str(number), env=env)
        assert cleared.returncode == 0, cleared.stderr

        # Report the real numbers for the completion notes -- printed
        # (not asserted) so a `-s` run leaves a durable trace, per this
        # ticket's honesty requirement.
        print(
            f"\n[029-004 hardware] target={family} {number} "
            f"raw={before_x, before_y} "
            f"unconfigured(mount_z=20)={unconfigured_x, unconfigured_y} "
            f"configured(mount_z=20, camera_position pushed live, no restart)="
            f"{configured_x, configured_y}"
        )
    finally:
        stop = _run(bin_path, "daemon", "stop", env=env)
        assert stop.returncode == 0, stop.stderr
