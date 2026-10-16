---
id: '007'
title: 'Report the pose numbers: CLI, MCP, camera list and the camera lens alias'
status: open
use-cases:
- SUC-001
- SUC-003
- SUC-004
depends-on: ['006']
github-issue: ''
issue: pose-based-playfield-calibration-with-marker-corners-and-measured-lens.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Report the pose numbers: CLI, MCP, camera list and the camera lens alias

## Description

Make the numbers visible to people and agents. `aprilcam camera calibrate`
prints method, confidence, corner count, corner reprojection error in px and
cm, the scale-agreement figure, angular spread, `lens model: measured`, and the
derived camera height / nadir / tilt, and the per-marker leave-one-out
residual (worst marker id and cm, plus the full per-marker table with
`--verbose` or equivalent) so a disagreeing marker is named; warnings (`reduced`, `low`, helpful
markers missing, fallback reason) go to stderr like other caveats. The MCP
`calibrate` and `get_calibration` tools return the same fields; `camera list`
shows method and confidence next to the calibrated flag. The existing blanket
caveat "reprojection_error_cm is degrees-of-freedom-limited with only <=4
matched ids" is replaced by the honest labels (it no longer applies to a
pose solve).

Also add the user-facing alias: `aprilcam camera lens ...` as an alias of
`aprilcam camera intrinsics ...` (help text says "lens model"), and use "lens
model" / "field calibration" wording in new output.

## Acceptance Criteria

- [ ] CLI success output for a pose calibration lists every number named
      above, including the worst leave-one-out marker; for centre-point it prints `method: centre-point (<reason>)`.
- [ ] CLI failure output names the failing gate, its threshold and the
      measured value; `missing_ids` wording unchanged.
- [ ] MCP `calibrate`/`get_calibration` expose the fields (leave-one-out
      residuals included, per marker) (documented in the
      tool docstrings, which are the agent-facing contract).
- [ ] `camera list` shows e.g. `calibrated (pose, full)` / `(centre-point)`.
- [ ] `aprilcam camera lens` behaves identically to `camera intrinsics`.
- [ ] `client/` and `mcp/` remain cv2-free.

## Implementation Plan

**Files to modify:** `src/aprilcam/cli/calibrate.py`, `src/aprilcam/cli/camera.py`
(alias + list column), `src/aprilcam/cli/_common.py` if shared formatting,
`src/aprilcam/mcp/tools/configuration.py`, `src/aprilcam/mcp/tools/perception.py`
(list), `src/aprilcam/client/models.py` if any formatting helpers.

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/cli tests/aprilcam/mcp -x`
- **New tests to write**: capsys-style CLI output tests per outcome (full,
  reduced, low, fallback, gate failure); MCP return-shape tests; alias
  parse test.
- **Verification command**: `uv run pytest tests/aprilcam/cli tests/aprilcam/mcp tests/test_no_cv2_in_client_modules.py`

## Documentation Updates

CLI help text; prose docs in ticket 012.
