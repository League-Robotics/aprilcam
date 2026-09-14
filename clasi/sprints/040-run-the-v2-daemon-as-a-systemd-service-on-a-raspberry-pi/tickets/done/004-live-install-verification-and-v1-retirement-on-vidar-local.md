---
id: '004'
title: Live install, verification, and v1 retirement on vidar.local
status: done
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

- [x] Pre-change state of `ros-cameras.service`, `camera-watchdog.timer`,
      and the v1 `aprilcamd`/`aprilcam-bridge@` units recorded in this
      ticket file (`systemctl is-enabled`/`is-active` output, verbatim).
      **The team-lead recorded the true pre-sprint state (all four ROS
      units `enabled`/`active`) at 02:05 UTC after the earlier
      SIGSEGV-induced power cycle; this ticket's own session started
      after that snapshot, with ROS already stopped by prior tickets.**
      See "Pre-change state" below for both recordings.
- [x] ROS camera services stopped; AprilCam v2 units installed
      (`deploy/aprilcamd.service`, `deploy/aprilcam-bridge@0.service`,
      `deploy/aprilcam-bridge@1.service` — the actual instance names;
      see Findings item 2 for a naming inconsistency fixed along the
      way) via the ticket-001 provision script + runbook, using a wheel
      built on the Mac and pinned (`aprilcam[daemon]==0.20260906.1`).
- [x] `aprilcam camera list -d vidar.local` (run from the Mac) shows
      camera i2c@80000 (`/dev/video71`) `present`/`usable` end to end,
      including a real captured frame (see next criterion). **Confirmed.**
- [x] Camera i2c@88000 (`/dev/video70`) is reported honestly rather than
      required to work: it is enumerated by `camera list`
      (`present`), and the daemon reports it `unusable`/failing cleanly
      (no crash, no effect on camera i2c@80000's operation) given its
      known physical fault (ticket 003 Finding 6). **Confirmed — no
      CSI cable reseat was reported before this ticket ran, and the
      fault reproduced exactly as ticket 003 described.** See Findings
      item 4 for the live `camera probe` output and journal evidence.
- [x] `aprilcam camera image -d vidar.local` returns a real frame for
      camera i2c@80000 (saved/inspected, not just "no error"). Attempted
      for camera i2c@88000 too, with the actual failure behavior
      (error message, timeout, etc.) recorded rather than skipped.
      **Confirmed** — see Findings item 5.
- [x] Tag detection attempted per sprint.md SUC-006, against camera
      i2c@80000 (the working camera): if a playfield/tags are in view,
      real detections recorded (tag id, pixel position, yaw quoted in
      this ticket); if not, this ticket says so explicitly rather than
      omitting the criterion. **Not verified: no playfield/tags in view**
      — see Findings item 6.
- [x] `aprilcam daemon list` (or mDNS browsing) from the Mac discovers
      vidar's daemon by name with no `-d` flag. **Partially — the
      daemon is discovered via mDNS with no `-d` flag, but is displayed
      as `192.168.1.144:5280`, not `vidar.local`; this is the exact,
      already-documented characteristic ticket 007 recorded as
      unaffected/out of scope (its Findings: "still reported by ip:port,
      not hostname, exactly as before this ticket"), not a new defect
      this ticket introduced or missed.** See Findings item 7.
- [x] The `-d vidar.local` form used throughout this ticket's criteria
      depends on ticket 007's fix landing first (this ticket's
      `depends-on` includes 007) — this ticket does not work around the
      hostname-connect timeout by falling back to `-d <ip>:5280` for its
      recorded verification. **007 was already `done` before this ticket
      started; every `-d vidar.local` command below used the hostname
      form directly, no IP fallback.**
- [x] `journalctl -u aprilcamd` on vidar shows a clean start: `Directories`
      resolution log line (`source=default` for config/state/socket, since
      systemd provisions them accessible to the `aprilcam` user already),
      TCP 5280 bind, mDNS advertising starting — no traceback quoted or
      paraphrased in this ticket for the record. **Confirmed** — see
      Findings item 8 for the exact quoted log lines.
- [x] `systemctl reboot` executed; after reboot, `aprilcamd` and both
      bridge units are `active (running)` with no manual intervention, and
      `aprilcam daemon list`/`camera list -d vidar.local` succeed again
      from the Mac. **Confirmed, across two reboots** — see Findings item
      9 (which also documents a real ROS-side defect the first reboot
      surfaced and the corrected procedure that fixed it for the second).
- [x] v1 units stopped, disabled, and their unit files removed or masked;
      confirmed via `systemctl list-unit-files | grep aprilcam` that only
      v2 unit names are present/enabled. **Confirmed** — see Findings
      item 10.
- [x] ROS services restored to their pre-change enabled/active state (or
      an explicit stakeholder decision to leave them stopped is quoted in
      this ticket) — confirmed with a final `systemctl is-enabled`/
      `is-active` check matching the "before" recording. **Confirmed, all
      four ROS units `enabled`/`active`** — see "End state" below.
- [x] `scripts/test-gate.sh` passes on the Mac checkout (no source changes
      are expected from this ticket, but confirms nothing broke).
      **A real mypy defect was found and fixed** (Findings item 11) —
      the gate now passes: `2210 passed, 48 skipped, 107 deselected`.

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

## Pre-change state

**True pre-sprint state (captured by the team-lead at 02:05 UTC, after
the earlier SIGSEGV-induced power cycle, before any sprint-040 ticket
touched vidar)**: all four ROS units `enabled`+`active` — this is the
"before" state this ticket restores at the end.

**State at the start of this ticket's own session** (`ssh ros@vidar.local`,
`uptime` showed 42 minutes since the same power cycle — prior tickets
003/006/007 had already stopped ROS and left it that way for this ticket
to continue from, per their own "State left on vidar.local" notes):

