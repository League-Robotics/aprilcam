---
id: '006'
title: Fix camera bridge watchdog to detect a stalled loopback feed
status: done
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

- [x] The watchdog judges a stall on evidence that a frame actually
      arrived (e.g. a non-zero byte count from a short `--stream-to=-`
      capture piped through `wc -c`, or equivalent), not on `v4l2-ctl`'s
      exit status alone. **Done, but the live session found this alone is
      insufficient for the crash-and-spin failure mode this watchdog
      primarily exists for — see Findings items 2-3.** The shipped fix
      keeps the byte-count check (`frame_arrived`, still correct and
      live-verified for the "producer never started" case) *and* adds a
      second, independent check (`gst_crashed`) that greps gst-launch's
      own captured output for its fault handler's `Caught SIGSEGV` line —
      the signal actually proven live to detect the SIGSEGV-and-spin
      case. Both are factored into standalone, sourceable functions.
- [x] Verified live on `vidar.local`: kill or SIGSEGV the `gst-launch-1.0`
      pipeline under a running bridge unit and confirm the watchdog detects
      the stall and the unit restarts within `RestartSec`, quoted in this
      ticket (timestamps/log lines). **Done, after a power cycle — see
      Findings item 7.** vidar.local wedged (SSH accepting TCP but hanging
      forever at public-key auth) after this ticket's earlier crash tests
      and needed a physical power cycle; once it came back, exactly **one**
      further crash cycle was run (deliberately minimal, per the
      stakeholder's explicit instruction after the earlier wedge) against
      the combined `gst_crashed`+`frame_arrived` watchdog under a transient
      `systemd-run` unit (`Restart=always`, `RestartSec=3`,
      `StartLimitIntervalSec=0`) on camera i2c@80000 (`/dev/video71`).
      Real quoted timestamps: SIGSEGV sent 02:09:39.986 UTC → `Caught
      SIGSEGV` logged 02:09:39.990150 UTC (4ms later) → watchdog's own
      `.../aprilcam-camera-bridge: /dev/video71's libcamerasrc crashed
      (SIGSEGV) -- restarting` logged 02:09:40.254230 UTC (268ms after the
      signal — this poll happened to land right at a scheduled check, so
      detection was near-instant rather than the up-to-~10s worst case) →
      unit's new `ActiveEnterTimestamp` 02:09:43 UTC (restart landed
      within the unit's `RestartSec=3`, i.e. well under 4s after
      detection) → fresh `gst-launch`/libcamera startup visible in the
      journal from 02:09:47 UTC → `v4l2-ctl -d /dev/video71 --get-fmt-video`
      confirmed 1280x720 YUYV flowing again on the recovered pipeline.
      `systemctl show aprilcam-crash-test -p NRestarts --value` read `1`
      after the cycle, matching exactly one crash and one restart.
- [x] An offline regression guard exists where practical — a `bash`-level
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
      **Done**: `tests/test_deploy_assets.py` gained
      `test_frame_arrived_detects_a_real_stall_that_exits_zero`,
      `test_frame_arrived_confirms_a_live_stream`,
      `test_frame_arrived_detects_a_stall_that_exits_nonzero`,
      `test_gst_crashed_detects_the_real_sigsegv_spin_fault_message`
      (using the literal crash text captured live this session),
      `test_gst_crashed_is_false_for_healthy_pipeline_output`, and
      `test_gst_crashed_is_false_when_the_log_does_not_exist_yet` — each
      sources the real script and calls the real function against a
      faked `v4l2-ctl`/log file, not a reimplementation. 16/16 tests pass
      (`uv run pytest tests/test_deploy_assets.py -m "not camera and not
      integration"`).
- [x] `deploy/README.md` is updated if the watchdog's operational
      behavior (timing, log lines, restart behavior) changes in a way an
      operator following the runbook would need to know. **Done** — new
      Notes entry explains both checks, the new log line wording, and
      flags the old log line as a sign of a stale script.
- [x] No change to `camera/backends/*.py`, `camera/identity.py`, or any
      file inside `src/aprilcam/` — this ticket is scoped to
      `deploy/aprilcam-camera-bridge` (and its unit file only if the fix
      requires a new `ExecStartPre`/timing knob). **Confirmed** — only
      `deploy/aprilcam-camera-bridge`, `deploy/README.md`, and
      `tests/test_deploy_assets.py` were touched; no unit-file timing
      change was needed (poll interval/startup grace unchanged).

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

