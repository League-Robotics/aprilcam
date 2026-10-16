---
date: 2026-10-15
tags: [calibration, intrinsics, charuco, lens-model, ov9782, ov9281, hd-usb-camera]
---

# ChArUco intrinsics calibration: what we learned doing it by hand

Prototype in `assets/charuco-prototype/`; the durable implementation is the
issue `charuco-board-intrinsics-calibration-as-a-daemon-workflow.md`.

## Results (installed into each camera's `calibration.json` the same day)

| Camera | Views | rms | fx (px) | HFOV | Distortion |
|---|---|---|---|---|---|
| 4 `arducam-ov9782` 1280x800 | 75 | 0.34 | 911 | 70° | k1 +0.03: essentially rectilinear |
| 2 `arducam-ov9281` 1280x800 | 178 | 0.32 | 932 | 69° | k1 +0.05: essentially rectilinear |
| 3 `hd-usb-camera` 1920x1080 | 37 | 0.25 | 1937 | 53° | k1 -0.35, k2 +0.12: **~110 px barrel at the corners** |

A second pass on 2/4 with a 5x7 board gave fx 877/890 (3-5 % lower). That
spread is real: with few steep tilts fx is weakly constrained, but for a
planar homography it does not matter (see "good enough" below). Camera 4's
old single-view estimate had fx = 473, half the true value.

Camera 3's September plumb-line check ("lens is rectilinear, k1 -0.12") was
wrong: it used the drawn cross, and lines through the principal point are
straight under any radial distortion. The daemon's nine-point k1 -0.30 was
close to right.

## Intrinsics vs extrinsics

The board measures the **lens**. Moving the camera or the table does not
invalidate it; only swapping a camera/lens, refocusing, or changing
resolution does. After any move, `aprilcam camera calibrate <n>` (field
markers, seconds) is all that is needed.

**Restart caveat -- resolved (sprint 044).** The old rule ("restart the
daemon before calibrate if an outside tool wrote the lens model") existed
because the prototype wrote into `calibration.json`, which the daemon only
read at start-up and then overwrote from its in-memory copy. The daemon
workflow removes the cause: the lens is installed by the daemon itself into
a per-camera `<state_dir>/cameras/<slug>/intrinsics.json` (and into memory),
and `calibrate` carries it forward (store model of matching size > prior
calibration's embedded lens > none). No restart is needed; just run
`aprilcam camera calibrate <n>` after installing to apply it (`camera list`
shows `lens-pending` until then).

## Running it (daemon workflow)

```
aprilcam camera intrinsics 2 3 4 [--board 5x7:37:28:DICT_5X5_100] [--install] [--no-gui] [-d HOST]
```

Board spec resolves `--board` > env `APRILCAM_CHARUCO_BOARD` > the default
above. With a display you get the fullscreen window (Enter installs the
cameras that are green, Esc aborts); over SSH use `--no-gui` for a text
status loop. MCP agents use `start_intrinsics` / `get_intrinsics_status` /
`finish_intrinsics` / `abort_intrinsics`. With no board at all,
`aprilcam camera calibrate <n> --with-intrinsics` is the fallback (refused
while a measured lens is installed).

**Differences from the prototype:** all vision runs in the daemon; it never
auto-finishes when green (a person decides); the result is a per-camera
`intrinsics.json`, not a rewrite of `calibration.json`; sessions read the
camera the daemon already owns (~8 fps) and keep corner arrays only, never
frames; an unpolled session auto-aborts after 120 s.

## Detection gotchas, in order of pain

1. **Field markers collide with the board.** calib.io's default boards use
   DICT_4X4_50, same as the playfield's corner markers (ids 0-11 both). The
   CharucoDetector sees duplicate ids and returns nothing. Fix: detect
   markers yourself, cluster by proximity (single-linkage, 2.5x marker
   side), run `detectBoard(markerCorners=…, markerIds=…)` per cluster. Also
   lets two boards in view count as two views. Better: order boards in a
   different dictionary (we now have 5x7, 37/28 mm, DICT_5X5_100).
2. **Markers are at the detector's limit at table height.** 22 mm markers
   are ~16 px on the 1280-wide cameras, <3 px per code bit; the colour
   OV9782 read 4-13 of 24 markers per frame. Detecting on a **2x upscaled
   frame** with `minMarkerPerimeterRate=0.01`,
   `adaptiveThreshWinSize 3..23 step 4`, `polygonalApproxAccuracyRate=0.05`,
   `perspectiveRemovePixelPerCell=8` took it to 30/35 corners. Interpolate
   corners on the native image so coordinates stay native.
3. **Board layout convention.** calib.io boards match OpenCV's *current*
   (non-legacy) layout for 6x8; auto-detect by trying both the first time
   markers are seen but no corners interpolate, then lock in.
4. **Tilt from a single board with a guessed K is ambiguous near flat.**
   `solvePnPGeneric(IPPE)` returns two poses; trust the tilt only when
   >= 18 corners and the runner-up error is > 1.3x the best. Otherwise a
   flat board reports 20° and ticks tilt items it did not earn.
5. **Absolute blur thresholds don't transfer between sensors.** Laplacian
   variance: OV9782 sharp ~8000, blurred ~1800; OV9281 sharp ~3200. Judge
   blur relative to the camera's running best (45 %) and gate motion on
   corner displacement vs the previous frame (12 px at ~5 fps; 2.5 px was
   far too strict and rejected slow sweeps that were not blurred).
6. **Coverage must be judged by board extent, not centroid**, over the
   outer 40 % bands, or a user cannot satisfy corner regions on two
   cameras that frame the table differently.
   *Update 2026-10-16:* the bands became equal thirds (`EDGE_FRACTION = 1/3`)
   at the stakeholder's request after a live run (unequal regions, some only
   reachable by turning the target sideways). Extent-based counting still
   makes the outer third easy to reach.

## "Good enough" -- the metric that finally let the stakeholder stop

Successive-solve convergence and a pose checklist both kept the
stakeholder waving a board for 10+ minutes with no sense of progress.
What matters for this application is **how much the world mapping could
still change**: solve on the full set and on random halves, undistort a
grid over the field area (central 85 %), fit a homography between the
two undistorted grids (a planar field absorbs any projective part), and
take the max residual. Good when < 4 px (~3 mm) and rms < 0.6 px. Show
next to it the size of the correction itself vs no lens model -- for
cameras 2/4 the correction (~3 px) equals its own uncertainty, i.e. those
lenses barely need one; camera 3's does.

Display-wise: fullscreen Tk, fonts scaled from screen height (banner
H/9), one plain-language NEXT line from the biggest coverage gap, 3x3
region tiles and four tilt tiles. Never auto-quit on green; offer Enter.
Every run is a fresh session unless explicitly resumed.
