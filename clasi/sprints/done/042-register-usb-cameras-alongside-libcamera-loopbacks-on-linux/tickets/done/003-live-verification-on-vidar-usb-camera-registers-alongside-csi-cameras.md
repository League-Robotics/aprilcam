---
id: '003'
title: 'Live verification on vidar: USB camera registers alongside CSI cameras'
status: done
use-cases:
- SUC-001
depends-on:
- '001'
github-issue: ''
issue: usb-cameras-ignored-on-a-linux-host-that-also-has-libcamera-loopbacks.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Live verification on vidar: USB camera registers alongside CSI cameras

## Description

Verify ticket 001's fix against real hardware on `vidar.local` (Pi 5,
`ssh ros@vidar.local`, passwordless sudo). This is a deployment +
manual-verification ticket, not a code-writing ticket, but it is a
first-class ticket because "the fix works on real mixed hardware" is
this sprint's actual success criterion — the offline tests in ticket
001 can only prove the merge logic is correct against fixtures, not
that the real enumeration, the real registry on disk, and the real
capture path behave as expected.

**Current live state (from the issue and team-lead's investigation):**
registry has `imx296` (#1, `libcamera:aprilcam-imx296-88000`) and
`imx296-2` (#2, `libcamera:aprilcam-imx296-80000`), `next_number` 13.
The imx296 (i2c@88000) port has the working CSI camera; imx296-2's port
(i2c@80000) is empty (unusable, expected). A Microdia "Streaming Camera"
UVC camera is attached at `/dev/video0` (`uvcvideo`, plus a metadata
node `/dev/video1`).

**Steps:**

1. Build a wheel for the current source tree (the version will **not**
   be bumped mid-sprint per this project's version-bump-cadence rule —
   confirm the wheel's contents are the fixed code, not rely on the
   version string to prove it).
2. Per `deploy/README.md`, install it as the `aprilcam` user with
   `pipx install --force <wheel>` (not a normal `pipx install`, which
   would no-op on an unchanged version string).
3. Before restarting `aprilcamd`, confirm the new code is actually on
   disk (e.g. inspect the installed package's `identity.py` for the
   `device_index`-keyed `_probe_libcamera` or the merged `resolve_all`
   — do not just trust that the install command succeeded).
4. Restart `aprilcamd` (systemd service).
5. From the Mac, using `uv run aprilcam ...`:
   - `camera probe -d vidar.local` — confirm `imx296` (#1, usable),
     `imx296-2` (#2, unusable), and the USB camera (a new number,
     usable) each appear exactly once.
   - `camera image -d vidar.local <usb-slug>` — confirm a real frame
     comes back and actually look at it (open/inspect the image, don't
     just check the call didn't error).
   - `camera image -d vidar.local imx296` (or equivalent) — confirm the
     existing CSI camera capture path is unaffected.
6. Leave the Pi in its intended final state: all four ROS camera units
   disabled, `aprilcamd` and both bridges enabled. Do not leave the
   system in a partially-migrated state.

**Hazard to avoid:** do not deliberately crash `libcamerasrc` to test
failure handling — team-lead notes it has previously wedged `sshd` on
this host. If something needs to be tested destructively, escalate
rather than risk losing SSH access to the Pi.

If live verification finds a real discrepancy from ticket 001's offline
tests (e.g. an enumeration detail the fixtures didn't capture), do not
silently patch around it here — reopen ticket 001 (or throw an
exception per this agent's exception protocol if it implies the
architecture decision itself was wrong) rather than making an
undocumented code change from within a verification ticket.

## Acceptance Criteria

- [x] Wheel built and installed via `pipx install --force` as the
      `aprilcam` user; confirmed the fixed `identity.py` is on disk
      before restarting the daemon.
- [x] `aprilcamd` restarted after the confirmed-fixed install.
- [x] `camera probe -d vidar.local` lists `imx296` (#1, usable),
      `imx296-2` (#2, unusable), and the USB camera (new number,
      usable), each exactly once.
- [x] `camera image -d vidar.local <usb-slug>` returns and the frame is
      inspected — confirmed to be a real image, not a placeholder or
      error frame.
- [x] `camera image` for `imx296` still works after the change.
- [x] `libcamerasrc`/`sshd` were not deliberately crashed during
      verification.
- [x] Final state confirmed: all four ROS camera units disabled;
      `aprilcamd` and both bridges enabled.

## Live Verification Results (2026-09-15)

Ran the full deploy/verify sequence against `vidar.local` (Pi 5).

- **Wheel**: `uv build --wheel` produced
  `dist/aprilcam-0.20260914.2-py3-none-any.whl` — same version string as
  the already-installed build (sprint 041), so `pipx install --force`
  was required (a plain install would have no-op'd). Confirmed the
  wheel's `aprilcam/camera/identity.py` contained the `device_index`
  keying and union-merge comments before shipping it.
- **Install**: `sudo -u aprilcam -H pipx install --force
  '/tmp/aprilcam-0.20260914.2-py3-none-any.whl[daemon]'` succeeded
  ("Installing to existing venv 'aprilcam'"). Before restarting,
  grepped the installed
  `.../pipx/venvs/aprilcam/lib/python3.12/site-packages/aprilcam/camera/identity.py`
  and confirmed the same `device_index`/union-merge text was present on
  disk.
- **Registry backup**: `sudo cp registry.json /tmp/registry.json.pre042`
  taken before restart; deleted after verification passed.
- **Restart**: `sudo systemctl restart aprilcamd` — active immediately.
  `journalctl -u aprilcamd` since the restart shows a clean sequence:
  directories resolved, `imx296-2 opened but delivered no frame within
  1.0s -- reporting unusable`, mDNS advertising, lifecycle started,
  `aprilcamd ready` — no traceback.
- **Probe** (`uv run aprilcam camera probe -d vidar.local`), exactly
  three entries, each once:
  ```
  [1]    imx296                           present  usable
  [2]    imx296-2                         present  unusable
  [13]   streaming-camera-streaming-cam   present  usable
  ```
  The `/dev/video1` metadata node does not appear. `camera list -d
  vidar.local` also shows exactly these three (note: `list` reports
  #2 as "usable" where `probe` correctly reports "unusable" for it —
  `list` reflects registry/last-known state rather than doing a live
  frame-probe; this is pre-existing `list` vs `probe` behavior,
  unrelated to ticket 001's enumeration-merge fix, so not touched here).
- **Registry** now has three records; #1 and #2 unchanged (same
  hardware keys, numbers 1/2). New USB record:
  `"hardware_key": "vidpid:0c45:636b@dev-video0"`, `"number": 13`,
  `"device_name": "Streaming Camera: Streaming Cam"`. `next_number`
  advanced to 14.
- **USB frame**: `camera image -d vidar.local
  streaming-camera-streaming-cam -o .../vidar-usb.jpg` wrote 123,927
  bytes. Inspected: a real (if overexposed/wide-angle, fisheye-like)
  photo of a garage workshop — pegboard, tools, workbench, ceiling
  joists and a bright light source blowing out part of the frame. Not
  a placeholder or error image.
- **CSI frame**: `camera image -d vidar.local imx296 -o
  .../vidar-csi.jpg` wrote 293,962 bytes — a clear, well-exposed wide
  shot of the same workshop (workbench, yellow stools, pegboard tools,
  bicycles hanging from the ceiling), confirming the existing CSI
  capture path (`/dev/video70`) is unaffected.
- **No regression**: `journalctl -u aprilcamd` since restart has no
  `rp1-cfe`, no duplicate camera entries, no errors/tracebacks.
  `aprilcam-bridge@1` continues its pre-existing harmless restart loop
  (`no libcamera camera at position 1`, restart counter already >5000
  before this deploy) — unrelated to this change, matches the
  documented "restart-loops harmlessly" expectation for the empty
  i2c@80000 port.
- **Final state**: `aprilcamd`, `aprilcam-bridge@0`, `aprilcam-bridge@1`
  all `enabled`+`active`(ish — bridge@1 activating/restarting as
  expected). All four ROS units (`ros-cameras`,
  `camera-watchdog.timer`, `camera-color-relay`, `web-video-server`)
  remain `disabled`+`inactive`, untouched.
- No discrepancy from ticket 001's offline tests was found; no code
  change was needed in this ticket. `libcamerasrc`/`sshd` were not
  deliberately crashed. Registry backup deleted after all checks
  passed.

## Testing

This ticket *is* the test — live, manual, against real hardware. No new
automated tests are written here (those are ticket 001's job).

- **Existing tests to run**: none (no source change in this ticket).
- **New tests to write**: none.
- **Verification command**: the `camera probe`/`camera image` sequence
  in the Description, run from the Mac against `-d vidar.local`.
