---
id: '006'
title: Fix camera bridge watchdog to detect a stalled loopback feed
status: in-progress
use-cases: []
depends-on:
- '001'
github-issue: ''
issue: camera-bridge-watchdog-never-detects-a-stalled-loopback.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Fix camera bridge watchdog to detect a stalled loopback feed

## Description

Found live on `vidar.local` during ticket 003 (see that ticket's Findings
item 4, and
`clasi/issues/camera-bridge-watchdog-never-detects-a-stalled-loopback.md`).
`deploy/aprilcam-camera-bridge` (ticket 001) tries to detect a stalled
pipeline — the known `libcamerasrc` failure mode where a SIGSEGV leaves
`gst-launch-1.0`'s fault handler spinning forever instead of exiting, so
`Restart=always` never fires — with:

```bash
if ! timeout 6 v4l2-ctl -d "$DEV" --stream-mmap --stream-count=1 >/dev/null 2>&1; then
    kill -9 "$GST_PID"; exit 1
fi
```

Confirmed live: `v4l2-ctl --stream-mmap` against a v4l2loopback device with
no live producer prints an "unsupported stream type"-style error but
**exits 0**. The check therefore never fires. After `libcamerasrc`
SIGSEGV'd on vidar, the bridge's own watchdog did not notice or restart in
~40s of observation — only the daemon's own backend-level reopen/self-heal
loop (confirmed working separately in ticket 003 Finding 5) noticed
anything was wrong, and that loop cannot restart the bridge process itself.
The v1 script this was carried forward from has the identical latent bug.

This ticket fixes `deploy/aprilcam-camera-bridge` only. It does not touch
`camera/backends/*.py` (already proven correct in ticket 003) or the
daemon's own self-heal logic (already proven correct in ticket 003).

## Acceptance Criteria

- [ ] The watchdog judges a stall on evidence that a frame actually
      arrived (e.g. a non-zero byte count from a short `--stream-to=-`
      capture piped through `wc -c`, or equivalent), not on `v4l2-ctl`'s
      exit status alone.
- [ ] Verified live on `vidar.local`: kill or SIGSEGV the `gst-launch-1.0`
      pipeline under a running bridge unit and confirm the watchdog detects
      the stall and the unit restarts within `RestartSec`, quoted in this
      ticket (timestamps/log lines).
- [ ] An offline regression guard exists where practical — a `bash`-level
      test (or a small pytest wrapper, consistent with this sprint's
      "shell scripts get `bash -n`/shellcheck, not a pytest harness built
      for this alone" convention from sprint.md's Test Strategy) that
      exercises the stall-detection logic against captured
      real-vs-simulated `v4l2-ctl` output/exit-code combinations, so the
      exact bug found here (exit 0 on a real stream failure) cannot
      regress silently. If the watchdog logic cannot be meaningfully
      unit-tested outside a real device, this ticket says so explicitly
      and relies on the live verification above plus code review instead
      of fabricating a test that doesn't actually exercise the bug.
- [ ] `deploy/README.md` is updated if the watchdog's operational
      behavior (timing, log lines, restart behavior) changes in a way an
      operator following the runbook would need to know.
- [ ] No change to `camera/backends/*.py`, `camera/identity.py`, or any
      file inside `src/aprilcam/` — this ticket is scoped to
      `deploy/aprilcam-camera-bridge` (and its unit file only if the fix
      requires a new `ExecStartPre`/timing knob).

## Implementation Plan

**Approach**: SSH to `ros@vidar.local` (passwordless sudo). Reproduce the
bug first (confirm `v4l2-ctl --stream-mmap --stream-count=1` against a
dead loopback still exits 0 on this exact vidar build, per ticket 003
Finding 4), then change the watchdog's stall criterion to check actual
data delivery rather than exit status — e.g. run a short, bounded capture
(`timeout 6 v4l2-ctl -d "$DEV" --stream-mmap --stream-count=1
--stream-to=- 2>/dev/null | wc -c`) and treat a zero-byte result as a
stall regardless of exit code. Redeploy the ad hoc bridge script to
`/tmp` on vidar (same pattern ticket 003 used — copy, run via `setsid`,
no systemd install), start it against a real loopback device, then
deliberately crash the underlying `libcamerasrc` process (`kill -SEGV` on
the gst pipeline PID, matching the crash ticket 003 observed
spontaneously) and confirm the fixed watchdog notices within one polling
interval and restarts the pipeline. Record the exact commands and
observed timing in this ticket.

**Files to modify**: `deploy/aprilcam-camera-bridge` (the watchdog loop);
`deploy/aprilcam-bridge@.service` only if a timing constant
(`RestartSec`, watchdog poll interval) needs to change alongside it;
`deploy/README.md` if operator-visible behavior changes.

**State changes allowed on vidar.local** (`ssh ros@vidar.local`,
passwordless sudo) — same constraints ticket 003 operated under:
- May copy `deploy/aprilcam-camera-bridge` to `/tmp` and run it directly
  via `setsid` against the existing `/dev/video70`/`/dev/video71`
  v4l2loopback devices (module already loaded; not this ticket's to
  touch).
- May deliberately crash the ad hoc bridge's own `libcamerasrc` process to
  test the watchdog — this is destructive only to the ad hoc process this
  ticket itself started, not to ROS or any other running service.
- Must **not** install or enable any systemd unit, and must **not** change
  ROS's (`ros-cameras.service`, `camera-watchdog.timer`) or the v1 units'
  enabled/active state — those remain exactly as ticket 003 left them
  (both ROS units stopped/enabled-untouched; v1 units inactive) for
  ticket 004 to manage. Confirm this state is unchanged at the end of this
  ticket's session (a `systemctl is-enabled`/`is-active` check is enough;
  no need to re-quote ticket 003's full recording).
- Must remove all throwaway files/processes from `/tmp` at the end of the
  session, matching ticket 003's cleanup convention.
- Camera i2c@88000 (`/dev/video70`) has the physical fault ticket 003
  found (Finding 6) and may still be unusable for a real
  `libcamerasrc`-fed test; if so, run the crash/restart verification
  against `/dev/video71` (camera i2c@80000) instead and note the
  substitution — the watchdog logic under test does not depend on which
  physical camera feeds the loopback.

**Testing plan**: no change to the offline pytest suite's hardware
dependencies. If a bash-level regression test is added, run it directly
(`bash path/to/test.sh` or equivalent) rather than folding it into the
pytest suite's collection, consistent with sprint.md's Test Strategy
("shell scripts get `bash -n` ... rather than a pytest wrapper"). If any
existing Python tests reference `deploy/aprilcam-camera-bridge` (unlikely
— check first), run those scoped, in the foreground.

**Documentation updates**: note the corrected stall-detection behavior in
`deploy/README.md` if operator-visible; flag anything ticket 005's
knowledge doc should mention (e.g. the exact `v4l2-ctl` exit-0-on-failure
quirk, useful beyond just this bridge script) directly in this ticket's
findings for ticket 005 to pull from.
