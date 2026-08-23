---
id: '004'
title: Operator-assisted image-matrix capture session for cameras 2, 3, 5
status: in-progress
use-cases:
- UC-10
depends-on:
- '003'
github-issue: ''
issue: aprilcam-v2-layered-test-system.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Operator-assisted image-matrix capture session for cameras 2, 3, 5

## Description

**This ticket requires a human physically present at the rig and
cannot be completed by an agent alone.** It is the sprint's one hard
human dependency — every ticket in ticket-group 1 and the
capture-tool-update half of ticket-group 2 (ticket 003) is sequenced
before this one specifically to minimize what blocks on it. Everything
downstream (tickets 005-008, the entire L0 pytest package) is gated on
this ticket landing successfully.

Using the updated capture tool from ticket 003, an operator captures a
fresh, confirmed image-matrix fixture generation (`fs-20260823`) for
cameras 2 (`hd-usb-camera`), 3 (`arducam-ov9782-usb-camera`), and 5
(`arducam-ov9281-usb-camera`), and confirms the field state that was
physically present during capture. The existing July corpus
(`unverified-20260729`) is left in place, not replaced — manifests
already carry `field_state_id`, so both generations coexist.

**Required real-world steps (agent cannot perform these):**
1. Make sure the real daemon is running (`aprilcam daemon start` if it
   is not) — as of 035-004 this is inverted from earlier guidance: the
   capture tool now *requires* a reachable daemon and refuses to run
   without one, rather than refusing while one holds the device. See
   this ticket's 2026-08-23 (035-004 rework) Handoff Note below for why.
   Closing any open `aprilcam view` window on the target camera is
   still worth doing, since a live view pushing its own config while
   this tool is sweeping the same camera would fight it.
2. Run the capture tool from a Terminal session (not from an IDE/agent
   shell) — macOS camera permission for a fresh process requires this.
3. Physically confirm what is on the field at capture time: AprilTags
   present, all 10 ArUco markers present, which colored
   rectangles/dots are placed — and report this back so it can be
   written into `field-state.json`'s `fs-20260823` entry.
4. Note camera-5 lighting conditions at capture time (day/night) in the
   manifest, since camera 5's exposure sweep spans both.
5. Confirm the session completed cleanly (baseline controls restored,
   no camera left misconfigured) before ending the Terminal session.

## Acceptance Criteria

