---
id: 008
title: Delete the single-view intrinsics estimator and --with-intrinsics
status: open
use-cases:
- SUC-007
depends-on: ['007']
github-issue: ''
issue: pose-based-playfield-calibration-with-marker-corners-and-measured-lens.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Delete the single-view intrinsics estimator and --with-intrinsics

## Description

Remove the single-view intrinsics estimator everywhere. Its fit is
non-physical (the fx/k1 ridge) and sprint 044's measured ChArUco lens model
replaces it.

Delete: `_IntrinsicsFit`, `_held_out_split`, `_initial_camera_matrix_guess`,
`_sanity_check_intrinsics`, `_estimate_intrinsics`, `_fit_intrinsics`, their
module constants (`MIN_SURVEYED_INTRINSICS_POINTS`, fx-ratio and k2/k1 bounds,
coefficient limits) and the module-docstring "Intrinsics estimation" section;
the `estimate_intrinsics` parameter from `Calibrator.calibrate`,
`DaemonService.calibrate`, `Daemon.calibrate` (`daemon/interfaces.py`),
`DaemonClient.calibrate`, the CLI `--with-intrinsics` flag, the MCP
`calibrate` parameter and `rpc.py`; `CalibrationResult.intrinsics_estimated`
/ `intrinsics_mode` / `elevated_features_used`; AprilTag collection in
`_detect_markers` (it existed only for the estimator). Proto: remove
`CalibrateRequest.estimate_intrinsics` (field 2) and `CalibrationResult`
fields 6-8, adding `reserved` entries so numbers are never reused; regenerate.

Keep: `LENS_SOURCE_ESTIMATED`, reading `lens_source: "estimated"` from old
`calibration.json`, and `Calibration.camera_matrix`/`dist_coeffs` (embedded
lens); carry-forward behaviour for old files is unchanged.

Close as superseded (via `move_issue_to_done` or the project's issue-closing
flow, with a note pointing at sprint 045):
`clasi/issues/estimate-intrinsics-crashes-with-exactly-six-ground-truth-points.md`,
`clasi/issues/later/coplanar-intrinsics-rejected-on-a-second-camera-reconsider-the-fx-plausibility-gate.md`.
Also review
`clasi/issues/later/sprint-030-unfinished-verification-distortion-benefit-unproven.md`:
it is superseded only if its remaining content is solely about proving the
estimator's distortion benefit; if so close it with the same note, and if it
holds any still-valid verification item, leave it open and say which part
survives. Record the decision either way in the ticket's completion notes.

## Acceptance Criteria

- [ ] `grep -rn "estimate_intrinsics\|with-intrinsics\|_fit_intrinsics\|_estimate_intrinsics" src/ tests/` is empty (docs/knowledge history excepted).
- [ ] `calibration.py` shrinks by roughly 400 lines; no dead constants remain.
- [ ] A `calibration.json` with `lens_source: "estimated"` (and a legacy file
      with a matrix and no source) still loads, reports its source in
      `get_calibration`/`camera list`, and is used by the centre-point path.
- [ ] `aprilcam camera calibrate --with-intrinsics` exits with the standard
      unknown-argument error; MCP schema lacks the parameter.
- [ ] Both superseded issues closed (third decided and recorded).
- [ ] Wire: reserved field numbers present in the proto; older client
      sending field 2 is ignored harmlessly.

## Implementation Plan

**Files to modify:** `src/aprilcam/calibration/calibration.py`,
`calibration/interfaces.py`, `calibration/DESIGN.md` (remove the estimator
section; final prose in 012), `daemon/service.py`, `daemon/rpc.py`,
`daemon/interfaces.py`, `client/daemon_client.py`, `client/models.py`,
`cli/calibrate.py`, `mcp/tools/configuration.py`, `proto/aprilcam_v2.proto` +
generated, `docs/design/...` references, plus every test that exercised the
estimator (delete those tests; keep any that cover shared helpers).

## Testing

- **Existing tests to run**: `uv run pytest tests -x --ignore=tests/aprilcam/calibration/test_full_calibrate_hardware.py`
- **New tests to write**: old-file-loads tests; CLI/MCP signature tests.
- **Verification command**: `uv run pytest tests/aprilcam/calibration tests/aprilcam/daemon tests/aprilcam/cli tests/aprilcam/mcp tests/aprilcam/client`

## Documentation Updates

Remove estimator mentions from `docs/design/` use cases and knowledge notes
that describe current behaviour (history entries stay as history); ticket 012
does the final sweep.