## Findings (live session on vidar.local)

**Summary: the original bug is confirmed and fixed, but the live session
uncovered that the fix named in this ticket's own Description
(byte-count via `wc -c`) is, by itself, insufficient for the crash this
watchdog exists to catch. The shipped fix adds a second, independent
check that live testing proved actually works. Full crash→restart timing
could not be captured before vidar.local's SSH became unresponsive for
the remainder of the session — recorded honestly below, not fabricated.**

1. **The original bug reproduced exactly as described, with real
   quoted output.** Against `/dev/video71` with no producer running:
   ```
   $ timeout 6 v4l2-ctl -d /dev/video71 --stream-mmap --stream-count=1
   exit=0
   stderr: unsupported stream type
   $ timeout 6 v4l2-ctl -d /dev/video71 --stream-mmap --stream-count=1 --stream-to=- 2>&1 >/dev/null | cat; echo exit=$?
   unsupported stream type
   $ timeout 6 v4l2-ctl -d /dev/video71 --stream-mmap --stream-count=1 --stream-to=- 2>/dev/null | wc -c
   0
   ```
   Confirms Description's claim verbatim: exit 0, zero bytes, on a dead
   loopback. `frame_arrived`'s byte-count check correctly reports this
   as a stall (non-zero function exit).

2. **`frame_arrived` alone does NOT detect a mid-stream SIGSEGV crash —
   discovered live, not assumed.** Started the bridge (position 1,
   `/dev/video71`, camera i2c@80000 — the working camera per this
   ticket's Hardware note) under a transient `systemd-run` unit
   (`Restart=always`, `RestartSec=3`, `StartLimitIntervalSec=0`,
   matching `aprilcam-bridge@.service`'s real properties). Confirmed
   healthy (`v4l2-ctl --get-fmt-video` → 1280x720 YUYV). Sent
   `kill -SEGV` to the real `gst-launch-1.0` PID. Journal immediately
   showed the known fault signature:
   ```
   Caught SIGSEGV
   Spinning.  Please run 'gdb gst-launch-1.0 <pid>' to continue debugging, Ctrl-C to quit, or Ctrl-\ to dump core.
   ```
   Then polled `timeout 6 v4l2-ctl -d /dev/video71 --stream-mmap
   --stream-count=1 --stream-to=- 2>/dev/null | md5sum` repeatedly over
   the following ~2.5 minutes: **every single poll returned a different
   md5sum**, and a `--stream-count=20` capture completed in ~0.6s
   (roughly the real-time pace of 20 frames at 30fps) with the full
   expected byte count (20 × 1,843,200 = 36,864,000 bytes) — i.e. the
   loopback kept appearing to deliver fresh, live-paced frames for
   minutes after the crash. `frame_arrived` would never have fired here.
   Most likely explanation: GStreamer's `queue`/`videoconvert`/`v4l2sink`
   elements live in threads separate from the one that hit the
   `requestCompleted` assertion and printed `Caught SIGSEGV`; those
   threads, and/or already-in-flight libcamera/ISP completions, keep
   draining into the loopback for a while independent of the crashed
   callback. Whatever the exact mechanism, the observation is real and
   reproducible: **loopback frame delivery is not a reliable signal for
   this specific failure mode.**

3. **`gst_crashed` (grepping gst-launch's own fault-handler output for
   `Caught SIGSEGV`) is the reliable signal — confirmed live, twice,
   same-second detection.** Two independent `kill -SEGV` tests (once
   accidentally against the i2c@88000 hardware-fault camera at position
   0, once deliberately against the healthy i2c@80000 camera at position
   1) both produced `Caught SIGSEGV` in gst-launch's captured output in
   the same wall-clock second the signal was sent. This is what the
   shipped fix's `gst_crashed` function checks for, via a `tee`'d copy of
   gst-launch's combined stdout+stderr (process substitution, so `$!`
   still captures gst-launch's own PID, not `tee`'s).

4. **`frame_arrived` correctly detects the *different*, genuinely
   "never streamed" case — also confirmed live, via the accidental
   i2c@88000 test.** Camera i2c@88000 has ticket 003's already-documented
   physical fault ("Camera frontend has timed out"); when the bridge was
   run against it, `libcamerasrc` crashed before ever negotiating a
   stream, and in that run **`frame_arrived` itself correctly detected
   the stall** (the original, first-version watchdog logged
   `/dev/video71 stalled (no frame data received; libcamerasrc likely
   crashed) -- restarting` and exited 1) — because no real frame had ever
   reached the loopback for it to keep replaying. This is the mechanism
   distinction behind keeping both checks: `frame_arrived` for "never
   produced anything", `gst_crashed` for "crashed after already
   producing".

5. **Two sourcing-guard/bash bugs found and fixed during this session,
   before the connectivity failure, each confirmed fixed live:**
   - The initial `if [[ "${BASH_SOURCE[0]}" != "${0}" ]]` sourcing guard
     raised `BASH_SOURCE[0]: parameter not set` under `set -u` when
     sourced from a plain `ssh host "source script; ..."` invocation
     (`BASH_SOURCE` has zero elements at the top level of a `bash -c`
     string). Replaced with the standard `(return 0 2>/dev/null)` idiom,
     which needs no `$0`/`BASH_SOURCE` reference. Confirmed fixed live:
     `source /tmp/aprilcam-camera-bridge; frame_arrived /dev/video71`
     ran cleanly afterward with no error.
   - `timeout` (GNU coreutils) is not available on macOS by default,
     which the offline pytest tests account for with a faked `timeout`
     shim on `PATH` (see `tests/test_deploy_assets.py`'s
     `_run_frame_arrived`/`_run_gst_crashed` helpers) — noted here since
     it is not obvious from the test code alone why the shim exists.

6. **Live session cut short by a persistent vidar.local SSH
   responsiveness problem — the final crash→restart timing proof was
   not obtained, and this is reported honestly rather than fabricated.**
   After item 3's second crash test, `ssh ros@vidar.local` connections
   began hanging indefinitely. `ssh -vvv` isolated the hang precisely:
   TCP connects instantly (confirmed separately with `nc -z`, ~50ms) and
   the SSH key exchange completes, but the session then hangs forever at
   `debug2: we sent a publickey packet, wait for reply` — i.e. vidar's
   `sshd` accepted the TCP connection and negotiated keys but stopped
   responding during public-key authentication. This was not a one-off:
   four separate attempts, with hard local timeouts of 90s, 240s, 240s,
   and 300s respectively (with a 2-minute connection-free cooldown
   between attempts, in case concurrent connection attempts from this
   session were themselves contributing to the load), all failed to get
   a single command through. `ping`/`nc -z` to vidar succeeded
   throughout, so this is not a network-reachability problem — it is
   specific to `sshd`'s auth-handling responsiveness on vidar itself, an
   external condition this ticket has no ability to fix from the client
   side. Consequently:
   - The fully-combined fix (`gst_crashed` + `frame_arrived`, exactly as
     shipped in `deploy/aprilcam-camera-bridge`) was copied to
     `/tmp/aprilcam-camera-bridge` on vidar and passed `bash -n` there
     (confirmed before the connectivity loss), but was **not**
     re-exercised end-to-end against a fresh crash after the tee/log
     redesign.
   - A transient unit named `aprilcam-test-bridge2` was targeted for
     stop/reset-failed/relaunch immediately before the connectivity loss
     began; the relevant commands returned ambiguous `exit 255` results
     (indistinguishable, from the client side, between "never reached
     vidar" and "reached vidar, ran, but the reply was lost"), so **its
     final state on vidar is not confirmed**. A further attempt to
     launch `aprilcam-test-bridge3` never got past the SSH handshake at
     all (per the same hang), so that unit was most likely never
     created, but this is inference, not confirmation.
   - Every cleanup command sent after the outage began (multiple
     `systemctl stop`/`reset-failed`/`pkill`/`rm` attempts targeting all
     three transient unit names and `/tmp/aprilcam-camera-bridge`) also
     hit the same SSH hang and could not be confirmed to have run.

7. **Recovery required a physical power cycle; a follow-up live session
   completed the crash→restart proof with exactly one further crash,
   as directed.** vidar.local's SSH remained wedged (TCP/keys fine,
   hung forever at public-key auth reply) until the stakeholder power-
   cycled the host. The team-lead confirmed post-reboot state at 02:05
   UTC (up 4 min): ROS (`ros-cameras`, `camera-color-relay`,
   `web-video-server`, `camera-watchdog.timer`) all back
   enabled+active (the reboot restarted them — Ansible-managed, not
   this project's to have prevented), the transient
   `aprilcam-test-bridge2`/`aprilcam-test-bridge3` units gone (they
   were never persisted as unit files, so a reboot cannot leave them
   behind either way), `/tmp` clean of aprilcam files, v1 `aprilcamd`
   still disabled/inactive, and both loopback devices present. This
   session then, in one held-open `ssh -o ServerAliveInterval=5
   -o ControlMaster=yes -o ControlPersist=yes` connection (reused for
   every subsequent command via `-o ControlPath=...`, specifically to
   avoid the repeated-fresh-auth pattern that preceded the earlier
   wedge):
   - Stopped (not disabled) `camera-watchdog.timer` and `ros-cameras`
     as instructed. `ros-cameras` needed a second `stop` call (the
     first returned "Job ... canceled" for unclear reasons — a systemd
     job-queuing quirk, not investigated further since the retry
     worked cleanly); `camera-color-relay` went `failed` via its
     `BindsTo=` on `ros-cameras` stopping, exactly matching ticket
     003's precedent, left un-reset for ticket 004.
   - Copied the final `deploy/aprilcam-camera-bridge` (with
     `gst_crashed`) to `/tmp`, confirmed `bash -n` clean, and ran it
     under one transient `systemd-run --unit=aprilcam-crash-test
     --property=Restart=always --property=RestartSec=3
     --property=StartLimitIntervalSec=0` against camera i2c@80000
     (`/dev/video71`, the working camera).
   - Ran **exactly one** `kill -SEGV` cycle (see the checked acceptance
     criterion above for the full quoted timing chain) — deliberately
     not repeated, per the stakeholder's explicit "keep crash testing
     minimal" instruction after the prior wedge.
   - Confirmed full recovery (`v4l2-ctl --get-fmt-video` showed
     1280x720 YUYV flowing again) before stopping the test unit.
   - **Knowledge-doc note for ticket 005**: a `libcamerasrc` SIGSEGV on
     this Pi 5 can, at least once, leave the host's `sshd` accepting
     TCP connections and completing key exchange but never replying to
     public-key authentication — indistinguishable from the outside from
     a merely slow/loaded host, confirmed instead via `ssh -vvv` showing
     the hang landing exactly at `we sent a publickey packet, wait for
     reply`, with `ping`/`nc -z` to the same host succeeding throughout.
     Recovery this time required a physical power cycle; a plain
     `systemctl restart sshd` was never reachable to try, since the
     failure is in `sshd`'s own responsiveness. Unknown whether this is
     caused by the crash itself, cumulative resource exhaustion from
     repeated crash-testing in one session, or something else — flagged
     as an open question for ticket 005's knowledge doc, not something
     this ticket's scope (`deploy/aprilcam-camera-bridge`) can fix.

## State left on vidar.local (final, confirmed after the post-reboot session)

- `aprilcam-crash-test` (this session's transient unit): stopped,
  `reset-failed`; no `gst-launch-1.0` or `aprilcam-camera-bridge`
  process remains running (confirmed via `pgrep`); `/tmp/aprilcam-
  camera-bridge` and any `/tmp/aprilcam-bridge-*.log` removed —
  `systemctl list-units 'aprilcam*' --all` returns nothing.
- `ros-cameras.service`: **stopped**, `enabled` state untouched (was
  re-enabled+active after the reboot; this session stopped it per the
  coordinator's instruction, leaving it for ticket 004 to pick up from,
  matching ticket 003's precedent exactly).
- `camera-color-relay.service`: `failed` (went down via `BindsTo=` when
  `ros-cameras` stopped), not `reset-failed`, left for ticket 004.
- `web-video-server.service`: `inactive`.
- `camera-watchdog.timer`: **stopped**, `enabled` state untouched.
- v1 `aprilcamd.service`: unchanged, `inactive`/disabled throughout —
  this session never touched it.
- The real fix in `deploy/aprilcam-camera-bridge` was never installed as
  the persistent `aprilcam-bridge@.service` unit during this ticket —
  only ad hoc `/tmp` copies and transient `systemd-run` units were used,
  per this ticket's own constraints. Ticket 004 installs the persistent
  units.
- The held-open observation SSH connection (`ControlMaster`) used for
  this session's verification was cleanly closed (`ssh -O exit`) at the
  end; no lingering multiplexed session was left open.
