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

## How it is applied

`camera.camera.Camera` looks for
`<state_dir>/cameras/<slug>/flatfield.npy` (constant
`FLATFIELD_FILENAME`, path from `DaemonService._flatfield_path_for`) --
next to `calibration.json`, because like a calibration it is a measured
artefact tied to the current mounting and lighting. When present, every
frame `Camera.read()` returns is multiplied by it and saturated back to
uint8, so detection, `get_frame`, streaming and calibration capture all
see the corrected image. Absent file: untouched frames.

- **Wrong resolution:** a map whose H/W do not match the frame is skipped
  with one warning in the daemon log; recapture at the new resolution.
- Cost: one float multiply + clip per frame, ~5-10 ms at 1280x800x3.

## Capturing a map: `aprilcam camera flatfield` (sprint 037)

The manual recipe below (build the map by hand from a captured burst,
copy the `.npy` into place, reset to reload) is **superseded**. As of
sprint 037 the daemon owns the whole workflow -- one command, no manual
script, no hard-coded polygon:

```
aprilcam camera flatfield <camera> [--exposure N] [--frames K] [--force] [--dry-run] [--clear]
```

Everything below happens inside the daemon
(`daemon.service.DaemonService.capture_flat_field` orchestrating
`calibration.flatfield.FlatFieldCapturer`'s pure math), triggered by this
one CLI command or the equivalent MCP tools (`capture_flatfield`,
`get_flatfield`, `clear_flatfield`):

1. **Field-interior mask.** Derived automatically -- from the camera's
   stored calibration homography if it has one, else from the linked
   playfield's ArUco corners detected live in one probe frame. No sheet
   polygon to hard-code; capture refuses with `NoFieldMask` if neither
   source is available (link a playfield or calibrate first).
2. **Temporary exposure override.** Pass `--exposure N` to drop exposure
   for the capture burst only -- both real cameras saturate on a plain
   white sheet at their normal working exposure (camera 3: 86% of the
   field at 255 at `exposure=6`; camera 5 >80% at `exposure=20`); the
   values that worked here were camera 3 `exposure=1`, camera 5
   `exposure=4`. The camera's prior stored/live configuration is
   restored automatically afterward -- on success, a refusal, or an
   exception alike. The map's *shape* is exposure-independent (sensor is
   linear); only its normalisation is arbitrary and it is normalised to
   mean 1 anyway.
