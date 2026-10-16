---
id: '012'
title: Live acceptance runbook and stakeholder verification on camera 3
status: done
use-cases:
- SUC-001
- SUC-004
- SUC-005
- SUC-006
depends-on:
- 008
- 009
- '011'
github-issue: ''
issue: charuco-board-intrinsics-calibration-as-a-daemon-workflow.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Live acceptance runbook and stakeholder verification on camera 3

## Description

Final verification against the issue's Acceptance section. Agents may not restart the daemon or open cameras, so this ticket delivers a runbook the stakeholder executes, plus a recorded result. It also measures the CPU/contention risk flagged in the architecture review.

## Acceptance Criteria

- [x] `docs/knowledge/` (or the ticket body) contains a step-by-step runbook: pipx/venv update, start the daemon once, `aprilcam camera intrinsics 3`, follow NEXT to GOOD ENOUGH in under a minute of board motion on the secondary field, Enter, then immediately `aprilcam camera calibrate 3` reports the measured model carried forward; no daemon restart anywhere.
- [x] *(deferred by stakeholder decision 2026-10-16 to the post-merge test on master; results go in the runbook's Results table)* Stakeholder records: time to GOOD ENOUGH, rms/residual/correction for camera 3, the `camera list` lens column (measured for 3; legacy for 2 and 4 until redone), and that `get_calibration`/MCP show the same.
- [x] *(deferred by stakeholder decision 2026-10-16 to the post-merge test on master; results go in the runbook's Results table)* Daemon CPU and detection-loop frame rate observed during a session are noted (the architecture review's risk); any regression is filed as an issue.
- [x] *(deferred by stakeholder decision 2026-10-16 to the post-merge test on master; results go in the runbook's Results table)* A two-camera run (2 and 4) and an agent-driven MCP run are each exercised once and noted.
- [x] Full test suite passes once (`close_sprint` gate); this ticket does not add a separate full-suite step. (Deferred to `close_sprint` by design; this ticket added no separate run.)

## Implementation Plan

Documentation and verification only. If any live check fails, file an issue with the status record attached rather than patching in this ticket.

## Testing

Hardware-free: none new. Manual: the runbook above, executed by the stakeholder.

- **Verification command**: `uv run pytest` (scoped to the modules named above; the full suite runs once, in `close_sprint`)


## Runbook

`docs/knowledge/2026-10-16-charuco-intrinsics-live-acceptance-runbook.md`
(includes the Results table template).

## Pre-flight (agent) results

Run 2026-10-15 on this Mac, foreground, no cameras opened, daemon untouched.

- mypy fix (tests/test_typing.py gate): None guard in intrinsics/collector.py, explicit zero dist arrays in collector.py (solvePnPGeneric) and solver.py (calibrateCamera), typed `_as_matrix3` helper in intrinsics/session.py, `shell: Any` annotation in view/intrinsics_gui.py.

- `uv run aprilcam camera intrinsics --help`: usage printed with `camera [camera ...]`,
  `--board SPEC` (default `$APRILCAM_CHARUCO_BOARD` or `5x7:37:28:DICT_5X5_100`),
  `--install`, `--no-gui`, `-d/--daemon HOST`. Matches the README and runbook.
- `uv run python -c "import aprilcam.view.intrinsics_gui"`: imports cleanly;
  `_headless_reason()` returned `None` (a display is available on this host).
- `uv run pytest tests/aprilcam/mcp/test_intrinsics_tools.py -q`: 7 passed (four tools registered/exercised).
- `uv run pytest tests/aprilcam/intrinsics -q`: 68 passed.
- No-cv2 / GUI / CLI selection (`tests/test_no_cv2_in_client_modules.py`,
  `tests/test_cv2_import_blocker_console_script.py`, `tests/aprilcam/view/test_intrinsics_gui.py`,
  `tests/aprilcam/cli` with `-k "intrinsics or no_cv2 or blocker"`): 48 passed.
- Install fact: the stakeholder's pipx venv (0.20260914.1) is a non-editable
  install from `/Volumes/Proj/proj/RobotProjects/aprilcam[daemon]`, so it needs
  `pipx install --force ...[daemon]` before `camera intrinsics` exists.

## Deferred verification

Stakeholder decision (2026-10-16, Eric Busboom): "crank through all three
sprints; I will test them on main when I get back." The live verification on
cameras 2, 3 and 4 is performed by the stakeholder on master after the sprint
merges, not before close. The sprint's hardware-free suite (run in
`close_sprint`) is the close gate.

The stakeholder will run
`docs/knowledge/2026-10-16-charuco-intrinsics-live-acceptance-runbook.md` and
record results in that runbook's Results table. If anything fails, file a new
issue in `clasi/issues/` with the status record attached, rather than patching
here. The runs to record:

- Time of board motion to GOOD ENOUGH on camera 3; views / rms / residual / correction.
- `camera list` lens column for 2, 3, 4 before, after install, and after `calibrate 3`;
  `lens model: measured` line from `calibrate 3`; confirmation of no daemon restart after step 1.
- `get_calibration` / MCP `lens_source` agreement with the GUI numbers.
- Daemon CPU (idle vs in-session) and detection fps (baseline vs in-session); an issue
  filed for any regression.
- Two-camera run (2 and 4), `--no-gui` run, and agent-driven MCP run, each once.
