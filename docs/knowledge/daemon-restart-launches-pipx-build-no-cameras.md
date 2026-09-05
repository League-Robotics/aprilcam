# `aprilcam daemon restart` launches the pipx build, which sees zero cameras — start the daemon with `uv run` from the repo

Hit twice on 2026-08-23, same session.

## Symptom

After `aprilcam daemon restart`, `camera list` shows **every camera
`absent`/`unusable`**, even after `camera probe`, while
`system_profiler SPCameraDataType` lists all of them. The daemon
answers RPC normally otherwise — it just enumerates nothing.

## Cause

`aprilcam daemon restart|start` spawns whatever `aprilcam` is on PATH —
on this host the **pipx install** (`~/.local/bin/aprilcam`, homebrew
Python 3.14). That venv is an editable install of the repo (CLI code is
current) but does **not** have the `[daemon]` extra, so the daemon side
has no OpenCV capture backend and enumerates zero devices — silently:
`daemon start` still prints "daemon started (5 camera(s))" (registry
count, not live devices) and `daemon status` says "running".

The working daemon runs from the repo venv (Python 3.13, full daemon
deps).

## Fix

```sh
aprilcam daemon stop
cd /Volumes/Proj/proj/RobotProjects/aprilcam
uv run aprilcam daemon start
```

Verify: `pgrep -fl "python.*-m aprilcam.daemon"` should show
`/Volumes/Proj/proj/RobotProjects/aprilcam/.venv/bin/python3`, and
`camera list` shows `present`/`usable`.

Remember the side effects of any daemon restart: client-registered
per-session tag mounts are dropped (clients re-register on reconnect),
and open views/streams die and must be reopened. On-disk camera
configs (exposure etc.) re-apply automatically.

Durable fix is filed:
`clasi/issues/later/daemon-start-should-fail-loudly-without-capture-backend.md`.

## Related wedge: "no frame available" from one camera, device still on USB

Seen 2026-08-23 on camera 5 (OV9281) after a session of repeated config
pushes + captures while a view streamed: `camera list` shows the camera
`present`/`unusable`, `get_frame`/`get_tags` error
"no frame available", `system_profiler` still lists the device, and
other cameras work. `camera probe` flips it back to `usable` but reads
still return nothing — the daemon process's AVFoundation capture
session for that device is wedged and only a **daemon restart** (per
the recipe above) recovers it. Views subscribed to the wedged camera
hang waiting for a first frame; kill and reopen them after the restart.

### Recurrence 2026-08-26 on camera 2 (`hd-usb-camera`) — three refinements

Same wedge, different camera, ~8 hours after a clean daemon start.
`camera image 2` and a `camera view 2` both failed with
**"no frame available from camera '2'"** while cameras 1, 3 and 5
captured normally and `system_profiler` still listed the device.

1. **`present`/`usable` does NOT mean "delivers frames."** Camera 2 read
   `present`/`usable` throughout — not `unusable` as the original note
   above reports. `discovery._probe_openable()` does `open()` then
   `close()` and never reads a frame, so a device whose capture session
   is wedged still probes `usable`. Do not use `usable` to rule the
   wedge out; only an actual capture does that.
2. **Killing the subscriber does not clear it.** A hung
   `aprilcam camera view 2` held both stream sockets
   (`stream-images-{raw,deskewed}-2.sock`). Killing that process left
   the wedge fully intact — the very next `camera image 2` failed
   identically. Only the daemon restart recovered it. So the hung view
   is a *symptom*, not the cause; don't stop after killing it.
3. **`camera probe` is not a diagnostic here.** With any stream live it
   reports every camera as `in use by a live subscriber` and skips
   them, and its skipped rows print `playfield=-`/`uncalibrated`, which
   looks alarmingly like config loss but is just probe's transient view.

Recovery was the standard one, and it worked:

```sh
aprilcam daemon stop && aprilcam daemon start
```

Afterwards camera 2 returned a real frame and detected exactly ArUco
1-4 (its documented expected set for the secondary playfield). As with
camera 3, `get_tags` was empty at `frame_index` 0 and 1 and populated
at 2 — poll until the index advances before concluding anything.


## Recurrence 2026-08-26 — and the durable fix: install the pipx build *with* the extra

Same symptom again (all six cameras `absent`/`unusable`, `camera view 3`
hanging instead of erroring). But the cause was **narrower than "no
OpenCV", and the diagnosis above would have missed it**:

`cv2` *was* installed in the pipx venv (opencv-contrib-python 5.0.0.93).
The module actually missing was **`cv2-enumerate-cameras`**. It is a
`[daemon]`-extra dependency, and `pipx_metadata.json` showed the install
had been done as:

```
pipx install /Volumes/Proj/proj/RobotProjects/aprilcam --force-reinstall
```

— local path, **no `[daemon]` extra**, no injected packages. Every other
daemon-extra dep (`mss`, `websockets`, `fpdf2`, `msgpack`,
`grpcio-reflection`) was missing too. `cv2` was present only because the
installed version (0.20260823.6) predated sprint 036 moving
opencv-contrib-python out of `[project.dependencies]`.

The failure is silent because `camera/identity.py`'s `_enumerate_cv2()`
catches the `ImportError` with a bare `pass` and returns `{}`;
`resolve_all()` iterates that empty table and returns `{}`; discovery
then finds zero hardware keys and marks *every* registry camera absent.

