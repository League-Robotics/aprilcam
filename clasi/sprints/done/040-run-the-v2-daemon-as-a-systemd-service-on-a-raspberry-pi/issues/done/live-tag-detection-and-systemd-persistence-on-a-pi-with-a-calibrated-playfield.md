---
status: done
sprint: '040'
tickets:
- 040-003
- 040-004
- 040-005
---

# Live AprilTag detection + systemd persistence verification on a Pi

## Resolution (sprint 040 ticket 005)

**Already covered before this sprint:** the TCP+mDNS discovery this issue's
own "Follow-up from sprint 014" note describes was verified again
incidentally by this sprint's live bring-up (ticket 004), but was not new
work — sprint 014 already proved it against a daemon started by hand. The
`EnumerateCameras`/file-proxy-RPC-shaped functionality this issue implicitly
assumes (calibration/config read via gRPC, not local disk) was also already
in place going into this sprint.

**Closed by this sprint (040):**

- **systemd persistence — fully verified.** Ticket 004 installed
  `deploy/aprilcamd.service` and both `aprilcam-bridge@.service` instances
  on `vidar.local`, confirmed a clean `journalctl -u aprilcamd` start (TCP
  bind via `ss`, mDNS advertising, no traceback), and confirmed auto-restart
  and mDNS rediscovery from the Mac across two real `systemctl reboot`
  cycles (the first reboot surfaced and the second confirmed the fix for a
  real `BindsTo=` defect in the ROS-disable sequence — see ticket 004
  Findings item 9).
- **Live tag detection — attempted, honestly reported as not verified, per
  this sprint's own SUC-006 design (not a gap this sprint failed to
  close).** Ticket 004 ran `aprilcam camera tags -d vidar.local` against
  the working camera (i2c@80000); the command succeeded cleanly with `(no
  tags detected)` because the camera's field of view was a workshop scene
  with no physical AprilTag/ArUco playfield in view — not a code or
  detection defect. Sprint 040 scoped this explicitly as best-effort
  ("if a playfield/tags are in view ... real detections; if not, the
  ticket says so plainly rather than claiming untested success" —
  sprint.md Success Criteria and SUC-006), so this outcome satisfies the
  sprint's own acceptance bar. Live tag detection against an actually
  staged playfield on vidar remains genuinely unverified and would need a
  physical playfield placed in view of one of vidar's cameras — a
  follow-up if/when that setup exists, not a defect in anything sprint 040
  shipped.
- **OV9782 manual exposure tuning** — not applicable to this sprint's
  hardware (vidar's cameras are IMX296 global-shutter modules, not the
  OV9782 this issue's deferred-verification note names); moot.

**Not addressed by this sprint, still open:** multi-daemon disambiguation
verified live (a second daemon on `vali.local`, `APRILCAM_DAEMON_HOST`
selecting between two live Pis) — sprint 040's Scope explicitly excludes
`vali.local` or any Pi other than `vidar.local`. The underlying
disambiguation *logic* (`>1` discovered → require `APRILCAM_DAEMON_HOST`)
is implemented and offline-tested in `client/discovery.py`, but a live
two-Pi verification was never in this sprint's scope and remains open for
a future sprint if that scenario needs live proof.

Follow-up from sprint 014, ticket 014-010. The remote-daemon feature was
brought up live on `vidar.local` (Pi 5) and verified end-to-end over TCP +
mDNS discovery from the Mac (EnumerateCameras, file-proxy RPCs, auto-discovery),
and four real defects found during that bring-up were fixed. The remaining
acceptance criteria require physical setup that was not available during the
sprint and are deferred here.

## Deferred verification (needs hardware/setup)

- **Live tag detection**: place an OV9782 (or equivalent global-shutter camera)
  on a host with a physical AprilTag/ArUco playfield in view, calibrate it, and
  confirm from the Mac:
  - `aprilcam tags` returns real tag detections from the Pi camera.
  - MCP golden path: `open_camera` → `create_playfield` → `get_tags` (world_xy
    populated) → `capture_frame` (base64 image) → `calibrate_playfield` (writes
    `calibration.json` on the Pi via the `SetCalibration` RPC).
  - OV9782 manual low-exposure tuning per `project_ov9782_exposure_tuning`.
- **systemd persistence**: copy `deploy/aprilcamd.service` to
  `/etc/systemd/system/`, `systemctl enable --now aprilcamd`, confirm
  `journalctl -u aprilcamd` shows TCP 5280 + mDNS, then `systemctl reboot` and
  confirm the daemon auto-restarts and is rediscovered from the Mac.
- **Multi-daemon disambiguation (live)**: run a second daemon (e.g. on
  `vali.local`) and confirm `>1` discovered → `APRILCAM_DAEMON_HOST` selects one,
  and the MCP `connect_daemon` tool switches between two live Pis.

## Notes / environment at sprint time

- `vidar.local` = user `jtl`, passwordless sudo, CSI camera (`/dev/video0-9`),
  used for the live bring-up. `vali.local` = user `eric`, no passwordless sudo,
  no USB camera attached.
- The daemon was started via `setsid python -m aprilcam.daemon` (not systemd) to
  avoid disrupting the lab machine; systemd install/reboot deferred deliberately.