- [ ] `tests/images/fs-20260823/<slug>/` exists for all three cameras
      (2, 3, 5), each with a full variation-matrix set of JPEGs, a
      `manifest.json`, and a pinned `calibration.json` snapshot (per
      ticket 003's schema).
- [ ] `tests/images/field-state.json` gains a `fs-20260823` entry with
      `status: "confirmed"` and non-empty `visible_april_tags`/
      `visible_aruco_tags`/`visible_rectangles`/`visible_dots` arrays,
      populated from the stakeholder's real-time confirmation during the
      session — not guessed or carried over from the July stub.
- [ ] The existing `unverified-20260729` entry and the July fixture
      files under `tests/images/arducam-ov9782-usb-camera/` and
      `tests/images/hd-usb-camera/` are untouched.
- [ ] Every new manifest records camera 5's lighting condition
      (day/night) at capture time.
- [ ] The real daemon and all cameras are left in their normal,
      pre-session working state after the session ends (baseline
      controls restored; `aprilcam daemon start` brings everything back
      up normally) — confirmed by the operator before closing the
      Terminal session.
- [ ] All new fixture files, the updated `field-state.json`, and the
      per-camera manifests are committed to git.

## Implementation Plan

**Approach**: This is an operational ticket, not a code-authoring one.
The team-lead/operator runs
`python testdev/capture_matrix.py <camera> --field-state-id fs-20260823`
for each of cameras 2, 3, 5 per the capture tool as reworked by 035-004
(daemon-driven — see the Handoff Note below), in a Terminal session,
**with the daemon running** (not stopped — 035-004 inverted this
precondition), following the ~15-minute procedure named in the sprint's
Test Strategy section. If the session surfaces a problem with the
capture tool itself (a bug, not an operator-confirmable field-state
question), that is a defect to report back before this ticket can
complete — not silently worked around here.

**Files created/modified**:
- `tests/images/fs-20260823/{hd-usb-camera,arducam-ov9782-usb-camera,arducam-ov9281-usb-camera}/**`
  (new)
- `tests/images/field-state.json` (new `fs-20260823` entry)

**Testing plan**:
- No automated test in this ticket — verification is the operator's
  real-time confirmation plus the acceptance criteria above. Tickets
  005-007 are what write the automated tests that consume these
  fixtures.
- **Verification command**: N/A (operational ticket; no `pytest` run is
  this ticket's own gate — the resulting fixtures are exercised by
  ticket 005 onward).

**Documentation updates**:
- None beyond the manifest/field-state.json data itself.

## Risks Carried From sprint.md

- July's baseline captures recorded zero AprilTags. If this session
  repeats that outcome, tickets 005/007's AprilTag-specific recall and
  world-accuracy assertions gain a debugging detour before they can be
  written meaningfully — flag this immediately if observed, rather than
  silently writing tests against an empty AprilTag set.
- Camera-5 lighting variance must be recorded in the manifest (see
  Acceptance Criteria) since its exposure sweep spans day/night.

## Handoff Notes (2026-08-23, 035-004 rework: capture tool is now daemon-driven)

`testdev/capture_matrix.py` was reworked to drive the **daemon**
instead of owning the camera device directly — implement-and-unit-test
only in this pass, **no real sweep was run** (camera 3 was reported
live in use). This directly addresses the blocker the note below this
one recorded (`check_daemon_not_running()` refusing whenever a daemon
holds the machine-global lock): that check is now **inverted**. A
reachable daemon is a precondition, not a blocker — `main()` calls
`require_daemon()` first and refuses with a clear message (exit 2) if
nothing answers, pointing at `aprilcam daemon start`.

**Why**: this project's architecture makes the daemon the sole vision
authority, and the daemon already exposes every operation this sweep
needs (`GetCameraConfig`/`SetCameraConfig`/`GetFrame`/`GetTags`) —
hand-verified live against camera 5 before writing any code (pushed
`exposure=120` → mean luminance 205.9; `--reset` → back to
`exposure=20`, mean 115.3). Direct device access (the old
`OpenCVBackend.open()` path) also fails outright under Claude Code —
macOS TCC denies camera access to the responsible bundle (see
`docs/knowledge/claude-code-camera-permission-run-via-terminal.md`) —
while the daemon path does not hit that wall at all.

**What changed** (full rationale is in `testdev/capture_matrix.py`'s
own module docstring — read that before touching this tool again):

- Camera selection now comes from `Daemon.list_cameras()` (present,
  playfield-linked, matched by slug/number) instead of live
  `camera.identity.resolve_all()` + `CameraRegistry` hardware
  enumeration. No more `cv2`, `OpenCVBackend`, or `uvc-util`
  subprocess anywhere in this tool.
- Each variation pushes ONE whole `CameraConfig` document per capture
  via `Daemon.set_camera_config` (baseline settings overlaid with the
  variation), rather than two separate raw `uvc-util` calls
  (`apply_controls(baseline)` then `apply_controls(variation)`).
- **The HD-cam gate-ordering fix (`_gate_first_order`) is now dead
  code and was deleted, not ported** — confirmed by reading
  `camera/camera.py`: `Camera.apply_config` already pushes
  `_GATE_KEYS` (`auto_exposure`, `auto_white_balance`) first,
  unconditionally, for every push it receives. What survives,
  translated to `CameraConfig.settings`'s five-key vocabulary, is
  `_ensure_gate_overrides` — `apply_config` only *reorders* a gate key
  that is already present in a push, it never *injects* one, so a
  variation touching `exposure`/`white_balance_temperature` without
  naming its own gate still needs that forced to manual client-side.
- New and load-bearing, found while writing the fake-daemon test
  suite (not anticipated going in): `_drop_settings_with_inactive_gate`.
  `Daemon.set_camera_config` is one atomic whole-document push (UC-03)
  with no per-key tolerance, unlike the old tool's discardable per-key
  `uvc-util -s` calls — so a baseline whose own gate is genuinely in
  auto mode (exactly the July HD-cam scenario: both gates auto) would
  otherwise fail to even *re-establish itself* as the sweep's starting
  point, since the gated value key it also carries (e.g. `exposure`)
  cannot be pushed while its gate isn't manual. This function drops
  such a value key from a push before it's attempted (baseline
  establish, every variation, and the `finally` restore all go through
  it) rather than reacting to the failure after the fact. The gate key
  itself is always left in place; only the value key it gates is
  omitted from what's actually pushed (still recorded informationally
  in the manifest's `baseline_controls`).
