---
id: '001'
title: Per-camera lens store and Calibrator carry-forward with legacy fallback
status: done
use-cases:
- SUC-004
- SUC-005
depends-on: []
github-issue: ''
issue: charuco-board-intrinsics-calibration-as-a-daemon-workflow.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Per-camera lens store and Calibrator carry-forward with legacy fallback

## Description

Move the lens model out of per-playfield `calibration.json` into a per-camera store, and make `Calibrator` consult it. Design: sprint.md Architecture, Decisions 1-3. A lens model is per camera and per resolution; it must survive playfield changes and failed field calibrations. This ticket is cv2-free storage plus the `Calibrator` carry-forward change; no ChArUco code yet.

## Acceptance Criteria

- [x] `calibration/lens.py` defines `LensModel` (camera_matrix, dist_coeffs, image_size, source, board_spec, views, rms_px, residual_px, correction_px, installed_at) and `LensStore` persisting `<state_dir>/cameras/<slug>/intrinsics.json` atomically (tmp+fsync+`os.replace`); corrupt/missing file tolerated (warning, treated as absent).
- [x] `LensModel`/provenance value types that clients will need live in a cv2-free module (`calibration/interfaces.py` additions or `lens.py` itself, which must not import cv2).
- [x] `LensStore.get(slug, image_size)` returns the measured model only when image size matches; a mismatch returns None and a reason the caller can surface.
- [x] `Calibrator` gains an optional `get_lens_model` constructor argument. Carry-forward order: store model (size-matched) > prior calibration's embedded matrix > none. Behaviour is unchanged when the argument is absent.
- [x] `Calibration` gains additive `lens_source` ('measured'|'estimated'|'legacy'), persisted in `calibration.json`; files without it that carry a matrix read back as 'legacy'; `to_dict`/`from_dict` round-trip old and new files.
- [x] `CalibrationResult` gains `lens_model_source` ('measured'|'estimated'|'legacy'|'none') populated by `calibrate` (reporting in CLI/MCP is ticket 010).
- [x] `estimate_intrinsics=True` is refused with an explicit failure result naming the installed measured model while one is installed (sprint.md Open Question 2).
- [x] A failed `calibrate` or a playfield change does not touch `intrinsics.json`.
- [x] `tests/test_no_cv2_in_client_modules.py` still passes.

## Implementation Plan

Files: create `src/aprilcam/calibration/lens.py`; modify `calibration/calibration.py`, `calibration/interfaces.py`, `daemon/service.py` (construct `LensStore` from `self._directories.state_dir`, pass `get_lens_model` into the `Calibrator`; no new RPCs here). Reuse the atomic-write pattern from `DaemonService._persist_calibration`. Read `Calibrator.calibrate` carry-forward branch (around the `prior = self._get_prior_calibration(...)` lines) and the existing restart-carries-forward test before changing it; keep it green. Size check uses the captured frame shape. Docs: module docstrings only here (DESIGN.md is ticket 011).

## Testing

New `tests/aprilcam/calibration/test_lens.py`: store round-trip, atomic write, corrupt file, size mismatch. Extend `tests/aprilcam/calibration/` Calibrator tests: store beats embedded matrix; legacy fallback when no store file (matrix present, no `lens_source`, reads 'legacy'); none; fresh `DaemonService` against an on-disk legacy `calibration.json` still carries the lens forward; failed calibrate leaves the store intact; `--with-intrinsics` refused while measured. Run `uv run pytest tests/aprilcam/calibration tests/aprilcam/daemon/test_service.py tests/test_no_cv2_in_client_modules.py`.

- **Verification command**: `uv run pytest` (scoped to the modules named above; the full suite runs once, in `close_sprint`)
