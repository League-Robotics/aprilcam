---
date: 2026-10-16
tags: [calibration, secondary-playfield, hd-usb-camera, survey, pose, aruco]
---

# Secondary playfield: the definition on disk was wrong, and the field surveyed itself

## What was wrong

`config/aprilcam/playfields/secondary-playfield.json` (and the live copy)
held a four-corner layout with ids 1-4 that did not match the table: in a
camera-3 frame id 4 sits at the top centre (north middle) and id 2 at the
top left, and ids 0 and 5 are present but were not in the file. That is
the eight-marker layout the 2026-09-04 resurvey recorded (2 NW, 0 NE,
3 SW, 1 SE, 4 N, 5 S, 6 E, 7 W). A four-point homography fits anything
exactly, so the old centre-point calibration never noticed. The first pose
calibration did: scale agreement 1.87 and a camera "height" of -1.6 cm.

## How it was fixed

1. The eight-marker layout at the nominal +-55 / +-35 cm fit a camera-3
   frame to 0.47 cm rms, but the pose solve still failed its gates:
   1.03 cm rms and 3.1 cm leave-one-out on marker 1. The hand survey is
   1 to 2 cm off on several markers, as the September entry suspected.
2. `docs/knowledge/assets/field-check/self_survey.py` measured the field
   from camera 3 alone: solve the planar pose from all marker corners
   (46 mm size, measured lens), project each marker centre onto the plane
   with the derived homography, re-anchor the frame on the four corner
   markers (origin = their centre, x along the west->east midpoints),
   iterate. It converged in one step: 12.8 px -> 1.21 px rms.
3. Measured positions (cm): 0 (54.8, 35.5), 1 (53.7, -35.5),
   2 (-53.6, 35.0), 3 (-54.9, -35.0), 4 (0.9, 36.6), 5 (-0.4, -35.0).
   Corner-centre rectangle 108.5 x 70.5 cm, which is the tape measurement
   the old file carried (108.5 x 70.8). Ids 6 and 7 are outside camera 3's
   view, keep nominal positions and are `helpful`, not `required`.
4. The loader validates every marker against `width_cm`/`height_cm`, so
   those are the outer extent (110.2 x 73.4), not the corner rectangle.

Result: `aprilcam camera calibrate 3` -> method pose, 24 corners, 0.10 cm
rms, 1.20 px, scale agreement 1.022, camera 135 cm over nadir (-7.9, 2.7),
tilt 3.4 deg, confidence `reduced` only because 6 and 7 are not seen.

## Caveat

The 0.10 cm residual is self-referential: the survey came from the same
camera and lens model, so it proves the definition is consistent with what
camera 3 sees, not that absolute positions are right. Absolute scale rests
on the 46 mm marker size and the lens model (scale agreement 1.022 means
up to ~2 %, about 1 cm at the field edge). A tape check of one corner-to-
corner distance (expect 108.5 cm between corner-marker centres east-west,
70.5 north-south) settles it. Markers 6 and 7 should be surveyed by the
same script from a camera that sees them, or measured by hand.

The same self-survey would have caught the main field's marker 7/8
question in seconds; use it before trusting any hand survey.
