---
status: in-progress
sprint: '037'
tickets:
- 037-001
- 037-002
- 037-003
- 037-004
- 037-005
- 037-006
---

# Flat-field gain map capture as a first-class daemon workflow

## Description

The room's lighting is uneven and cannot easily be fixed. Measured
2026-09-02 on a white tarp over `main-playfield`: camera 3
(`arducam-ov9782-usb-camera`) sees a **2.65x** light-field spread (the
northeast corner gets under half the centre's light) plus a ~30% green
deficit that gives every frame a magenta cast; camera 5
(`arducam-ov9281-usb-camera`) sees 1.32x. Colour-based measurements are
unusable without correction: two identical post-it notes rendered as
saturated yellow and pale peach purely from where they sat.

The correct model is a per-pixel, per-channel multiplicative gain map
(flat-field correction), not gamma or CLAHE. See
`docs/knowledge/flat-field-gain-map.md`.

### What already exists (out-of-process change, commit `7f4c0ac`)

The **apply** side is done: `camera.camera.Camera.read()` multiplies every
frame by `<state_dir>/cameras/<slug>/flatfield.npy` when the file exists
(`FLATFIELD_FILENAME`; path wired by
`DaemonService._flatfield_path_for`), saturating back to uint8, and
reloads the file on `open()` and `reset()` so install/replace/clear is a
file copy plus `reset_camera_config` with no restart. On the sheet
itself this flattens camera 3 to 1.48x and camera 5 to 1.11x.

**Capture** is still a manual recipe
(`docs/knowledge/flat-field-gain-map.md`,
`docs/knowledge/assets/build_flatfield_2026-09-02.py`): an operator pushes
a low-exposure override so the white sheet does not clip, grabs frames
through `get_frame`, builds the map with OpenCV in a scratch script with a
hard-coded field polygon, copies it into the state dir, and resets. That
is not a workflow anyone else can repeat.

### Needed

1. **A daemon-side `CaptureFlatField` RPC** (proto, `daemon/rpc.py`,
   `client` wrapper, MCP tool, and CLI
   `aprilcam camera flatfield <cam> [--exposure N] [--frames K] [--clear]
   [--dry-run] [--force]`) that runs the whole sequence inside the daemon:
   push a temporary exposure override, capture K frames, normalise each
   to the batch mean (the lights flicker ~25% frame-to-frame at short
   exposures), average, median-blur to kill sheet wrinkles and specular
   highlights, fill outside the field with the field mean, wide Gaussian
   to keep only the low-frequency light field, per-channel
   `gain = mean / L`, clip to a sane range, write `flatfield.npy`
   atomically, restore the working config, and reload the map live. The
   CLI and MCP server are cv2-free thin clients (sprint 015 boundary,
   `tests/test_no_cv2_in_client_modules.py`), so **all** of the pixel
   math lives daemon-side, the same way `calibrate` does.
2. **Field-interior mask from the linked playfield**, not a hard-coded
   pixel polygon: derive it from the current calibration's homography
   (the playfield rectangle mapped to pixels) or, uncalibrated, from the
   detected ArUco corner markers. No calibration and no markers visible
   (the sheet covers them) means: use the last stored calibration, and
   refuse if there is none.
3. **Quality gate.** Report the measured light-field spread (p98/p2)
   before and after, per channel. Refuse without `--force` when any
   captured frame clips inside the field, when the spread is implausible
   (e.g. >10x, a sheet not covering the field), or when the frames
   disagree with each other beyond flicker (the sheet moved).
4. **Surface flat-field state** wherever calibration state is surfaced:
   `list_cameras` / `get_camera_config`-style reporting (present,
   `captured_at`, frame shape, gain range) and a `stale` flag set on the
   same events that mark a calibration stale (camera reopen; a config
   push that changes resolution). `--clear` deletes the file and clears
   the live map.
5. **Tests.** Unit tests for the map-building math on synthetic frames
   (a known gradient must be flattened to within a tolerance; wrinkle
   noise must not leak into the map; clipping must be refused), RPC
   round-trip tests against the fake backend, and a `hardware`-marked
   test that runs a real capture and is skipped cleanly when the rig is
   absent.

### Out of scope

Automatic detection that the lighting has changed since capture; more
lights in the northeast corner (the real fix for that corner).
