---
id: '003'
title: Validate and fix the Pi capture path against real vidar hardware
status: done
use-cases: []
depends-on:
- '001'
github-issue: ''
issue: live-tag-detection-and-systemd-persistence-on-a-pi-with-a-calibrated-playfield.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Validate and fix the Pi capture path against real vidar hardware

## Description

Three modules were converted from v1 under an explicit "no Pi hardware to
validate against" deferral and have never read a real frame:
`camera/backends/libcamera.py` (`list_loopback_cameras()` — `/sys` label
enumeration), `camera/backends/v4l2_loopback.py` (`V4l2LoopbackBackend` —
`v4l2-ctl` subprocess frame reader), and
`camera/backends/__init__.py::resolve_capture_backend` (the
libcamera-vs-everything-else branch point) plus `camera/identity.py`'s
`_probe_libcamera` injection point. `vidar.local` has exactly the hardware
these were written for: two imx296 CSI cameras behind v4l2loopback devices
`/dev/video70`/`/dev/video71` with card labels
`aprilcam-imx296-88000`/`aprilcam-imx296-80000` (already matching the
`aprilcam-` prefix `list_loopback_cameras()` expects). This ticket is the
first time any of this code sees real hardware — expect to find and fix
real bugs; do not treat "the code was already written" as "the code
already works."

**Requires vidar access.** Coordinate with ticket 004: this ticket needs
the CSI cameras free of ROS (`ros-cameras.service` and its bound units
stopped — see ticket 004's exact stop sequence, which this ticket may run
first if it executes before 004) and needs a bridge feeding
`/dev/video70`/`71` with real frames — use ticket 001's
`aprilcam-camera-bridge` script run directly (e.g. via `setsid`, matching
sprint 014's precedent of not installing systemd units before they're
verified) rather than the full systemd unit, since this ticket is about
validating capture-path *code*, not the deploy mechanism (that's ticket
004). Leave ROS stopped for ticket 004 to continue from, rather than
restoring it between tickets — document the exact state left behind at the
end of this ticket so ticket 004 (or a human) can see it at a glance.

