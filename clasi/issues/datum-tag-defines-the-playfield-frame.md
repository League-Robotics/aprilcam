---
status: pending
---

# A datum tag defines the playfield frame: its centre is (0, 0) and its top edge is 0° heading

## Description

Stakeholder decision, 2026-10-16. The playfield boards come off and go back
on, sometimes deliberately turned round depending on where the operator is
sitting. Nothing gets re-glued. So the frame cannot be defined by a camera
("north is the far edge as camera 2 sees it", the August convention) and it
cannot be defined by re-measuring markers every time. **One tag on the board
is the datum.** The definition records which tag that is. By definition:

- the datum tag's centre is the origin (0, 0);
- the direction its top edge points is heading 0° (north, +y);
- east (+x) is 90° clockwise from that, seen from above.

On the main playfield the datum is **AprilTag 1**. Measured today in the
current (camera-defined) frame it sits at (-0.1, +0.6) cm and its top edge
points -87.5°, i.e. the current frame is rotated about 180° from the datum
frame and offset by half a centimetre. That is exactly the situation the
datum rule makes irrelevant.

## What to build

**Definition.** `playfield` gains `datum: {"family": "apriltag", "id": 1}`
(or `"aruco"`), with the tag's `size_mm`. Every other surveyed coordinate
in the definition is in the datum frame. The datum entry itself carries no
coordinates; (0, 0, 0°) is implied and the loader rejects a definition that
tries to place it elsewhere. Fields without a datum keep today's
behaviour, with a warning that the frame is camera-defined.

**Calibration.** The pose solve is unchanged (every marker corner, 46 mm
sizes, measured lens). After solving, the camera's pose is re-expressed in
the datum frame: the datum tag's detected corners give its centre and
heading in the solved frame, and the stored mapping is composed with that
rigid transform so `to_world` of the datum centre is (0, 0) and its top
edge reads 0°. The datum tag is `required`: no datum visible, no
calibration. The result reports the datum's measured size agreement and
corner residual like any marker.

**Survey in the datum frame.** The existing marker surveys are in the old
camera-defined frame. Provide a one-time conversion that re-expresses a
definition's marker positions in the datum frame from a calibrated camera
(the self-survey script in `docs/knowledge/assets/field-check/` already
re-anchors on the corner markers; re-anchor on the datum instead) and apply
it to `main-playfield.json`. Consumers that assumed the old axes (robot
code, drawn overlays, the `playfield view` deskew which puts north at the
top) follow the datum from then on, so a board put back the other way round
simply shows the deskewed view rotated, which is correct.

**Bump detection (joins the staleness issue).** With the datum declared,
"bumped" has a definition: the datum tag, re-detected at low rate through
the stored mapping, no longer reads (0, 0, 0°) within tolerance. A pure
translation is compensated by shifting the mapping; a rotation beyond
tolerance or a residual that compensation cannot remove flags
`calibration_stale` and asks for a recalibration. See
`calibration-staleness-from-evidence-pose-based.md`.

**Reporting.** `camera calibrate`, `get_calibration`, `camera list` and the
MCP tools name the datum tag and show its measured position/heading
residual at calibration time (should be ~0 by construction, so the number
to show is the datum's corner residual and size agreement).

## Acceptance

- A definition with `datum` loads; the datum has no coordinates; a
  definition placing it elsewhere is rejected with a clear message.
- Calibrating the main field: `to_world` of AprilTag 1's centre is
  (0, 0) within 0.1 cm and its heading reads 0° within 0.5°, on cameras 2
  and 4, regardless of which way round the board sits.
- Turning the board 180° (or the camera) and recalibrating gives the same
  marker coordinates in the datum frame within survey tolerance; the
  deskewed view rotates accordingly.
- A field whose datum is not visible fails to calibrate, naming the datum.
- `main-playfield.json` converted to the datum frame; the robot-side
  consumers of world coordinates are listed and checked in the runbook.
- Hardware-free tests on synthetic layouts at several board orientations;
  live runbook on the main field.
