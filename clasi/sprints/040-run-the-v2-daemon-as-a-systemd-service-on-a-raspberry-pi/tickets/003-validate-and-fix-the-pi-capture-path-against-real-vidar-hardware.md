---
id: '003'
title: Validate and fix the Pi capture path against real vidar hardware
status: open
use-cases: []
depends-on: ['001']
github-issue: ''
issue: live-tag-detection-and-systemd-persistence-on-a-pi-with-a-calibrated-playfield.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Validate and fix the Pi capture path against real vidar hardware

## Description

Three modules were converted from v1 under an explicit "no Pi hardware to
validate against" deferral and have never read a real frame:
`camera/backends/libcamera.py` (`list_loopback_cameras()` — `/sys` label
enumeration), `camera/backends/v4l2_loopback.py` (`V4l2LoopbackBackend` —
`v4l2-ctl` subprocess frame reader), and
`camera/backends/__init__.py::resolve_capture_backend` (the
libcamera-vs-everything-else branch point) plus `camera/identity.py`'s
`_probe_libcamera` injection point. `vidar.local` has exactly the hardware
these were written for: two imx296 CSI cameras behind v4l2loopback devices
`/dev/video70`/`/dev/video71` with card labels
`aprilcam-imx296-88000`/`aprilcam-imx296-80000` (already matching the
`aprilcam-` prefix `list_loopback_cameras()` expects). This ticket is the
first time any of this code sees real hardware — expect to find and fix
real bugs; do not treat "the code was already written" as "the code
already works."

**Requires vidar access.** Coordinate with ticket 004: this ticket needs
the CSI cameras free of ROS (`ros-cameras.service` and its bound units
stopped — see ticket 004's exact stop sequence, which this ticket may run
first if it executes before 004) and needs a bridge feeding
`/dev/video70`/`71` with real frames — use ticket 001's
`aprilcam-camera-bridge` script run directly (e.g. via `setsid`, matching
sprint 014's precedent of not installing systemd units before they're
verified) rather than the full systemd unit, since this ticket is about
validating capture-path *code*, not the deploy mechanism (that's ticket
004). Leave ROS stopped for ticket 004 to continue from, rather than
restoring it between tickets — document the exact state left behind at the
end of this ticket so ticket 004 (or a human) can see it at a glance.

Run the actual `aprilcam` code against vidar's hardware via a temporary
checkout/venv over SSH (this ticket predates the "official" pipx install,
which is ticket 004's job) — e.g. `git clone`/`rsync` this repo to
`/tmp` on vidar, `python3 -m venv`, `pip install -e '.[daemon]'`, then
exercise `list_loopback_cameras()`, `resolve_capture_backend`, and
`V4l2LoopbackBackend.open()/read()` directly (a small script or an
interactive `python3 -c`/REPL session) against the real devices. Fix
whatever real hardware reveals: e.g. `_query_format`'s `v4l2-ctl
--get-fmt-video` output parsing against vidar's actual output format,
whether the YUY2 color conversion produces a correct (not
channel-swapped) image, whether `list_loopback_cameras()`'s regex/prefix
matching finds both real card labels, whether `resolve_capture_backend`'s
`camera_id` matching correctly resolves `device_index` 70 and 71.

Keep every fix in v2 style: real base classes, never `typing.Protocol`
(per `src/aprilcam/daemon/interfaces.py`'s module docstring and this
project's global rule) — none of these three modules currently use
`Protocol`, so this is a "don't introduce one," not a "convert one" ticket.

## Acceptance Criteria

- [ ] `list_loopback_cameras()` run on vidar's real `/sys` tree returns
      exactly two `LibcameraDevice` entries with `device_index` 70 and 71
      and `camera_id` matching the real card labels.
- [ ] `resolve_capture_backend` given each real `hardware_key`
      (`libcamera:aprilcam-imx296-88000`, `libcamera:aprilcam-imx296-80000`)
      constructs a `V4l2LoopbackBackend` pointed at the correct
      `/dev/videoN`.
- [ ] `V4l2LoopbackBackend.open()`/`.read()` against each real device
      returns frames; a captured frame is confirmed visually correct (not
      garbled, not channel-swapped) — save at least one sample frame from
      each camera during this ticket's session for the record.
- [ ] Every real defect found is fixed, and each fix has an offline unit
      test (faked `/sys` tree or faked subprocess output reproducing the
      exact condition found) added to the existing test files for these
      modules — so the fix is guarded without needing hardware to
      re-verify.
- [ ] `scripts/test-gate.sh` passes with the new/updated tests included.
- [ ] This ticket's file records: what was found, what was fixed, and the
      exact state ROS/the ad hoc bridge processes were left in on vidar
      for ticket 004 to pick up from.

## Implementation Plan

**Approach**: SSH to `ros@vidar.local`. Stop `ros-cameras.service` (and
bound units) per ticket 004's documented sequence (coordinate — read that
ticket first if executing out of the planned order). Get a bridge feeding
both loopback devices (ticket 001's script, run by hand). Deploy a
throwaway checkout/venv of this repo to vidar and exercise the three
capture-path modules directly against real hardware. Iterate: fix, re-test
live, then port every finding back to an offline, hardware-independent
unit test before moving on. Only touch `camera/backends/*.py` and
`camera/identity.py` — no interface changes are expected, but if real
hardware genuinely requires one (e.g. a return-shape change), that is
still in scope; note it explicitly in this ticket rather than working
around it with a hack.

**Files to modify** (as needed; do not touch what isn't broken):
`src/aprilcam/camera/backends/libcamera.py`,
`src/aprilcam/camera/backends/v4l2_loopback.py`,
`src/aprilcam/camera/backends/__init__.py`,
`src/aprilcam/camera/identity.py`, and their corresponding test files
under `tests/`.

**Testing plan**: scope the offline run to the touched modules
(`.claude/rules/source-code.md`: "scope the run to the modules the ticket
touches"), e.g. `uv run pytest tests/aprilcam/camera/ -k
"backends or identity"` plus any newly added tests; run in the foreground,
never backgrounded. The live/hardware verification itself has no pytest
form — it is manual, over SSH, and its outcome is recorded in this ticket
file directly.

**Documentation updates**: note any real-hardware finding worth capturing
for ticket 005's knowledge doc (e.g. exact `v4l2-ctl --get-fmt-video`
output format seen on vidar) directly in this ticket's Description/
Acceptance Criteria checkboxes as they're checked off, so ticket 005 has a
primary source to pull from.
