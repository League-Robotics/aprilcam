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
