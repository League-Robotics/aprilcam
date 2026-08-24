"""Elevated ("with raised blocks") acceptance coverage (sprint 035
ticket 016, UC-29, SUC-003 step 4) -- opt-in only, gated entirely
behind ``APRILCAM_TEST_ELEVATED=1``.

**Why this module is separate from ``test_inventory.py``.** Every test
in ``test_inventory.py`` (ticket 015) runs unconditionally in a normal
acceptance pass. This module never does -- it depends on real, raised
AprilTag references being physically staged on the field, which is not
this rig's current state (the stakeholder confirmed no elevated
features are currently placed). Splitting it into its own module makes
the gate a single, auditable ``pytestmark`` entry rather than a
per-test condition scattered through the inventory file.

**What "elevated" means here.** ``camera locate <cam> --dry-run
--reference TAG:SLUG:HEIGHT_CM ...`` (``src/aprilcam/cli/locate.py``)
solves a camera's ground nadir and optical height from AprilTags
mounted *above* the field plane (on a physical block/stand of known
height) at surveyed playfield positions. ``--dry-run`` reports the full
solve -- nadir, height, RMS residual, condition number -- without ever
writing ``camera_position`` to disk or pushing it live, regardless of
fit quality (``cmd_locate``'s own dry-run branch returns before the
quality-gated-write logic runs at all). See
``docs/test-plans/staged-elevated-locate.md`` for the concrete
blocks/tags/heights this module's one real test uses, and for the plain
statement that a real, non-dry ``locate`` is a deliberate, separate
stakeholder action, never something any test in this suite performs.

**The gate, and how it is proven rather than documented.** ``pytestmark``
below carries a module-wide ``pytest.mark.skipif`` alongside the three
markers every ``tests/acceptance/`` test must carry
(``conftest.py``'s marker audit). A normal, unset-env acceptance run
still *collects* every item in this module (the skipif marker does not
prevent collection, only execution) -- which matters for two reasons:
first, it is what lets ``pytest_collection_modifyitems`` verify the
marker set on these items at all; second, it is what makes the node-id
allowlist's collection-time AST scan
(:func:`tests.acceptance.conftest.find_disallowed_calls`) inspect this
module's source on *every* acceptance run, not only ones where
``APRILCAM_TEST_ELEVATED=1`` happens to be set. So the "no non-dry
locate, no calibrate" guarantee for this module holds structurally even
under the default (unset) skip path -- it is not a guarantee that only
kicks in once someone flips the env var.
:func:`test_this_modules_dry_run_locate_call_passes_the_node_id_allowlist`
below is this ticket's own additional, direct proof of that: it reads
this module's own source, asserts zero violations, and asserts that the
same call shape with ``--dry-run`` removed *is* flagged -- so the claim
"the allowlist genuinely permits ``--dry-run`` while still blocking a
non-dry call" is demonstrated against this exact module's own call, not
merely against ticket 015's synthetic examples elsewhere.

**Zero writes.** Proven by ``--dry-run``'s own contract (nothing here
reaches ``cmd_locate``'s write branch at all) *and*, independently, by
``conftest.py``'s autouse sha256 no-write guard, which applies to every
test collected under this directory including this module -- this
module carries no exemption from it.

**Without blocks raised (the state every reader of this file will
actually observe), coverage for this geometry continues to rest on**
``tests/aprilcam/calibration/test_geometry.py:485-887`` -- in
particular ``test_solve_camera_geometry_real_fixture_regression``
(the exact same two references this module's live test uses, pinned as
a headless regression: nadir (3.05, -2.75) cm, height 128.25 cm,
rms_residual_cm ~0.16, condition_number ~67.4, all comfortably inside
the ``RMS_RESIDUAL_THRESHOLD_CM``/``CONDITION_NUMBER_THRESHOLD`` gates
this module's live test checks). This module does not weaken or
replace that coverage; it adds a live-hardware corroboration on top of
it, when and only when blocks are staged and the operator opts in.
"""

from __future__ import annotations

import math
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Dict

import pytest

from aprilcam.cli.locate import CONDITION_NUMBER_THRESHOLD, RMS_RESIDUAL_THRESHOLD_CM
from aprilcam.client.discovery import probe_socket_reachable
from aprilcam.config.directories import Directories
from aprilcam.daemon.lifecycle import SOCKET_FILENAME
from tests.acceptance.conftest import find_disallowed_calls

#: The single opt-in switch (sprint.md Decision 5). Unset (the default)
#: on every machine except one where a human has deliberately staged
#: raised blocks and wants this module to actually exercise them.
_ELEVATED_ENV_VAR = "APRILCAM_TEST_ELEVATED"


