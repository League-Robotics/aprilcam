---
id: '002'
title: Repoint all camera calibrate pointers to playfield calibrate
status: open
use-cases: ["SUC-002"]
depends-on: ["001"]
github-issue: ''
issue: move-calibrate-to-playfield-calibrate.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Repoint all camera calibrate pointers to playfield calibrate

## Description

After ticket 001, every user-facing pointer to `camera calibrate` must say
`playfield calibrate` (stakeholder decision 4). The deprecated alias itself
(and its test) is the only remaining mention. Leave historical
`docs/knowledge/*`, archived sprints (`clasi/sprints/done`), `archive/`,
and `.claude/worktrees` alone.

Files (from a grep at planning time; re-grep, do not trust this list):
- Code strings/docstrings: `src/aprilcam/cli/playfield.py` (NotCalibrated
  hint ~L401 and docstrings ~L57, L350), `cli/_common.py` (~L82, L216),
  `cli/camera.py` (~L590 docstring), `daemon/streaming.py`,
  `view/viewer.py` (~L104, L979, L1011), `view/intrinsics_model.py` (~L315).
  Hint strings that tests assert on need their tests updated:
  `tests/aprilcam/view/test_intrinsics_model.py`,
  `tests/aprilcam/view/test_intrinsics_gui.py`,
  `tests/aprilcam/cli/test_playfield_cli.py`, daemon tests that match text.
- Packaged guide: `src/aprilcam/AGENT_GUIDE.md` (~L64, L250); also describe
  `playfield calibrate [CAMERA]` and HOST-N.
- DESIGN docs (canonical, after the sprint-overlay content is merged at
  close; edit the pointers now): `src/aprilcam/cli/DESIGN.md`,
  `calibration/DESIGN.md`, `daemon/DESIGN.md`, `view/DESIGN.md`.
- `docs/design/use-cases/cli.md`, `cli-interface-trace.md`, `daemon.md`,
  `docs/design/development-plan.md`.
- `README.md` (~L155, L184); `deploy/README.md` if it mentions it.

Do not edit generated `src/aprilcam.egg-info`.

## Acceptance Criteria

- [ ] `grep -rn "camera calibrate"` over `src`, `tests`, `README.md`,
      `deploy`, `docs/design` (excluding `src/aprilcam.egg-info`) matches
      only the deprecated alias (its code, help, deprecation message, its
      test) and deliberate "formerly `camera calibrate`" notes.
- [ ] The NotCalibrated hint reads `... calibrate first with 'aprilcam
      playfield calibrate <camera>'`.
- [ ] Tests asserting old hint text updated and passing.
- [ ] AGENT_GUIDE documents `playfield calibrate [CAMERA]`.

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/cli tests/aprilcam/view tests/aprilcam/daemon/test_service.py -q` (plus any test file the grep shows).
- **New tests to write**: none beyond updating assertions; a small test
  that the NotCalibrated hint mentions `playfield calibrate` if none exists.
- **Verification command**: the grep above, plus the scoped pytest run.
