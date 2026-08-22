"""Hardware verification for ``aprilcam tags mount register|clear``
(ticket 025-005, UC-12) -- against whichever attached camera is
genuinely configured and calibrated against the physical
``main-playfield`` (discovered at run time via
``tests/aprilcam/hardware_camera.py``, never a hardcoded device name),
calibrated per ``test_calibration_hardware.py``'s own ground truth
(AprilTag 1 at centre, ~0.18-0.21 cm reprojection error).

**Scope, deliberately narrower than UC-12 as a whole.** Ticket 004
found that ``calibration.interfaces.Calibration`` has no camera-geometry
field anywhere in the codebase (no camera ground position or height
above the field is modelled), and shipped a stopgap:
``DaemonService._camera_geometry_for`` returns ``(WorldPoint(0, 0),
0.0)`` for every camera -- height ``0.0`` is
``calibration.geometry.project_tag_to_field_plane``'s own documented
"nothing to correct" sentinel. **Consequence: ``mount_z`` (parallax
correction) has no real, observable effect on hardware today.** Only
``mount_x``/``mount_y``/``mount_yaw_rad`` (the offset+yaw composition,
``calibration.geometry.apply_mount_offset``) have a real effect on a
live camera right now, and this module verifies exactly those --
deliberately **not** ``mount_z``/parallax, which cannot show an effect
under the current stopgap; a test expecting one would be testing a
known, already-disclosed gap, not a regression. Adding real camera
position/height (``CameraConfig`` + proto/loader plumbing) is a real
follow-up for a future ticket, not attempted here.

Every test is ``@pytest.mark.camera`` and skips cleanly (module-wide,
via ``_skip_if_arducam_not_attached``) on a machine without that camera
attached -- ``uv run pytest -m "not camera"`` must be unaffected.

**Via the CLI, via a real subprocess** -- the acceptance criteria name
the CLI commands explicitly (``aprilcam tags mount register``/``clear``,
``aprilcam camera tags``), so this module spawns the real ``aprilcam``
console script, matching ``test_calibration_hardware.py``'s own
restart-survival test and ``test_detection_hardware.py``'s CLI tests
(same ``cli_env``/``_run``/``_aprilcam_bin`` shape, duplicated locally
per those modules' own stated precedent of every ``@pytest.mark.camera``
module defining its own).
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

from aprilcam.config.directories import Directories
from tests.aprilcam.hardware_camera import resolve_hardware_camera_slug

pytestmark = pytest.mark.camera

#: Resolved lazily, once per test, by :func:`_skip_if_arducam_not_attached`
#: against this machine's real, persisted system state (see
#: tests/aprilcam/hardware_camera.py) -- never hardcoded.
ARDUCAM_SLUG: str = ""

#: main-playfield.json's centre marker -- always in frame if the
#: arducam is genuinely aimed at the field.
APRILTAG_1_ROW = "apriltag 1"

#: The real, checked-in playfield definition this module pushes --
#: the same file ``test_calibration_hardware.py`` names as ground truth.
MAIN_PLAYFIELD_JSON = (
    Path(__file__).resolve().parents[3] / "config" / "aprilcam-old" / "playfields" / "main-playfield.json"
)

#: Camera 3's baseline settings (sprint 025's ticket brief) -- this
#: module never pushes a settings document, so nothing here should ever
#: disturb them; recorded for the completion notes' own record, not
#: asserted against (no RPC in this module reads UVC control values
#: back).
CAMERA_3_BASELINE_SETTINGS = (
    "auto-exposure-mode=1, exposure-time-abs=6, gain=2, "
    "auto-white-balance-temp=false, white-balance-temp=4600"
)


def _skip_if_arducam_not_attached() -> None:
    global ARDUCAM_SLUG
    ARDUCAM_SLUG = resolve_hardware_camera_slug()


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
    return subprocess.run(
        [str(bin_path), *args], capture_output=True, env=env, timeout=timeout
    )


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """Isolated environment for a real ``aprilcam`` subprocess --
    duplicated locally, matching ``test_calibration_hardware.py``'s own
    stated precedent of every ``@pytest.mark.camera`` module defining
    its own."""
    short_dir = _short_tmp_dir("ac-tags-cli-")
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


def _parse_all_tags(
    stdout: bytes,
) -> Tuple[Tuple[str, int, float, float, Optional[float]], ...]:
    """Every ``(family, number, world_x, world_y, yaw_rad)`` row in a
    real ``aprilcam camera tags <camera>`` invocation's stdout, in the
    order printed."""
    rows = []
    for match in _ANY_TAG_ROW_RE.finditer(stdout.decode()):
        family, number, x, y, yaw = match.groups()
        rows.append(
            (family, int(number), float(x), float(y), (None if yaw == "-" else float(yaw)))
        )
    return tuple(rows)


def _poll_for_any_tag(
    bin_path: Path, env: Dict[str, str], *, attempts: int = 20, delay: float = 0.5
) -> Tuple[str, int, float, float, Optional[float]]:
    """Retry ``camera tags`` until **some** tag is detected, and return
    its first row -- deliberately not pinned to any specific family/id.

    UC-12's mount correction applies identically to any ``TagId``, so
    this test does not need a specific tag to be physically present --
    only *some* real, currently-visible one, matching the ticket's own
    "a tag actually visible on camera 3 right now" phrasing. The field's
    10 surveyed ArUco corner markers are fixed and always answer this as
    long as the camera can see the field at all; a mobile AprilTag on a
    stand (this ticket's own worked example) may or may not currently be
    placed in frame, and this test does not depend on it being there.
    """
    last_stdout = b""
    for _ in range(attempts):
        result = _run(bin_path, "camera", "tags", ARDUCAM_SLUG, env=env)
        assert result.returncode == 0, result.stderr
        last_stdout = result.stdout
        rows = _parse_all_tags(result.stdout)
        if rows:
            return rows[0]
        time.sleep(delay)
    pytest.skip(
        f"no tag detected across {attempts} attempts -- check the arducam "
        f"is aimed at main-playfield. Last output:\n{last_stdout.decode()}"
    )
    raise AssertionError("unreachable")  # pragma: no cover - pytest.skip always raises


def _poll_for_tag(
    bin_path: Path,
    env: Dict[str, str],
    family: str,
    number: int,
    *,
    attempts: int = 20,
    delay: float = 0.5,
) -> Tuple[float, float, Optional[float]]:
    """Retry ``camera tags`` until *family*/*number*'s own row appears
    (not just any row -- once a target tag is chosen, every subsequent
    read must be of that same tag, since other tags may enter or leave
    frame independently)."""
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


def test_register_mount_offset_via_cli_moves_reported_position_clear_reverts_it(cli_env, tmp_path):
    """UC-12.S-3 (off-centre tag reports the robot's reference point),
    hardware-verified via the real CLI -- **x/y/yaw only**, per this
    module's scope note above: mount_z/parallax is not attempted.

    1. Calibrate the real arducam against the real field.
    2. Read *some* currently-visible tag's raw (unregistered) world
       position/yaw via ``aprilcam camera tags`` -- whichever one the
       camera can see right now (module docstring's
       :func:`_poll_for_any_tag`: the correction math is identical for
       any ``TagId``, so this test does not depend on any one specific
       tag being physically present).
    3. ``aprilcam tags mount register <family> <number> --x 8 --y 3``
       -- an 8 cm forward / 3 cm left offset in the tag's own
       (approximately east-facing, yaw near 0) frame.
    4. Confirm the reported position moved by approximately the
       registered offset (``calibration.geometry.apply_mount_offset``'s
       own composed math: with yaw near zero, ``robot_x ~= tag_x - 8``,
       ``robot_y ~= tag_y - 3``).
    5. ``aprilcam tags mount clear <family> <number>`` and confirm the
       position reverts to (approximately) the original raw reading.
    """
    _skip_if_arducam_not_attached()
    bin_path = _aprilcam_bin()
    env = cli_env

    camera_config_path = tmp_path / "arducam-config.json"
    camera_config_path.write_text(
        json.dumps({"name": ARDUCAM_SLUG, "playfield": "main-playfield"}), encoding="utf-8"
    )

    start = _run(bin_path, "daemon", "start", env=env)
    assert start.returncode == 0, start.stderr
    try:
        probe = _run(bin_path, "camera", "probe", env=env)
        assert probe.returncode == 0, probe.stderr

        pushed_pf = _run(bin_path, "playfield", "config", str(MAIN_PLAYFIELD_JSON), env=env)
        assert pushed_pf.returncode == 0, pushed_pf.stderr
        pushed_cfg = _run(bin_path, "camera", "config", str(camera_config_path), env=env)
        assert pushed_cfg.returncode == 0, pushed_cfg.stderr

        # A generous timeout -- ``CALIBRATION_FRAME_COUNT`` (30 frames)
        # bounds the real work, but a live camera occasionally needs
        # noticeably longer than the default 30s to deliver and detect
        # across all of them.
        calibrated = _run(bin_path, "camera", "calibrate", ARDUCAM_SLUG, env=env, timeout=90.0)
        assert calibrated.returncode == 0, calibrated.stderr
        assert b"succeeded" in calibrated.stdout, calibrated.stdout

        # -- before: raw, unregistered reading of *some* visible tag ----
        family, number, before_x, before_y, before_yaw = _poll_for_any_tag(bin_path, env)

        # -- register an 8cm/3cm offset via the real CLI ---------------
        registered = _run(
            bin_path, "tags", "mount", "register", family, str(number),
            "--x", "8.0", "--y", "3.0", env=env,
        )
        assert registered.returncode == 0, registered.stderr
        assert f"registered {family} {number}" in registered.stdout.decode()

        # -- after: mount-corrected reading of that same tag ------------
        after_x, after_y, after_yaw = _poll_for_tag(bin_path, env, family, number)

        # calibration.geometry.apply_mount_offset: robot_yaw = yaw -
        # mount_yaw_rad (0 here, so robot_yaw == observed yaw); the
        # registered offset is rotated into world space by robot_yaw and
        # subtracted. With the target tag's real observed yaw close to 0
        # (east), this reduces to approximately (before_x - 8, before_y - 3).
        yaw_for_rotation = before_yaw if before_yaw is not None else 0.0
        cos_r, sin_r = math.cos(yaw_for_rotation), math.sin(yaw_for_rotation)
        expected_x = before_x - (8.0 * cos_r - 3.0 * sin_r)
        expected_y = before_y - (8.0 * sin_r + 3.0 * cos_r)

        assert after_x != pytest.approx(before_x, abs=0.5), (
            f"registering an 8cm/3cm mount offset did not move the "
            f"reported position: before=({before_x}, {before_y}), "
            f"after=({after_x}, {after_y})"
        )
        assert after_x == pytest.approx(expected_x, abs=1.5), (
            f"mount-corrected world.x {after_x} not within tolerance of "
            f"expected {expected_x:.2f} (raw before={before_x})"
        )
        assert after_y == pytest.approx(expected_y, abs=1.5), (
            f"mount-corrected world.y {after_y} not within tolerance of "
            f"expected {expected_y:.2f} (raw before={before_y})"
        )

        # -- clear reverts it --------------------------------------------
        cleared = _run(bin_path, "tags", "mount", "clear", family, str(number), env=env)
        assert cleared.returncode == 0, cleared.stderr
        assert f"cleared mount for {family} {number}" in cleared.stdout.decode()

        reverted_x, reverted_y, _reverted_yaw = _poll_for_tag(bin_path, env, family, number)
        assert reverted_x == pytest.approx(before_x, abs=1.0), (
            f"clearing the mount did not revert world.x: before={before_x}, "
            f"reverted={reverted_x}"
        )
        assert reverted_y == pytest.approx(before_y, abs=1.0), (
            f"clearing the mount did not revert world.y: before={before_y}, "
            f"reverted={reverted_y}"
        )

        # Report the real numbers for the completion notes -- printed
        # (not asserted) so a `-s` run leaves a durable trace of the
        # actual hardware verification, per this ticket's honesty
        # requirement.
        print(
            f"\n[025-005 hardware] target={family} {number} "
            f"before={before_x, before_y, before_yaw} "
            f"after(+x8,+y3)={after_x, after_y, after_yaw} "
            f"reverted={reverted_x, reverted_y}"
        )
    finally:
        stop = _run(bin_path, "daemon", "stop", env=env)
        assert stop.returncode == 0, stop.stderr


def test_list_via_cli_shows_the_registration_then_empty_after_clear(cli_env, tmp_path):
    """``aprilcam tags mount list`` reflects the live registry via the
    real CLI: ``(no registered tags)`` before anything is registered,
    one row while registered, ``(no registered tags)`` again after
    ``clear`` -- the same round trip as the headless
    ``tests/aprilcam/cli/test_tags_cli.py`` tests, but through a real
    daemon and a real subprocess."""
    _skip_if_arducam_not_attached()
    bin_path = _aprilcam_bin()
    env = cli_env

    start = _run(bin_path, "daemon", "start", env=env)
    assert start.returncode == 0, start.stderr
    try:
        empty_before = _run(bin_path, "tags", "mount", "list", env=env)
        assert empty_before.returncode == 0, empty_before.stderr
        assert "(no registered tags)" in empty_before.stdout.decode()

        registered = _run(
            bin_path, "tags", "mount", "register", "aruco", "5",
            "--x", "1.0", "--yaw-deg", "45.0", env=env,
        )
        assert registered.returncode == 0, registered.stderr

        listed = _run(bin_path, "tags", "mount", "list", env=env)
        assert listed.returncode == 0, listed.stderr
        out = listed.stdout.decode()
        assert "aruco" in out and "5" in out
        assert "x=1.00" in out
        assert "yaw=45.0deg" in out

        cleared = _run(bin_path, "tags", "mount", "clear", "aruco", "5", env=env)
        assert cleared.returncode == 0, cleared.stderr

        empty_after = _run(bin_path, "tags", "mount", "list", env=env)
        assert empty_after.returncode == 0, empty_after.stderr
        assert "(no registered tags)" in empty_after.stdout.decode()
    finally:
        stop = _run(bin_path, "daemon", "stop", env=env)
        assert stop.returncode == 0, stop.stderr