def _elevated_enabled() -> bool:
    return os.environ.get(_ELEVATED_ENV_VAR) == "1"


pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.camera,
    pytest.mark.integration,
    pytest.mark.skipif(
        not _elevated_enabled(),
        reason=(
            f"{_ELEVATED_ENV_VAR}=1 not set -- elevated ('with raised blocks') "
            "coverage is opt-in only (sprint 035 ticket 016 / sprint.md Decision "
            "5) and does not run in a normal acceptance pass. See "
            "docs/test-plans/staged-elevated-locate.md to stage blocks and opt "
            "in."
        ),
    ),
]

#: Camera 3 -- the same camera CLAUDE.md's "sacred camera_position" note
#: and ``test_inventory.py``'s ``CAMERA_3_SLUG`` both name; the only
#: camera this rig has ever had elevated references staged against
#: (sprint 029/030).
CAMERA_SLUG = "arducam-ov9782-usb-camera"

#: The exact two references sprint 029 measured live on this same
#: camera/playfield and ``test_geometry.py::
#: test_solve_camera_geometry_real_fixture_regression`` pins as a
#: headless regression (module docstring above) -- AprilTag 15 on a
#: 17.2cm block at ``dot-northeast-orange`` (surveyed (50, 30) cm),
#: AprilTag 14 on a 15.0cm block at ``dot-southwest-orange`` (surveyed
#: (-50, -30) cm). Documented in full, with placement instructions, in
#: docs/test-plans/staged-elevated-locate.md.
_REFERENCE_1 = "15:dot-northeast-orange:17.2"
_REFERENCE_2 = "14:dot-southwest-orange:15.0"


def _aprilcam_bin() -> Path:
    candidate = Path(sys.executable).parent / "aprilcam"
    if not candidate.exists():  # pragma: no cover - environment-dependent
        pytest.skip(f"no aprilcam console script next to {sys.executable}")
    return candidate


def _run(bin_path: Path, *args: str, env: Dict[str, str], timeout: float = 30.0):
    return subprocess.run([str(bin_path), *args], capture_output=True, env=env, timeout=timeout)


def _skip_if_no_local_daemon_reachable(directories: Directories) -> None:
    """Same shape as ``test_camera_geometry_hardware.py``'s own guard of
    the same name -- ``camera locate`` drives detection through the
    daemon's ``GetTag`` RPC, so with no local daemon reachable the CLI
    would exit before it can ever reach the "not detected" path this
    test's own skip logic depends on. Never starts a daemon (CLAUDE.md's
    hard rule) -- only probes whether one is already listening."""
    socket_path = directories.socket_dir / SOCKET_FILENAME
    if not probe_socket_reachable(socket_path):
        pytest.skip(
            f"no local aprilcam daemon reachable at {socket_path} -- this test "
            "drives 'camera locate --dry-run' through the daemon's GetTag RPC "
            "and needs a real daemon already running (never started by a test; "
            "see CLAUDE.md's hard rule and "
            "docs/knowledge/daemon-camera-grant-and-detection-priming.md)"
        )


#: Matches ``cmd_locate``'s own "solved: ..." report line exactly
#: (``src/aprilcam/cli/locate.py``), pulling out all four solved values.
_SOLVED_RE = re.compile(
    r"nadir=\(([-0-9.]+), ([-0-9.]+)\) cm\s+height=([-0-9.]+) cm\s+"
    r"rms_residual_cm=([-0-9.]+)\s+condition_number=([-0-9.]+)"
)


# ---------------------------------------------------------------------------
# The one real, live test: dry-run locate against physically staged blocks.
# ---------------------------------------------------------------------------