- No live uvc-util range query for the generic fallback sweep (HD-cam,
  camera 2) any more — the daemon has no RPC exposing hardware control
  ranges. Fixed ranges from `camera/backends/uvc_util.py`'s own
  documented control ranges (exposure 1..5000, gain 0..100,
  white_balance_temperature 2800..6500) are used instead —
  `capture_matrix.yaml`'s `fallback:` section now carries these
  explicitly.
- `capture_matrix.yaml`'s `controls:` maps (both the ov9782/ov9281
  dedicated entries and the fallback section) were re-keyed from raw
  `uvc-util` control names (`exposure-time-abs`, `auto-exposure-mode`,
  …) to `CameraConfig.settings`'s own vocabulary (`exposure`,
  `auto_exposure`, …) — the only vocabulary `set_camera_config`
  accepts. Values are numerically unchanged; the ov9281 exposure sweep
  is still 5–300, still no white-balance axis. The stale `--dry-run`
  docstring/code mismatch a prior pass flagged was also dropped from
  the module docstring's Usage section while rewriting it.
- Baseline detection counts now come from `Daemon.get_tags` instead of
  a local `TagDetector` pass; the per-image dark/washed luminance
  check now decodes the daemon's JPEG with Pillow + numpy (both base
  installs) instead of `cv2` — this tool has no OpenCV dependency at
  all any more, consistent with the project's "vision boundary" rule
  even though `testdev/` isn't one of the three named thin clients.
- `aprilcam.client.discovery.Discovery` / `aprilcam.daemon.interfaces.
  Daemon` covered every operation needed — `get_camera_config`,
  `set_camera_config`, `get_frame`, `get_tags`, `list_cameras`. No
  shelling out to the `aprilcam` CLI was necessary anywhere.

**Testing**: `uv run pytest tests/aprilcam/testdev/test_capture_matrix.py`
— 41 passed. Everything is exercised against a small in-memory
`FakeDaemon` (a real `Daemon` subclass modeling `Camera.apply_config`'s
gate-first/whole-push-atomic contract) — no real daemon, no real
hardware, no `uvc-util` subprocess. Includes a reproduction of the
July HD-cam gate-rejection bug against the fake and a proof the fix
still holds through the new push path.

