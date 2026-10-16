---
id: '013'
title: Live acceptance runbook on cameras 2, 3 and 4
status: open
use-cases:
- SUC-001
- SUC-003
- SUC-004
- SUC-005
- SUC-006
depends-on: ['012']
github-issue: ''
issue: pose-based-playfield-calibration-with-marker-corners-and-measured-lens.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Live acceptance runbook on cameras 2, 3 and 4

## Description

Write the stakeholder-run live acceptance runbook and record pre-flight
results, in the style of sprint 044 ticket 012
(`clasi/sprints/044-*/tickets/done/` and its runbook). Programmers execute the
pre-flight (hardware-free) part; the live part is run by the stakeholder on
cameras 2, 3 and 4 and results are recorded in the ticket afterwards. This
ticket also owns confirming or correcting the planned defaults (Open
Questions 2-4; 1 is resolved by detection, 2 is answered for the main field at 46 mm) against reality and retuning thresholds in `gates.py` if the
numbers demand it.

Runbook contents (each step lists the command, expected output, and a
pass/fail field):
1. Pre-flight: lens model installed for each camera at the running
   resolution (`camera list` shows `lens: measured`); definition has sizes;
   the daemon runs this branch.
2a. **Main field, cameras 2 and 3 (10 markers, 46 mm, mixed
   orientations):** `camera calibrate` on each; expect `method: pose`, rms
   corner reprojection within the gate, no marker flagged by the leave-one-out
   residual (reference worst 0.75 cm), and derived world positions of the ten
   markers within 0.5 cm of the centre-point solve's (which fits to 0.23 cm
   rms on the corrected definition with the camera-2 legacy lens). Pass
   criterion is "at least as good as centre-point". As a negative check,
   temporarily swap the positions of ids 9 and 10 in a scratch definition: the
   calibrate result must name a marker via the leave-one-out residual.
2. **Camera 3, secondary field (four corner markers):** `camera calibrate 3`;
   expect `method: pose`, 16 corners, px and cm error, scale agreement within
   threshold, `lens model: measured`, height and nadir plausible (tape-check
   the height).
3. **Fewer markers:** cover two, then three corners (mark them `helpful` or use
   a two-marker diagonal definition): reduced + warning. Leave one: low
   confidence. Cover a `required` marker: fails by id, previous calibration
   intact.
4. **No-lens fallback:** temporarily remove/rename a camera's
   `intrinsics.json` (or use a different resolution): `method: centre-point`
   with reason, results as before the sprint.
5. **Camera 2, parallax:** calibrate, then read a registered mounted tag with
   `where`/`get_tags`; the pose-derived position should reproduce ~144 cm over
   nadir (3.47, -2.85) within tolerance and floor positions should match the
   `camera locate` result.
6. **Camera 4:** repeat step 2 on the third camera; record any gate that needs
   retuning.
7. **ChArUco at origin** (field with no markers, or temporarily ignoring
   them): lay the board per the definition, calibrate, remove it; compare
   `to_world` of known marker positions with the marker-based solve (< 0.5
   cm). Record the physical placement convention confirmed.
8. `camera calibrate N --with-intrinsics` is rejected; old calibration files
   still load after a daemon restart.
9. Results table; thresholds changed (if any) with the evidence; issues found
   filed as new issues.

## Acceptance Criteria

- [ ] Runbook committed under the ticket (or `docs/knowledge/`), runnable
      without reading the source.
- [ ] Pre-flight results (suite green, synthetic acceptance reproduced)
      recorded.
- [ ] Live results table filled in by the stakeholder; thresholds/defaults
      adjusted and documented, or confirmed unchanged.
- [ ] Main-field acceptance (step 2a) recorded for cameras 2 and 3: rms,
      worst leave-one-out, and the ten-marker comparison to centre-point.
- [ ] Open Questions 2-4 each marked answered (with the value) or "default
      kept".

## Implementation Plan

Writing and measurement only; any code change that results (threshold tweaks)
is a small edit to `gates.py` constants plus a test update, recorded here.

## Testing

- **Existing tests to run**: full suite once (`uv run pytest`) as the
  pre-flight.
- **New tests to write**: only if a threshold change needs a regression test.
- **Verification command**: `uv run pytest`

## Documentation Updates

Knowledge-base entry from ticket 012 updated with the tuned numbers.
