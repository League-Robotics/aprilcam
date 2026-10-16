---
date: 2026-10-16
tags: [calibration, pose, lens, acceptance, runbook, sprint-045, playfield, aruco, charuco]
---

# Sprint 045 live acceptance runbook: pose-based field calibration on cameras 2, 3, 4

Executed by the stakeholder (agents may not open cameras or restart the
daemon). It verifies sprint 045: `aprilcam camera calibrate` solves the
camera's planar pose from marker corners through the camera's **measured**
lens model, falling back to the centre-point fit when it cannot. Background:
`2026-10-16-pose-based-field-calibration.md` (conventions, gates, authoring)
and `2026-10-16-charuco-intrinsics-live-acceptance-runbook.md` (sprint 044,
the lens model step this runbook reuses).

Fill in the **Results** table at the bottom. If anything fails, do not patch:
file an issue and attach the result record (see "If something fails").

## Physical facts (read first)

- **Main-playfield markers are 46 mm** (outer black square); the definition
  already says so (`playfield.marker_size_mm: 46`).
- **Ids 9 and 10 are recorded where they physically are**: 9 east at (35, 0),
  10 west at (-35, 0) (definition updated 2026-10-16; do not move the tags).

## 0. Install and restart (needed: pipx is a plain copy)

```sh
cd /Volumes/Proj/proj/robot-projects/aprilcam
git branch --show-current                  # expect master (post-merge)
pipx install --force /Volumes/Proj/proj/robot-projects/aprilcam
pipx inject aprilcam 'mss>=9.0' 'websockets>=12.0' 'cv2-enumerate-cameras>=1.3' 'msgpack>=1.0' 'grpcio-reflection>=1.60' 'opencv-contrib-python>=4.8'   # the [daemon] extra; pipx 1.8 cannot parse 'path[extra]'
aprilcam camera calibrate --help           # expect --verbose, NO --with-intrinsics
aprilcam camera lens --help                # alias of camera intrinsics; must print usage
aprilcam daemon stop
aprilcam daemon start
aprilcam camera list
```

This is the one daemon restart. The sprint's claim is that **no restart is
needed afterwards**; if you restart again, note when and why. Gotchas
(`daemon-restart-launches-pipx-build-no-cameras.md`,
`2026-09-06-cameras-plugged-in-after-daemon-start-need-a-restart.md`): if every
camera shows absent/unusable, start the daemon from the repo with
`uv run aprilcam daemon start`; plug all cameras in first; close other apps
holding cameras. Alternative to pipx: run everything as `uv run aprilcam ...`.

Pass/fail: [ ]

## 1. Pre-flight state

```sh
aprilcam camera list
```

Expect cameras 2, 3, 4 present, `lens=legacy`. Confirm the definition sizes:
`grep marker_size_mm config/aprilcam/playfields/*.json` (main: 46; secondary:
none). Pass/fail: [ ]

## 2. Confirm the definition matches the field (out-of-process check)

With the daemon running:

```sh
aprilcam camera image 2 --encoding png -o FRAME.png
uv run python docs/knowledge/assets/field-check/field_check.py FRAME.png \
  config/aprilcam/playfields/main-playfield.json OUT.png \
  --calibration ~/.local/share/aprilcam/cameras/<slug>/calibration.json
```

(`<slug>` for camera 2 is `arducam-ov9281-usb-camera`.) Expect all ten ids
seen, none missing/stray/duplicated, and about **0.23 cm rms** with leave-one-out
worst about 0.75 cm. If it reports about **30 cm rms**, the tags are still
off: a marker has moved or the definition is wrong. Do not continue until it is right. Open
`OUT.png` to eyeball the annotation. Pass/fail: [ ]

## 3. Measure the lens on each camera (sprint 044)

```sh
aprilcam camera intrinsics 3     # also: 2, then 4 (or: intrinsics 2 4)
```

Follow the NEXT line to GOOD ENOUGH and press Enter (details in the sprint 044
runbook). Then `aprilcam camera list`: expect `lens=measured` for each camera
you did. A calibration at a different resolution than the lens needs a lens
at that resolution. Pass/fail: [ ] (2) [ ] (3) [ ] (4)

