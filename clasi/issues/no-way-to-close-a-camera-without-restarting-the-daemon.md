---
status: pending
---

# There is no way to close a held-open camera without restarting the daemon

## Description

2026-09-06: every `camera tags`/`camera image` call opens its camera
and the daemon holds it open for its lifetime (`service._camera_for`,
"held-open Camera ... opening it on first use"). With five cameras on
one USB 2.0 root port, opening two more for a quick look starved the
others (repeated heartbeat stalls, reopen failures), and the only way
to release bandwidth was a full daemon restart, which also drops every
live subscriber.

### Needed

- A `close_camera` RPC and `aprilcam camera close <camera>` that
  releases the capture worker (refused while a live subscriber holds
  it, or with `--force`).
- Optionally an idle timeout: a camera with no subscriber and no
  burst for N seconds is closed automatically.
