---
title: Tag Detection Under Variable Lighting
blurb: Why tags drop out under glare and low contrast, what the v2 pipeline does about it, and how to tune exposure for your ambient light.
order: 7
updated: 2026-08-23
tags: [detection, lighting, exposure, opencv, tuning, ops]
---

# Tag Detection Under Variable Lighting

## Why tags drop out

AprilTag/ArUco detection depends on **local black/white contrast**, not
absolute brightness. Lighting breaks it three ways:

- **Glare / overexposure**: direct light washes a tag out — the white
  border bleeds into the data cells and the bit pattern is gone. Past
  sensor saturation this is unrecoverable: no software can restore
  contrast the sensor never captured. Measured live in daylight, a
  saturated frame drops AprilTag detection to **zero** while ArUco
  partially survives.
- **Low contrast / underexposure**: tags become faint grey-on-grey.
  Notably, detection often *keeps working* on a badly underexposed frame
  — tags carry enough local contrast even when `aprilcam camera view` looks
  nearly black. "Dark image but tags still detect" is a real, diagnosed
  state, not a contradiction.
- **Uneven illumination**: a bright edge and a shadowed centre mean no
  single global threshold works. This is the case the pipeline and
  detector configuration below are specifically built for.

## The v2 detection pipeline

All vision runs in the daemon (see [daemon.md](daemon.md)); every
`get_tags` burst runs one frame through
[vision/pipeline.py](https://github.com/League-Robotics/aprilcam/blob/master/src/aprilcam/vision/pipeline.py)
and then
[vision/detector.py](https://github.com/League-Robotics/aprilcam/blob/master/src/aprilcam/vision/detector.py),
orchestrated by
[daemon/detection.py](https://github.com/League-Robotics/aprilcam/blob/master/src/aprilcam/daemon/detection.py).

### Preprocessing stages (`run_pipeline`, fixed order)

1. **Grayscale** — BGR → single channel.
2. **Downscale** (`proc_width`, **off by default**) — optional shrink
   for cheaper detection; detected corners are mapped back to the
   original frame's pixel space exactly.
3. **Illumination flattening** (**on by default**) — estimates the
   low-frequency illumination field with a large Gaussian blur
   (51 px kernel), then **divides** the image by that estimate and
   rescales around mid-grey 128. A shadowed tag square and a well-lit
   one end up at comparable intensity, which is what the detector's
   thresholding needs. (v1 used a subtractive high-pass here; v2 uses
   division — same intent, different math.)
4. **CLAHE** (**off by default**) — optional local-contrast boost
   (clip 2.0, 8×8 tiles) for scenes flattening alone doesn't fix.
5. **Sharpen** (**off by default**) — a fixed 3×3 unsharp mask for
   soft-focus cameras.

The daemon runs the defaults: flattening only, at native resolution.
v1's multi-scale union pipeline (several preprocess variants at several
scales, hits unioned) **does not exist in v2** — at 10–15 s/frame it was
incompatible with per-burst detection. v2 runs one fast pass per frame
and handles marginal tags temporally (below); calibration compensates
by averaging detected corners over **30 frames**, smoothing per-frame
lighting/exposure jitter.

### Detector configuration

`TagDetector` runs exactly two families — AprilTag **36h11** and ArUco
**4×4 (DICT_4X4_50)** — always both, with inverted-marker detection
unconditionally on. The tuning that matters most for uneven lighting is
the widened adaptive-threshold sweep, an earned constant from real
playfield captures (v1's finding, carried into v2):

```
adaptiveThreshWinSizeMin  = 3
adaptiveThreshWinSizeMax  = 53   # OpenCV default 23
adaptiveThreshWinSizeStep = 4    # OpenCV default 10
```

More threshold passes at more window sizes catch tags at different
local contrast levels — more effective than relaxing geometric
thresholds. Also set: sub-pixel corner refinement, `quad_decimate=1.0`
(no decimation), and the AprilTag quad-detector constants
`aprilTagMinWhiteBlackDiff=3`, `aprilTagMinClusterPixels=5`,
`aprilTagMaxLineFitMse=20` (applied where the OpenCV build exposes
them).
[vision/aruco_compat.py](https://github.com/League-Robotics/aprilcam/blob/master/src/aprilcam/vision/aruco_compat.py)
shims the OpenCV 4.6 vs 4.7+ ArUco API so the same detector runs on a
stock Raspberry Pi / Ubuntu OpenCV.

### After detection: filtering lighting noise temporally

Lighting noise that survives the detector is handled downstream in
`daemon/detection.py`:

- **Confirmation streak (3)** — a tag must appear in 3 consecutive
  bursts before it is reported at all. Glare and noise produce
  occasional *false* ArUco decodes (phantom ids observed live, up to 2
  consecutive frames); the streak keeps them out of the ring buffer.
- **Disappearance grace (8)** — a confirmed tag that flickers under
  marginal contrast keeps its confirmed status for up to 8 missed
  bursts, avoiding full re-confirmation on every dropout.
- **Motion noise floor** —
  [vision/motion.py](https://github.com/League-Robotics/aprilcam/blob/master/src/aprilcam/vision/motion.py)
  refuses to report a heading/velocity when two samples are within
  0.5 cm: sub-pixel detector jitter (fed directly by per-frame lighting
  and exposure variation) would otherwise be dressed up as a confident
  direction of travel. `speed` is still reported honestly; direction is
  `None` below the floor.

## Exposure is manual — and ambient-dependent

**The daemon never auto-adjusts exposure.** Camera controls come from
the per-camera config (`settings`: `auto_exposure`, `exposure`, `gain`,
`auto_white_balance`, `white_balance_temperature`, plus resolution) and
are pushed to hardware by
[camera/camera.py](https://github.com/League-Robotics/aprilcam/blob/master/src/aprilcam/camera/camera.py).
Operationally relevant behaviour, all verified live:

- On macOS, controls go through a `uvc-util` control backend —
  `cv2.VideoCapture.set()` silently fails for every one of these keys
  on the project's real cameras.
- Gate ordering is handled for you: `auto_exposure` goes manual before
  `exposure` is pushed, `auto_white_balance` off before
  `white_balance_temperature`.
- After a fresh open, 5 frames are drained before the first control
  push (AVFoundation settles asynchronously; pushing earlier loses the
  write ~60% of the time).
- **`aprilcam camera config <file>` pushes are live-only, never
  persisted.** A daemon restart reverts to the on-disk file. Persist a
  winner by editing `~/.config/aprilcam/cameras/<slug>/config.json`,
  then `aprilcam camera config --reset <n>` re-applies it live.
- UVC controls are shared mutable state — other processes can stomp
  them, and the daemon never re-asserts values it believes are applied.
  If the image goes dark while tags still detect, compare
  `aprilcam camera config <n> --dump` against `uvc-util -g` readbacks,
  then `camera config --reset <n>`.
- Config gotcha: `auto_white_balance` must be a number (`0`), not a
  JSON boolean.

### Known-good settings (this project's cameras)

| Camera | Sensor | Daylight | Night |
|---|---|---|---|
| camera 3 (`arducam-ov9782-usb-camera`, color) | OV9782 | `exposure: 6, gain: 2, auto_exposure: 1, auto_white_balance: 0, white_balance_temperature: 4600` | same |
| camera 5 (`arducam-ov9281-usb-camera`, mono) | OV9281 | `exposure: 20` (20–30 all detect; ≥ 75 saturates, 0 AprilTags; 10 drops ArUco) | `exposure: ~300` |

These are per-sensor and per-ambient — **there is no one right value**,
and the values do not cross-apply between cameras. The nighttime-tuned
setting is the daytime failure mode and vice versa.

### Re-tune recipe (~1 minute)

When tags stop detecting after a lighting change: push candidate
exposures with `aprilcam camera config`, capture a frame per candidate,
and run `vision.pipeline.run_pipeline` + `vision.detector.TagDetector`
offline on each — this measures raw per-frame detectability without the
confirmation gate. Persist the winner to the on-disk config and
`camera config --reset`. Full detail in
[docs/knowledge/dark-camera-image-uvc-drift-reset.md](https://github.com/League-Robotics/aprilcam/blob/master/docs/knowledge/dark-camera-image-uvc-drift-reset.md).

## Lighting the field (physical fixes beat software)

Unchanged truths from v1's testing, still the highest-leverage moves:

1. **Diffuse the lighting** — indirect light or a diffuser eliminates
   the specular glare that saturates tags outright.
2. **Matte tag prints** — glossy paper reflects light straight back at
   the camera; matte scatters it and keeps contrast.
3. **Print tags larger** — small tags sit at the detector's decodability
   limit; doubling tag size beats any software tuning.
4. **Placement** — tags near a reflective field edge suffer edge glare;
   a few cm inward helps.

See [overview.md](overview.md) for where detection fits in the overall
system, and [daemon.md](daemon.md) for running and configuring the
daemon that does all of this.
