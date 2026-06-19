---
id: '014-010'
title: Live bring-up on vali.local (HARDWARE/NETWORK REQUIRED)
status: open
use-cases:
  - SUC-006
depends-on:
  - 014-009
---

# 014-010: Live bring-up on vali.local (HARDWARE/NETWORK REQUIRED)

## Description

Deploy the daemon to `vali.local` (aarch64, Ubuntu 24.04.4, Python 3.12.3,
avahi active, `eric` not yet in `video`) and verify the full golden path from
the Mac. This ticket requires network access to `vali.local` and a physical
OV9782 camera plugged into the Pi.

**This ticket may be deferred** if the camera is not yet attached or
connectivity issues arise. All code and test tickets (001–009) can complete
independently.

## Acceptance Criteria

- [ ] OV9782 camera is plugged in to `vali.local` and appears as `/dev/video0`.
- [ ] `eric` added to `video` group (provision script ran or manual
      `sudo usermod -aG video eric`).
- [ ] Wheel built on Mac (`uv build --wheel`), scp'd to `~/wheels/` on `vali`.
- [ ] `pipx install "aprilcam[daemon]==<ver>" --find-links ~/wheels` succeeds on `vali`.
- [ ] `mss` headless check: `ssh eric@vali.local python -c "import aprilcam.daemon"` exits 0.
- [ ] opencv-contrib confirmed importable: `python -c "import cv2; print(cv2.__version__)"`.
- [ ] `deploy/aprilcamd.service` copied to `/etc/systemd/system/` on `vali`.
- [ ] `systemctl enable --now aprilcamd` on `vali` → daemon starts.
- [ ] `journalctl -u aprilcamd -f` shows daemon bound on TCP 5280 and mDNS registered.
- [ ] From Mac: `aprilcam cameras` (no env vars set) auto-discovers `vali.local`
      and lists the OV9782 camera.
- [ ] From Mac: `aprilcam tags` returns tag data from the Pi camera.
- [ ] From Mac: MCP golden path via `aprilcam mcp`:
  - `open_camera` → returns camera handle on `vali.local`.
  - `create_playfield` → playfield created.
  - `get_tags` → tags returned.
  - `capture_frame` → image returned as base64.
  - `calibrate_playfield` → calibration written on `vali` via `SetCalibration` RPC.
- [ ] After `systemctl reboot` on `vali`, daemon auto-restarts and reconnects.
- [ ] OV9782 manual exposure set (`APRILCAM_UNDISTORT=1` or config, per
      `project_ov9782_exposure_tuning` memory note).

## Implementation Plan

### Step-by-step runbook

Follow `deploy/README.md` exactly. Document any deviations as issues in the
runbook for follow-up.

1. Plug in OV9782 to `vali.local`.
2. SSH to `vali`: `ssh eric@vali.local`.
3. Run `deploy/provision-pi.sh eric@vali.local` from the Mac (or manually
   follow the provision steps).
4. Log out and back in to `vali` for the `video` group to take effect.
5. On Mac: `uv build --wheel` in repo root.
6. `scp dist/aprilcam-*.whl eric@vali.local:~/wheels/`.
7. On `vali`: `pipx install "aprilcam[daemon]==<ver>" --find-links ~/wheels`.
8. Verify imports (mss, cv2).
9. `scp deploy/aprilcamd.service eric@vali.local:/tmp/`
   then on `vali`: `sudo cp /tmp/aprilcamd.service /etc/systemd/system/`
   and `sudo systemctl daemon-reload && sudo systemctl enable --now aprilcamd`.
10. Verify from Mac. Run each acceptance criterion check.

### Documentation Updates

- Update `deploy/README.md` with any corrections found during bring-up.
- If opencv-contrib pip wheel not found: document the piwheels fallback.
- If `mss` import breaks headless: document the fix (lazy import or extra).
