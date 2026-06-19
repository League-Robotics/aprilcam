---
id: 014-009
title: Pi deployment assets and local TCP golden-path integration test
status: done
use-cases:
- SUC-006
depends-on:
- 014-008
---

# 014-009: Pi deployment assets and local TCP golden-path integration test

## Description

Create all deployment files and write a local TCP integration test that
exercises the golden path without hardware. The deploy files are:
- `deploy/aprilcamd.service` — systemd unit extending Sprint 013's scaffold.
- `deploy/provision-pi.sh` — provision script for Ubuntu 24.04 aarch64.
- `deploy/README.md` — runbook: build wheel, scp, pipx install, systemd,
  seed calibration, firewall, mss check, vidar note.

The integration test starts the daemon in TCP-only mode on localhost (no
camera hardware), connects a client over TCP, and verifies:
`EnumerateCameras` → `ListCameras` → `ListPlayfields` → `GetVersion`.

## Acceptance Criteria

- [x] `deploy/aprilcamd.service` exists and contains:
  - `[Unit] After=network-online.target avahi-daemon.service`
  - `[Service] User=eric SupplementaryGroups=video`
  - `RuntimeDirectory=aprilcam StateDirectory=aprilcam LogsDirectory=aprilcam ConfigurationDirectory=aprilcam`
  - `Environment=APRILCAM_DATA_DIR=/home/eric/aprilcam-data`
  - `Environment=APRILCAM_SOCKET_DIR=/run/aprilcam`
  - `ExecStart=...aprilcam daemon --tcp --tcp-port 5280 --unix`
  - `Restart=on-failure RestartSec=5`
- [x] `deploy/provision-pi.sh` exists, is executable (`chmod +x`), and:
  - Accepts `user@host` as first argument.
  - Over SSH: runs `apt install` for required packages, adds user to `video` group,
    runs `pipx ensurepath`, creates `~/aprilcam-data/{cameras,playfields}`.
  - Documents the `pipx install "aprilcam[daemon]==<ver>"` step as a manual
    step (wheel must be scp'd first).
- [x] `deploy/README.md` exists and documents:
  - Wheel build: `uv build --wheel` or `python -m build --wheel`.
  - Wheel copy: `scp dist/aprilcam-*.whl eric@vali.local:~/wheels/`.
  - pipx install with `--find-links ~/wheels`.
  - systemd install and enable.
  - Calibration seeding (`rsync` or remote calibrate via MCP).
  - Firewall note: port 5280 + stream ephemeral ports on `0.0.0.0`.
  - OV9782 exposure note (reference `project_ov9782_exposure_tuning`).
  - `vidar.local` SSH key note (passwordless auth not yet in place).
  - `mss` headless check: `python -c "import aprilcam.daemon"` must succeed
    without display.
  - opencv-contrib fallback: `--extra-index-url https://www.piwheels.org/simple`
    if pip cannot find the wheel.
- [x] `tests/test_local_tcp_integration.py` exists with a test that:
  - Starts the daemon in-process on a free TCP port (no Unix socket, no camera).
  - Waits for the daemon to bind (polls `started_event` with 10 s timeout).
  - Connects `DaemonControl(host="127.0.0.1", port=<actual_port>)`.
  - Calls `enumerate_cameras()`, `list_cameras()`, `list_playfields()`.
  - Asserts no exception raised.
  - Tears down the daemon via `_shutdown_event` and thread join.
  - Marks test with `pytest.mark.integration` (deselected in normal CI).
- [x] `uv run pytest -m "not integration"` passes (integration tests skipped
      in normal CI run).
- [x] `uv run pytest -m integration` passes on a machine with the daemon
      startable.

## Implementation Plan

### `deploy/aprilcamd.service`

Copy the unit from the issue spec (verbatim in the architecture update §6)
and adapt to also include the Sprint 013 `*Directory=` directives. The unit
runs as `User=eric` (not DynamicUser) for the Pi use case.

### `deploy/provision-pi.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail
TARGET="${1:?Usage: $0 user@host}"

ssh "$TARGET" bash -s << 'PROVISION'
set -euo pipefail
sudo apt-get update -qq
sudo apt-get install -y python3-venv python3-pip pipx \
    v4l-utils libgl1 libglib2.0-0 avahi-daemon
sudo usermod -aG video "$USER"
pipx ensurepath
mkdir -p ~/aprilcam-data/cameras ~/aprilcam-data/playfields
echo "Provision complete. Log out and back in for group changes to take effect."
PROVISION
```

### Integration test

The test requires the daemon to support `--no-camera` or to not crash when
no cameras are present. Verify this is the case. If `EnumerateCameras` returns
an empty list when no cameras are attached, the test assertion is `len(cameras) == 0`.

```python
# tests/test_local_tcp_integration.py
import pytest, subprocess, time, socket as _socket
from aprilcam.client.control import DaemonControl

@pytest.mark.integration
def test_local_tcp_daemon_golden_path(tmp_path):
    # Start daemon on a random TCP port
    proc = subprocess.Popen(
        ["python", "-m", "aprilcam.daemon", "--tcp", "--tcp-port", "0",
         "--data-dir", str(tmp_path)],
        stderr=subprocess.PIPE,
    )
    try:
        # Parse actual port from stderr (daemon logs "TCP port N")
        port = _wait_for_port(proc, timeout=15)
        dc = DaemonControl(host="127.0.0.1", port=port)
        with dc:
            cameras = dc.enumerate_cameras()
            open_cams = dc.list_cameras()
            playfields = dc.list_playfields()
            # Basic assertions — no hardware needed
            assert isinstance(cameras, list)
            assert isinstance(open_cams, list)
            assert isinstance(playfields, list)
    finally:
        proc.terminate()
        proc.wait(timeout=5)
```

### Files to Create/Modify

- `deploy/aprilcamd.service` — new.
- `deploy/provision-pi.sh` — new (executable).
- `deploy/README.md` — new.
- `tests/test_local_tcp_integration.py` — new.
- `pytest.ini` or `pyproject.toml` `[tool.pytest.ini_options]` — add
  `integration` marker definition.

### Documentation Updates

`deploy/README.md` is the primary documentation artifact for this ticket.
