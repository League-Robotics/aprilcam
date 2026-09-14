---
id: '004'
title: Live install, verification, and v1 retirement on vidar.local
status: open
use-cases: []
depends-on:
- '001'
- '002'
- '003'
- '006'
- '007'
github-issue: ''
issue:
- plan-daemon-runs-on-a-remote-raspberry-pi-every-client-discovers-it-and-connects-over-tcp.md
- live-tag-detection-and-systemd-persistence-on-a-pi-with-a-calibrated-playfield.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Live install, verification, and v1 retirement on vidar.local

## Description

This is the sprint's centerpiece: install v2's real deploy assets (ticket
001, fixed capture path from ticket 003, install-safety findings from
ticket 002, the fixed watchdog from ticket 006, the fixed `-d
vidar.local` hostname connect from ticket 007) on `vidar.local` and prove
the full acceptance criteria named in sprint.md's Success Criteria.
**Requires SSH as `ros@vidar.local`** (key auth, passwordless sudo).
Every state change made to shared/ROS-owned services must be recorded
exactly (before/after) so it can be restored.

**Hardware update from ticket 003 (Finding 6):** camera i2c@88000
(`/dev/video70`, ROS `camera0`) has a physical fault — `libcamerasrc`
reproducibly SIGSEGVs with `Camera frontend has timed out! Please check
that your camera sensor connector is attached securely.`, reproduced four
times including directly with the `cam` tool (bypassing GStreamer/the
bridge/AprilCam entirely). This was **already broken under ROS before
this sprint touched vidar**, not something this sprint's work caused: ROS's
own `camera0` node ran at 0.2% CPU (vs. `camera1`'s 4%) and ROS's own
watchdog logged `camera0`'s stream repeatedly serving 22 bytes. This
ticket's acceptance criteria below are adjusted accordingly — see the
per-criterion notes. If the stakeholder reseats the CSI ribbon cable (or
swaps the sensor) before this ticket runs, verify both cameras end to end
as originally scoped and note in this ticket that the hardware issue was
resolved.

**Exact state changes allowed on vidar, and what must be recorded:**

- Stop `ros-cameras.service` (this takes `camera-color-relay.service` and
  `web-video-server.service` down with it via `BindsTo=`/`PartOf=`) and
  stop `camera-watchdog.timer`. Record their `systemctl is-enabled` and
  `is-active` state *before* touching anything.
- For the reboot-persistence check specifically: additionally `systemctl
  disable ros-cameras.service` and `systemctl disable
  camera-watchdog.timer` so they don't race AprilCam for the cameras
  across the reboot (`camera-watchdog.service` itself skips when
  `ros-cameras` is not `active`, so disabling the timer, not the service,
  is the minimal correct change — verify this reasoning against the
  actual unit file rather than assuming it).
- Do **not** edit any Ansible-managed unit file. Only touch
  enabled/active state via `systemctl`.
- Stop and disable the v1 `aprilcamd.service` and
  `aprilcam-bridge@.service` units (already inactive per the sprint's
  starting state — confirm, don't assume) and remove or mask the unit
  files once v2 is verified working, so a stray `systemctl start
  aprilcamd` cannot resurrect the `jtl`-owned v1 install. Leave the
  `/home/jtl/aprilcam` checkout on disk (not this ticket's job to delete
  another user's files) — just confirm no process under `jtl` survives
  verification.
- At the end of this ticket, restore ROS to its original
  enabled/active state (both the service and the timer) **unless the
  stakeholder has explicitly decided otherwise** (sprint.md Architecture,
  Open Question 1) — if no such decision has been communicated, restore
  is the default and must actually happen, not just be mentioned as an
  intention.

## Acceptance Criteria

- [ ] Pre-change state of `ros-cameras.service`, `camera-watchdog.timer`,
      and the v1 `aprilcamd`/`aprilcam-bridge@` units recorded in this
      ticket file (`systemctl is-enabled`/`is-active` output, verbatim).
- [ ] ROS camera services stopped; AprilCam v2 units installed
      (`deploy/aprilcamd.service`, `deploy/aprilcam-bridge@70.service`,
      `deploy/aprilcam-bridge@71.service` or equivalent instance names)
      via the ticket-001 provision script + runbook, using a wheel built
      on the Mac and pinned (`aprilcam[daemon]==<version>`).
- [ ] `aprilcam camera list -d vidar.local` (run from the Mac) shows
      camera i2c@80000 (`/dev/video71`) `present`/`usable` end to end,
      including a real captured frame (see next criterion). This is the
      required, must-pass criterion for this ticket.
- [ ] Camera i2c@88000 (`/dev/video70`) is reported honestly rather than
      required to work: it is enumerated by `camera list`
      (`present`), and the daemon reports it `unusable`/failing cleanly
      (no crash, no effect on camera i2c@80000's operation) given its
      known physical fault (ticket 003 Finding 6). Quote the actual
      `camera list`/`camera probe` output for it in this ticket. If the
      stakeholder has reseated the CSI cable before this ticket runs,
      verify it end to end instead and note that the hardware issue was
      resolved — do not assume the fault is unfixable.
- [ ] `aprilcam camera image -d vidar.local` returns a real frame for
      camera i2c@80000 (saved/inspected, not just "no error"). Attempted
      for camera i2c@88000 too, with the actual failure behavior
      (error message, timeout, etc.) recorded rather than skipped.
- [ ] Tag detection attempted per sprint.md SUC-006, against camera
      i2c@80000 (the working camera): if a playfield/tags are in view,
      real detections recorded (tag id, pixel position, yaw quoted in
      this ticket); if not, this ticket says so explicitly rather than
      omitting the criterion.
- [ ] `aprilcam daemon list` (or mDNS browsing) from the Mac discovers
      vidar's daemon by name with no `-d` flag.
- [ ] The `-d vidar.local` form used throughout this ticket's criteria
      depends on ticket 007's fix landing first (this ticket's
      `depends-on` includes 007) — this ticket does not work around the
      hostname-connect timeout by falling back to `-d <ip>:5280` for its
      recorded verification. If 007 is not yet done when this ticket
      would otherwise start, wait for it rather than substituting the IP
      form.
- [ ] `journalctl -u aprilcamd` on vidar shows a clean start: `Directories`
      resolution log line (`source=default` for config/state/socket, since
      systemd provisions them accessible to the `aprilcam` user already),
      TCP 5280 bind, mDNS advertising starting — no traceback quoted or
      paraphrased in this ticket for the record.
- [ ] `systemctl reboot` executed; after reboot, `aprilcamd` and both
      bridge units are `active (running)` with no manual intervention, and
      `aprilcam daemon list`/`camera list -d vidar.local` succeed again
      from the Mac.
- [ ] v1 units stopped, disabled, and their unit files removed or masked;
      confirmed via `systemctl list-unit-files | grep aprilcam` that only
      v2 unit names are present/enabled.
- [ ] ROS services restored to their pre-change enabled/active state (or
      an explicit stakeholder decision to leave them stopped is quoted in
      this ticket) — confirmed with a final `systemctl is-enabled`/
      `is-active` check matching the "before" recording.
- [ ] `scripts/test-gate.sh` passes on the Mac checkout (no source changes
      are expected from this ticket, but confirms nothing broke).

## Implementation Plan

**Approach**: SSH to `ros@vidar.local`. Follow `deploy/README.md` (ticket
001) in order. Build the wheel on the Mac first (`python -m build --wheel`
or `uv build --wheel`), bump-version per this repo's own cadence only if
this is happening at sprint close (per `.claude/rules/git-commits.md` —
this ticket itself does not bump; that's `close_sprint`'s job), `scp` the
wheel to vidar, run `provision-pi.sh`, install via pipx, install/enable
the module config → bridge units → daemon unit in that documented order,
then work through every Acceptance Criteria item in sequence, recording
results as they're obtained rather than after the fact from memory.

**Files to modify**: none in `src/aprilcam/` expected — this is an
operational ticket. If live verification reveals a defect that tickets
001-003 didn't catch, fix it here directly (small, targeted) and note it;
do not silently work around a real bug just to make the checklist pass.

**Testing plan**: no new pytest tests from this ticket specifically (its
verification is inherently live/manual); run `scripts/test-gate.sh` once
at the end to confirm the Mac checkout is still clean.

**Documentation updates**: none directly — ticket 005 consumes this
ticket's recorded findings (exact `journalctl` behavior, the ROS-stop
procedure, any real quirk hit during install) to write the knowledge doc
and wiki update. Keep this ticket's own notes concrete and quotable so
ticket 005 doesn't have to re-derive them.
