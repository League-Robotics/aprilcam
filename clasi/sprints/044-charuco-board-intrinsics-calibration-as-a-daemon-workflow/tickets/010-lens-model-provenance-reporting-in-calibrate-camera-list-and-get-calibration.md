---
id: '010'
title: Lens-model provenance reporting in calibrate, camera list, and get_calibration
status: open
use-cases: [SUC-004, SUC-005]
depends-on: ["001", "006"]
github-issue: ''
issue: charuco-board-intrinsics-calibration-as-a-daemon-workflow.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Lens-model provenance reporting in calibrate, camera list, and get_calibration

## Description

Report lens-model provenance everywhere an operator looks. The wire `Calibration` converter currently drops `camera_matrix`/`dist_coeffs` (`client/models.calibration_from_proto`), so provenance needs explicit new fields. Also include the cheap clean refusal for the six-ground-truth-point `_estimate_intrinsics` crash (`clasi/issues/estimate-intrinsics-crashes-with-exactly-six-ground-truth-points.md`).

## Acceptance Criteria

- [ ] `CameraState` gains `lens_model` ('measured'|'estimated'|'legacy'|'absent') and `lens_pending` (store model differs from the one embedded in the current calibration); proto and converters updated (rerun `scripts/compile_proto.py`).
- [ ] `camera list` rows show `lens=measured|estimated|legacy|absent` and a `lens-pending` flag; row prefix unchanged.
- [ ] `Calibration` interface/proto gain `lens_source` (and a small provenance summary for measured); MCP `get_calibration` exposes it; `get_calibration` for a never-calibrated camera that has a stored lens still reports None (lens status is on `CameraState`).
- [ ] `aprilcam camera calibrate` output states which lens model it carried forward (measured / estimated / legacy / none), including the resolution-mismatch warning, and reports the refusal of `--with-intrinsics` over a measured model; `--with-intrinsics` help text documents it as the no-board fallback.
- [ ] The six-point `_estimate_intrinsics` crash becomes a clean `intrinsics estimation rejected` result with a reason; no traceback.

## Implementation Plan

Modify `types.py` (`CameraState`), `proto/aprilcam_v2.proto` + regenerated pb2, `daemon/rpc.py`, `client/models.py`, `daemon/service.py` (`list_cameras` overlay beside `_calibration_flags`), `cli/_common.py` (row format), `cli/calibrate.py`, `mcp/tools/configuration.py`, `calibration/calibration.py` (six-point guard).

## Testing

Extend CLI list/calibrate tests and service tests: legacy file reports 'legacy', no data 'absent', store model 'measured', pending flag logic, calibrate message per source, proto round trip, six-point regression test, MCP get_calibration. Run `uv run pytest tests/aprilcam/cli tests/aprilcam/daemon tests/aprilcam/mcp tests/aprilcam/calibration tests/aprilcam/client`.

- **Verification command**: `uv run pytest` (scoped to the modules named above; the full suite runs once, in `close_sprint`)