def test_elevated_dry_run_locate_reports_full_solve_within_quality_gates(
    real_directories: Directories,
) -> None:
    """Runs ``camera locate <cam> --dry-run --reference ...`` against
    the real, currently-running production daemon (never an isolated
    sandbox -- ``--dry-run`` never writes, so pointing this at real
    state is safe, matching ``test_camera_geometry_hardware.py``'s own
    "deliberate non-isolation decision" for its equivalent test).

    Skips cleanly, naming the real cause, in every case where this
    cannot genuinely run: no local daemon reachable, or the reference
    AprilTags not currently detected (the expected state today -- no
    blocks are staged; see the module/operator docs). Only once blocks
    are staged does this test exercise its actual assertions: RMS ≤
    ``RMS_RESIDUAL_THRESHOLD_CM`` (1.0 cm) and condition_number ≤
    ``CONDITION_NUMBER_THRESHOLD`` (447.2, "≤ 447" in the ticket's own
    rounded language) -- the exact constants ``cmd_locate`` itself
    gates a real (non-dry) write on, imported here rather than
    re-declared so the two can never drift apart.
    """
    _skip_if_no_local_daemon_reachable(real_directories)
    bin_path = _aprilcam_bin()
    env = dict(os.environ)  # the real, already-running production daemon

    result = _run(
        bin_path,
        "camera",
        "locate",
        CAMERA_SLUG,
        "--reference",
        _REFERENCE_1,
        "--reference",
        _REFERENCE_2,
        "--dry-run",
        env=env,
    )
    stdout, stderr = result.stdout.decode(), result.stderr.decode()

    if result.returncode != 0 and "not detected" in stderr:
        pytest.skip(
            "reference AprilTags 14/15 are not physically staged/elevated on "
            "the field right now -- see docs/test-plans/staged-elevated-"
            f"locate.md to stage them. camera locate's own response: "
            f"{stderr.strip()}"
        )

    assert result.returncode == 0, stderr
    assert "dry-run: reporting only -- nothing written" in stdout, stdout
    assert "solved:" in stdout, stdout

    match = _SOLVED_RE.search(stdout)
    assert match, stdout
    nadir_x, nadir_y, height_cm, rms_residual_cm, condition_number = (
        float(group) for group in match.groups()
    )

    assert height_cm > 0.0, stdout  # sanity: a real optical height was solved
    assert math.isfinite(nadir_x) and math.isfinite(nadir_y), stdout

    assert rms_residual_cm <= RMS_RESIDUAL_THRESHOLD_CM, (
        f"rms_residual_cm={rms_residual_cm} exceeds the "
        f"{RMS_RESIDUAL_THRESHOLD_CM} cm quality gate -- full output:\n{stdout}"
    )
    assert condition_number <= CONDITION_NUMBER_THRESHOLD, (
        f"condition_number={condition_number} exceeds the "
        f"{CONDITION_NUMBER_THRESHOLD} quality gate -- full output:\n{stdout}"
    )


# ---------------------------------------------------------------------------
# Self-verifying proof that this module's own dry-run call passes the
# node-id allowlist, and that the same shape without --dry-run does not.
# ---------------------------------------------------------------------------


def test_this_modules_dry_run_locate_call_passes_the_node_id_allowlist() -> None:
    """Direct proof, specific to this module's own call (not just
    ticket 015's synthetic examples), that the guard genuinely
    discriminates dry-run from non-dry.

    Reads this file's own source and asserts
    :func:`~tests.acceptance.conftest.find_disallowed_calls` finds
    nothing -- this module's real, executed ``_run(..., "--dry-run",
    ...)`` call above must not be flagged. Then builds a synthetic
    snippet with the identical call shape *minus* ``--dry-run`` and
    asserts it IS flagged -- proving the guard would catch it if this
    module (or a future edit to it) ever dropped that flag, rather than
    relying on ``--dry-run`` never being removed by convention.

    This is deliberately narrower than, and a complement to,
    ``test_inventory.py::
    test_node_id_allowlist_blocks_calibrate_and_non_dry_locate`` (ticket
    015's own self-test, which already re-scans every real ``.py`` file
    under this directory including this one) -- that test proves the
    guard has no blind spot across the whole directory; this test proves
    the guard's verdict on *this specific call shape* in both the
    dry and non-dry forms, so a reader of this module alone (without
    also reading ``test_inventory.py``) can see the discrimination
    demonstrated directly.
    """
    own_source = Path(__file__).read_text(encoding="utf-8")
    assert find_disallowed_calls(own_source, filename=str(Path(__file__))) == [], (
        "this module's own dry-run locate call was unexpectedly flagged by "
        "the node-id allowlist"
    )

    non_dry_variant = textwrap.dedent(
        f'''
        def test_would_write_real_camera_position():
            _run(
                bin_path,
                "camera",
                "locate",
                {CAMERA_SLUG!r},
                "--reference",
                {_REFERENCE_1!r},
                "--reference",
                {_REFERENCE_2!r},
                env=env,
            )
        '''
    )
    violations = find_disallowed_calls(
        non_dry_variant, filename="<synthetic-non-dry-elevated-locate>"
    )
    assert violations, (
        "the guard failed to catch this module's own call shape with "
        "--dry-run removed"
    )
    assert "locate" in violations[0]