```
ros-cameras.service            enabled=enabled active=inactive
camera-watchdog.timer          enabled=enabled active=inactive
camera-color-relay.service     enabled=enabled active=failed
web-video-server.service       enabled=enabled active=inactive

aprilcamd.service               enabled=disabled active=inactive
aprilcam-bridge@0.service       enabled=disabled active=inactive
aprilcam-bridge@1.service       enabled=disabled active=inactive
```

v1 unit files on disk, confirmed still present and disabled:
`/etc/systemd/system/aprilcamd.service` (`User=jtl`,
`ExecStart=/home/jtl/aprilcam/.venv/bin/python -m aprilcam.daemon`),
`/etc/systemd/system/aprilcam-bridge@.service` (`User=jtl`). No `jtl`-owned
AprilCam/libcamera/gst process was running (only `jtl`'s ordinary desktop
session — `pipewire`, `wireplumber`, `systemd --user`, unrelated to
AprilCam and not touched by this ticket).

## Findings (live session on vidar.local)

1. **`aprilcamd.service`'s `After=` line referenced the wrong bridge
   instance names — fixed.** `deploy/aprilcamd.service` had
   `After=network-online.target aprilcam-bridge@70.service
   aprilcam-bridge@71.service`, but the actual bridge instances
   (per `deploy/aprilcam-bridge@.service`'s own install comment and
   `deploy/README.md` Step 4) are `aprilcam-bridge@0`/`@1` (the 0-based
   camera *position*, not the video device number — `%i` in the unit
   expands to the position, and the script derives `/dev/video7%i`
   itself). Referencing non-existent unit names in `After=` is not a
   hard failure (it's just an ordering directive `systemd` silently
   no-ops for units that don't exist), but it meant the daemon unit's
   documented "start after the bridges" intent never took effect.
   Fixed to `aprilcam-bridge@0.service aprilcam-bridge@1.service`.
   No test previously asserted the `After=` line's content
   (`tests/test_deploy_assets.py` only checks `User=`/directory/env
   directives), so nothing was masking this — it was simply never
   exercised until this ticket installed the units for real.

2. **`provision-pi.sh`'s v4l2loopback idempotence check was silently
   broken by a `pipefail`/`SIGPIPE` interaction — fixed.** Running
   `provision-pi.sh` a second time (module config unchanged, module
   already loaded) still printed `reloading v4l2loopback (config changed
   or module not loaded)` and reloaded the module — exactly the behavior
   the script's own comment says must never happen ("a running bridge
   would lose its device out from under it"). Root cause, reproduced
   directly on vidar: `lsmod | grep -q '^v4l2loopback'` under
   `set -o pipefail` returns exit **141** (`lsmod` receives `SIGPIPE`
   when `grep -q` closes its stdin the instant it finds a match, and
   with `pipefail` that 141 becomes the whole pipeline's exit status,
   not `grep`'s logical 0/found result); the script's `!` negation of
   that 141 evaluates to true ("not loaded"), so the reload branch fired
   on every run regardless of actual state. Confirmed directly:
   ```
   set -euo pipefail
   lsmod | grep -q '^v4l2loopback'; echo grep_exit=$?
   ```
   exited the whole `bash -c` invocation with code 141 before the `echo`
   ever ran. **Fix**: replaced the `lsmod | grep -q` pipe with a direct
   `[ ! -d /sys/module/v4l2loopback ]` sysfs check — no pipe, no
   `lsmod` output parsing, no `SIGPIPE` exposure. Re-verified live on
   vidar: three consecutive `provision-pi.sh` runs after the fix printed
   `v4l2loopback already loaded with unchanged config -- not touching it`
   every time module state was actually unchanged, and the module was
   never reloaded out from under the already-running bridge units.
   This directly matters for SUC-003's acceptance criteria ("running
   provision-pi.sh twice ... produces no errors and no duplicate
   users/groups/files") and for operational safety (re-running
   provisioning against a live Pi must never risk yanking a camera
   device out from under a running bridge, which this bug did on every
   single re-run before the fix).

3. **Provisioning and pipx install — clean, no surprises.**
   `./deploy/provision-pi.sh ros@vidar.local` (after the fix above):
   apt packages already present, `libcamera already working ('cam -l'
   lists cameras)`, created the `aprilcam` system user, added `aprilcam`
   and `ros` to the `video` group, confirmed
   `/etc/modprobe.d/aprilcam.conf`/`/etc/modules-load.d/aprilcam.conf`
   already matched (v1's identical config, exactly as sprint.md
   predicted — the script was a no-op for those two files on every run).
   `uv build --wheel` produced `dist/aprilcam-0.20260906.1-py3-none-any.whl`;
   `sudo -u aprilcam -H pipx install
   "/tmp/aprilcam-0.20260906.1-py3-none-any.whl[daemon]"` succeeded
   cleanly (`installed package aprilcam 0.20260906.1, installed using
   Python 3.12.3`). Headless smoke test
   (`sudo -u aprilcam -H env -u DISPLAY
   /var/lib/aprilcam/.local/share/pipx/venvs/aprilcam/bin/python -c
   "import aprilcam.daemon"`) printed `import OK`. The installed venv
   python (`/var/lib/aprilcam/.local/share/pipx/venvs/aprilcam/bin/python`,
   a symlink to `python3`, confirmed via `stat` as `aprilcam`) matches
   `aprilcamd.service`'s `ExecStart=` exactly, byte for byte.

4. **Both bridge units installed and behave exactly as tickets
   003/006 predicted.** `aprilcam-bridge@0` (`/dev/video70`, camera
   i2c@88000, the known-faulty camera) and `aprilcam-bridge@1`
   (`/dev/video71`, camera i2c@80000, working) both reached
   `active` immediately after `enable --now`. `/dev/video71`
   negotiated `1280x720 'YUYV'` per `v4l2-ctl --get-fmt-video`;
   `/dev/video70` reported `VIDIOC_G_FMT: failed: Invalid argument`
   (no negotiated format — no bridge ever stays up long enough to
   negotiate one). `aprilcam-bridge@0`'s journal showed the exact
   ticket-006 watchdog behavior working live: `Camera frontend has
   timed out! Please check that your camera sensor connector is
   attached securely.` → `GstLibcameraSrcState::requestCompleted...
   assertion ... failed` → `Caught SIGSEGV` → `aprilcam-camera-bridge:
   /dev/video70's libcamerasrc crashed (SIGSEGV) -- restarting` →
   clean `Restart=always` relaunch, repeating on an roughly-10s cycle
   (`NRestarts=10` after a few minutes) with **zero effect** on
   `aprilcam-bridge@1` (`NRestarts=0` the whole session). Live
   `camera probe -d vidar.local` (forced, non-cached, from the Mac):
   ```
     [1]    imx296                           present  unusable  ...
     [2]    imx296-2                         present  usable    ...
   ```
   `camera list` (cached) showed both `usable` immediately after
   install, exactly matching ticket 003 Finding 8's "list is a cached
   value, use probe for a live read" note — not a new bug.

5. **Frame capture — confirmed for i2c@80000, cleanly failed for
   i2c@88000.** `aprilcam camera image -d vidar.local imx296-2 -o
   testdev/vidar-040-004/cam2_imx296-2.jpg` → `wrote 230750 bytes ...
   (jpeg)`; the saved frame is a real, correctly-colored workshop photo
   (no channel swap — natural skin tones, blue shirt renders blue).
   `aprilcam camera image -d vidar.local imx296 ...` (the faulty camera)
   → `error: daemon at 'vidar.local' did not answer: no frame available
   from camera 'imx296'` — a clean, one-line client error, **no crash**.
   The daemon's own journal did log a full Python traceback for that
   same failed `GetFrame` RPC (`ERROR aprilcam.daemon.rpc: unhandled
   error serving RPC` → `RuntimeError: no frame available from camera
   'imx296'`, from `daemon/service.py:863` via `daemon/rpc.py:905`) —
   the daemon did not crash and camera 2 was unaffected, so this meets
   the acceptance criterion's actual bar ("no crash, no effect on the
   working camera"), but logging a full traceback for an *expected*
   condition (a known-faulty camera being asked for a frame) is log
   noise that would flood journald on a Pi with a permanently broken
   camera. Out of this ticket's file scope (`daemon/rpc.py`/
   `daemon/service.py`, not `deploy/` or the three backend modules) —
   flagged via a spawned follow-up task rather than fixed here.