## 4. Main field, cameras 2 and 3 (10 markers, 46 mm, mixed orientations)

```sh
aprilcam camera calibrate 2 --verbose
aprilcam camera calibrate 3 --verbose        # camera 3 must see the main field here
```

Expect: `lens model: measured ...`, `method: pose`, `confidence: full`,
`corners used: 40`, reprojection error within the gates (<= 1.5 px and
<= 1.0 cm), `scale agreement` within 0.05, `angular spread` >= 10 deg, a
`leave-one-out worst:` line (reference worst 0.75 cm; gate 2 cm), no marker
flagged, and the leave-one-out table in `--verbose`. Record rms px/cm, scale,
worst marker and cm.

Ten-marker comparison against centre-point: for each camera, note the world
positions of the ten markers (`aprilcam camera tags N` or MCP `get_tags` with
the markers visible, or `where`). Then compare with the centre-point result
(temporarily move the camera's `intrinsics.json` aside, see step 7, and
calibrate: `method: centre-point`, 0.23 cm rms reference). Pass criterion:
**derived positions of the ten markers within 0.5 cm of the centre-point
solve's, and pose at least as good as centre-point.** Restore the lens file.

Negative check: copy `main-playfield.json` to a scratch definition with the
positions of ids 9 and 10 swapped (there is no CLI to link a camera to a field: use the MCP tool
`set_playfield` to point camera 2 at the scratch definition, if it accepts a
path or name; otherwise swap the two entries in `main-playfield.json` itself
and restore it with `git checkout`), and calibrate: the
failure must **name marker 9 or 10** via the leave-one-out residual, and the
previous calibration must remain. Point the camera back afterwards.

Pass/fail: [ ] cam 2 [ ] cam 3 [ ] comparison [ ] negative check

## 5. Camera 3, secondary field (four corner markers)

```sh
aprilcam camera calibrate 3 --verbose
```

Today this stays on the centre-point path: `method: centre-point (definition
has no marker size)`. Measure a secondary-field corner marker's outer black
square in mm with calipers, then add it to
`config/aprilcam/playfields/secondary-playfield.json` under `"playfield"`:
`"marker_size_mm": <mm>` and calibrate again. Expect `method: pose`, 16 corners,
px and cm error, scale agreement under 0.05, `lens model: measured`, height and
nadir plausible. Tape-measure the camera height and enter it in the table.

Sprint 030 leftover (`clasi/issues/later/sprint-030-unfinished-verification-distortion-benefit-unproven.md`):
with the measured lens, re-run `aprilcam camera locate 3` (see `--help`;
needs the AprilTag references) and record whether the solved height moves from
128.25 cm toward 127 cm. If cheap, re-measure the radial error correlation
(it was -0.96 for the legacy lens) and record it.

Pass/fail: [ ]

## 6. Fewer markers, policy, and no-lens fallback

Fewer markers on a sized field (mark entries `"policy": "helpful"` in a scratch
definition, or use a two-marker diagonal definition): cover two, then three
corners: expect `confidence: reduced` plus a warning (exactly two markers) or
full. Leave one visible: `confidence: low`. Cover a `required` marker:
calibrate fails naming the id and the previous calibration is intact
(`aprilcam camera list` unchanged).

No-lens fallback: rename a camera's lens file, calibrate, rename it back.

```sh
mv ~/.local/share/aprilcam/cameras/<slug>/intrinsics.json{,.off}
aprilcam camera calibrate 2
mv ~/.local/share/aprilcam/cameras/<slug>/intrinsics.json{.off,}
```

