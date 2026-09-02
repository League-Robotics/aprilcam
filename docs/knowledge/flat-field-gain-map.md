---
date: 2026-09-02
tags: [camera, lighting, flat-field, gain-map, colour, exposure, daemon, state-dir]
---

# Flat-field gain map: flattening uneven lighting per camera

The room's lighting is not uniform and cannot easily be changed. Measured
2026-09-02 on a white tarp laid over `main-playfield`:

| camera | darkest 2% | median | brightest 2% | spread |
|---|---|---|---|---|
| 3 `arducam-ov9782-usb-camera` (colour) | 61 | 133 | 161 | 2.65x |
| 5 `arducam-ov9281-usb-camera` (mono) | 104 | 122 | 137 | 1.32x |

Camera 3's northeast corner gets under half the light of the centre, and
its green channel runs ~30% below red/blue everywhere (the magenta cast
in every frame). Eyes adapt locally and never notice; a colour threshold
does. Two identical post-it notes rendered as saturated yellow and pale
peach purely from where they sat.

## The model: a per-pixel, per-channel multiplier

Uneven light is multiplicative (`observed = reflectance x illumination`),
so the correction is a per-pixel gain, **not** a gamma or tone curve.
Computing one gain per channel also removes the colour cast, because
the white reference is forced to the same grey in every channel.

## How it is applied (the out-of-process change, 2026-09-02)

`camera.camera.Camera` looks for
`<state_dir>/cameras/<slug>/flatfield.npy` (constant
`FLATFIELD_FILENAME`, path from `DaemonService._flatfield_path_for`) --
next to `calibration.json`, because like a calibration it is a measured
artefact tied to the current mounting and lighting. When present, every
frame `Camera.read()` returns is multiplied by it and saturated back to
uint8, so detection, `get_frame`, streaming and calibration capture all
see the corrected image. Absent file: untouched frames.

- **Install / replace:** copy the `.npy` into place, then
  `aprilcam camera config --reset <n>` (or the MCP `reset_camera_config`).
  `reset()` re-reads the file. No daemon restart.
- **Clear:** delete the file, then the same reset.
- **Wrong resolution:** a map whose H/W do not match the frame is skipped
  with one warning in the daemon log; recapture at the new resolution.
- Cost: one float multiply + clip per frame, ~5-10 ms at 1280x800x3.

## Capturing a map (manual recipe until the daemon grows a command)

1. Lay a plain white sheet over the whole field. A glossy tarp works;
   wrinkles and specular highlights are suppressed below.
2. **Drop exposure until nothing clips.** Both cameras saturate on a
   white sheet at their working exposure (camera 3: 86% of the field at
   255 at `exposure=6`; camera 5 >80% at `exposure=20`). Push a live
   override via `set_camera_config` / `aprilcam camera config` -- used:
   camera 3 `exposure=1`, camera 5 `exposure=4`. The map's *shape* is
   exposure-independent (sensor is linear); only its normalisation is
   arbitrary and it is normalised to mean 1 anyway.
3. Capture 4-8 frames per camera (`get_frame`/`aprilcam camera image`).
   At short exposures the lights' flicker shows as a ~25% frame-to-frame
   swing on camera 3 -- normalise each frame to the batch mean before
   averaging.
4. Build with [`assets/build_flatfield_2026-09-02.py`](assets/build_flatfield_2026-09-02.py)
   (paths and the field polygon are hard-coded; edit them): average,
   median-blur 31 px (kills wrinkles), fill outside the field with the
   field mean (no border bleed), Gaussian sigma 40 px (keeps only the
   low-frequency light field), `gain = mean / L` per channel, clipped to
   `[0.3, 4]`, saved as float32 `(H, W, 3)`.
5. `reset_camera_config` both cameras to restore the working exposure,
   copy the maps into place, reset again.

Result on the sheet itself: camera 3 spread 2.65x -> 1.48x (residual is
the steep east edge), camera 5 1.32x -> 1.11x.

## Gotchas

- **A map is only valid for one mounting and one lighting state.** Move a
  camera or change the lights and it silently mis-corrects; nothing
  detects that yet. Recapture is a two-minute job with the tarp.
- Gain of ~2.7x in camera 3's dark corner amplifies sensor noise by the
  same factor. Tags still detect (the detector already flattens
  illumination internally for its own purposes); colour measurements
  there are noisier than at the centre. The real fix for that corner is
  another light; the map is the workaround.
- The daemon-side `frame_dark` advisory and detector preprocessing see
  the *corrected* frame. If a map is badly wrong the symptom is a
  washed-out or blotchy `camera view`, not a detection error.
