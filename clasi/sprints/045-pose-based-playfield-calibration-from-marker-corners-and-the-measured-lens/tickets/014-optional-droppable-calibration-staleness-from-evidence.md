---
id: '014'
title: 'Optional (droppable): calibration staleness from evidence'
status: open
use-cases:
- SUC-008
depends-on: ['006', '009']
github-issue: ''
issue: pose-based-playfield-calibration-with-marker-corners-and-measured-lens.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Optional (droppable): calibration staleness from evidence

## Description

**OPTIONAL AND DROPPABLE.** Do this only if tickets 001-013 are done and the
sprint has room. If not done, leave it open (or mark it dropped), file a
follow-up issue titled "Calibration staleness from evidence (pose-based)", and
note the decision at sprint close. Nothing else in the sprint depends on it
(`completes_issue` is false so the sprint's issue still archives).

Today `calibration_stale` flips only on camera reopen and on the
opportunistic `compute_drift_cm` check against matched markers
(`calibration.compute_drift_cm`, the `DetectionEngine` drift hook). With a
stored pose, any visible known marker can be re-solved at low rate against the
stored camera pose: reproject each visible matched marker's corners through the
stored lens and pose and compare to the detected corners (px) and field
position (cm). A persistent disagreement beyond tolerance flips stale via
`DaemonService._mark_stale`; insufficient evidence (marker covered, dark frame)
returns "cannot verify" -- never "confirmed" (UC-11.S-3 stance, matching
`compute_drift_cm`'s `None`).

Keep it inside the existing drift hook (no new thread): extend
`compute_drift_cm`'s neighbour with a pose-aware variant used when
`Calibration.method == "pose"`, with hysteresis (N consecutive disagreeing
checks) and a low-rate gate (reuse the existing check cadence).

## Acceptance Criteria

- [ ] Synthetic: shifting the camera pose by a few cm/degrees flips stale
      within the hysteresis window; an unmoved camera with realistic noise
      never does over a long run.
- [ ] A single visible marker suffices; zero visible reads "cannot verify".
- [ ] Centre-point calibrations keep the existing drift behaviour unchanged.
- [ ] No extra per-frame cost (reuses the detections already produced).

## Implementation Plan

**Files to modify:** `src/aprilcam/calibration/calibration.py` (or `pose.py`
for the reprojection helper), `src/aprilcam/daemon/detection.py` (hook),
`src/aprilcam/daemon/service.py` (wiring only).

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/daemon/test_detection.py tests/aprilcam/calibration -x`
- **New tests to write**: as listed.
- **Verification command**: `uv run pytest tests/aprilcam/daemon tests/aprilcam/calibration`

## Documentation Updates

Calibration DESIGN.md staleness section (add after the fact if built).