Expect `method: centre-point (...)` with a reason and results as before the
sprint. (A different capture resolution also forces it: "lens measured at a
different resolution".) Pass/fail: [ ] fewer [ ] low [ ] required [ ] fallback

## 7. Camera 2, parallax and pose

```sh
aprilcam camera calibrate 2 --verbose
```

Expect camera line near `height 144 cm, nadir (3.5, -2.9)`. Then register a
mounted tag and read it with `where` / `get_tags`: a tag at known height reads
within tolerance (about 0.5 cm) of its real floor position, and floor positions
match `aprilcam camera locate 2 --dry-run` (a comparison note prints if pose
and locate differ by more than 5 cm). Pass/fail: [ ]

## 8. Camera 4

Repeat step 4 (or 5, per the field it sees) on camera 4. Record any gate that
needs retuning (constants in `src/aprilcam/calibration/gates.py`; do not edit
here, file an issue with the numbers). Pass/fail: [ ]

## 9. ChArUco at origin, removed option, old files

ChArUco target: on a field with no markers (or ignoring them), add to the
definition
`"calibration_target": {"kind": "charuco", "board": "6x8:28:22:DICT_4X4_50",
"centre_x_cm": 0, "centre_y_cm": 0, "rotation_deg": 0}`, lay the board centred
at the origin with its top edge north, calibrate (`pose: markers+target` or
target-only), remove the board; compare `to_world` of known marker positions
with the marker-based solve (< 0.5 cm). Record the placement convention
confirmed.

Removed option: `aprilcam camera calibrate 2 --with-intrinsics` must be
rejected (unrecognized arguments). Old calibration files (including
`lens_source: estimated`) must still load: `aprilcam camera list` and
`get_calibration` for each camera work after the step-0 restart.
Pass/fail: [ ] target [ ] rejected [ ] old files load

## If something fails

Do not patch. Capture: the exact command, terminal output and exit status;
`aprilcam camera calibrate N --verbose` output (stdout and stderr);
`aprilcam camera list`; `aprilcam daemon status`; the annotated
`field_check.py` output; whether a daemon restart was involved. File it as an
issue in `clasi/issues/` (via the team lead) with that record attached.

## Results (fill in)

| # | Measurement | Result | Pass? |
|---|---|---|---|
| 0 | Install updated; `calibrate --help` has `--verbose`, no `--with-intrinsics`; `camera lens --help` works; daemon restarted once | | |
| 1 | `camera list` before: lens for 2 / 3 / 4 (expect legacy x3); sizes in definitions | 2: / 3: / 4: | |
| 2 | field_check on camera 2 frame: ids seen, rms cm, worst leave-one-out (expect 0.23 / 0.75) | | |
| 3 | Measured lens installed: 2 / 3 / 4 (`lens=measured`) | 2: / 3: / 4: | |
| 4 | Camera 2 main field: method, confidence, corners, rms px / cm, scale, spread, worst LOO marker / cm | | |
| 4 | Camera 3 main field: same | | |
| 4 | Ten-marker world positions vs centre-point: max difference (cm), both cameras (target < 0.5) | 2: / 3: | |
| 4 | Negative check (9/10 swapped scratch def): marker named, previous calibration intact | | |
| 5 | Camera 3 secondary: size measured (mm); method; corners; px / cm error; scale; height; nadir | | |
| 5 | Camera 3 tape-measured height (cm) vs solved | / | |
| 5 | `camera locate 3` solved height with measured lens (was 128.25; target ~127) | | |
| 5 | Radial error correlation re-measured (was -0.96), if done | | |
| 6 | Two / three corners covered: reduced + warning; one: low; required covered: fails by id, prior intact | | |
| 6 | No-lens fallback: `method: centre-point` with reason | | |
| 7 | Camera 2 pose: height / nadir (ref 144 / (3.47, -2.85)); mounted-tag `where` error; vs `locate` | | |
| 8 | Camera 4: method, rms px / cm, worst LOO; gates needing retuning | | |
| 9 | ChArUco at origin vs marker solve (cm, target < 0.5); placement convention | | |
| 9 | `--with-intrinsics` rejected; old calibration files load after restart | | |
| 10 | Daemon restarts after step 0 (expect 0) | | |
| 10 | Open Questions 2-4 confirmed or changed (marker size secondary; gate thresholds; pose-vs-configured precedence) | | |

Thresholds changed (if any) with evidence; issues filed:

Date/time of run, host, git commit of the install (`git rev-parse --short HEAD`):
