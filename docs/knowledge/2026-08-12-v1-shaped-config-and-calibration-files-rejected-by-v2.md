---
date: 2026-08-12
tags: [camera, config, calibration, migration, v1, v2, uvc-util, exposure, daemon]
related-tickets: []
---

# v1-shaped `config.json` / `calibration.json` are silently rejected by v2

## Problem

The stakeholder reported camera 3 (`arducam-ov9782-usb-camera`, the
calibrated `main-playfield` camera) as working — they had been watching
a live view of it that same week. The v2 daemon disagreed: every command
that *opened* the camera failed.

```
$ aprilcam camera tags 3
error: daemon at 'localhost' did not answer: unsupported or rejected setting: '_note'
$ aprilcam camera image 3
error: daemon at 'localhost' did not answer: unsupported or rejected setting: '_note'
```

Both things were true at once. **The live view was the v1 daemon.** v2
had never successfully opened that camera, because the camera's on-disk
state was still entirely v1-shaped — and neither file announces itself
as stale.

Confirming which daemon wrote a file is the fastest way to settle this:
a `calibration.json` containing `camera_matrix` / `corner_pixels` /
`static_markers` / `rms_error` was written by v1, because v2's
`Calibration.to_dict` emits none of those keys.

## Symptoms and how to recognise them

Three distinct failures, in the order you hit them:

1. `unsupported or rejected setting: '<key>'` on **any** command that
   opens the camera, where `<key>` is obviously not a camera control
   (`_note`, `controls`, `program`).
2. After fixing (1): `unsupported or rejected setting: 'exposure-time-abs'`
   — a key that *is* a real `uvc-util` control name, which makes this one
   genuinely confusing.
3. `daemon status` reports the camera `present usable` but
   `uncalibrated`, while `calibration.json` plainly exists on disk. The
   only clue is a **warning**, not an error, in the daemon log:
   `corrupt calibration file for <camera> at <path>: 'calibrated_at' -- ignoring`.

Note that (3) is deliberately non-fatal — `daemon/service.py` treats a
malformed calibration as "no calibration held" rather than failing to
start (the same stance `camera/registry.py` takes). That is correct
behaviour, but it means a stale calibration is *invisible* unless you
read the log.

## Cause

### `config.json` — two independent shape mismatches

v1 wrote a **nested** settings block. v2's
`config/loader.py:parse_camera_config` reads `settings` as a **flat**
map and hands it to `Camera.apply_config`, which validates every key
against `_settable_names()` and raises `UnsupportedSetting` on the
first one it does not recognise:

```jsonc
// v1 — rejected by v2. 'controls' and 'program' are not control names.
"settings": {
  "controls": { "exposure-time-abs": "157", "gain": "20" },
  "program": "uvc-util"
}
```

The second mismatch is the subtle one, and it is why fixing the nesting
alone is not enough. **v2's setting keys are abstract names, not raw
`uvc-util` control names.** The mapping lives in
`camera/backends/uvc_util.py:_CONTROLS`:

| v2 config key               | actual `uvc-util` control  |
|-----------------------------|----------------------------|
| `auto_exposure`             | `auto-exposure-mode`       |
| `exposure`                  | `exposure-time-abs`        |
| `gain`                      | `gain`                     |
| `auto_white_balance`        | `auto-white-balance-temp`  |
| `white_balance_temperature` | `white-balance-temp`       |

Writing `exposure-time-abs` into `settings` therefore fails *even
though that is the correct name to pass to `uvc-util` on the command
line* — which is exactly the trap, because probing the hardware by hand
(`uvc-util -I 1 -s exposure-time-abs=157`) succeeds and makes the
daemon look wrong. The abstract names are also what makes
`_GATE_KEYS = ("auto_exposure", "auto_white_balance")` work: v2 pushes
those two first so auto-modes are off before values are written.

The correct v2 shape:

```json
"settings": { "auto_exposure": "1", "exposure": "20", "gain": "0" }
```

Anything at the **top level** other than `name`, `playfield`, and
`settings` is silently ignored by the loader — so v1's
`camera_position`, `device_name`, `resolution`, and `static_marker_ids`
neither help nor hurt. Keep them only as reference data, and do not
assume `resolution` is being honoured (it is not; resolution belongs in
`settings` as `frame_width`/`frame_height`). `camera_position.height`
in particular is *not* wired to anything yet — see the open issue
`camera-geometry-for-real-parallax-correction.md`.

Keys prefixed with `_` are **not** a comment convention. A `_note`
inside `settings` is treated as a control name and hard-fails the whole
push. Put notes at the top level instead, where they are ignored.

### `calibration.json` — a different v1 shape

`Calibration.from_dict` (`calibration/calibration.py`) requires
`homography`, `calibrated_at`, and a `playfield` that is a **string**.
v1 wrote neither of the first two and made `playfield` a dict:

