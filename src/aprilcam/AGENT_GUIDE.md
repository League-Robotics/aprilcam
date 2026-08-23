# AprilCam Agent Guide

You are working with **AprilCam**: a camera daemon plus a client
library and CLI for AprilTag/ArUco fiducial detection, playfield
homography, and world-coordinate reporting on robotics playfields.

This text is packaged with the software and answers "what is this and
how do I use it" **before anything is configured, before a daemon is
even running.** Read it first, before making any tool call.

## 1. World Frame (read this before touching any coordinate)

- **Origin** — A1-centred: the surveyed centre AprilTag (AprilTag 1)
  sits at world `(0, 0)`.
- **Axes** — `+x` is **east**, `+y` is **north**. Units are
  **centimetres**. The origin shift onto A1 is a plain subtraction on
  both axes, never a y-flip.
- **Heading / yaw** — `atan2(vy, vx)`; **0 = east, counter-clockwise
  positive**. A tag's own facing direction (`yaw_rad`) uses the same
  convention; motion direction (`heading_rad`) is derived from
  velocity and can differ from facing (e.g. a robot driving backwards).
- Never infer the frame from observed data — it is fixed, and it is
  stated here so you don't have to guess and risk a sign error.

> World-frame summary: A1-centred, +x east, +y north, units cm; heading 0 = east, counter-clockwise positive (CCW positive).

## 2. Tag Addressing: family + number, never a bare id

Every tag is identified by **family and number together** —
`TagFamily.APRILTAG` or `TagFamily.ARUCO` plus an integer number, never
a number alone. AprilTag 1 and ArUco 1 are two different tags, and a
single playfield may legitimately hold both at different positions.
Numbers are always **non-negative** in every request and response —
there is no negative tag id anywhere in this interface, even though the
daemon may use one internally to disambiguate families on its own side.

## 3. Bring-Up Order

Nothing answers a world-coordinate query until this sequence has
happened, in order:

1. **Start the daemon** (`aprilcam daemon start`) — it enumerates
   attached cameras but knows nothing about a playfield yet.
2. **Push or load a playfield definition** (`aprilcam playfield
   config <file>`, or install the definition file where the daemon
   reads its on-disk configuration directory) — the daemon now has
   surveyed ground truth to detect features against and to calibrate
   cameras with.
3. **Configure a camera** (`aprilcam camera config <file>`) — link a
   camera to that playfield definition.
4. **Calibrate** (`aprilcam camera calibrate <camera>`) — maps that
   camera's pixels to the playfield's world coordinates. Dimensions
   come from the linked playfield definition; nothing is measured by
   hand.
5. **Confirm ready** (`aprilcam camera list` / `aprilcam daemon
   status`) — a camera reporting `calibrated: true` is ready to answer
   world-coordinate queries.
6. **Query** — `aprilcam camera tags <camera>`, `aprilcam camera image
   <camera>`, `aprilcam playfield where <name> <query>`, and so on.

Skipping a step does not fail loudly everywhere — it degrades. An
uncalibrated camera still answers tag queries, just with pixels instead
of world coordinates (next section).

> Bring-up order: UC-01 start daemon -> UC-02 push or load playfield -> UC-03 configure camera -> UC-04 calibrate -> UC-05 confirm ready -> UC-06 onward query.

## 4. What "Uncalibrated" Means

A camera that has not completed step 4 above still detects tags and
answers queries — it just cannot report world coordinates. Every tag
record carries a `calibrated` flag and a `world` field that is
`Optional`; on an uncalibrated camera `calibrated` is `false` and
`world` is **always `None`** — never a fabricated `(0, 0)`, and never a
value silently computed from a homography that does not exist. `pixel`
is the only usable position in that state. Treat an uncalibrated result
as pixel-only diagnostic data, never as a stand-in for world
coordinates.

## 5. Surveyed Definition vs. Live Vision

Two questions look similar but are answered from different sources:

- **"Where is tag 7 right now?"** is answered from **live vision** —
  the daemon's current camera frame. No detection this frame, no
  answer (an explicit not-detected, distinct from a position at
  `(0, 0)`).
- **"Where is the blue dot?"** / **"where is `<named feature>`?"**
  (`aprilcam playfield where <name> <query>`) is answered from the
  **surveyed playfield definition**, not from a camera. It needs no
  calibration and opens no camera — but it also means a feature is
  reported exactly where it was surveyed even if it has since been
  physically moved or removed from the field. That is a real
  limitation, not a bug: the definition is ground truth for this
  query, never live observation.

## 6. Mounts and Annotations Are Per-Session — Re-register Every Time

**This is the rule most likely to bite you.** Two kinds of state live
only in the daemon's memory and vanish on **every** daemon restart:

