---
status: pending
---

# Image-matrix fixtures: test suite + capture-tool follow-ups

The canonical captured test fixtures now exist (2026-07-29):
`tests/images/arducam-ov9782-usb-camera/` (19 images: exposure 3→40,
gain 1→16, auto-exposure, WB 3000/6500/auto, 2 stress combos; `exp-40`
washed as expected) and `tests/images/hd-usb-camera/` (9 images: gain
sweep, auto-exposure, auto-WB toggles), each with `manifest.json`, plus
`tests/images/field-state.json`. Produced by
`testdev/capture_matrix.py` + `testdev/capture_matrix.yaml` per
[docs/test-plans/camera-image-matrix.md](../../docs/test-plans/camera-image-matrix.md);
recorded in [design.md](../../docs/design/design.md) §8. Regenerate
with `uv run python testdev/capture_matrix.py --all` (cameras free,
daemon stopped; on this Mac the process needs Camera permission —
Terminal has it, the Claude app does not yet).

## Remaining work

1. **Test suite** (`tests/test_image_matrix.py`) per plan §6:
   tag recall per configuration (washout cliff documented as an
   assertion), world-coordinate accuracy vs. surveyed positions,
   geometry-convention regressions (+x east / +y north / north-up),
   calibration robustness, color detection, preprocessing
   effectiveness. Skip cleanly when fixtures are absent.
2. **HD USB control ordering** (found in the first run): its
   `exposure-time-abs` and `white-balance-temp` values are rejected on
   a live handle while auto modes are active — the tool must switch
   `auto-exposure-mode` to manual / disable auto-WB *before* pushing
   values (or reopen per variation), then re-run to fill the 12 skipped
   variations.
3. **Verify field state**: baseline detections found only ArUco 4×4
   markers (9 on the Arducam, 4 on the HD cam), zero 36h11 AprilTags —
   stakeholder to confirm whether AprilTags are currently on the field;
   update `field-state.json` from "unverified" accordingly. If tags ARE
   present, investigate detection (echoes
   [[calibration-json-missing-world-scale-metadata-yields-zero-world-coords]]'s
   missing-pipeline-block finding).
4. Decide whether fixture JPEGs are committed to git (plan says yes,
   ~5 MB/camera) and commit them with the tool.