**The tell is that ALL cameras read `absent`, not one.** A genuinely
unplugged camera cannot produce that. Two agents lost time on this
assuming a cable problem.

`camera view <n>` then *hangs* rather than erroring: the CLI connects
fine, opens the window, and subscribes to `stream_images` for a camera
the daemon believes is absent, so no first frame ever arrives.

### Fix (preferred over the `uv run` workaround above)

Install the pipx build with the extra, so `daemon restart` spawning the
PATH `aprilcam` is no longer a trap:

```sh
pipx install --force '/Volumes/Proj/proj/RobotProjects/aprilcam[daemon]'
aprilcam daemon stop && aprilcam daemon start
```

Verify all seven land — a missing one is the bug recurring:

```sh
SP=~/.local/pipx/venvs/aprilcam/lib/python3.14/site-packages
for p in mss websockets fpdf msgpack grpc_reflection cv2 cv2_enumerate_cameras; do
  [ -e "$SP/$p" ] && echo "PRESENT $p" || echo "MISSING $p"
done
```

Then `aprilcam daemon status` should show the physical cameras
`present`/`usable`. Note `get_tags` returns empty on the first call
after a restart (`frame_index: 1`, detection loop still cold) — query
again before concluding detection is broken.

Do **not** `pipx install cv2-enumerate-cameras` on its own: that creates
an isolated app venv (it was present on this host, exposing `lscam`)
which `aprilcam` can never import from. Use the extra, or
`pipx inject aprilcam <pkg>`.

## `daemon start`/`restart` fails with "exited before becoming ready" while a foreground run works (fixed 2026-09-03)

Cause: `client/lifecycle.py`'s launcher waited only 10 s for the child's
control socket, but the daemon probes every camera (open + settle reads)
*before* opening the socket -- measured 11.1 s on this six-camera rig
even with every open refused, longer when they succeed -- so the launcher
killed a healthy child every time. `python -m aprilcam.daemon` in the
foreground has no such bound, which is why it "worked". The default is
now 90 s (a dead child is still caught immediately via `poll()`). If the
error ever comes back with no stderr detail, suspect the bound first.

### Recurrence 2026-09-04 on camera 2 (`hd-usb-camera`), same signature

`camera view 2` hung while camera 3 viewed fine. `camera list`: camera 2
`present unusable` (self-heal reopen budget spent); `camera image 2`
fails fast with "no frame available"; `camera probe` flipped it to
`usable` and reported "in use by a live subscriber" (the hung view's
stream subscription), but reads still failed and it flipped back to
`unusable` within seconds. As before, only a daemon restart recovers
it; close the hung view first (its subscription dies with the daemon
anyway). The new control watchdog does not help here -- the controls
read fine, the capture session itself is dead. Durable fix is still
`clasi/issues/camera-capture-session-wedge-is-silent-and-needs-a-daemon-restart.md`.

## As of sprint 038: the wedge now self-heals, without a daemon restart

Sprint 038 gave every opened camera its own capture worker process
(`camera.backends.worker_backend.WorkerBackend`) instead of driving
`cv2.VideoCapture` in-process — the root cause of every recurrence
above was that the AVFoundation capture-session wedge is **process-local**,
so no in-process reopen strategy (delays, backoff, fresh backend
objects) could ever clear it; only a process boundary does. On top of
that, `DaemonService`'s existing control-drift watchdog thread now also
runs `check_stalled_cameras()` (a worker's heartbeat gone stale drives
the same close()+open() respawn `Camera`'s demand-driven self-heal
would eventually trigger, but proactively, even with nobody reading
that camera) and, on a slower interval
(`STALLED_CAMERA_RETRY_INTERVAL_S`, `daemon/service.py`), retries a
camera whose reopen budget was fully spent instead of leaving it dead
until a restart. In practice: a wedge like the ones documented above
should now clear itself within roughly the watchdog interval or the
exhausted-retry interval, with no operator action, on any camera using
the worker backend.

**`WorkerBackend` is the default on macOS as of this sprint** (Linux/Pi
still defaults to `OpenCVBackend`). To force the old, pre-038
in-process `cv2.VideoCapture` behaviour back on for diagnosis or
comparison — e.g. to confirm a new symptom is or isn't this same
process-local wedge — set `APRILCAM_CAPTURE_BACKEND=opencv` in the
daemon's environment before starting it:

```sh
APRILCAM_CAPTURE_BACKEND=opencv uv run aprilcam daemon start
```

(`APRILCAM_CAPTURE_BACKEND=worker` forces the new backend explicitly on
any platform; any other/unset value uses the per-platform default.)
With `opencv` forced, every recurrence recipe above (kill the hung
view, then `aprilcam daemon stop && aprilcam daemon start`) still
applies unchanged, since none of sprint 038's self-healing exists for
that backend.

**First soak on the sprint-038 build (2026-09-05, daemon 0.20260904.1, worker
backend):** a raw image stream held on camera 2 for 75 minutes at a steady
12.6-14.1 fps, with a one-shot `get_frame` every minute (75/75 OK) and zero
stall/respawn/reopen events in the daemon log. Every earlier wedge on this
camera arrived within 15-50 minutes of a stream starting. Not proof the
wedge cannot recur inside a worker -- only that if it does, the respawn
path is now armed to catch it within ~15 s; watch the log for
`check_stall`/respawn lines.
