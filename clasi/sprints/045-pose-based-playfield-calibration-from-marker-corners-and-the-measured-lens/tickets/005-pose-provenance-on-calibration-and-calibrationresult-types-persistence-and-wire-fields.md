---
id: '005'
title: 'Pose provenance on Calibration and CalibrationResult: types, persistence and
  wire fields'
status: open
use-cases:
- SUC-001
- SUC-004
depends-on: ['004']
github-issue: ''
issue: pose-based-playfield-calibration-with-marker-corners-and-measured-lens.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Pose provenance on Calibration and CalibrationResult: types, persistence and wire fields

## Description

Give a solve somewhere to put its numbers, end to end, additively. This is
the "derived homography / camera position + wire fields" step: types,
persistence, and the proto/rpc/client path, with no behaviour change yet (the
Calibrator fills them in ticket 006).

`CalibrationResult` gains (all optional, default `None`/empty): `method`
(`"pose"` | `"centre-point"`), `method_reason` (why centre-point was used),
`confidence` (`"full"` | `"reduced"` | `"low"`), `corner_count`,
`reprojection_error_px`, `scale_agreement` (ratio), `angular_spread_deg`,
`camera_height_cm`, `camera_nadir` (WorldPoint), `camera_tilt_deg`,
`notes: tuple[str, ...]`, `helpful_missing_ids`. `Calibration` gains the
persisted subset: `method`, `confidence`, `camera_height_cm`,
`camera_nadir_x_cm`/`_y_cm`, `camera_tilt_deg`, `reprojection_error_px`,
`corner_count`, `scale_agreement`. Declared fields, never properties; frozen
`kw_only` dataclasses; no `typing.Protocol`. `Calibration` gets one method,
`camera_geometry()` -> `Optional[(WorldPoint, height_cm)]`, returning the pose
position only when `method == "pose"` and not `stale` (used by ticket 009).

`to_dict`/`from_dict` omit absent fields (old files byte-identical on write
and load unchanged). Proto: additive fields on `CalibrationResult`,
`Calibration`, and `CameraState` (`calibration_method`,
`calibration_confidence`, plain strings, empty = unknown, like `lens_model`).
`rpc.py`, `client/models.py`, `types.CameraState`, and
`DaemonService._calibration_flags`/`list_cameras` carry them. Do **not** touch
the soon-to-be-deleted `intrinsics_*` fields here (ticket 008).

## Acceptance Criteria

- [ ] New fields exist on the value types and round-trip through
      `to_dict`/`from_dict`; a pre-sprint `calibration.json` loads with all new
      fields `None` and re-serialises identically.
- [ ] Proto regenerated (`uv run --extra dev python scripts/compile_proto.py`);
      `calibration_result_to_proto`/`from_proto` and
      `calibration_to_proto`/`from_proto` round-trip the new fields; `optional`
      presence preserved.
- [ ] `CameraState.calibration_method`/`calibration_confidence` populated in
      `list_cameras` from the held calibration (empty for never-calibrated or
      old files).
- [ ] `tests/test_no_cv2_in_client_modules.py` passes.
- [ ] `Calibration.camera_geometry()` unit-tested (pose/fresh, pose/stale,
      centre-point, absent).

## Implementation Plan

**Files to modify:** `src/aprilcam/calibration/interfaces.py`,
`src/aprilcam/calibration/calibration.py` (`Calibration` dataclass only),
`src/aprilcam/types.py`, `proto/aprilcam_v2.proto` + generated,
`src/aprilcam/daemon/rpc.py`, `src/aprilcam/daemon/service.py` (flags only;
keep it thin), `src/aprilcam/client/models.py`.

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/calibration tests/aprilcam/daemon tests/aprilcam/client tests/test_no_cv2_in_client_modules.py`
- **New tests to write**: persistence back-compat, proto round-trips,
  `camera_geometry()` cases, `list_cameras` overlay.
- **Verification command**: `uv run pytest tests/aprilcam/calibration tests/aprilcam/daemon tests/aprilcam/client`

## Documentation Updates

Proto comments; prose docs in ticket 012.
