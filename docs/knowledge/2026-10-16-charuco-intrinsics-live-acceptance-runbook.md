---
date: 2026-10-16
tags: [calibration, intrinsics, charuco, acceptance, runbook, sprint-044, hd-usb-camera, ov9281, ov9782]
---

# Sprint 044 live acceptance runbook: ChArUco intrinsics on cameras 2, 3, 4

Executed by the stakeholder (agents may not open cameras or restart the
daemon). It verifies sprint 044's success criteria: from a cold daemon,
`aprilcam camera intrinsics 3` reaches GOOD ENOUGH in under a minute of
board motion; Enter installs; an immediate `aprilcam camera calibrate 3`
reports the measured model carried forward; **no daemon restart between
install and calibrate**. It also measures the CPU/contention risk from the
sprint's architecture review. Background: `2026-10-15-charuco-intrinsics-calibration-lessons.md`.

Fill in the **Results** table at the bottom (copy it into the ticket, or
paste the filled table back to the team lead). If anything fails, do not
patch: file an issue and attach the status record (see "If something fails").

Cameras: 2 = arducam-ov9281, 3 = hd-usb-camera 1920x1080 (strong barrel
distortion, the main target), 4 = arducam-ov9782.

**Board:** use the **5x7, 37/28 mm, DICT_5X5_100** board (the default; no
flag needed). The two 6x8 28/22 mm DICT_4X4_50 calib.io boards share the
playfield corner-marker dictionary; the daemon clusters around the collision
so they work, but poorly at table height. Only if you must use one:
`aprilcam camera intrinsics 3 --board 6x8:28:22:DICT_4X4_50` (or
`export APRILCAM_CHARUCO_BOARD=6x8:28:22:DICT_4X4_50`). Keep the playfield
corner markers out of view when you do.

## 0. Update the install (needed: it is NOT an editable install)

`~/.local/pipx/venvs/aprilcam/pipx_metadata.json` shows the pipx venv was
installed as a plain (non-editable) copy from
`/Volumes/Proj/proj/robot-projects/aprilcam[daemon]`, version 0.20260914.1.
It does not contain sprint 044 and will not until reinstalled:

```sh
cd /Volumes/Proj/proj/robot-projects/aprilcam      # same checkout as robot-projects/aprilcam
git branch --show-current                          # expect sprint/044-charuco-...
pipx install --force '/Volumes/Proj/proj/robot-projects/aprilcam[daemon]'   # first time, or after the checkout moves
pipx reinstall aprilcam                                                        # every time after that
aprilcam camera intrinsics --help                  # must print usage, not "invalid choice"
```

(`pipx reinstall aprilcam` reuses the stored `path[daemon]` spec, so it is the
normal update command. It only fails if the checkout path has moved, in which
case run the first line again with the new path.) Keep the `[daemon]` extra: without it the daemon has no OpenCV
capture backend. Alternative that avoids pipx entirely: run every command
below as `uv run aprilcam ...` from the checkout.

## 1. Start the new daemon build (one-time restart, the last one)

The running daemon predates sprint 044. Restart it once:

```sh
aprilcam daemon stop
aprilcam daemon start
aprilcam camera list
```

Gotchas on this Mac (see `daemon-restart-launches-pipx-build-no-cameras.md`
and `2026-09-06-cameras-plugged-in-after-daemon-start-need-a-restart.md`):

- If `camera list` shows every camera `absent`/`unusable` after the start,
  the pipx build enumerated no devices. Stop it and start from the repo:
  `aprilcam daemon stop; cd /Volumes/Proj/proj/robot-projects/aprilcam; uv run aprilcam daemon start`.
- Cameras plugged in after the daemon started only appear after a restart,
  so plug everything in first.
- A restart drops tag mounts and closes open views/streams; reopen them.
- Close any other app/view holding cameras 2/3/4, and stop the v1 daemon
  (`pgrep -fl aprilcamv1`) if one is running.
- The daemon is local here. If you are targeting the Pi instead, use
  `-d HOST` on every command and `deploy/README.md` for its restart.

Do not restart the daemon again for the rest of this runbook. If you have to,
that is itself a failure of the sprint claim: note when and why.

## 2. Baseline: `camera list` before

```sh
aprilcam camera list
```

Expect, for cameras 2, 3, 4: `lens=legacy` (the 2026-10-15 models embedded in
their `calibration.json`) and **no** `lens-pending` flag. Record the three
lens words in the Results table (row "list before").

## 3. Camera 3, the GUI run (the headline check)

Have the board in hand and a stopwatch. Start the clock when you start
moving the board, not when the window opens.

```sh
aprilcam camera intrinsics 3
```

A fullscreen window opens (dark background) showing, for camera 3: a large
state banner (collecting / GOOD ENOUGH), a single plain-language NEXT line
naming the biggest coverage gap (for example "move the board to the top-left"
or "tilt the board more"), a 3x3 grid of region tiles (which parts of the
frame the board has covered), four tilt tiles, the view count, and once
solving starts rms / residual / correction in pixels. Keys: **Enter**
installs every camera that is green (GOOD ENOUGH); on a camera that is not
good it only shows "not GOOD ENOUGH yet -- keep going" and never forces.
**Esc** (or closing the window) aborts, releasing the camera. Nothing else is
bound, and it never auto-quits on green.