6. **Tag detection: not verified, no playfield in view (SUC-006,
   honestly reported).** `aprilcam camera tags -d vidar.local imx296-2`
   → `(no tags detected)` — ran cleanly, no error, but the captured
   frame (a workshop scene, see item 5) has no AprilTag/ArUco markers in
   view. Per SUC-006's Main Flow branch 2, this is recorded as "not
   verified: no playfield/tags in view," not inferred as a failure or
   silently omitted.

7. **`aprilcam daemon list` mDNS browse still reports `ip:port`, not
   hostname — matches ticket 007's own documented, accepted behavior.**
   ```
   localhost (local):
     cameras: 11
     playfields: main-playfield, secondary-playfield
   192.168.1.144:5280:
     cameras: 2
     playfields: (none)
   ```
   The daemon *is* discovered with no `-d` flag (the point of SUC-007);
   only the display string is an IP rather than `vidar.local`. Ticket
   007's own Findings recorded this exact behavior as "unchanged ...
   still reported by ip:port, not hostname, exactly as before this
   ticket" and explicitly scoped the mDNS-browse display format as
   unaffected/out of scope for its fix. This ticket does not introduce a
   new fix for it, per this ticket's "no source changes expected beyond
   real defects tickets 001-003 [and 006-007] didn't catch" scope —
   007 already caught and consciously accepted this.

