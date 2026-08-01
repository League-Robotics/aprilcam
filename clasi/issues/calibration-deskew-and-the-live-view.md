---
status: pending
---

# Calibration, deskew, and the live view

## Description

The third v2 vertical slice. The stakeholder's framing: **"be able to
fetch an image from a camera that is calibrated and deskewed, and then
show the view."** This is the sprint where pixel coordinates become
centimeters and someone can finally *look* at the playfield.

Covers development-plan stages 6 (calibration and world coordinates)
and 7 (deskew and the published image products), plus the slice of
stage 9 (playfield configuration) that calibration cannot proceed
without, plus a first viewer.

### Playfield definitions — a prerequisite, not a bonus

Calibration matches detected corner ArUcos against **surveyed
positions**, so the playfield definition is the ground truth the whole
sprint stands on. At minimum: load a definition, hold it, serve it
back, and link a camera to it (`CameraConfig.playfield`). The real
`main-playfield` definition already exists at
`config/aprilcam-old/playfields/main-playfield.json` — 134.3 × 89.3 cm,
origin at AprilTag 1 in the field centre, perimeter ArUco 1–8 plus
interior 9 and 10, 6 rectangles, 8 dots.

Whether the full whole-document push/serve config service
(`SetPlayfield`/`GetPlayfield`) lands here or only the loading half is
a scoping decision for the sprint — but nothing can calibrate without
some path from that file to the daemon.

### Calibration

Multi-frame averaged detection of the corner ArUcos, matched to their
surveyed positions, solving the pixel→world homography. **All-or-
nothing against the definition's required id set** (UC-04): 9 of 10
found is a failure naming which marker is missing, never a degraded
success. Persisted to `calibration.json` under the state dir, reloaded
at startup — a calibrated camera stays calibrated across restarts
(UC-01), which is why calibration survives while pushed config does
not. Provenance (`calibrated_at`, `playfield`) is recorded, and
replacing a definition marks calibrations against it stale.

### World coordinates become real

Everything sprint 021 honestly reported as `None` now has a value on a
calibrated camera: `TagRecord.world`, `world_corners`, `yaw_rad`,
`heading_rad`, `world_velocity`, `speed`, and `calibrated=True`.
Uncalibrated cameras keep the sprint-021 behaviour exactly — this adds
a capability, it does not change what an uncalibrated camera reports.

**The geometry conventions are sacrosanct** (`docs/design/design.md`
§6, `specification.md` §7). They cost enormous effort to get right and
every sign and axis in this sprint is guilty until proven innocent:
A1-centred origin, +x east, +y north, cm; the origin shift is a **pure
subtraction on both axes, never a y-flip**; on the deskewed image north
is up and east is right, so world→pixel flips y **exactly once**,
inside the published mapping and never again in client code; heading
and yaw are `atan2`-based with 0 = east, CCW positive.

### Deskew

The calibrated top-down image, computed **once in the daemon** and
shared. **Sizing rule: fixed bounding box** — the field rectangle is
scaled aspect-preserved to fit a maximum frame size (default 800×800,
per-playfield override), so the output is uniform-scale by
construction. `main-playfield` at 134.3 × 89.3 yields 800 × 532. The
resulting `px_per_cm` is **derived and reported, never chosen**, and
rides in the frame metadata along with `world_extent` so clients
convert deskewed-pixel ↔ world-cm exactly. `ImageFrame`'s anchor
invariant holds: image (0,0) is the field's north-west corner.

A deskewed request on an uncalibrated camera **fails explicitly**
(`NotCalibrated`) and never silently substitutes a raw frame.

### CLI and the view

- `aprilcam camera calibrate <camera>` — triggers calibration in the
  daemon (the CLI sends a command, not pixels), reports matched and
  missing ids.
- `aprilcam playfield image` — one deskewed frame (UC-19).
- **A live view window** — the stakeholder wants to see it. Note the
  design's spelling: `camera view` is the **raw** live video (UC-16,
  "raw means raw" — no deskew, no overlays; it is what you use to aim
  the camera *before* calibrating), while `playfield view` is the
  deskewed, calibrated view with tags drawn (UC-18). The stakeholder
  said "`aprilcam camera view`" while describing the calibrated
  deskewed result, so **the sprint must resolve which command(s) ship
  and record it** — shipping the raw one is genuinely useful for
  aiming during calibration, and the deskewed one is what "show the
  view" means here. Both are defensible; silently picking one is not.
- The viewer needs frames continuously, and **streams do not exist
  yet** (`ImageStream`/`TagStream` are declared but unimplemented).
  Decide deliberately: poll `get_frame` at a modest rate for a first
  viewer, or build the stream. Polling is the smaller, honest first
  step; record whichever is chosen and why.

## Cause

Sprint 020 gave the daemon cameras; sprint 021 gave it eyes. Both
report pixels, because there is no world frame yet. Every downstream
capability — world positions, the deskewed stream, annotations, the
`where` query, anything a robot can act on — waits on calibration.

## Proposed fix

Implement against the interface skeletons, which the sufficiency traces
verified: `Calibrator`, `Calibration`, `CalibrationResult`, `Deskewer`
(`src/aprilcam/calibration/interfaces.py`), `Daemon.calibrate`/
`get_calibration`/`get_frame(deskewed=True)`/`set_playfield`/
`get_playfield`/`list_playfields` (`daemon/interfaces.py`),
`PlayfieldDefinition`/`Feature`/`ConfigLoader` (`config/interfaces.py`),
and `Playfield` (`playfield/interfaces.py`). All are real base classes
or frozen dataclasses now — implementations **derive** (no
`typing.Protocol` anywhere; see `daemon/interfaces.py`'s module
docstring).

Mine `archive/src/aprilcam/` per `CLAUDE.md`'s archive rule — v1 solved
homography, deskew geometry, and the calibration flow, and the hard-won
sign conventions are in there. Take the approach and the earned
constants; leave the structure. Note `specification.md` §6 explicitly
condemns v1's `homocal` legacy CLI and its upper-left-origin corner
table as a foot-gun — do not carry that forward.

## Verification

- Calibrate camera 3 (`arducam-ov9782-usb-camera`, aimed at the real
  `main-playfield`) and have it succeed against the definition's
  required ArUco set; occlude one marker and confirm it fails naming
  that id rather than degrading.
- AprilTag 1 sits at the field centre by definition, so a calibrated
  reading of it must be world ≈ (0, 0) — a direct, physical check that
  the origin convention is right.
- Move a tag physically east; reported x increases. Move it north; y
  increases. On the deskewed image the same motions go right and up.
  (This is the check that catches a y-flip, the single most expensive
  error in the system.)
- A deskewed frame of `main-playfield` is 800 × 532, reports its
  derived `px_per_cm`, and its corners map to the surveyed field
  corners.
- A deskewed request on an uncalibrated camera fails explicitly.
- Calibration survives a daemon restart.
- The view window opens and shows the field live.

## Related

- `docs/design/development-plan.md` stages 6, 7, and the stage 9 slice
- `docs/design/use-cases/daemon.md` UC-02, UC-04, UC-05, UC-06
- `docs/design/use-cases/cli.md` UC-15 (calibrate), UC-16 (raw view),
  UC-18 (playfield view), UC-19 (playfield image)
- `docs/design/design.md` §6 and `specification.md` §7 — the sacrosanct
  geometry conventions
- Sprints 020 and 021 — cameras and detection, which this completes