Procedure: hold the board in the secondary field and follow NEXT: sweep the
board across all nine regions, then tilt it in four directions, moving
steadily (a very fast wave gets rejected as blurred; the last-reject text
tells you why). Stop the clock when the banner turns GOOD ENOUGH. Target:
**under one minute of board motion**. Record the time, then the rms,
residual and correction numbers shown. Press **Enter**.

The window closes. The finish message reads
`[3] lens model INSTALLED (error <rms> px, fx <fx>, hfov <deg> deg)` followed by
`run aprilcam camera calibrate 3 to apply` (it may appear in the window
rather than the terminal; note where). Exit status 0 (`echo $?`). Record the
text. The session is finished; the camera is
released.

## 4. `camera list` after install, then `calibrate`, then list again

```sh
aprilcam camera list
```

Expect camera 3: `lens=measured` **and** `lens-pending`; cameras 2 and 4
still `lens=legacy`, no pending flag. Then, without any restart:

```sh
aprilcam camera calibrate 3
```

Expect success and the line `lens model: measured (from the per-camera lens
store, carried forward)`. (Field markers must be in view as usual for
calibrate.) Then:

```sh
aprilcam camera list
```

Expect camera 3: `lens=measured`, `lens-pending` gone. If `calibrate`
instead says `legacy` or `estimated`, that is a failure: capture the output.

## 5. `get_calibration` agrees (MCP)

From any MCP client attached to `aprilcam mcp` (for example Claude Code with
the aprilcam server), call `get_calibration` with `camera="3"`. Expect
`lens_source: "measured"`, and `lens_views`, `lens_rms_px`,
`lens_residual_px`, `lens_installed_at` filled in, matching the numbers
from step 3 (rms/residual to rounding). There is no separate CLI command
for `get_calibration`; the `camera list` lens column is the CLI view of
the same field. Also call it for camera 2 and 4 and expect
`lens_source: "legacy"`.

## 6. Daemon CPU and detection frame rate during a session

This is the architecture review's risk (2x-upscale detection plus periodic
`calibrateCamera` inside the daemon process; contention with the detection
loop). Measure it during a **fresh** camera-3 session (use a new session so
nothing is cached): run `aprilcam camera intrinsics 3` again, which is
allowed because the earlier session ended (the new session simply starts
collecting again; Esc when finished, do not install, or install: either is
fine. Note that installing replaces the step-3 lens with a new one).

Three terminals, all while the board is being moved:

1. CPU of the daemon process (sample every 2 s, record idle vs during):
   ```sh
   DP=$(pgrep -f "aprilcam.daemon" | head -1); echo $DP
   top -pid $DP -stats pid,command,cpu,mem -l 15 -s 2
   ```
   Take a baseline for 10 s with no session first (%CPU idle), then the
   range during the session. (`ps -o %cpu= -p $DP` is the one-shot form;
   `top` cpu is per-core, so 100% = one core.)
2. Detection-loop frame rate (the existing way: the tag stream, one JSON
   line per detected frame with `timestamp` and `frame_index`). Run it for
   ~20 s with no session (baseline) and again for ~20 s during a session:
   ```sh
   aprilcam camera tags 3 --follow --json | head -n 200 | python3 -c '
   import sys, json
   t = [json.loads(l)["timestamp"] for l in sys.stdin]
   print("frames", len(t), "fps", (len(t) - 1) / (t[-1] - t[0]))'
   ```
   If the camera sees no tags the stream may be sparse; keep the playfield
   markers in view of camera 3 (or use camera 2/4) so the baseline is
   meaningful. The intrinsics session itself reads about 8 fps from the
   frames the daemon already owns.
3. The session terminal itself.

Record idle CPU, in-session CPU (typical and peak), baseline fps and
in-session fps. A noticeable drop (say fps falling by more than about 25 %,
or CPU pinned above one core for the whole session, or the board view
stuttering) is a regression: file an issue, do not patch.

## 7. Two-camera run (2 and 4)

```sh
aprilcam camera intrinsics 2 4
```

The window shows both cameras (a section per camera; note the layout). Move the board so each camera
sees it in turn or both at once. Each camera gets its own GOOD ENOUGH. Press
Enter when at least one is green: only green cameras install; a camera that
is not yet good keeps collecting and Enter again installs it later. When
both are installed the window closes and the exit status is 0; if you Esc
with one not installed the status is 1. Record per-camera time, rms,
residual, correction, then `aprilcam camera list` (2 and 4 should now read
`lens=measured` with `lens-pending`; optionally `aprilcam camera calibrate 2`
and `4` to apply). Expect little correction for 2/4 (their lenses are almost
rectilinear: correction near 3 px).

## 8. `--no-gui` text run

Over SSH, or with `--no-gui`, on any camera (use 4 again; installs replace
the previous result, which is fine):

```sh
aprilcam camera intrinsics 4 --no-gui
```