8. **`journalctl -u aprilcamd` clean-start evidence, quoted verbatim**
   (first install, before the reboot check):
   ```
   aprilcam.config.directories: config_dir resolved to /etc/aprilcam (source=default)
   aprilcam.config.directories: state_dir resolved to /var/lib/aprilcam (source=default)
   aprilcam.config.directories: socket_dir resolved to /run/aprilcam (source=default)
   __main__: aprilcamd logging to /var/lib/aprilcam/aprilcamd.log
   aprilcam.camera.discovery: probe: imx296 opened but delivered no frame within 1.0s -- reporting unusable
   aprilcam.daemon.service: control watchdog started (every 15s)
   aprilcam.daemon.mdns: mdns: advertising aprilcam-vidar._aprilcam._tcp.local. on port 5280
   aprilcam.daemon.lifecycle: daemon lifecycle started: lock=/run/aprilcam/daemon.lock socket=/run/aprilcam/control.sock
   __main__: aprilcamd ready (pid=7784): socket=/run/aprilcam/control.sock
   ```
   No traceback in this window. **Note for ticket 005**: v2's daemon
   does not log an explicit "TCP bind" INFO line (unlike mDNS, which
   does log "advertising..."); the TCP bind was confirmed instead via
   `sudo ss -tlnp | grep 5280` → `LISTEN 0 4096 *:5280 *:*
   users:(("python",pid=7784,fd=10))`, and independently by every
   `-d vidar.local` command from the Mac succeeding. Not a defect —
   just worth the knowledge doc naming the right verification command
   since the log line the AC describes doesn't literally exist.

