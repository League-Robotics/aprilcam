---
status: pending
---

# Flat-field capture: absolute target level, configurable gain cap, and nearest-edge extension instead of mean fill

## Description

Three limits of sprint 037's `capture_flat_field` math
(`calibration/flatfield.py`) showed up the first day it was used on the
real rig (2026-09-03, camera 3 `arducam-ov9782-usb-camera` with a
neutral-density filter fitted). Each was worked around by rebuilding the
map by hand from raw frames; the workflow should do them itself.

1. **The corrected sheet is normalised to its own mean, so the output is
   dim.** With the sheet reading ~110 at the working exposure the daemon's
   map produced a corrected field of ~104. The stakeholder wanted it
   bright. Rebuilding with an absolute target of 235 for the sheet gave
   a corrected field of 225-235. Add a `--target` (default: the current
   behaviour, or 235) so brightness is a capture parameter, and report
   the resulting corrected level.

2. **The gain cap of 4.0 is too low for a vignetted corner.** Through the
   ND filter camera 3's north-east corner reads ~25/255 on a white sheet,
   needing ~9x. At the cap the corner stayed at ~130 and pink (green
   needed the most). A cap of 16 corrected it to 233, neutral. Make the
   cap configurable (`--max-gain`), report how much of the field hits
   it, and warn rather than silently clamp.

3. **Filling outside the mask with the interior mean bleeds into steep
   corners.** The Gaussian blur (sigma 40) mixes the bright mean fill
   into the dark corner's light-field estimate: measured, the raw corner
   was 25 but the estimate was 58, so the corner got half the gain it
   needed even below the cap. Extending the field by *nearest in-field
   value* (distance-transform labels, `cv2.distanceTransformWithLabels`)
   before blurring gave an estimate of 25 at the corner and a fully
   corrected result, and allowed a tighter blur (median 21, sigma 20)
   without wrinkle leakage on a well-laid sheet. Replace the mean fill
   with nearest-edge extension; consider exposing the blur radius.

Related, already fixed out of process (commit `fca2d62`): the capture
burst read frames through `Camera.read()` with the live map applied;
it now uses `Camera.read_raw()`. Also see
`flat-field-capture-should-white-balance-against-a-known-neutral-reference.md`
-- note that capturing at the *working* exposure (no `--exposure`
override) removed the green/magenta patches that issue describes, so its
remaining scope is the global neutral reference only.

### Acceptance

- `capture_flat_field(target=..., max_gain=...)` through RPC, client, CLI
  and MCP; defaults preserve today's numbers unless the stakeholder
  decides 235/16 should be the new defaults.
- Nearest-edge extension replaces the mean fill; a synthetic test with a
  steep dark corner must correct to within 5% of target.
- The report includes corrected-level percentiles and percent of field
  at the cap.
