---
id: '010'
title: 'Calibration target schema: ChArUco spec and placement in the definition, loader
  and wire'
status: open
use-cases:
- SUC-006
- SUC-002
depends-on: ['002']
github-issue: ''
issue: pose-based-playfield-calibration-with-marker-corners-and-measured-lens.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Calibration target schema: ChArUco spec and placement in the definition, loader and wire

## Description

Let a definition name a calibration target instead of (or alongside) markers:
a ChArUco board laid flat on the field. Add `calibration_target` to the
definition: `kind: "charuco"`, `board` (the sprint 044 spec string
`COLSxROWS:SQUARE_MM:MARKER_MM:DICT`, parsed with `BoardSpec.parse` semantics),
`centre_x_cm`/`centre_y_cm` (default 0, 0 -- Open Question 7), `rotation_deg`
(default 0; 0 = board upright, top edge toward north, lattice rows increasing
southward). Value type `CalibrationTarget` (frozen, kw_only, declared fields)
in `config/interfaces.py`, cv2-free, so `config` does not import `intrinsics`;
the board string is validated structurally in the loader without importing
`intrinsics`. A definition may carry markers, a target, or both; one with
neither is still valid (as today) but cannot be calibrated by pose.

Wire: `PlayfieldDefinition.calibration_target` (new message
`CalibrationTarget`), plumbed through `rpc.py`, `client/models.py`,
`client/daemon_client.py`. Regenerate protobuf. Keep the sprint 044
"additive, optional" pattern.

## Acceptance Criteria

- [ ] Loader parses, validates (spec well-formed, dictionary name known to the
      accepted set, centre within the field +/- board half-size, finite
      rotation) and round-trips `calibration_target`; absent = unchanged.
- [ ] Wire round-trip test; old definitions encode with the field unset.
- [ ] `required_marker_ids()` semantics unchanged by the presence of a target
      (a target-only definition has no required markers).
- [ ] cv2-free test still passes.

## Implementation Plan

**Files to modify:** `src/aprilcam/config/interfaces.py`, `config/loader.py`,
`proto/aprilcam_v2.proto` + generated, `daemon/rpc.py`, `client/models.py`,
`client/daemon_client.py`.

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/config tests/aprilcam/client tests/aprilcam/daemon/test_rpc.py tests/test_no_cv2_in_client_modules.py`
- **New tests to write**: loader accept/reject matrix; proto round-trip.
- **Verification command**: `uv run pytest tests/aprilcam/config tests/aprilcam/client tests/aprilcam/daemon`

## Documentation Updates

Schema section of `config/DESIGN.md` (final pass in ticket 012).