**Not done in this pass, still blocking this ticket's own acceptance
criteria**: no real capture has been run against cameras 2/3/5, no
`tests/images/fs-20260823/` fixtures exist yet, and `field-state.json`
has not gained a confirmed `fs-20260823` entry. **Camera 3 was reported
live in use during this pass** — pushing configs to it during a real
sweep will visibly change the stakeholder's live view, so the actual
operator session (this ticket's real deliverable) should only proceed
once the rig is confirmed free, per the note below.

## Handoff Notes (2026-08-23, stood down before capture)

A prior pass on this ticket found the real daemon and
`aprilcam playfield view 3` already running mid-session (started from
an interactive Terminal, not a stray leftover) and correctly held
rather than killing them — `check_daemon_not_running()` in
`testdev/capture_matrix.py` refuses to run against any camera while
any daemon holds the machine-global lock, regardless of which camera
pattern is passed. The stakeholder confirmed this was his own
deliberate, active rig session and asked us to wait; the ticket went
back to `open` for re-dispatch once he reports the rig is free. No
capture was run, no files were written under `tests/images/`, no
camera or daemon state was touched. **Whoever picks this back up must
re-check daemon/viewer status fresh — do not assume the rig is still
clear from this note.**

**1. Stakeholder decision on field state (2026-08-23) — AprilTags 10,
11, 15, 100 are TRANSIENTS, not ground truth.** Only AprilTag 1 is
real, surveyed ground truth. The expected field state for
`fs-20260823` (and the `visible_april_tags` array written into
`field-state.json`) is:

- AprilTags: **[1]** only — do **not** include 10, 11, 15, or 100.
- ArUco: all **10** field markers (ids 1–10).
- Rectangles: all **6** surveyed rectangles.
- Dots: all **8** surveyed dots.

This resolves the discrepancy the prior pass flagged between the
issue's expected-tag list and `config/aprilcam/playfields/main-playfield.json`:
the survey file was right all along (it surveys exactly 1 AprilTag, 10
ArUco tags, 6 rectangles, 8 dots) — the issue's larger AprilTag count
was counting transients that happen to be on the field sometimes, not
part of the field's ground truth. Writing 10/11/15/100 into
`field-state.json` as expected detections would make L0 go red every
time the field is cleared of transients, so don't.

**2. Prep already done — reuse it, don't redo it:**

- **July corpus before-snapshot** (to verify untouched-ness after the
  next capture): `tests/images/hd-usb-camera/` = 9 JPEGs
  (`auto-exposure-1.jpg`, `auto-wb-false.jpg`, `baseline.jpg`,
  `gain-128.jpg`, `gain-13.jpg`, `gain-204.jpg`, `gain-255.jpg`,
  `gain-38.jpg`, `gain-76.jpg`) + `manifest.json`
  (md5 `184b9f9cbb373a8ce070f03cb2aba9c2`).
  `tests/images/arducam-ov9782-usb-camera/` = 19 JPEGs
  (`auto-exposure.jpg`, `baseline.jpg`, `exp-03/04/08/10/12/16/24/40.jpg`,
  `gain-01/04/08/16.jpg`, `stress-highexp-logain.jpg`,
  `stress-lowexp-higain.jpg`, `wb-3000/6500/auto.jpg`) + `manifest.json`
  (md5 `9d2b32e7d0a41bdad4499b3ca0572366`). Re-check these md5s after
  the real capture to confirm the July generation was left alone.
- **Live camera enumeration** (enumeration-only, no `Camera.open()`,
  confirmed safe to run outside Terminal): registry number 2 =
  `hd-usb-camera` (hardware_key `avf:0x211412032e40317`), 3 =
  `arducam-ov9782-usb-camera` (`avf:0x21141400c456366`), 5 =
  `arducam-ov9281-usb-camera` (`avf:0x21141110c456366`). Other camera
  config dirs on disk (`arducam-ov9782-usb-camera-15`,
  `global-shutter-camera`, `brio-501`, `usb2-video`) currently have no
  matching registry record or aren't in scope — a per-camera pattern
  won't accidentally hit them. Use the exact slugs as patterns:
  `hd-usb-camera`, `arducam-ov9782-usb-camera`,
  `arducam-ov9281-usb-camera`.
- **Calibration snapshots exist for all three targets**:
  `~/.local/share/aprilcam/cameras/<slug>/calibration.json` is present
  and valid JSON for all three (state_dir resolves to the
  `~/.local/share/aprilcam` fallback on this dev machine, since
  `/var/lib/aprilcam` doesn't exist) — ticket 003's
  `pin_calibration_snapshot` will have a real file to copy for each
  camera.
- **Minor doc/code mismatch spotted, out of scope for this ticket**:
  `testdev/capture_matrix.py`'s module docstring documents a
  `--dry-run` flag; `parse_args` currently has no such option.
