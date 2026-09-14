---
status: in-progress
sprint: '040'
tickets:
- 040-006
---

# Camera bridge watchdog never detects a stalled loopback feed

Found live on vidar.local during sprint 040 ticket 003 (see that ticket's
Findings item 4).

`deploy/aprilcam-camera-bridge` (sprint 040 ticket 001) guards against the
known libcamerasrc failure mode — a SIGSEGV after which `gst-launch-1.0`'s
fault handler spins forever instead of exiting, so systemd never restarts the
bridge — with this loop:

```bash
if ! timeout 6 v4l2-ctl -d "$DEV" --stream-mmap --stream-count=1 >/dev/null 2>&1; then
    kill -9 "$GST_PID"; exit 1
fi
```

On vidar, `v4l2-ctl --stream-mmap` against a v4l2loopback device with no live
producer fails with an "unsupported stream type"-style error but **exits 0**.
So the check never fires: after libcamerasrc SIGSEGV'd, the bridge's watchdog
did not notice or restart in ~40 s of observation. The loopback stays frozen,
and only the daemon's own reopen loop notices. The v1 script this was carried
forward from (`/usr/local/bin/aprilcam-camera-bridge` on vidar) has the same
latent bug.

## Expected

The watchdog must judge stall on evidence that a frame actually arrived, not on
`v4l2-ctl`'s exit status. Examples: bytes written with `--stream-to=-` piped
through a byte count, stderr/stdout content, or gst process state such as a
spinning fault handler. The criterion must be verified live on vidar by killing
or SIGSEGV-ing the gst pipeline and confirming the unit restarts. The fix needs
an offline regression guard where practical.