- **Mount registrations** (`aprilcam tags mount register`) — if you
  want a tag's reported position to be the *robot's* position
  (correcting for the tag's height and its offset from the robot's own
  reference point) rather than the raw tag's position, you register a
  mount for that tag. That registration does **not** survive a daemon
  restart.
- **Annotations** (drawings pushed to a playfield, visible to every
  other client) likewise do not survive a restart.

If you register a mount once and assume it persists across sessions,
you will silently get the *raw tag's* pose back instead of the
*robot's* pose after any daemon restart — with no error and no
warning, just a plausible-looking number that is wrong. **Re-register
your mount (and re-draw your annotations) at the start of every
session that needs them, not just once ever.** `aprilcam tags mount
list` shows what is currently registered if you need to check before
trusting a reported position.

> Per-session rule: mount registrations (UC-12) and annotations (UC-28) do not survive a daemon restart -- re-register and re-draw them at the start of every session that needs them.

**A second, independent precondition — calibrated is not the same as
located.** A nonzero `mount_z` (the tag's height above the field,
driving the parallax correction) needs the *observing* camera to have
been **located**, not merely calibrated. `register_tag` only refuses a
nonzero `mount_z` outright when the daemon holds no calibration for
*any* camera at all — a calibrated-but-never-located camera passes
that check and accepts the registration, but silently keeps reporting
the tag's raw, unelevated position: `mount_x`/`mount_y`/
`mount_yaw_rad` still apply normally, only the height correction is
inert. This is a plausible-looking number that will not announce
itself as wrong unless you check for it.

Two ways to check: `register_tag`'s own response carries a warning at
the moment of registration when this precondition is not met, and
`list_tag_parameters` reports a `mount_z_applied` flag per
registration so you can check afterward too, without cross-referencing
camera config by hand. Both are informational only — registration
always succeeds either way.

**There is no MCP tool for locating a camera.** Locating
(`aprilcam camera locate`) is CLI-only, by design (physically placing
elevated reference tags on stands at surveyed positions is a hands-on
operator task, not something an agent can do remotely). If you need
`mount_z` to actually apply and are not sure the observing camera has
been located, **ask your operator to run `aprilcam camera locate`**
against it — you cannot do this yourself over MCP. Locating is also
not guaranteed to succeed on the first try: a camera mounted very
close to straight-down (near-nadir) can make the solve numerically
ill-conditioned, so do not assume it always just works.

## 7. Command / Capability Overview

Every capability below is reachable identically from the CLI, from the
Python client library (`aprilcam.client.discovery.Discovery().connect()`
returns a `Daemon`), and from the MCP server (`aprilcam mcp [-d HOST]`)
— an MCP tool call for each CLI command below, same behaviour, same
world-frame and staleness rules as everywhere else in this guide —
**except `camera locate`, marked below, which is CLI-only** (§6).

| Area | Command | What it does |
|---|---|---|
| Daemon | `aprilcam daemon start\|stop\|restart\|status` | lifecycle |
| Camera | `aprilcam camera list` | enumerate cameras |
| | `aprilcam camera probe` | force a deep hardware re-probe |
| | `aprilcam camera image <camera>` | capture one raw frame |
| | `aprilcam camera tags <camera>` | current tag detections |
| | `aprilcam camera calibrate <camera>` | calibrate against the linked playfield (§3) |
| | `aprilcam camera locate <camera> --reference TAG:SLUG:HEIGHT_CM ...` | solve real ground position + height from staged elevated references — **CLI-only, no MCP tool** (§6) |
| | `aprilcam camera config <file>` | push a camera config document |
| | `aprilcam camera config <camera> --dump\|--reset` | read the live config, or discard the pushed override |
| | `aprilcam camera view <camera>` | live raw view |
| Playfield | `aprilcam playfield list` | enumerate playfield definitions the daemon holds |
| | `aprilcam playfield config <file>` | push a playfield definition |
| | `aprilcam playfield config <name> --dump` | read the live definition |
| | `aprilcam playfield image <name>` | one deskewed, calibrated frame |
| | `aprilcam playfield view <name>` | live deskewed view |
| | `aprilcam playfield annotations <name> [--clear]` | list or clear drawings (§6) |
| | `aprilcam playfield where <name> <query>` | surveyed-feature lookup (§5) |
| Tags | `aprilcam tags mount register\|clear\|list` | mobile-tag mount registry (§6) |
| Guide | `aprilcam guide [agent\|robot]` | this text |

If you are writing a robot control program rather than working
interactively, read the companion **`robot`** guide next
(`aprilcam guide robot`, or `Guides().get("robot")` in Python) — it
covers the high-frequency Python path in more depth, including worked
examples for reading tags and registering a mount.

---
*Packaged with the software (`aprilcam.guides`), read identically with
no daemon running (`client.guides.Guides`) or over the wire
(`Daemon.get_guide`) — both read this same file, so the two can never
disagree about what it says.*
