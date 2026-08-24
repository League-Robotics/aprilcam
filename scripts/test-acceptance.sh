#!/usr/bin/env bash
# The canonical L2 acceptance-suite runner (sprint 035 ticket 017).
#
# Runs the `acceptance`-marked layer (tests/acceptance/, sprint 035
# ticket-group 4, UC-29) against the REAL, already-running daemon and
# the real, physical field -- never an isolated tmp root; that
# isolation belongs to L0/L1 instead (tests/image_matrix/,
# tests/aprilcam/*_hardware.py). Full output is tee'd to a timestamped
# log file under .clasi/log/ (already gitignored -- see
# .clasi/log/.gitignore) so a run's exact pytest output survives after
# the terminal scrolls away.
#
# The tee runs under `pipefail` (part of `set -euo pipefail` below),
# so a pytest failure still fails this script -- tee-ing output never
# masks a real exit code.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

LOG_DIR=".clasi/log"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/acceptance-$(date +%Y%m%d-%H%M%S).log"

cat <<PREFLIGHT
========================================================================
 test-acceptance.sh -- PREFLIGHT CHECKLIST
========================================================================

 1. RUN THIS FROM A REAL TERMINAL WINDOW -- never from Claude Code or
    any other agent/IDE shell. Same TCC reasoning as
    test-hardware.sh's checklist; see
    docs/knowledge/daemon-camera-grant-and-detection-priming.md

 2. THE OPERATOR'S REAL \`aprilcam daemon\` MUST ALREADY BE RUNNING,
    STARTED FROM TERMINAL. Unlike most of test-hardware.sh's suite,
    every test here talks to the real daemon on its normal socket --
    it never starts one of its own, and skips cleanly if the daemon is
    unreachable. THIS SCRIPT NEVER RUNS \`aprilcam daemon
    start\`/\`stop\`/\`restart\` -- if the daemon is down, stop and
    ask the operator to start it from Terminal.

 3. DO NOT SET APRILCAM_CONFIG_DIR / APRILCAM_STATE_DIR before running
    this. tests/acceptance/conftest.py *requires* the real config/state
    roots and hard-refuses to run (pytest UsageError, exit 4) if either
    isolation override is set -- this layer exists specifically to
    test the operator's real environment. This script does not set,
    export, or unset either variable itself. If you hit that failure,
    check your shell for a leftover export from other test work.

 4. THIS LAYER IS NO-WRITE-GUARDED. Every real file under the real
    config_dir/state_dir is sha256-hashed before and after every test;
    camera_position (CLAUDE.md: sacred) is proven byte-identical, not
    assumed. A guard trip names the exact file and is a real bug to
    investigate, never something to silence.

 5. AMBIENT LIGHT applies here too -- see docs/knowledge/
    dark-camera-image-uvc-drift-reset.md. A dusk/night run understates
    detection recall through no fault of the code; live tests here are
    written to skip with a diagnostic rather than fail hard on that.

 6. ELEVATED ("raised block") COVERAGE IS SEPARATE AND OPT-IN, gated
    behind APRILCAM_TEST_ELEVATED=1 and requiring physically staged
    blocks. This script does not set that variable, so that coverage
    skips cleanly by default. Read
    docs/test-plans/staged-elevated-locate.md in full before ever
    setting it by hand.

 Log file for this run: $LOG_FILE
========================================================================
PREFLIGHT

echo "Running: uv run pytest -m acceptance $* (tee'd to $LOG_FILE)"
echo

uv run pytest -m acceptance "$@" 2>&1 | tee "$LOG_FILE"