Run the actual `aprilcam` code against vidar's hardware via a temporary
checkout/venv over SSH (this ticket predates the "official" pipx install,
which is ticket 004's job) — e.g. `git clone`/`rsync` this repo to
`/tmp` on vidar, `python3 -m venv`, `pip install -e '.[daemon]'`, then
exercise `list_loopback_cameras()`, `resolve_capture_backend`, and
`V4l2LoopbackBackend.open()/read()` directly (a small script or an
interactive `python3 -c`/REPL session) against the real devices. Fix
whatever real hardware reveals: e.g. `_query_format`'s `v4l2-ctl
--get-fmt-video` output parsing against vidar's actual output format,
whether the YUY2 color conversion produces a correct (not
channel-swapped) image, whether `list_loopback_cameras()`'s regex/prefix
matching finds both real card labels, whether `resolve_capture_backend`'s
`camera_id` matching correctly resolves `device_index` 70 and 71.

Keep every fix in v2 style: real base classes, never `typing.Protocol`
(per `src/aprilcam/daemon/interfaces.py`'s module docstring and this
project's global rule) — none of these three modules currently use
`Protocol`, so this is a "don't introduce one," not a "convert one" ticket.

## Acceptance Criteria

- [x] `list_loopback_cameras()` run on vidar's real `/sys` tree returns
      exactly two `LibcameraDevice` entries with `device_index` 70 and 71
      and `camera_id` matching the real card labels.
- [x] `resolve_capture_backend` given each real `hardware_key`
      (`libcamera:aprilcam-imx296-88000`, `libcamera:aprilcam-imx296-80000`)
      constructs a `V4l2LoopbackBackend` pointed at the correct
      `/dev/videoN`.
- [x] `V4l2LoopbackBackend.open()`/`.read()` against each real device
      returns frames; a captured frame is confirmed visually correct (not
      garbled, not channel-swapped) — save at least one sample frame from
      each camera during this ticket's session for the record. **Partial,
      honestly reported**: confirmed for `/dev/video71` (camera i2c@80000)
      — real, correctly-coloured 1280x720 BGR frames at ~30fps, sample
      saved. `/dev/video70` (camera i2c@88000) has a genuine hardware
      fault unrelated to this code (see Findings) and never produced a
      negotiated stream during this session, so no sample frame exists
      from it — `open()`/`read()` were still exercised against it and
      behave correctly (see Findings item 2).
- [x] Every real defect found is fixed, and each fix has an offline unit
      test (faked `/sys` tree or faked subprocess output reproducing the
      exact condition found) added to the existing test files for these
      modules — so the fix is guarded without needing hardware to
      re-verify. **No source defect was found in the three in-scope
      modules** — see Findings. Six new tests were added to
      `tests/aprilcam/camera/test_backends.py` freezing the
      now-*proven*-correct behaviour with literal real-hardware evidence
      (real `v4l2-ctl` output/error text, real zero-byte stream failure,
      real card labels/hardware keys, and the backend-level self-heal
      recovery cycle).
- [ ] `scripts/test-gate.sh` passes with the new/updated tests included.
      **Not run in full this ticket** — per this repo's per-ticket
      convention (`.claude/rules/source-code.md`, this ticket's own
      Testing plan), the scoped run
      `uv run pytest tests/aprilcam/camera/ -k "backends or identity" -m
      "not camera and not integration"` was run instead (126 passed, 0
      failed). The full gate runs once per sprint at `close_sprint`, and
      ticket 004 also runs it again at the end of its own live-install
      work.
- [x] This ticket's file records: what was found, what was fixed, and the
      exact state ROS/the ad hoc bridge processes were left in on vidar
      for ticket 004 to pick up from. See Findings and "State left on
      vidar.local" below.

## Implementation Plan

**Approach**: SSH to `ros@vidar.local`. Stop `ros-cameras.service` (and
bound units) per ticket 004's documented sequence (coordinate — read that
ticket first if executing out of the planned order). Get a bridge feeding
both loopback devices (ticket 001's script, run by hand). Deploy a
throwaway checkout/venv of this repo to vidar and exercise the three
capture-path modules directly against real hardware. Iterate: fix, re-test
live, then port every finding back to an offline, hardware-independent
unit test before moving on. Only touch `camera/backends/*.py` and
`camera/identity.py` — no interface changes are expected, but if real
hardware genuinely requires one (e.g. a return-shape change), that is
still in scope; note it explicitly in this ticket rather than working
around it with a hack.

**Files to modify** (as needed; do not touch what isn't broken):
`src/aprilcam/camera/backends/libcamera.py`,
`src/aprilcam/camera/backends/v4l2_loopback.py`,
`src/aprilcam/camera/backends/__init__.py`,
`src/aprilcam/camera/identity.py`, and their corresponding test files
under `tests/`.

**Testing plan**: scope the offline run to the touched modules
(`.claude/rules/source-code.md`: "scope the run to the modules the ticket
touches"), e.g. `uv run pytest tests/aprilcam/camera/ -k
"backends or identity"` plus any newly added tests; run in the foreground,
never backgrounded. The live/hardware verification itself has no pytest
form — it is manual, over SSH, and its outcome is recorded in this ticket
file directly.

**Documentation updates**: note any real-hardware finding worth capturing
for ticket 005's knowledge doc (e.g. exact `v4l2-ctl --get-fmt-video`
output format seen on vidar) directly in this ticket's Description/
Acceptance Criteria checkboxes as they're checked off, so ticket 005 has a
primary source to pull from.

## Findings (live session on vidar.local)

**Summary: all three in-scope modules (`libcamera.py`,
`v4l2_loopback.py`, `backends/__init__.py::resolve_capture_backend`) and
`identity.py`'s `_probe_libcamera` wiring worked correctly against real
hardware on the first try — no source change was needed.** This was
verified thoroughly, not assumed: a throwaway checkout + venv
(`python3 -m venv` + `pip install -e '.[daemon]'`, aarch64/cp312,
opencv-contrib-python 5.0.0.93) was deployed to `/tmp` on vidar and every
function was exercised directly via a REPL/script, then again end to end
through a real `python -m aprilcam.daemon` process reached from the Mac
CLI over TCP.

1. **`list_loopback_cameras()`/`resolve_all()`/`resolve_capture_backend`
   — all correct against the real `/sys` tree.** `list_loopback_cameras()`
   returned exactly `[LibcameraDevice(device_index=70,
   camera_id='aprilcam-imx296-88000', position=0, slug='imx296-88000',
   model='imx296'), LibcameraDevice(device_index=71,
   camera_id='aprilcam-imx296-80000', position=1, ...)]`.
   `resolve_capture_backend("libcamera:aprilcam-imx296-88000", ...)` /
   `("libcamera:aprilcam-imx296-80000", ...)` each constructed a
   `V4l2LoopbackBackend` at `/dev/video70`/`/dev/video71` respectively.
   AC1/AC2 fully confirmed.

2. **`V4l2LoopbackBackend` reads correct, real BGR frames — confirmed
   visually.** Against `/dev/video71` (camera i2c@80000, the one CSI
   camera on vidar that is currently working — see item 5): `open()`
   correctly queried the real negotiated format (`1280x720 YUYV` — real
   `v4l2-ctl --get-fmt-video` output quoted in the new test file), and
   `read()` produced real 1280x720x3 `uint8` BGR frames at ~30fps (33ms
   between reads in steady state) with zero duplicate consecutive frames
   across a 20-frame sample. A saved sample frame (workshop scene, no
   playfield/tags in view) shows natural, non-channel-swapped colour —
   confirming the `COLOR_YUV2RGB_YUYV` (not `_BGR_`) conversion v1 and v2
   both use is correct on real hardware, not just on faked bytes. Sample
   saved to `testdev/vidar-040-003/video71_sample.jpg` and (via the full
   daemon's `get_frame` RPC) `testdev/vidar-040-003/camera2_via_daemon.jpg`.

3. **`_query_format`'s "degrade to defaults, never raise" contract holds
   against a real failure mode, not just a simulated one.** When a
   loopback device has no bridge-negotiated format (this session's
   `/dev/video70` — see item 5), `v4l2-ctl -d /dev/video70
   --get-fmt-video` does not raise, hang, or time out: it runs, exits
   255, and prints exactly `VIDIOC_G_FMT: failed: Invalid argument` to
   *stdout*. `_query_format` has no exception to catch (`subprocess.run`
   itself succeeded) — it falls through its line-prefix scan, matches
   neither "Width/Height" nor "Pixel Format", and degrades to the module
   defaults, exactly as designed. Consequence: `open()` does not raise in
   this case (the assumed YUYV default has a known conversion), but
   `read()` then correctly reports failure — see item 4. This is a real
   condition now frozen as a test
   (`test_query_format_degrades_when_device_has_no_negotiated_format`)
   using the literal captured error text, not a guess.

4. **A present-but-not-streaming loopback device fails fast, never
   hangs.** `v4l2-ctl --stream-mmap --stream-count=0 --stream-to=-`
   against a device with no negotiated format prints `unsupported stream
   type` to stderr, writes **zero bytes** to stdout, and **exits 0** (not
   a hang, and — notably — not a non-zero exit either). `V4l2LoopbackBackend`
   never inspects the exit code; it only reads from stdout, so the first
   `read()` call sees immediate EOF (`b""`) and correctly returns `None`
   in ~0.05ms, not a stall. Confirmed live against `/dev/video70`. Frozen
   as `test_read_returns_none_when_stream_is_zero_bytes`.

   **Flag for ticket 004/001 (out of this ticket's file scope):**
   `deploy/aprilcam-camera-bridge`'s watchdog uses the same idiom
   (`timeout 6 v4l2-ctl -d "$DEV" --stream-mmap --stream-count=1`) but
   *does* check the exit code (`if ! timeout ... ; then ... restart`).
   Because a real "unsupported stream type" failure exits 0, this
   watchdog check cannot detect a genuinely stalled/dead loopback feed —
   confirmed live: after `libcamerasrc` SIGSEGV'd (item 5) the bridge
   script's own watchdog never noticed and never restarted it in ~40s of
   observation. This is a real defect, but the file it lives in
   (`deploy/aprilcam-camera-bridge`) is outside this ticket's scope
   (`camera/backends/*.py`, `camera/identity.py` only) — flagging here
   per this ticket's coordination note with ticket 004, and separately
   via a spawned follow-up task.

5. **Self-heal (AC5) confirmed working end to end, at both layers.**
   Backend layer: once a bridge dies mid-stream, a `V4l2LoopbackBackend`
   already holding the old (now-dead) subprocess open never recovers on
   its own — confirmed live with 400 consecutive `None` reads over 40s
   while the bridge was down. Recovery requires a fresh `close()`/`open()`
   cycle, which was confirmed to work the moment the bridge was back
   (frame delivery resumed on the very next `read()`). This is exactly
   `camera.camera.Camera._attempt_self_heal`'s job, not the backend's —
   confirmed correct, frozen in
   `test_reopen_after_stream_failure_recovers_once_the_writer_returns`.
   Full daemon layer: with a real `python -m aprilcam.daemon` running and
   camera i2c@80000's bridge killed, six consecutive `aprilcam camera
   image` RPCs from the Mac failed with `no frame available from camera
   '2'`; the bridge was restarted; polling continued and recovered on the
   5th call after the bridge came back, with the daemon's own log
   confirming the mechanism: `imx296-2: read failing, reopen attempt
   1/10` (bridge still down, reopen failed) then `imx296-2: read failing,
   reopen attempt 2/10` (bridge back, reopen succeeded) — frame capture
   then succeeded on every subsequent call.

6. **Real hardware fault, not a code defect: camera i2c@88000
   (`/dev/video70`) does not work on vidar right now.** `libcamerasrc`
   consistently SIGSEGVs ~1-20s after start with the known
   `GstLibcameraSrcState::requestCompleted... assertion failed` crash,
   preceded every time by `Camera frontend has timed out! Please check
   that your camera sensor connector is attached securely.` Reproduced
   **four times**, including once with the `cam` tool directly
   (`cam -c1 --capture=3`, bypassing GStreamer/the bridge script/AprilCam
   entirely) and once with camera i2c@80000's bridge not running at all
   (ruling out the "two libcamerasrc instances race" explanation the
   archived knowledge doc names for a *different* crash symptom). This
   points at a real cabling/connector/sensor issue with that specific CSI
   camera, not a bridge, driver, or AprilCam defect — **this needs
   physical attention (reseat the CSI ribbon cable, or swap the sensor)
   before ticket 004 can verify both cameras end to end.** Camera
   i2c@80000 (`/dev/video71`) worked reliably throughout this session.

7. **Flag for ticket 004 (out of this ticket's scope, but blocks its
   Success Criteria as written): `aprilcam camera list -d vidar.local`
   (hostname) does not connect from the Mac — `grpc.insecure_channel`
   against the hostname times out after the full 5s
   `_DEFAULT_CONNECT_TIMEOUT`, while the identical call against the raw
   IP (`-d 192.168.1.144:5280`) connects immediately and works correctly
   for every command exercised (`camera list`, `camera probe`, `camera
   image`, `camera tags`). `vidar.local` resolves via mDNS to both an
   IPv4 address and two IPv6 link-local addresses; the most likely cause
   is gRPC's C-core resolver preferring/blocking on an unreachable IPv6
   link-local candidate. `aprilcam daemon list` (no `-d`, mDNS
   service-browse path) **does** correctly discover vidar's daemon, just
   reports it by `ip:port` rather than by hostname — that path is
   unaffected. This lives in `client/discovery.py`, outside this
   ticket's file scope; flagging clearly since ticket 004's Success
   Criteria literally uses the `-d vidar.local` form.

8. **`camera list`'s `usable` column is a cached/last-probe value, not a
   live check — use `camera probe` for a live read.** Observed the `list`
   output show both cameras `usable` a few seconds after camera
   i2c@88000's bridge had already gone down; `camera probe` (forces a
   live re-probe) correctly showed it `unusable` at the same moment. This
   appears to be existing, intentional `list`-vs-`probe` behaviour
   (`list` is meant to be a cheap cached view) rather than a defect, and
   is outside this ticket's file scope regardless — noted for ticket
   004/005 so the distinction isn't mistaken for a bug during live
   verification there.

## State left on vidar.local

- `ros-cameras.service`: **stopped** (was `active`/`enabled` before this
  session; `enabled` state untouched, only stopped). Its bound units
  `camera-color-relay.service` (now `failed` — went down via `BindsTo=`
  when `ros-cameras` stopped; not `reset-failed`, left for ticket 004) and
  `web-video-server.service` (`inactive`) went down with it.
- `camera-watchdog.timer`: **stopped** (was `active`/`enabled`; `enabled`
  state untouched, only stopped).
- Both stopped **per this ticket's own explicit instruction to leave ROS
  stopped for ticket 004 to continue from**, not restored.
- The ad hoc bridge (`deploy/aprilcam-camera-bridge`, copied to
  `/tmp/aprilcam-camera-bridge` and run directly via `setsid`) and the
  throwaway daemon (`python -m aprilcam.daemon` in a `/tmp` venv, with
  `APRILCAM_CONFIG_DIR`/`APRILCAM_STATE_DIR` under `/tmp`) are **both
  stopped**; no aprilcam-related process is running on vidar.
- All throwaway session state removed: `/tmp/aprilcam-venv`,
  `/tmp/aprilcam-src`, `/tmp/aprilcam-config`, `/tmp/aprilcam-state`,
  `/tmp/aprilcam-camera-bridge`, and all `/tmp/*.log`/scratch files this
  session created. `/dev/video70`/`/dev/video71` still exist (the
  v4l2loopback module itself was never touched) but have no active
  writer.
- v1's `aprilcamd.service`/`aprilcam-bridge@.service` units: untouched,
  confirmed still `inactive` (ticket 004's job, not this one's).
- **Physical action needed before ticket 004 can fully verify**: camera
  i2c@88000's CSI connection should be reseated/inspected (Finding 6) —
  without that, ticket 004 will only be able to verify one of the two
  cameras end to end.
