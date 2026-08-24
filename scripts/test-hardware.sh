#!/usr/bin/env bash
# The canonical L1 live-hardware test runner (sprint 035 ticket 017).
#
# Runs everything marked `camera` or `integration` that is NOT also
# marked `acceptance` -- sprint 035 ticket-group 3's live-hardware
# system suite (tickets 009, 011, 012, 013), collected against
# whatever camera hardware is actually attached to this machine.
#
# `and not acceptance` is what keeps this suite out of
# tests/acceptance/'s no-write-guarded L2 layer, even though every
# acceptance test also carries `camera`/`integration` on top of
# `acceptance` (sprint.md Decision 4, so the *existing*
# "not camera and not integration" gate keeps excluding L2 too, with
# zero edits). Verified empirically at ticket 017 time:
#   uv run pytest -m "(camera or integration) and not acceptance" --collect-only
#     -> 85 node ids
#   uv run pytest -m "acceptance" --collect-only
#     -> 19 node ids
#   zero overlap between the two sets (diffed directly, not assumed).
#
# Exits non-zero on any test failure (pytest's own exit code),
# including collection errors -- same contract as test-gate.sh.
#
# This script never starts, stops, or restarts any daemon (see
# checklist item 2 below) -- it only runs pytest.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

cat <<'PREFLIGHT'
========================================================================
 test-hardware.sh -- PREFLIGHT CHECKLIST (read every item before continuing)
========================================================================

 1. RUN THIS FROM A REAL TERMINAL WINDOW -- never from Claude Code, any
    other agent shell, or an IDE-launched terminal. macOS grants camera
    access (TCC) to the RESPONSIBLE PROCESS, and every child process
    inherits that grant. Terminal.app is authorized for the camera;
    Claude Code's helper bundle is not, and there is no toggle in
    System Settings to fix that from its side. A daemon started
    underneath the wrong shell -- including the private, per-test
    daemons most of this suite spins up itself -- will enumerate
    cameras fine and then fail every open. Full facts:
    docs/knowledge/daemon-camera-grant-and-detection-priming.md

 2. IF THE OPERATOR'S OWN `aprilcam daemon` IS RUNNING, LEAVE IT
    RUNNING -- and it must have been started from Terminal, same
    reason as item 1. Most tests in this suite spin up their own
    short-lived, isolated daemon per test (a child of this shell, so
    it inherits whatever TCC grant this shell has -- which is exactly
    why item 1 matters). THIS SCRIPT NEVER RUNS `aprilcam daemon
    start`/`stop`/`restart`, and neither should you on its behalf: if
    a daemon this suite needs is down, stop and ask the operator to
    start it from Terminal. Restarting the real daemon from an agent
    silently breaks every camera on the machine for everyone else.

 3. CAMERAS MUST BE FREE. Close any running `aprilcam view`, stop any
    other concurrent test run, and make sure no second checkout's
    daemon is holding cameras 2/3/5 open. A camera that is genuinely
    busy elsewhere is skipped or a different one is substituted, not
    waited for -- see tests/aprilcam/hardware_camera.py.

 4. AMBIENT LIGHT AFFECTS RECALL. Camera 5's exposure is
    ambient-dependent -- there is no one right value across the day.
    Measured directly: several ArUco markers that are solid in
    daylight drop below detection threshold at dusk. A run at night or
    dusk understates recall through no fault of the code. Tests are
    written to skip cleanly on missing detections rather than fail
    hard, but do not read a dusk/night run as a verdict on detection
    quality. See docs/knowledge/dark-camera-image-uvc-drift-reset.md.

 5. WHAT A HEALTHY RUN LOOKS LIKE: ~85 tests collected (camera or
    integration, minus acceptance). With cameras 2/3/5 attached, the
    operator's daemon up, and run in daylight from Terminal: mostly
    PASSED, with a handful of SKIPPED for documented reasons (no
    second physical AprilCam host reachable on the LAN, a specific
    camera genuinely busy elsewhere, etc).

    WHAT A BROKEN-LOOKING (BUT NOT ACTUALLY BROKEN) RUN LOOKS LIKE FROM
    THE WRONG SHELL: NOT a clean wall of skips -- most of this suite's
    camera-presence checks only verify a camera is *attached*
    (enumeration), which still succeeds under the wrong TCC grant; the
    *open* is what fails. So the same ~85 tests collect, but many
    report FAILED (not skipped), with "RuntimeError: failed to open
    camera at index N" / OpenCV's "not authorized to capture video
    (status 0)" in the traceback -- confirmed directly: an agent-shell
    run of this exact script produced 18 failed / 24 passed / 42
    skipped. Do not read those failures as a broken rig or a code
    regression -- re-run from Terminal with the rig's cameras attached
    before concluding anything is actually wrong. See "The misleading
    symptom" section of
    docs/knowledge/daemon-camera-grant-and-detection-priming.md.
========================================================================
PREFLIGHT

echo "Running: uv run pytest -m \"(camera or integration) and not acceptance\" $*"
echo

exec uv run pytest -m "(camera or integration) and not acceptance" "$@"
