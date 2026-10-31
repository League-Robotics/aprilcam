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

An uncalibrated record's only orientation reading is `image_yaw_deg` —
a diagnostic, **image-pixel-space** field: `0°` means the tag's front
edge points toward the top of the image, and the angle increases
**clockwise as seen on screen**. This is a different field, and a
different rotational sense, from the world-frame `yaw_rad` you get once
a camera is calibrated (`yaw_rad` is CCW-positive, §1) — do not mix the
two or assume one is a stand-in for the other.

Watching an uncalibrated camera's tags with a single `aprilcam camera
tags <camera>` call right after any gap often reports "(no tags
detected)" even with tags plainly in view — the daemon's multi-frame
confirmation filter has to rebuild its confirmed streak from zero on
each isolated call. `aprilcam camera tags <camera> --follow` streams
continuously instead (one row per tag per frame, until Ctrl-C) and lets
that streak build up, which makes it the reliable way to watch an
uncalibrated camera's tag rotation over time — a one-shot call is not.
Add `--json` for JSON Lines output (one object per line, `image_yaw_deg`
included) if you are piping the stream into another process rather than
reading it yourself.

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

## 6. Mounts Persist; Annotations Are Still Per-Session

Two kinds of state used to both live only in the daemon's memory and
vanish on every restart. That is no longer true for one of them — get
this distinction right, or you'll either redo work that is no longer
necessary or trust a stale annotation that already vanished:

- **Mount registrations** (`aprilcam tags mount register`) — if you
  want a tag's reported position to be the *robot's* position
  (correcting for the tag's height and its offset from the robot's own
  reference point) rather than the raw tag's position, you register a
  mount for that tag. **This now survives a daemon restart**: the
  registration is written to `state_dir/mounts/registry.json` and
  reloaded automatically the next time the daemon starts. Register a
  tag once and it stays registered — there is no re-registration
  ritual at the start of every session. The only way to remove a
  registration is `unregister_tag` (`aprilcam tags mount clear`); a
  restart no longer clears one for you, so a tag re-mounted on a
  different robot needs an explicit `clear` first, not just a wait for
  the next restart.
- **Annotations** (drawings pushed to a playfield, visible to every
  other client) are **unaffected by this change and remain
  per-session** — held only in the daemon's memory, gone on every
  restart, same as always. You must still re-draw them at the start of
  every session that needs them.

`aprilcam tags mount list` still shows what is currently registered —
unaffected by this change, and worth checking if you're unsure whether
a registration you expect is actually in effect, whether it came from
this session or was reloaded from a previous one.

> Per-session rule: mount registrations (UC-12) now persist across a daemon restart (written to state_dir/mounts/registry.json, reloaded at startup) and are removed only by an explicit unregister_tag/`aprilcam tags mount clear` -- annotations (UC-28) remain per-session and must still be re-drawn at the start of every session that needs them.

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

## 7. Detection Cadence and Tag History

Detection is **demand-driven, not continuous, by default.** An idle
camera runs zero detections — `get_tags`/`get_tag` each trigger exactly
one detection burst on that call, synchronously, and nothing else in
the daemon ever runs one on its own. That has a direct consequence for
`get_tag_history` (UC-26): its 300-frame ring buffer holds the last 300
*detection bursts*, not the last 300 seconds. Those two are the same
thing — roughly 10 wall-clock seconds of continuous history — only
while at least one `stream_tags` subscriber is active on that camera,
which drives bursts at a steady cadence for as long as the subscriber
lasts. With nobody streaming, the buffer still fills, but only as fast
as something polls it: consecutive frames in the returned history can
be seconds, minutes, or hours apart.

This is intentional architecture, not a bug or a stall — but it means
**never assume evenly-spaced history frames.** Each `TagFrame` in the
history carries its own `timestamp` and `frame_index`; those, not the
frame's position in the returned sequence, are the only authority on
how far apart two frames actually were. Before computing velocity,
heading, or any other rate from consecutive history frames, check their
timestamps — or start a `stream_tags` subscription first, which is the
only way to guarantee continuous, evenly-paced history.

> Cadence summary: detection is demand-driven (idle = zero detections); `get_tag_history`'s 300-frame buffer means ~10 wall-clock seconds only while a stream subscriber is active, otherwise gaps can be minutes or hours — trust each frame's own `timestamp`/`frame_index`, never assumed spacing.

**Motion-field interpretation.** Once a tag has two or more world
samples on file, each new `TagRecord` attempts to fill in
`heading_rad`, `world_velocity`, and `speed` from the two most recent
of them. When those two samples are too close together to tell real
motion from ordinary detector jitter, **all three come back `None`
together** — not just the direction fields. `speed is None` here means
the same thing `heading_rad is None` has always meant: "the
displacement between these two samples is below the reliable noise
floor" (the same small, sub-centimeter threshold `heading_rad` uses),
not "the daemon failed to compute a speed." This matters most at tight
query cadence: two samples close together in time turn a small,
constant amount of jitter into a proportionally larger `jitter / dt`,
so a fast poll is exactly the case where a stationary tag would
otherwise look like it is creeping — trust the `None`, don't treat it
as a dropped measurement.

## 8. Command / Capability Overview

Every capability below is reachable identically from the CLI, from the
Python client library (`aprilcam.client.discovery.Discovery().connect()`
returns a `Daemon`), and from the MCP server (`aprilcam [-d HOST] mcp`)
— an MCP tool call for each CLI command below, same behaviour, same
world-frame and staleness rules as everywhere else in this guide —
**except `camera locate`, marked below, which is CLI-only** (§6).

| Area | Command | What it does |
|---|---|---|
| Daemon | `aprilcam daemon start\|stop\|restart\|status` | lifecycle |
| Camera | `aprilcam camera list` | enumerate cameras |
| | `aprilcam camera probe` | force a deep hardware re-probe |
| | `aprilcam camera image <camera>` | capture one raw frame |
| | `aprilcam camera tags <camera> [--follow] [--json]` | current tag detections, or a continuous stream (§4) |
| | `aprilcam camera calibrate <camera>` | calibrate against the linked playfield (§3) |
| | `aprilcam camera flatfield <camera>` | build (or `--clear`) a flat-field gain-map lighting correction (§9) |
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

## 9. Flat-Field Gain-Map Capture (Optional, but fixes bad colour/threshold reads)

Room lighting over a playfield is rarely uniform — one corner can read
at half the brightness of the centre, and colour channels can drift
unevenly (a magenta or green cast that is worse in some areas than
others). Eyes adapt to this automatically; a colour or brightness
threshold does not. A **flat-field gain map** is a per-pixel,
per-channel correction, captured once per camera, that flattens this
out before every frame that camera reports — detection, `get_frame`,
streaming, and calibration capture all see the corrected image.

**When to run it** — any time after a camera is linked to a playfield
(§3 step 3); calibrating first (step 4) gives a more precise
field-interior mask to build the correction against, but is not
required — an uncalibrated-but-linked camera's playfield ArUco corners
are detected live instead. Re-run it whenever the camera is remounted
or the lighting changes materially — an existing map is only valid for
the mounting/lighting state it was captured under (`get_flatfield`'s
`stale` flag is advisory, never automatic; nothing currently detects
staleness for you).

**How** — lay a plain white or light-coloured sheet over the whole
field first; the map is built from how unevenly *that* reads back, so
capturing over an empty or non-reference scene produces a poor
correction even on success. Then call `capture_flatfield(camera)` (CLI:
`aprilcam camera flatfield <camera>`). A white sheet saturates most
cameras at their normal working exposure, so the daemon needs a low
exposure for the capture burst — pass one via the optional `exposure`
parameter if the default does not work; whatever you pass is a
*temporary* override for the burst only, and the camera's prior
configuration is restored afterward regardless of outcome. `frames`
controls how many frames are averaged (daemon default if omitted).

Like `calibrate`, there is no partial success: either every
quality-gate check on the measured lighting spread passed and the map
was built and persisted, or the attempt was refused
(`FlatFieldResult.reason` names which check) and any prior-good map is
left untouched. `force=True` persists anyway, naming what it
overrode. `dry_run=True` evaluates and reports without writing
anything. A camera with no derivable field mask at all (no calibration
*and* no linked playfield) fails with a precondition error before any
capture is attempted, the same way `calibrate` fails fast on
`NoPlayfieldLinked`.

**Checking state** — `get_flatfield(camera)` returns the current map's
metadata (`present`, `stale`, when it was captured), or `None` if this
camera has never had one built. `list_cameras` also carries
`flatfield_present`/`flatfield_stale` on every `CameraState`, the same
way `calibrated`/`calibration_stale` already report calibration —
check there first if you just want a quick yes/no across every camera.
`clear_flatfield(camera)` deletes the map and drops the live-applied
correction immediately, no daemon restart needed.

> Flat-field summary: optional per-camera lighting correction, captured with a white sheet over the field and (usually) a temporary low-exposure override; `capture_flatfield`/`get_flatfield`/`clear_flatfield` mirror `calibrate`/`get_calibration`'s explicit-failure-is-data and staleness-is-advisory contracts exactly.

### Lens intrinsics with a ChArUco board

To calibrate a camera's lens, a human holds a printed ChArUco board while
you drive the session: `start_intrinsics(camera, board=None)`, then poll
`get_intrinsics_status(camera)` about once per second. Read `next_hint`
and relay it to the human; `banner` is `show_board`, `keep_going` or
`good_enough`. Call `finish_intrinsics(camera, install=True)` only at
`good_enough` (a not-good solve returns `installed: false` with a
`reason`; `force=True` overrides, only with human approval). Always call
`abort_intrinsics(camera)` if you give up. A session that is not polled
for 120 s aborts itself.

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
