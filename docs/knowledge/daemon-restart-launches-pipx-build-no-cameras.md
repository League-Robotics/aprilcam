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
