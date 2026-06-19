---
id: 014-010
title: Live bring-up on vali.local (HARDWARE/NETWORK REQUIRED)
status: done
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

**Executed on vidar.local** (Pi 5, user `jtl`, passwordless sudo, CSI camera)
rather than vali.local (no sudo, no camera at time of execution).

## Acceptance Criteria

- [ ] OV9782 camera is plugged in to `vali.local` and appears as `/dev/video0`. — DEFERRED to follow-up issue `live-tag-detection-and-systemd-persistence-on-a-pi-with-a-calibrated-playfield.md` (needs physical AprilTag playfield + reboot window)
- [x] `jtl` added to `video` group on vidar.local (adapted from `eric` on vali).
- [x] Wheel built on Mac (`uv build --wheel`), scp'd to `~/wheels/` on the Pi.
- [x] `pipx install "aprilcam[daemon]==0.20260619.7"` succeeded on the Pi (aarch64/cp312).
- [x] `mss` headless check: `import aprilcam.daemon` exits 0.
- [x] opencv-contrib confirmed importable: `cv2.__version__` = 4.13.0.
- [ ] `deploy/aprilcamd.service` copied to `/etc/systemd/system/` on `vali` + `systemctl enable --now aprilcamd` + journalctl verification + reboot persistence. — DEFERRED to follow-up issue `live-tag-detection-and-systemd-persistence-on-a-pi-with-a-calibrated-playfield.md` (needs physical AprilTag playfield + reboot window)
- [x] From Mac: `aprilcam cameras` (no env vars set) auto-discovers `vidar.local` (vidar @ 192.168.1.144 via mDNS) and lists cameras over TCP.
- [ ] From Mac: `aprilcam tags` returns tag data from the Pi camera. — DEFERRED to follow-up issue `live-tag-detection-and-systemd-persistence-on-a-pi-with-a-calibrated-playfield.md` (needs physical AprilTag playfield + reboot window)
- [ ] From Mac: MCP golden path via `aprilcam mcp`:
  - `open_camera` → returns camera handle on `vali.local`.
  - `create_playfield` → playfield created.
  - `get_tags` → tags returned.
  - `capture_frame` → image returned as base64.
  - `calibrate_playfield` → calibration written on `vali` via `SetCalibration` RPC.
  — DEFERRED to follow-up issue `live-tag-detection-and-systemd-persistence-on-a-pi-with-a-calibrated-playfield.md` (needs physical AprilTag playfield + reboot window)
- [ ] After `systemctl reboot` on `vali`, daemon auto-restarts and reconnects. — DEFERRED to follow-up issue `live-tag-detection-and-systemd-persistence-on-a-pi-with-a-calibrated-playfield.md` (needs physical AprilTag playfield + reboot window)
- [ ] OV9782 manual exposure set (`APRILCAM_UNDISTORT=1` or config, per
      `project_ov9782_exposure_tuning` memory note). — DEFERRED to follow-up issue `live-tag-detection-and-systemd-persistence-on-a-pi-with-a-calibrated-playfield.md` (needs physical AprilTag playfield + reboot window)

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

## Live Verification Results (vidar.local, 2026-06-19)

Deployed aprilcam 0.20260619.7 to vidar.local (Pi 5, user `jtl`, aarch64/cp312)
via pipx. cv2 4.13.0 and zeroconf import confirmed OK; `import aprilcam.daemon`
headless check passed with exit 0.

Daemon started successfully: TCP listener on `[::]:5280` plus unix socket; mDNS
service registered via zeroconf/avahi.

From the Mac, verified over TCP:
- `EnumerateCameras` RPC via explicit IP (192.168.1.144), `.local` hostname
  (vidar.local), and mDNS auto-discovery (no flags) — all three paths worked.
- File-proxy RPCs round-tripped: `set_paths`/`get_paths` and `get_calibration`.
- `ListPlayfields` returned correctly.

Confirmed on the Pi:
- `aprilcam --agent` launched correctly.
- `aprilcam config` reported XDG layout: data=`~/.local/share/aprilcam`,
  log=`~/.local/state/aprilcam`, socket=`/run/user/1000/aprilcam`.

**4 defects found live and fixed (commits tagged 014-010):**

- **(A) macOS / no-`XDG_RUNTIME_DIR` client crash**: client crashed creating
  `/run` when `XDG_RUNTIME_DIR` was unset on macOS. Fix: `_default_dirs()`
  now falls back to a temp directory.
- **(B) gRPC cannot resolve `.local` mDNS names**: gRPC's DNS resolver does
  not use the mDNS stack, so `.local` hostnames failed. Fix: `DaemonControl`
  now resolves host→IP via `getaddrinfo` before passing to gRPC.
- **(C) Daemon advertised `127.0.1.1` over mDNS**: loopback address was
  advertised, making remote discovery useless. Fix: daemon now advertises
  the primary routable IPv4 address.
- **(D) `ServiceBrowser` handler had wrong parameter name**: the zeroconf
  `ServiceBrowser` callback used `zeroconf_instance` (wrong) instead of
  `zeroconf`, so discovery always returned `[]`. Fix: renamed parameter to
  `zeroconf`.

**Deferred:** live AprilTag detection (OV9782 + `/dev/video0`) and systemd
persistence (enable/reboot cycle) are tracked in the follow-up issue
`live-tag-detection-and-systemd-persistence-on-a-pi-with-a-calibrated-playfield.md`.