3. **K-frame burst.** `--frames K` (default 6, matching the manual
   recipe's "4-8 frames"). Each frame is normalised to the batch mean
   before averaging -- corrects the ~25% frame-to-frame light flicker
   measured at short exposures on camera 3 without needing a flicker
   model.
4. **Build.** Average, median-blur 31 px (kills sheet wrinkles and
   specular highlights), fill outside the mask with the mask's own
   interior mean (no border bleed), Gaussian sigma 40 px (keeps only the
   low-frequency light field), `gain = target / L` per channel, clipped
   to `[0.3, 4]`.
5. **Quality gate.** Refuses (measured spread still reported either way)
   on in-field clipping, implausible p98/p2 spread (>10x), or
   frame-to-frame disagreement beyond the flicker tolerance already
   normalised out in step 3 -- this is what catches the sheet (or
   anything else in the mask) *moving* mid-burst. Pass `--force` to
   persist anyway; the CLI/MCP report names exactly which check(s) were
   overridden. `--dry-run` evaluates and reports without writing
   anything or touching the live-applied map.
6. **Persist and reload.** On success (or a forced capture),
   `flatfield.npy` + a `flatfield.json` metadata sidecar are written
   atomically under `<state_dir>/cameras/<slug>/`, and the live map on
   the already-open camera is reloaded in place -- no daemon restart.
7. **`--clear`** deletes both files and drops the live-applied map the
   same way, immediately, no restart. Mutually exclusive with every
   capture-only flag (refused client-side before any RPC).

Presence and staleness surface next to calibration state in
`aprilcam camera list` / `camera probe` (`flatfield=present|absent`,
`flatfield-stale` on the same triggers that already stale a calibration:
camera reopen, a resolution-changing config push) -- there is no
separate "is my map still good" command to remember.

Result on a white sheet: camera 3 spread 2.65x -> 1.48x (residual is the
steep east edge), camera 5 1.32x -> 1.11x -- unchanged from the original
manual measurement below, since the math is the same; only how it is
triggered changed.

### The old manual recipe (superseded, kept for history)

Before sprint 037, building a map meant: lay a plain white sheet over the
field, drop exposure by hand via `set_camera_config` and capture frames
via `get_frame`, run
[`assets/build_flatfield_2026-09-02.py`](assets/build_flatfield_2026-09-02.py)
against hard-coded paths and a hard-coded field polygon to do the
averaging/blur/gain math offline, then copy the resulting `.npy` into
place and `reset_camera_config` to reload it. The script is kept only as
a historical reference for the math it validated (see
`calibration/flatfield.py`'s own module docstring, which names it as the
scratch implementation sprint 037's real quality-gated version was
tuned against) -- do not use it as the capture path; `aprilcam camera
flatfield` above replaced it end to end.

## Gotchas

- **A map is only valid for one mounting and one lighting state.** Move a
  camera or change the lights and it silently mis-corrects; nothing
  detects this automatically yet (out of scope for sprint 037). Recapture
  is a one-command job: `aprilcam camera flatfield <camera> --exposure N`.
  A camera reopen or a resolution-changing config push does mark the
  stored map `stale` in `camera list`/`probe` -- a prompt to recapture,
  not a correctness check on the lighting itself.
- Gain of ~2.7x in camera 3's dark corner amplifies sensor noise by the
  same factor. Tags still detect (the detector already flattens
  illumination internally for its own purposes); colour measurements
  there are noisier than at the centre. The real fix for that corner is
  another light; the map is the workaround.
- The daemon-side `frame_dark` advisory and detector preprocessing see
  the *corrected* frame. If a map is badly wrong the symptom is a
  washed-out or blotchy `camera view`, not a detection error.
- **Real cameras clip on a white sheet at their normal working
  exposure.** Always pass a low `--exposure` override (see step 2 above)
  -- capturing at the working exposure trips the quality gate's
  clipping check almost every time.

## Gotcha found the first time the map went live (2026-09-02): the sheet is not neutral

With the tarp-derived map applied, camera 3's magenta cast was replaced by
a **green** cast. The map forces every channel of the sheet to the same
grey, but the tarp as this camera sees it (B152 G110 R152 at exposure 1)
is far more green-deficient than white printer paper at the working
exposure (roughly B220 G200 R225), so green was over-boosted ~20%.

Fix applied by hand: sample the white borders of all detected tags in a
raw working-exposure frame, push them through the map, and scale the map's
channels so the median mapped white is neutral. Camera 3 scalars were
B 1.106 / G 0.870 / R 1.055; after that, whites agree to +/-4% in G/R across
the field and wood reads a natural brown. The installed
`flatfield.npy` is the rebalanced one; the tarp-neutral original is beside
it as `flatfield.npy.tarp-neutral.bak`. Rerunning `camera flatfield`
overwrites both with an unbalanced map until the issue
`clasi/issues/flat-field-capture-should-white-balance-against-a-known-neutral-reference.md`
is implemented -- rebalance again afterwards.

Also: white paper already clips at camera 3's working exposure before any
map, and the map's >1 gains clip more of the field. For colour
measurements drop the exposure; tags do not care.

## 2026-09-03: ND filter on camera 3 -- exposure retune, and the map is out of date

A neutral-density filter went onto camera 3. Consequences, measured:

- **Exposure ceiling.** At 30 fps the OV9782's exposure control stops
  having any effect above ~60 (frame median luma 107-111 for exposure
  60, 80, 100, 130). Gain helps only a little (median 111 -> 133 from
  gain 4 to 16) while clipping the tag paper more. Persisted:
  `exposure 60, gain 4` (was 6 / 2), field median ~110, no clipping.
  `power-line-frequency` on this camera reads 2 (60 Hz) already.
- **The camera moved ~30 px** (down) when the filter was fitted:
  calibration is stale and must be redone, which needs the mat off
  markers 4, 9, 10 and the centre AprilTag.
- **Corner markers 3, 5, 7 are flaky** through the filter at every
  exposure, with and without the gain map, and not because of blur
  (contrast-normalised sharpness unchanged). The filter adds a purple
  veiling haze at the frame edges (visible in every frame); that is the
  leading suspect. Unresolved.
- **The gain map is not being applied.** A `camera flatfield --clear`
  test followed by copying the `.npy` back and `camera config --reset`
  left the daemon with no flat-field record and frames uncorrected
  (magenta cast back, west/east wood ratio 3.5x). The map was also built
  without the filter and at the old pointing, so it is stale anyway.
  Recapture with the tarp (`aprilcam camera flatfield 3 --exposure 20`),
  then re-run the tag-paper white balance until the follow-up issue lands.
  Stakeholder chose to stop before this step ("good enough").

## 2026-09-03 (later): capture at the working exposure, and the burst-reads-corrected-frames bug

- **Capture at the working exposure, not a low override, whenever the
  sheet does not clip there.** Through the ND filter the sheet reads
  ~110 at camera 3's working `exposure 60 / gain 8`, so no override is
  needed. A map captured at exposure 20 and applied at 60/8 left a green
  patch in the NE and a magenta patch in the SE of the corrected sheet:
  this sensor's per-channel response does not scale proportionally
  between those settings. The map already normalises all channels to one
  common grey, so it *is* a per-pixel, per-channel white balance -- the
  patches were a capture/working mismatch, not a missing feature.
- **Bug fixed (commit fca2d62, out of process):** the capture burst read
  frames through `Camera.read()`, which applies the currently loaded map,
  so a recapture with a map live built a residual-only map and replaced
  the good one. `Camera.read_raw()` now feeds
  `_capture_flatfield_frames`. **On a daemon older than that commit,
  run `camera flatfield <cam> --clear` immediately before capturing.**
- **Brightness is a map property.** The daemon normalises the corrected
  sheet to its own mean (~104 here), which reads dim. Camera 3's installed
  map was rebuilt by hand from raw frames with an absolute target of 235
  for the sheet and a gain cap of 12 (`scratchpad` script, same
  median-31/Gaussian-40 math): corrected sheet p50 234, neutral to 2%
  everywhere but the NE corner. Follow-up for the workflow: a `--target`
  (absolute level) and a higher/configurable cap.
- **The ND filter vignettes the NE corner** of camera 3 to ~25/255 raw on
  a white sheet; even a 12x gain leaves it at ~130 and pink. That is the
  corner where ArUco 3 fails to detect. Physical fix: re-centre the
  filter or use a larger one.
- After hand-installing `flatfield.npy`/`flatfield.json` while the daemon
  runs, `camera list` says `flatfield=absent` (the in-memory record was
  cleared) although `Camera` applies the file on the next reset; the
  record comes back on the next daemon start.
- **Correction to the NE-corner note above:** the corner *can* be
  corrected. The daemon's math fills outside the field mask with the
  field's mean before the sigma-40 blur, and at a steep dark corner that
  bright fill bleeds in (raw 25, estimate 58 -> half the needed gain).
  Rebuilding with nearest-in-field extension (`cv2.distanceTransformWithLabels`),
  median 21 / sigma 20, target 235, cap 16 gives a corrected white field of
  225-235 everywhere, NE 233 neutral; only the last few corner pixels sit
  at the green cap. Installed on camera 3 (`flatfield_cam3_bright235_edge.npy`
  in the session scratchpad is the copy). Filed as
  `clasi/issues/flat-field-capture-target-level-gain-cap-and-edge-extension.md`.
- **2026-09-03, spotlight added in the NE corner, both maps rebuilt.**
  Camera 3 (hand-built at working exposure, `scratchpad/build_map_v2.py`,
  target 235, cap 16): raw sheet p2 33 (was 25), no pixel at the cap,
  corrected sheet 228-238 with the NE corner at 232/236/235. Camera 5
  (daemon workflow, `--clear` then `--exposure 4`): spread 1.26x -> 1.08x.
  Frame-to-frame flicker at exposure 60 is 0.6%, versus ~25% at
  exposure 1 without the ND filter -- the longer exposure integrates
  out the mains flicker.
- **Why more light in the NE corner barely helps (2026-09-03, third
  rebuild):** the raw white-sheet profile from centre to the NE corner
  falls smoothly 123 -> 24 starting about halfway out, while the other
  three corners stay at 93-139. A smooth, one-sided falloff of that
  depth is the ND filter sitting off-centre and shading the lens toward
  the NE; it is a *throughput* loss (~20% at the corner), so light adds
  proportionally (a spotlight took the corner from 25 to 33, not to 100).
  Re-centre the filter rather than adding lamps. With the map (target
  235, cap 16) the corrected sheet is 232-241 everywhere regardless, at
  the cost of ~8x noise amplification in that corner.
