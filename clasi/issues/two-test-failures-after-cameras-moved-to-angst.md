---
status: pending
---

# Two test failures after the playfield cameras moved to angst

## Description

Found while closing sprint 049 (2026-11-03), running the full suite on gala.
Neither failure comes from sprint 049's changes.

1. **`tests/acceptance/test_inventory.py::test_mcp_stdio_round_trip_against_the_real_daemon`
   fails every time.** It calls `get_tags` on `arducam-ov9782-usb-camera`
   through gala's local daemon and asserts no error. That camera now lives on
   angst, so the daemon answers "registered but not currently present". The
   other tests in that file skip when their camera is absent
   ("not currently available on this rig"); this one has no such guard. Give
   it the same presence skip, or point the acceptance layer at the rig's real
   host now that the cameras are spread across machines.

2. **`tests/aprilcam/client/test_lifecycle.py::test_start_stop_status_round_trip_against_a_real_daemon_process`
   is flaky under load.** It passed 6 of 6 times on its own, but in runs of
   `tests/aprilcam/client` it failed about 1 time in 5, and once in the full
   suite. It spawns a real daemon against gala's real cameras with
   `start_timeout=15.0`. The neighbouring test
   `test_default_start_timeout_covers_a_real_multi_camera_probe` notes that a
   real camera probe measured 11 s. The likely cause is the start timeout
   racing the camera probe when the machine is busy. Raise the timeout, or
   keep the spawned daemon from probing real cameras.

Sprint 049 closed with `uv run pytest -q -m "not acceptance"` because of (1).