9. **Reboot persistence — confirmed on the second of two reboots; the
   first surfaced a real ROS-side defect in the planned disable
   sequence.** Before the first reboot, only `ros-cameras.service` and
   `camera-watchdog.timer` were disabled (per sprint.md's literal
   instruction), leaving `camera-color-relay.service` and
   `web-video-server.service` **enabled**. Both of the latter carry
   `PartOf=ros-cameras.service` **and** `BindsTo=ros-cameras.service`;
   `BindsTo=` is a start-time requirement dependency in both directions
   (not just the stop-propagation direction sprint.md's reasoning
   focused on) — so systemd starting either enabled dependent unit at
   boot pulled `ros-cameras.service` back up even though it was
   individually disabled. Live evidence: after the first
   `systemctl reboot`, `ros-cameras.service` showed `active` despite
   `disabled`; its journal showed `camera_node-2` (the i2c@80000 node —
   the *same* sensor AprilCam's already-running `aprilcam-bridge@1`
   held) fail with `Pipeline handler in use by another process` /
   `terminate called after throwing an instance of 'std::runtime_error'`,
   then `ros-cameras` crash-looped (`Restart=always`) printing repeated
   `request '...' cancelled` errors. **AprilCam's own units were
   unaffected** (`aprilcam-bridge@1`/`aprilcamd` both `NRestarts=0`,
   `/dev/video71` format unchanged, `camera probe -d vidar.local` still
   showed `imx296-2` `usable` throughout) — the race hurt ROS, not
   AprilCam. This was stopped immediately (`sudo systemctl stop
   ros-cameras.service`), then **all four** ROS units
   (`ros-cameras.service`, `camera-watchdog.timer`,
   `camera-color-relay.service`, `web-video-server.service`) were
   disabled — the corrected, complete sequence — and a **second**
   `systemctl reboot` was run to confirm the fix. Result: all three
   AprilCam units (`aprilcamd`, `aprilcam-bridge@0`, `aprilcam-bridge@1`)
   came up `active (running)` with no manual intervention in both
   reboots (reboot 1: `sudo systemctl reboot` at 02:52:43 UTC → SSH
   reachable again by 02:53:30 UTC, ~47s; reboot 2: 02:55:45 UTC →
   02:56:31 UTC, ~46s); `ros-cameras.service` correctly stayed `inactive`
   in reboot 2 with all four ROS units disabled; `aprilcam daemon list`
   and `aprilcam camera list -d vidar.local` both succeeded from the Mac
   after reboot 2 with the daemon journal showing the same clean
   `Directories`/mDNS-advertising start as item 8, no traceback.
   **Corrected switch-over/reboot procedure for ticket 005**: disabling
   `ros-cameras.service` and `camera-watchdog.timer` alone is
   insufficient to keep ROS down across a reboot — `camera-color-relay.
   service` and `web-video-server.service` must also be disabled (not
   just stopped), because their `BindsTo=` pulls `ros-cameras.service`
   back up at boot otherwise. No Ansible-managed unit file was edited —
   only `systemctl enable|disable` state was touched, per this ticket's
   constraint. Separately confirmed `camera-watchdog.sh`'s own skip
   logic directly from its source
   (`STATE="$(systemctl is-active ros-cameras ...)"`; `[ "$STATE" !=
   "active" ]` → `logger ... "skipping probe"; exit 0`) — sprint.md's
   reasoning that disabling the *timer* (not patching the service) is
   sufficient once `ros-cameras` is actually down was correct; the gap
   was only in *keeping* `ros-cameras` down across a reboot, not in the
   watchdog's own behavior.

10. **v1 retirement — confirmed via `systemctl list-unit-files`,
    `systemctl cat`, and process ownership.**
    ```
    aprilcam-bridge@.service                       indirect        enabled
    aprilcamd.service                              enabled         enabled
    ```
    Only the two v2 unit names exist; both now hold v2's content
    (`systemctl cat` confirmed `User=aprilcam` and the pipx `ExecStart=`
    path on both, not `jtl`/`/home/jtl/aprilcam/.venv`). `/usr/local/bin/
    aprilcam-camera-bridge` (the same path v1 used) now holds v2's
    script byte-for-byte (`md5sum` matches `deploy/aprilcam-camera-bridge`
    exactly: `241ecf2c907dfc1d143d22b420899f30`). `ps -u jtl` showed only
    `jtl`'s ordinary desktop session (`pipewire`, `wireplumber`,
    `systemd --user`, etc.) — no AprilCam/libcamera/gst-launch process
    under `jtl`, before or after this ticket's work. `/home/jtl/aprilcam`
    left in place on disk (permission-denied to read as `ros`, confirming
    it's untouched, not deleted), per the sprint's explicit instruction
    not to delete another user's files.

11. **`scripts/test-gate.sh` failure found and fixed: a real mypy type
    error in ticket 007's `client/discovery.py` change.**
    `resolve_ipv4_preferred`'s `return family, sockaddr[0]` failed mypy
    (`Incompatible return value type (got "tuple[AddressFamily, str |
    int]", expected "tuple[int, str] | None")`) — `getaddrinfo`'s
    typeshed return type is a union of the `AF_INET`/`AF_INET6` sockaddr
    tuple shapes, which widens `sockaddr[0]`'s inferred type past plain
    `str` even though the first element is always the address string for
    both families. This was a pre-existing gap in ticket 007 (already
    `done`/merged to this branch before this ticket started), not
    something this ticket's own live-verification work introduced — but
    per this ticket's own Acceptance Criteria and `.claude/rules/
    source-code.md`'s test-gate requirement, it blocked `scripts/
    test-gate.sh` and was fixed here: `return int(family),
    str(sockaddr[0])`, an explicit-cast fix with no behavior change
    (both values were always plain `int`/`str` at runtime). Re-ran
    `uv run mypy src/aprilcam/client/discovery.py` → `Success: no issues
    found`; the scoped `tests/aprilcam/client/test_discovery.py -m "not
    camera and not integration"` → `33 passed, 1 deselected`; the full
    `scripts/test-gate.sh` → `2210 passed, 48 skipped, 107 deselected`.

## End state on vidar.local

Switch-over commands executed, in order, ready for ticket 005's runbook:

```bash
# Stop and disable the v2 AprilCam units
sudo systemctl disable --now aprilcamd aprilcam-bridge@0 aprilcam-bridge@1
sudo systemctl reset-failed aprilcam-bridge@0 aprilcam-bridge@1   # Restart=always units report "failed" on a manual stop signal; cosmetic only

# Restore ROS to its original enabled+active state (all four units)
sudo systemctl enable --now ros-cameras.service camera-watchdog.timer \
    camera-color-relay.service web-video-server.service
```

To switch back to AprilCam later, the reverse (per this ticket's own
"corrected sequence" finding — disable **all four** ROS units, not just
two):

```bash
sudo systemctl disable --now ros-cameras.service camera-watchdog.timer \
    camera-color-relay.service web-video-server.service
sudo systemctl enable --now aprilcam-bridge@0 aprilcam-bridge@1 aprilcamd
```

Final confirmed state (`systemctl is-enabled`/`is-active`, verbatim):

```
ros-cameras.service            enabled=enabled    active=active
camera-watchdog.timer          enabled=enabled    active=active
camera-color-relay.service     enabled=enabled    active=active
web-video-server.service       enabled=enabled    active=active
aprilcamd.service               enabled=disabled   active=inactive
aprilcam-bridge@0.service       enabled=disabled   active=inactive
aprilcam-bridge@1.service       enabled=disabled   active=inactive
```

— matching the true pre-sprint "before" state exactly (all four ROS
units `enabled`+`active`). `ps -u jtl` and a repo-wide process scan
confirmed no AprilCam/libcamera/gst-launch process remained under any
user. The pipx install (`aprilcam 0.20260906.1`) and the v2 unit files
were left in place (stopped/disabled, not removed), so the switch back
to AprilCam is exactly the two-command sequence above with no
reinstall needed. `/tmp` on vidar was cleaned of the wheel and the
scp'd unit/script copies used during this session.

## Files changed by this ticket (small, targeted fixes — not a rewrite)

- `deploy/aprilcamd.service` — `After=` line now references the actual
  bridge instance names (`@0`/`@1`), not `@70`/`@71` (Findings item 1).
- `deploy/provision-pi.sh` — v4l2loopback-loaded check replaced with a
  direct `/sys/module` test to fix the `pipefail`/`SIGPIPE` idempotence
  bug (Findings item 2).
- `src/aprilcam/client/discovery.py` — explicit `int()`/`str()` casts in
  `resolve_ipv4_preferred`'s return to satisfy mypy (Findings item 11).

No changes to `camera/backends/*.py` or `camera/identity.py` — ticket
003 already confirmed those work correctly against real hardware, and
this ticket's live session did not contradict that.