| v1 file has | v2 requires |
|---|---|
| `camera_matrix`, `dist_coeffs`, `corner_pixels`, `static_markers`, `rms_error`, `tags_used`, `calibrated_playfield` | `homography`, `calibrated_at`, `playfield` (str), `matched_ids`, `reprojection_error_cm`, `stale` |
| `"playfield": {"width": 134.3, "height": 89.3}` | `"playfield": "main-playfield"` |

**Do not migrate a v1 `calibration.json` by hand.** It is tempting —
the stored `corner_pixels` matched live detections to ~1 px, so the
geometry was still accurate — but v1 used the upper-left-origin table
that v2 deliberately removed, and importing its homography risks
importing a different world-frame convention with it. A silently wrong
world frame is far worse than an honest `uncalibrated`. Recalibrate
instead; it takes seconds once the field is clear.

## Fix

1. Rewrite `config.json` in v2 shape (flat `settings`, abstract key
   names). Back up the old one first.
2. Restart the daemon (`aprilcam daemon restart`) — config is applied at
   camera-open time, so an edit alone changes nothing.
3. Delete or ignore the v1 `calibration.json` and re-run
   `aprilcam camera calibrate <n>`.

Verified end state on this machine, 2026-08-12: all 10 ArUco ids
matched, reprojection error **0.168 cm**, world coordinates landing
within ~3 mm of the surveyed values with A1 at the origin, and a clean
deskewed frame at 800x532 / 5.96 px per cm.

## Gotcha: exposure is lighting-dependent, and a stale value looks like a code bug

The config carried `exposure=157, gain=20`, measured 2026-08-08 with a
note explaining that the camera powers up at `exposure-time-abs=6` and
renders the field nearly black. Under the lighting three days later
those same values blew the frame out to **pure white** — 0-1 tags
detected, presenting identically to a detection regression.

A quick sweep is the fastest way to tell the difference. Set the control
directly with `uvc-util` (no daemon restart needed; the daemon only
applies config at open time) and count detections:

```bash
for e in 5 10 20 40 60 100 157; do
  uvc-util -I 1 -s exposure-time-abs=$e >/dev/null 2>&1; sleep 0.6
  echo "exposure=$e -> $(aprilcam camera tags 3 2>&1 | grep -cE '^\s+(apriltag|aruco)')"
done
```

`exposure=20, gain=0` was correct that day. **Re-sweep whenever
detection degrades; do not assume the stored value is still right.**

## Gotcha: one-shot `camera tags` needs a few polls to warm up

Sprint 024 added presence-based temporal confirmation to kill phantom
detections, so the *first* `camera tags` call after a camera opens
often reports `(no tags detected)` or only one or two markers even when
the frame is perfect. Poll three or four times before concluding
anything about detection quality — full recall typically appears by the
third call. A single empty poll is not evidence of a problem.

## Gotcha: occluded fiducials fail calibration with a moving target

Calibration enforces the all-fiducials rule (UC-04), so anything resting
on the field blocks it:

```
calibration failed for '3': missing ArUco id(s): 4, 10
```

Ids 4 and 10 are the east edge and east-internal markers; a robot
assembly parked on that side of the field hides exactly those two. The
missing set **changes between runs** as things move, which reads like
flakiness — during this session the reported set drifted across `(9)`,
`(3,4,5)`, `(4,10)`, and `(6..10)` before the field was cleared and all
ten matched. Check the physical field before debugging the detector.
Cross-reference the id -> position map in the playfield definition
(`~/.config/aprilcam/playfields/<name>.json`) to work out which part of
the field to clear.

This also silently poisons the hardware test suite. A full
`uv run pytest` during the occluded window produced 14 failures spread
across sprints 020-026 modules, all tracing to that one physical
condition and none to any code defect. After clearing the field and
recalibrating, the same unfiltered suite ran **1324 passed, 4 skipped**.

## Gotcha: a camera that cannot open can destabilise the daemon's threads

Before the config was fixed, running the full unfiltered suite
reproducibly hard-crashed with `Fatal Python error: Aborted` (SIGABRT,
exit 134) — always inside the sprint-027 streaming detection worker
thread during garbage collection, observed at both
`calibration/geometry.py:pixel_to_world` and
`daemon/detection.py:_pixel_corners`, reached via
`daemon/streaming.py:_run`. Each subset (headless-only, hardware-only)
passed cleanly on its own; only the combined run aborted.

After the camera-3 config was repaired so the camera actually opens, the
same unfiltered command completed cleanly twice. The crash was never
directly root-caused, so treat this as a correlation worth knowing
rather than a settled fix: **if the streaming detection thread starts
aborting during GC, check whether a camera is failing to open first.**

## Related

- `docs/knowledge/2026-08-01-uvc-util-camera-control-macos.md` — why
  `uvc-util` exists at all, and the control-ordering rule.
- `clasi/issues/hardening-cleanup-and-the-design-doc-set.md` — records
  the v1/v2 control-socket collision, the sibling problem to this one:
  a running v1 daemon shares the conventional socket path, so a v2
  client can reach a v1 daemon and get `Method not found!`.
- `clasi/issues/camera-geometry-for-real-parallax-correction.md` — why
  `camera_position` is currently inert.