It prints a status block whenever it changes (view count, coverage, NEXT,
rms/residual/correction, banner) at about 3 Hz. Enter installs only when
GOOD ENOUGH (otherwise it prints "not GOOD ENOUGH yet -- keep going"). Ctrl-C
aborts and releases the camera. Also confirm once that
`aprilcam camera intrinsics 4 --no-gui --install` installs by itself, once,
as soon as it is GOOD ENOUGH, and exits 0. Record pass/fail.

## 9. Agent-driven MCP run

Use an MCP client (Claude Code with the aprilcam MCP server). The human holds
the board; the agent drives. Ask the agent to calibrate camera 2's lens and
relay guidance. The tool sequence it should perform:

1. `start_intrinsics(camera="2")` (optionally `board="5x7:37:28:DICT_5X5_100"`)
   returns the initial status dict (state collecting, banner not good_enough).
2. `get_intrinsics_status(camera="2")` repeatedly, at least every few seconds
   (the session auto-aborts after 120 s without polling). The agent relays
   `next_hint` to the human until `good` is true / `banner` is `good_enough`.
3. `finish_intrinsics(camera="2", install=True)` returns
   `installed: true`, the final status, and the lens summary.
   (`abort_intrinsics(camera="2")` is the bail-out.)
4. Verify `get_calibration(camera="2")` shows `lens_source: "measured"`.

Record whether the sequence worked as above, how many polls, and any tool
error text.

## 10. If something fails

Do not patch in this ticket. Capture and file an issue
(`clasi/issues/` via the team lead, or paste to them):

- The exact command and its terminal output, plus the exit status.
- The last status record for the camera: via MCP `get_intrinsics_status`
  (the full dict: state, view_count, rms_px, residual_px, correction_px,
  banner, next_hint, last_reject, errors) or the last block of `--no-gui`
  output.
- `aprilcam camera list` output and `aprilcam daemon status` output (and the
  daemon log, wherever your launch directed it).
- Whether a daemon restart was involved (the sprint claims none after
  step 1).
- A stuck session: `aprilcam camera intrinsics <n> --no-gui` then Ctrl-C, or
  MCP `abort_intrinsics`; it also aborts itself after 120 s unpolled.

## Results (fill in)

| # | Measurement | Result | Pass? |
|---|---|---|---|
| 0 | Install update done (pipx force-install; `camera intrinsics --help` works) | | |
| 1 | Daemon restarted once; camera list shows 2/3/4 present, usable | | |
| 2 | `camera list` before: lens column for 2 / 3 / 4 (expect legacy x3), lens-pending (expect none) | 2: / 3: / 4: | |
| 3 | Camera 3 GUI: time of board motion to GOOD ENOUGH (target < 60 s) | s | |
| 3 | Camera 3: views / rms (px) / residual (px) / correction (px) at install | / / / | |
| 3 | Finish message text; exit status (expect 0) | | |
| 4 | `camera list` after install: 3 (expect measured + lens-pending), 2 and 4 (expect legacy) | 2: / 3: / 4: | |
| 4 | `camera calibrate 3` printed `lens model: measured`; no restart needed | | |
| 4 | `camera list` after calibrate: 3 measured, lens-pending gone | | |
| 5 | MCP `get_calibration` camera 3: lens_source, views, rms, residual match step 3 | | |
| 6 | Daemon CPU %: idle / in-session typical / in-session peak | / / | |
| 6 | Detection fps (`camera tags --follow --json`): baseline / in-session | / | |
| 6 | Any regression (fps drop > 25 %, CPU pinned, stutter); issue filed? | | |
| 7 | Two-camera run `intrinsics 2 4`: per-camera time / rms / residual / correction; exit status | 2: / 4: | |
| 8 | `--no-gui` run (Enter, Ctrl-C abort, `--install`) | | |
| 9 | Agent-driven MCP run (start, polls, finish, get_calibration) | | |
| 10 | Daemon restarts after step 1 (expect 0) | | |

Date/time of run, host, git commit of the install (`git rev-parse --short HEAD`):

## Live check: image-based lens GUI (sprint 046)

Cameras 2 and 3 already carry measured lenses (promoted on 2026-10-16); camera 4
is the one to measure. Run:

```
aprilcam camera lens 2 4
```

1. Two columns appear, each with a live camera image (not just 3x3 tiles).
2. A labelled coverage grid is drawn over each image.
3. Hold the board in view: its outline follows it; move it away and the outline
   disappears.
4. As the board covers a region, that cell goes amber then green, at the same
   moment the corresponding tile would.
5. Rate comparison: perform the same board motion for one minute twice, once
   with the GUI (image on) and once with `aprilcam camera lens 2 4 --no-gui`
   (text mode, baseline). Count kept views per minute in each run and record
   them below.
6. Stop the daemon mid-session: the image dims and the banner shows
   `[cam] frame: ...`. Restart the daemon before further runs.
7. Remote daemon (`-d HOST`): each camera pulls a JPEG at about 4.5 fps, so
   expect noticeable bandwidth; a slow `get_frame` also delays that poller
   step (no time budget).

| Run | Kept views per minute |
|---|---|
| Image on (GUI) | |
| Baseline (`--no-gui`) | |

Date/host/commit of this check:
