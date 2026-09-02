---
date: 2026-09-02
tags: [camera, locate, parallax, mount_z, calibration, elevated-tags, main-playfield]
---

# A two-reference `camera locate` passes its quality gate even when one reference is misplaced

Measured live 2026-09-02 on cameras 3 (`arducam-ov9782-usb-camera`) and
5 (`arducam-ov9281-usb-camera`), both on `main-playfield`, after two
permanent elevated AprilTags were added: **21 over `dot-southwest-orange`
(-50, -30)** and **22 over `dot-northeast-orange` (50, 30)**, both on
15 cm stands (a "6 inch" stand -- when someone says "6" for these stands
they mean inches, not cm; the raw parallax displacement only fits 15 cm).

## What happened

`aprilcam camera locate <cam> --reference 21:dot-southwest-orange:15
--reference 22:dot-northeast-orange:15 --dry-run` reported, on **both**
cameras independently:

| camera | solved nadir | solved height | rms residual | condition |
|---|---|---|---|---|
| 3 | (28.1, 1.6) | 156.0 cm | 0.79 cm | 64.6 |
| 5 | (33.2, 2.5) | 156.5 cm | 0.77 cm | 64.7 |

Camera 3's sensor plane is physically measured at ~127 cm and its stored,
sprint-029 `camera_position` is height 128.2 cm, nadir (3.1, -2.8). A
156 cm solve is wrong by ~28 cm, yet **rms 0.79 cm is under the 1.0 cm
`RMS_RESIDUAL_THRESHOLD_CM` and the condition number is fine**, so a
non-dry-run would have written it without `--force`.

## Why

With exactly two references there are 4 equations for 3 unknowns, so a
single misplaced reference is absorbed into the fit rather than showing
up as a large residual. Here tag 22 is the culprit: with camera 3's
stored geometry and `mount_z=15` registered, the parallax-corrected
positions are

| tag | corrected (cam 3) | surveyed dot | error |
|---|---|---|---|
| 21 | (-50.7, -30.6) | (-50, -30) | 0.9 cm |
| 22 | (46.1, 29.7) | (50, 30) | **3.9 cm west** |

Re-solving offline with tag 22 at its corrected position instead of the
dot gives nadir (5.2, -0.3), height 121 cm, rms 0.09 cm -- i.e. the two
references become self-consistent and agree with the stored geometry.
The displacement direction of tag 22 rules out a height error alone:
it is laterally off the dot (the stand's cantilever arm puts the tag
several cm from the base plate, so "base on the dot" is not "tag over
the dot").

## Rules

- **Never trust a two-reference `locate` solve on its own.** Always
  `--dry-run` first and compare the solved height against a tape-measure
  height. Three or more stands, not collinear with the nadir, is the
  only configuration where a bad reference shows up in the residual.
- **Check each reference the other way round** before locating: register
  it with its stand height on an already-located camera and confirm the
  corrected position lands on the surveyed feature. A stand that fails
  this check will corrupt every solve it participates in.
- `list_tag_parameters`' `mount_z_applied` flag is **daemon-wide**
  ("some camera is located"), not per camera. It reported `true` for
  tags 21/22 while camera 5 was still unlocated and still reporting raw,
  uncorrected positions for them. Check `get_camera_config(<cam>)` for a
  `camera_position` block to know whether *that* camera corrects.
- Detection priming (see `daemon-camera-grant-and-detection-priming.md`)
  applies to `get_tag` too: the first two polls after a calibrate showed
  neither elevated tag on camera 3 and nothing at all on camera 5. Poll
  3-5 times before concluding a tag is not visible.

## Follow-up the same day: after re-seating both stands

The stakeholder repositioned tags 21 and 22. Recalibrated both cameras
again (reprojection 0.17 / 0.20 cm) and re-ran the check:

| tag | corrected (cam 3, stored geometry) | surveyed dot | error |
|---|---|---|---|
| 21 | (-50.6, -30.5) | (-50, -30) | 0.8 cm |
| 22 | (50.6, 29.6) | (50, 30) | 0.7 cm |

With consistent references the two-reference `locate --dry-run` now
agrees between cameras and with itself:

| camera | solved nadir | solved height | rms residual |
|---|---|---|---|
| 3 | (3.0, 1.0) | 119.8 cm | 0.18 cm |
| 5 | (6.2, 1.7) | 120.8 cm | 0.17 cm |

Camera 5's solve was **written** (it had no `camera_position` before).
Camera 3's stored sprint-029 position (nadir (3.1, -2.8), height
128.2 cm, tape-measured plane ~127 cm) was kept. Note the ~8 cm height
gap between the two methods: `height` scales almost linearly with the
reference stand height, so a 15 cm assumed stand whose tag *centre* is
really ~16 cm above the field would account for the whole difference.
Measure the tag-centre height, not the stand, before trusting either
number to better than ~5 cm.

**Procedure that worked, in order** (the contamination guard refuses a
reference that is registered with `mount_z > 0`, so the unregister step
is not optional): calibrate -> poll 3-5 times -> `unregister_tag` 21 and
22 -> `locate --dry-run` -> `locate` (write) -> `register_tag` 21 and 22
with `mount_z=15` -> poll and check corrected positions.

## State left on the rig after this session

- Cameras 3 and 5 recalibrated (all 10 ArUco matched). Camera 3 kept
  its stored lens model.
- AprilTags 21 and 22 registered with `mount_z=15` (persisted in
  `state_dir/mounts/registry.json`).
- Camera 5 located at nadir (6.2, 1.7), height 120.8 cm, written to
  `~/.config/aprilcam/cameras/arducam-ov9281-usb-camera/config.json`
  and pushed live. Camera 3 unchanged at its sprint-029 position.
