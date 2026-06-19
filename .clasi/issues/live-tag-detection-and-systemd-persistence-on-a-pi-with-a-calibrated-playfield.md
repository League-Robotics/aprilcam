---
status: pending
---

# Live AprilTag detection + systemd persistence verification on a Pi

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
