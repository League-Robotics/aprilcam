---
id: '012'
title: Live acceptance runbook and stakeholder verification on camera 3
status: open
use-cases: [SUC-001, SUC-004, SUC-005, SUC-006]
depends-on: ["008", "009", "011"]
github-issue: ''
issue: charuco-board-intrinsics-calibration-as-a-daemon-workflow.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Live acceptance runbook and stakeholder verification on camera 3

## Description

Final verification against the issue's Acceptance section. Agents may not restart the daemon or open cameras, so this ticket delivers a runbook the stakeholder executes, plus a recorded result. It also measures the CPU/contention risk flagged in the architecture review.

## Acceptance Criteria

- [ ] `docs/knowledge/` (or the ticket body) contains a step-by-step runbook: pipx/venv update, start the daemon once, `aprilcam camera intrinsics 3`, follow NEXT to GOOD ENOUGH in under a minute of board motion on the secondary field, Enter, then immediately `aprilcam camera calibrate 3` reports the measured model carried forward; no daemon restart anywhere.
- [ ] Stakeholder records: time to GOOD ENOUGH, rms/residual/correction for camera 3, the `camera list` lens column (measured for 3; legacy for 2 and 4 until redone), and that `get_calibration`/MCP show the same.
- [ ] Daemon CPU and detection-loop frame rate observed during a session are noted (the architecture review's risk); any regression is filed as an issue.
- [ ] A two-camera run (2 and 4) and an agent-driven MCP run are each exercised once and noted.
- [ ] Full test suite passes once (`close_sprint` gate); this ticket does not add a separate full-suite step.

## Implementation Plan

Documentation and verification only. If any live check fails, file an issue with the status record attached rather than patching in this ticket.

## Testing

Hardware-free: none new. Manual: the runbook above, executed by the stakeholder.

- **Verification command**: `uv run pytest` (scoped to the modules named above; the full suite runs once, in `close_sprint`)
