# The daemon's camera access is inherited — never restart it from an agent

**Learned the hard way:** 2026-08-23, sprint 035.

## Rule

**Only start the daemon from the operator's Terminal. No agent may run
`aprilcam daemon start`, `stop`, or `restart`.**

If the daemon dies or wedges, an agent must STOP and ask the operator to
restart it. Recovering it is a ten-second human action; an agent
"helpfully" restarting it silently breaks every camera on the machine.

## Why

macOS TCC grants camera access to the *responsible process*, and a child
process inherits that attribution. The daemon has no grant of its own —
it borrows one from whatever launched it.

- Launched from **Terminal.app** (which is normally authorized for
  camera): the daemon can open cameras.
- Launched from **Claude Code**: it cannot. Claude Code runs from a
  nested helper bundle
  (`~/Library/Application Support/Claude/claude-code/<ver>/claude.app`)
  with no `NSCameraUsageDescription`, so macOS refuses the request
  outright and never even adds an entry to
  System Settings → Privacy & Security → Camera. There is no toggle to
  find and no way to grant it from that pane.

Running `aprilcam daemon restart` from an agent therefore *replaces a
working daemon with a broken one*. That is exactly what happened during
sprint 035: cameras were fine, an agent restarted the daemon to clear an
unrelated wedge, and every camera open began failing until the operator
re-ran the same command from Terminal.

Note this is a **separate TCC service from file access**. Granting
Claude Full Disk Access does nothing for the camera.

## The misleading symptom

**Enumeration keeps working while opens fail.**
`aprilcam daemon status` will happily report every camera as
`present usable`, because device discovery does not require the camera
grant. Only the open fails:

```
error: daemon at 'localhost' did not answer: failed to open camera at index 3
```

Direct opens surface the real cause; the daemon path hides it:

```
OpenCV: not authorized to capture video (status 0), requesting...
```

**Consequence for tests:** a rig-availability check built on
enumeration will report the rig present and then fail at open. Gate
hardware tests on an **open or a frame fetch succeeding**, never on
enumeration alone.

## Agents do not need camera access anyway

The daemon is the sole vision authority (sprint 015). Everything an
agent needs is available through it from an ordinary, unprivileged
process — no camera grant, no direct device access:

| Need | Command |
|---|---|
| Camera inventory / health | `aprilcam daemon status` |
| Detections with world coords | `aprilcam camera tags <n>` |
| Frame as JPEG | `aprilcam camera image <n> --output <path>` |
| Read live config | `aprilcam camera config --dump <n>` |
| Set exposure/gain | `aprilcam camera config <file.json>` |
| Undo a pushed setting | `aprilcam camera config --reset <n>` |

Or in-process via `aprilcam.client` (`Discovery().connect()` → typed
`Daemon`), which covers the same surface.

Verified: pushing `exposure=120` to camera 5 moved frame mean brightness
from 115.3 to 205.9, and `--reset` restored it. A pushed config is a
**live override** — it does not modify on-disk config files, so it is
reversible and safe, but it also means a file-hashing guard sees nothing.

If you catch yourself building scaffolding to open a camera directly
(driving Terminal over AppleScript, say), stop: you are working around
the architecture instead of using it.

## Detection needs priming — one poll proves nothing

After a daemon restart or a fresh camera open, **the first poll returns
zero tags.** Detections must survive the confirmation streak
(`CONFIRMATION_STREAK`, with sprint 034's `CONFIRMATION_GRACE = 8`)
before they are reported.

Measured on camera 3 immediately after a restart:

| Poll | Tags |
|---|---|
| 1 | 0 |
| 2 | 10 |
| 3-5 | 11 (complete) |

Always prime with several polls before recording a baseline count. This
is the most likely explanation for the July capture run's "baselines
detected zero AprilTags" — the tool sampled once per configuration and
recorded the cold-start zero.

## Visibility is per-camera, not global

There is no single "field state" that all cameras share. Measured
2026-08-23 with the full field set up:

| Camera | Playfield | Sees |
|---|---|---|
| 3 `arducam-ov9782-usb-camera` | main | AprilTag 1 + ArUco 1-11 |
| 5 `arducam-ov9281-usb-camera` | main | AprilTag 1 + ArUco 2,3,4,5,6,8,9,10 (not 1, 7) |
| 2 `hd-usb-camera` | **secondary** | ArUco 1,2,3,4 only, no AprilTag |

Camera 2 genuinely has few markers in view — that is the physical
layout, not a fault, and its 4-marker calibration reports a ~0
reprojection error only because four points fit a homography exactly.
Treat that residual as meaningless, not as precision.

Expected-detection sets in tests must therefore be **per camera**.
