---
id: '003'
title: 'Live verification on vidar: USB camera registers alongside CSI cameras'
status: open
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

- [ ] Wheel built and installed via `pipx install --force` as the
      `aprilcam` user; confirmed the fixed `identity.py` is on disk
      before restarting the daemon.
- [ ] `aprilcamd` restarted after the confirmed-fixed install.
- [ ] `camera probe -d vidar.local` lists `imx296` (#1, usable),
      `imx296-2` (#2, unusable), and the USB camera (new number,
      usable), each exactly once.
- [ ] `camera image -d vidar.local <usb-slug>` returns and the frame is
      inspected — confirmed to be a real image, not a placeholder or
      error frame.
- [ ] `camera image` for `imx296` still works after the change.
- [ ] `libcamerasrc`/`sshd` were not deliberately crashed during
      verification.
- [ ] Final state confirmed: all four ROS camera units disabled;
      `aprilcamd` and both bridges enabled.

## Testing

This ticket *is* the test — live, manual, against real hardware. No new
automated tests are written here (those are ticket 001's job).

- **Existing tests to run**: none (no source change in this ticket).
- **New tests to write**: none.
- **Verification command**: the `camera probe`/`camera image` sequence
  in the Description, run from the Mac against `-d vidar.local`.
