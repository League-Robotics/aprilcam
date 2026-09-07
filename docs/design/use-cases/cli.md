# Use Cases — CLI

**Status:** IN PROGRESS — drafted 2026-07-29, awaiting review
**Index:** [README.md](README.md) — jobs, actors, conventions, worked example

The `aprilcam` command-line interface. Every command here is a **thin
daemon client or a local utility** — the CLI never opens a camera and
never does vision work itself (see [specification.md](../specification.md)
§2). Per the process rule in [process.md](../process.md), every capability
the CLI exposes is also reachable from the Python module; the CLI is a
shell over the module, never the other way round.

Primary actor throughout is **A-03, the human developer**, except daemon
lifecycle (UC-13) which is **A-04, the daemon operator**.

---

## Command surface

```
aprilcam daemon list                      # discover daemons via mDNS
aprilcam daemon start|stop|restart|status # lifecycle, local by default
aprilcam camera list                      # cameras on a daemon
aprilcam camera probe                     # interrogate hardware, derive names
aprilcam camera calibrate <camera>        # requires a playfield
aprilcam camera view <camera>             # live raw video
aprilcam camera image <camera>            # one raw frame
aprilcam camera config <file>             # push a camera config (self-naming)
aprilcam camera config <cam> --dump       # read back live camera config
aprilcam playfield view                   # deskewed, calibrated, tags drawn
aprilcam playfield image                  # one deskewed frame
aprilcam playfield tags                   # list detected tags (DEFERRED -- see UC-20)
aprilcam camera tags <camera>             # list detected tags, camera-addressed (sprint 021)
aprilcam playfield config <file>          # push a playfield definition
aprilcam playfield config <name> --dump   # read back live definition
aprilcam playfield annotations            # list the annotation store (UC-28)
aprilcam playfield annotations --clear [--layer NAME]  # clear all / one layer
aprilcam tags generate                    # make AprilTag/ArUco images
aprilcam mcp                              # run the MCP server
```

## Global conventions

### Daemon targeting — `-d` / `--daemon`

Every command that talks to a daemon accepts `-d HOST` (long form
`--daemon HOST`). **Omitted means local.** This replaces v1's
host-enumeration behaviour, where `aprilcam cameras` listed cameras
across every discovered host at once — that is gone; you name the daemon
or you get the local one.

`-h` remains `--help`, as everywhere else in Unix. The daemon flag is
`-d` precisely so that `aprilcam camera list -h` prints help rather than
erroring about a missing hostname.

### Fan-out — `--all`

Daemons advertise themselves over mDNS, so the CLI can address more than
one. Fan-out is restricted to **read-only commands**:

| Fan-out allowed | Single daemon only |
|---|---|
| `daemon list`, `daemon status` | `daemon start`, `stop`, `restart` |
| `camera list`, `camera probe` | `camera calibrate` |
| `camera tags`[^tags-spelling] | `camera view`, `playfield view` |
| `camera image`, `playfield image` | `camera config`, `playfield config` (push) |
| `playfield annotations` (list) | `playfield annotations --clear` (any annotation write) |
| `config --dump` | — |

[^tags-spelling]: `camera tags <camera>` joins `camera image`/`camera
list`/`camera probe` in the read-only, fan-out-eligible group as of
sprint 021. `playfield tags` was the originally drafted spelling for
this row (client-side camera resolution via `CameraState.playfield`)
and remains the eventual form once playfields exist — deferred, not
dropped; see UC-20's command-spelling note.

The rule: **nothing that changes state or streams interactively fans
out.** Calibrating every camera on the network should not be one typo
away, and a live video window has no sensible multi-daemon meaning.
Attempting `--all` on a restricted command is an error naming the
restriction, not a silent single-target fallback.

When a command does fan out, **every row is labelled with its daemon**,
and one unreachable daemon does not fail the whole command — it is
reported as unreachable alongside the results that did come back.

### Playfield commands resolve their camera

`playfield view`, `playfield image`, and `playfield tags` name no
camera, but the daemon API is **camera-addressed, always** — the UC-06
decision stands, with no daemon-side shortcut. The resolution is the
**CLI's, client-side** (resolved 2026-07-31): it reads the camera→
playfield links from `CameraState`; exactly one linked, present camera
→ use it; zero or several → error naming the linked cameras, with
`--camera` to disambiguate. The implicitness lives in the shell, where
a human can see and correct it — never in the daemon, where it would
become a wrong answer that looks right.

### Camera addressing — number or slug

Every camera-addressed command (`camera image`, `camera calibrate`, and
every future one, including this sprint's `camera tags`) accepts either
form a camera can be named: **a decimal number resolves the camera
currently holding that persistent registry number** — the same number
`camera list`/`camera probe` print in their leading `[N]` column —
**anything else resolves by slug**, exactly as before. There is no
ambiguity between the two: a slug always contains at least one
non-digit character (`camera/identity.py`'s `device_name_slug`
invariant), so an all-digits token can only ever mean a number.

Resolution happens once, daemon-side, in `_camera_for` — every adapter
(this CLI, the MCP server once it lands, a future ROS node, direct
robot programs) inherits the same rule automatically rather than each
reimplementing the lookup (`adapters.md` §2's duplication test). A
camera the registry remembers but that is not currently plugged in is
addressable by either form and fails with a distinct "camera present:
no" error, never "unknown camera" — a registry entry outliving its
hardware is the normal case (UC-25), not a rejection.

---

### UC-13 — Discover and manage daemons

**Job:** JTBD-05 · **Primary actor:** A-04 · **Also serves:** A-03
**Status:** draft
**Interfaces:** CLI, Python

**Goal.** The operator finds out which daemons exist on the network,
including whether one is running locally, and starts/stops/restarts the
local one.

**Preconditions.** None for `list`. Lifecycle commands assume the
operator can manage processes on the target machine.

**Main success path — discovery.**
1. `aprilcam daemon list` queries mDNS for the advertised service.
2. Output lists each daemon found: host, port, whether it is **this
   machine's** local daemon, and enough state to be useful — reachable,
   how many cameras, which playfields.

**Main success path — lifecycle.**
`aprilcam daemon start|stop|restart|status`, defaulting to the local
daemon. `status` reports whether it is running and its basic state.

**Postconditions.** For `list`/`status`, none — reads. For lifecycle, the
local daemon is running or stopped as requested.

**Failure modes.**
- No daemons found → empty list, not an error. A field with nothing
  running is a normal state.
- mDNS unavailable or blocked on the network → report that discovery
  itself failed, distinctly from "discovery worked and found nothing."
  These are very different situations for someone debugging.
- `start` when already running → refuse, naming the running instance
  (the UC-01 singleton rule).
- `stop` when not running → succeed quietly, or report not-running? Not
  an error either way.
- Lifecycle command with `-d` naming a remote host → the CLI cannot
  start a process on another machine. Refuse with a clear message
  pointing at SSH.

**Open questions.**
- Does `daemon status -d remote` work (query a remote daemon's status)
  even though `start`/`stop -d remote` cannot? Reading remote status is
  perfectly possible and useful; only process control is local-only.
  Drafted as: yes, `status` accepts `-d`, lifecycle does not.
- Should `daemon list` show daemons that advertise but do not answer?
  Yes — an advertising-but-dead daemon is exactly what you want to see.

#### Scenarios

**UC-13.S-1 — List daemons, one local and one on the field** · _happy path_
A daemon runs on the developer's laptop and another on a Raspberry Pi
over the field. `aprilcam daemon list` shows both, marks the laptop's as
local, and reports each one's camera count and linked playfields.

**UC-13.S-2 — No daemons running** · _happy path_
Nothing is running anywhere. The command reports an empty list and exits
zero. This is not an error.

**UC-13.S-3 — Local lifecycle round trip** · _happy path_
`aprilcam daemon status` reports not running. `start` starts it; `status`
now reports running with the camera count from UC-01. `restart` cycles
it — and per the UC-01 configuration model, any RPC-pushed config is
discarded, with the daemon coming back on its on-disk configuration.
`stop` stops it.

**UC-13.S-4 — Refuse to start a remote daemon** · _failure path_
`aprilcam daemon start -d pi-field.local` fails, explaining that process
lifecycle is local-only and that the operator should SSH to the host.
Nothing is started anywhere.

**UC-13.S-5 — mDNS blocked** · _failure path_
Multicast is filtered on the network. `aprilcam daemon list` reports that
discovery failed, **not** an empty list — the distinction matters because
an empty list would send the developer looking for a dead daemon instead
of a network problem. A daemon named explicitly with `-d` still works.

---

### UC-14 — List cameras on a daemon

**Job:** JTBD-05 · **Primary actor:** A-03
**Status:** draft
**Interfaces:** CLI, Python, MCP

**Goal.** The developer lists the cameras a daemon owns, locally or
remotely, with enough state to know what is usable.

**Preconditions.** A daemon is running on the target.

**Main success path.**
1. `aprilcam camera list` (local) or `-d pi-field.local` (remote).
2. Each camera is listed with its stable enumeration number and
   identity, its linked playfield if any, whether it is calibrated, and
   whether it is currently readable.

**Postconditions.** None — a read.

**Failure modes.**
- No daemon on the target → clear connection error naming the host,
  suggesting `aprilcam daemon list`.
- No cameras → empty list, not an error (UC-01.S-2).
- A camera present but unopenable → listed and flagged unusable, never
  silently omitted.

**Open questions.** None.

#### Scenarios

**UC-14.S-1 — List local cameras** · _happy path_
`aprilcam camera list` on the machine from UC-01.S-1 lists
`arducam-ov9782-usb-camera` and `hd-usb-camera` with their enumeration
numbers, showing the Arducam linked to `main-playfield` and calibrated,
the HD camera unlinked and uncalibrated.

**UC-14.S-2 — List remote cameras** · _happy path_
`aprilcam camera list -d pi-field.local` returns the Pi's cameras, not
the laptop's. Output is otherwise identical in form.

**UC-14.S-3 — Fan out across the network** · _happy path_
`aprilcam camera list --all` lists cameras from every discovered daemon,
each row labelled with the daemon it belongs to. This is the deliberate
replacement for v1's implicit multi-host listing — now opt-in and
labelled rather than the default.

**UC-14.S-4 — One daemon unreachable during fan-out** · _failure path_
Two daemons advertise; the Pi has been unplugged. `--all` returns the
laptop's cameras and reports the Pi as unreachable. The command does not
fail wholesale because one target is down.

---

### UC-15 — Calibrate a camera from the command line

**Job:** JTBD-06, JTBD-01 · **Primary actor:** A-03
**Status:** draft
**Interfaces:** CLI, Python, MCP

**Goal.** The developer runs calibration for a camera and gets a clear
success or a failure that says exactly what to fix.

**Preconditions.** Camera exists on the target daemon **and is linked to
a playfield**. The physical field is in view.

**A playfield is mandatory.** Calibration means "establish the mapping
between this camera's pixels and that field's coordinates." With no
playfield there is no coordinate system to map to and **nothing to
calibrate** — so the command **errors out** rather than attempting
anything. This is a precondition failure, not a calibration failure.

**Main success path.**
1. `aprilcam camera calibrate arducam-ov9782-usb-camera` (`-d` for
   remote).
2. The CLI triggers calibration **in the daemon** — it does no vision
   work itself — and reports progress.
3. Per UC-04, success requires **every ArUco fiducial the playfield
   definition specifies** to be detected. The CLI reports success, or
   failure naming the missing ids.

**Postconditions.** On success, the camera has a stored calibration and
world coordinates become available. On failure, nothing is stored and any
previous calibration is untouched.

**Failure modes.**
- Camera has no playfield link → **error**: nothing to calibrate.
- Not every specified fiducial visible → failure naming the missing ids
  (UC-04.S-2).
- Camera unreadable → error.
- `--all` → refused; calibration is single-daemon by the fan-out rule.

**Open questions.** Should the CLI offer to link a playfield inline when
one is missing, or strictly require a separate configuration step?
Drafted as: strictly separate — an interactive prompt in the middle of a
calibration run is how you end up with a camera pointed at the wrong
field.

#### Scenarios

**UC-15.S-1 — Successful calibration** · _happy path_
`arducam-ov9782-usb-camera` is linked to `main-playfield` with all 10
specified ArUco fiducials in view. The command reports success and which
ids were matched. `aprilcam camera list` afterwards shows the camera
calibrated.

**UC-15.S-2 — No playfield linked** · _failure path_
`hd-usb-camera` has no playfield link. `aprilcam camera calibrate
hd-usb-camera` exits non-zero saying the camera has no playfield and
therefore nothing to calibrate. No frames are captured and no
calibration attempt is made.

**UC-15.S-3 — Fiducials missing** · _failure path_
The Arducam is linked correctly but a robot covers ArUco 6. The command
fails, naming ArUco 6 as missing, and leaves the prior calibration
intact.

**UC-15.S-4 — Fan-out refused** · _failure path_
`aprilcam camera calibrate --all` is refused, explaining that calibration
targets exactly one daemon.

---

### UC-16 — Watch live raw video from a camera

**Job:** JTBD-02 · **Primary actor:** A-03
**Status:** draft
**Interfaces:** CLI, Python

**Goal.** The developer watches what a camera actually sees, with its
configured settings applied but **no processing** — the honest camera
image, for aiming the camera and judging exposure.

**Preconditions.** Camera exists and is readable. **No calibration
needed** — this is the view you use *before* calibrating.

**Raw means raw.** Camera settings (exposure, gain, white balance) are
applied because they are properties of the capture itself. Nothing else
is: no deskew, no homography, no tag overlays, no annotations. This is
deliberately the counterpart to `playfield view` (UC-18) — when a
deskewed view looks wrong, this is how you tell whether the problem is
the camera or the geometry. The rule is now load-bearing for the
annotation store (UC-28): annotations are world-coordinate-only and a
raw frame carries no world→pixel mapping to render them with, so raw
frames embed no annotation set — by contract, not merely by taste.

**Main success path.**
1. `aprilcam camera view arducam-ov9782-usb-camera` (`-d` for remote).
2. A window opens showing live frames from the raw stream (UC-07).
3. The developer closes it; the CLI unsubscribes.

**Postconditions.** None. Per UC-07, the daemon returns to idle capture
when the last subscriber leaves.

**Failure modes.**
- Camera unreadable → error rather than a black window.
- No display available (headless SSH session) → clear error suggesting
  `camera image` instead. This is a common situation with a Pi over the
  field and deserves a real message.
- `--all` → refused; interactive viewing is single-target.
- Frames too dark to see anything → still displayed, with the advisory
  dark-frame flag surfaced rather than leaving the developer guessing.

**Open questions.** How is the stream rendered — a local GUI window, or
something that works over SSH? The headless case is the common one for a
field Pi.

#### Scenarios

**UC-16.S-1 — Aim an uncalibrated camera** · _happy path_
A newly attached `arducam-ov9782-usb-camera`, no playfield, no
calibration. `aprilcam camera view` shows live raw video, and the
developer physically aims the camera until all of `main-playfield`'s
ArUco fiducials are in frame — the necessary prerequisite for UC-15.S-1.

**UC-16.S-2 — Judge an exposure experiment** · _happy path_
While pushing the exposure sweep of UC-01.S-4a, the developer watches
this view and sees each pushed config take effect live, which is how the
winner gets chosen.

**UC-16.S-3 — Headless host** · _failure path_
Run over SSH on the field Pi with no display. The command fails with a
message saying no display is available and pointing at `camera image`,
rather than crashing with a toolkit backtrace.

---

### UC-17 — Capture a single raw frame

**Job:** JTBD-02 · **Primary actor:** A-03 · **Also serves:** A-02
**Status:** draft
**Interfaces:** CLI, Python, MCP

**Goal.** The developer captures one raw frame and gets it where it is
needed — a file, or stdout for piping.

**Preconditions.** Camera exists and is readable. No calibration needed.

**Output modes.** One frame, three destinations:
- **raw bytes to stdout** — for piping into another tool
- **base64 to stdout** — for pasting, or for text-only transports
- **a file** — in a chosen image format

**Main success path.**
1. `aprilcam camera image arducam-ov9782-usb-camera` with an output
   option (`-d` for remote).
2. The CLI fetches one frame (UC-06) and writes it to the chosen
   destination.

**Postconditions.** A file exists, or bytes have been written to stdout.
No daemon state changes.

**Failure modes.**
- Camera unreadable → error, nothing written. A partially written file is
  worse than no file.
- Unwritable path → error before capture, not after.
- Raw bytes to a terminal → refuse rather than corrupt the terminal,
  unless explicitly forced.
- Unsupported format → error listing the supported ones.

**Open questions.**
- Which formats? PNG and JPEG at minimum. Is lossless (PNG) the default,
  given these frames are often used as test fixtures where JPEG artifacts
  would be baked into the ground truth?
- Default destination with no options given — stdout, or a generated
  filename? Stdout-by-default is pipe-friendly but surprising.

#### Scenarios

**UC-17.S-1 — Write a PNG for a test fixture** · _happy path_
`aprilcam camera image arducam-ov9782-usb-camera -o frame.png` writes one
lossless frame. This is how the captured fixture matrix
(`archive/tests/images/`) gets regenerated.

**UC-17.S-2 — Pipe raw bytes** · _happy path_
The frame is streamed as bytes to stdout and piped to another tool
without a temporary file.

**UC-17.S-3 — Base64 over a text channel** · _happy path_
Base64 output captured from a headless SSH session on the field Pi —
the workaround for UC-16.S-3's missing display.

**UC-17.S-4 — Refuse to dump binary to a terminal** · _failure path_
Raw-bytes mode with stdout attached to a terminal and no redirect. The
command refuses with a message about redirecting, instead of filling the
terminal with binary.

---

### UC-18 — Watch the live calibrated playfield

**Job:** JTBD-02, JTBD-01 · **Primary actor:** A-03
**Status:** draft
**Interfaces:** CLI, Python

**Goal.** The developer watches the deskewed, calibrated top-down view of
the playfield with detected tags drawn on it — the "is the whole system
actually working" view. This is what `aprilcam view` does in v1.

**Preconditions.** A camera linked to the playfield **and calibrated**
(UC-05 satisfied).

**Main success path.**
1. `aprilcam playfield view` (`-d` for remote).
2. A window shows the deskewed stream (UC-07) with tags from the tag
   stream (UC-11) drawn in place — north up, east right.
3. The developer closes it; the CLI unsubscribes.

**Postconditions.** None.

**Failure modes.**
- Camera not calibrated → explicit error saying calibration is required
  and pointing at `camera calibrate`. **Never** silently fall back to the
  raw view: a raw frame that looks plausible but is not deskewed is
  exactly how someone ends up trusting wrong coordinates.
- No playfield → error.
- Calibration goes stale mid-view → surfaced in the view (UC-11.S-3),
  not silently ignored.
- No display → as UC-16.S-3, point at `playfield image`.
- `--all` → refused.

**Open questions.** The panel model itself is settled (UC-28, spec §9
#8): collapsible panels toggle layers by name — built-in layers (tags,
features) plus each named daemon annotation layer. **Resolved (sprint
032):** which layers are **on by default** in `playfield view`'s
annotated mode. Tag ids, world coordinates (drawn via the crosshair),
and orientation (drawn via the "hat") are all on by default, rendered
over the deskewed frame; the tag sidebar (numeric id/position/heading/
speed per tag) is also on by default alongside them. `--bare` is the
opt-out: the plain deskewed stream, no overlay, no sidebar. Velocity
**vectors** — a drawn arrow on the frame itself showing a tag's heading
and speed — remain out of scope; only the sidebar's velocity *columns*
(numeric speed/heading text) are in scope this sprint, and the two
should not be conflated by a future reader. Still open: surveyed
features' on/off default, and whether client annotation layers start
visible or hidden. Also open: whether the CLI *authors* annotations
(draw a marker from a script) or only lists and clears them (UC-28).

#### Scenarios

**UC-18.S-1 — Confirm the system end to end** · _happy path_
After UC-15.S-1, `aprilcam playfield view` shows the field deskewed to
its 134.3 × 89.3 proportions, north up. AprilTag 1 is drawn at the centre
of the image, and a robot's AprilTag 7 is drawn where the robot actually
is. Moving the robot east moves the drawn tag right. This annotated view
— tag ids, hats, crosshairs, and the tag sidebar — is what opens by
default; the same command with `--bare` shows the plain deskewed stream
instead, with no overlay and no sidebar (sprint 032), mirroring how
`camera view` (UC-16) is the honest, unprocessed counterpart.

**UC-18.S-2 — Uncalibrated camera** · _failure path_
The camera is linked but never calibrated. The command errors, naming
calibration as the missing step, and does **not** show a raw camera
frame as a substitute.

---

### UC-19 — Capture a single deskewed playfield frame

**Job:** JTBD-02 · **Primary actor:** A-03 · **Also serves:** A-02
**Status:** draft
**Interfaces:** CLI, Python, MCP

**Goal.** As UC-17, but the calibrated top-down playfield image rather
than the raw camera view.

**Preconditions.** Calibrated camera linked to the playfield.

**Main success path.** `aprilcam playfield image` with the same output
modes as UC-17 — raw bytes, base64, or a file.

**Postconditions.** A file or stdout output. No daemon state change.

**Failure modes.** As UC-17, plus: not calibrated → error, never a raw
frame substituted.

**Open questions.** Should the world↔pixel metadata (px-per-cm, field
dimensions) accompany the image — as a sidecar file, or embedded? Without
it the image cannot be converted back to world coordinates, which is most
of what makes it useful.

#### Scenarios

**UC-19.S-1 — Save a deskewed frame** · _happy path_
`aprilcam playfield image -o field.png` writes the 800×532 deskewed
image, north up, east right.

**UC-19.S-2 — Uncalibrated** · _failure path_
Uncalibrated camera. Command errors; no file is written and no raw frame
substituted.

---

### UC-20 — List the tags on the playfield

**Job:** JTBD-01 · **Primary actor:** A-03 · **Also serves:** A-02
**Status:** draft
**Interfaces:** CLI, Python, MCP

**Goal.** The developer gets the tags currently detected on the
playfield, with world positions — the quick "what does the system see
right now" check, without opening a GUI.

**Preconditions.** UC-05 satisfied for world coordinates.

**Command spelling — sprint 021 ships `camera tags`, `playfield tags` is
deferred.** This use case was drafted around `aprilcam playfield tags`,
resolving its camera client-side from `CameraState.playfield` links
(the "Playfield commands resolve their camera" convention above). That
resolution has nothing to resolve until the config-service sprint
builds playfields — `CameraState.playfield` is always `None` until
then, so `playfield tags` would fail on every invocation, not degrade
gracefully. Sprint 021 (tag detection) ships **`aprilcam camera tags
<camera>`** instead — camera-addressed, mirroring the already-shipped
`camera image <camera>` — as its main success path below. `playfield
tags` remains the eventual form once playfields exist and is not
removed from the command surface, only deferred (sprint 021's
Architecture Decision 3 has the full reasoning).

**Main success path (sprint 021, as shipped).**
1. `aprilcam camera tags <camera>` (`-d` for remote).
2. **All** detected tags are listed, per UC-10 — there is no count or
   limit — each with family and id. This sprint every camera is
   uncalibrated by construction (no calibration subsystem exists yet),
   so every row carries an explicit uncalibrated flag plus pixel
   position rather than a world position (UC-20.S-3). Once calibration
   and playfields exist, `camera tags` gains world position, heading,
   and velocity with UC-12 mount corrections applied, and `playfield
   tags` becomes reachable as the camera-implicit form of the same
   data.

**Postconditions.** None — a read.

**Failure modes.**
- Nothing detected → empty list, not an error.
- Uncalibrated → pixel positions flagged as uncalibrated, per UC-10.S-3.
  Never world coordinates from an absent homography.

**Open questions.** Is there a machine-readable output mode (JSON) as
well as the human table? **Partially answered by sprint 039:**
`aprilcam camera tags <camera> --json` emits JSON Lines (one object per
line) instead of the human table — this package's first
machine-readable output mode — and the same sprint's `--follow` adds
continuous streaming in place of a one-shot burst (UC-20.S-4). The
Python module remains the primary programmatic path; `--json`/`--follow`
cover shell-pipeline use instead.

#### Scenarios

**UC-20.S-1 — See three robots** · _happy path_
AprilTags 7, 12, 30 plus surveyed centre tag 1 are on the field.
`aprilcam playfield tags` lists exactly 4 tags with world coordinates
inside the 134.3 × 89.3 field. Requires calibration and a playfield
link, neither of which exists yet (sprint 021) — this is the eventual
`playfield tags` behaviour, not what ships this sprint.

**UC-20.S-2 — Fan out across the network** · _happy path_
`aprilcam playfield tags --all` lists tags from every discovered daemon's
playfield, each row labelled with its daemon. Same deferral as S-1;
sprint 021's fan-out equivalent is `aprilcam camera tags <camera>
--all`, labelled per daemon exactly as `camera list`/`camera probe`
already are.

**UC-20.S-3 — Uncalibrated camera** · _failure path_
Tags are listed with pixel positions and an explicit uncalibrated flag,
with no world coordinates presented. This is exactly what sprint 021
ships: every camera is uncalibrated by construction, so `aprilcam
camera tags <camera>` always takes this path this sprint, never S-1's.

**UC-20.S-4 — Continuous watch, machine-readable or not** · _happy path_
`aprilcam camera tags <camera> --follow` streams one row per tag per
frame, continuously, until Ctrl-C (closing cleanly, `EXIT_OK`, not a
traceback) instead of S-3's single one-shot burst — this is the mode
the daemon's multi-frame confirmation filter needs to reliably report
tags at all, since a one-shot call right after a gap rebuilds its
confirmed streak from zero. Add `--json` for JSON Lines output (one
parseable object per line, same fields as the human row) instead of the
printed table. `--follow --all` is refused before any connection is
attempted — mirroring S-2's fan-out note, a single blocking stream
cannot fan out the way a one-shot RPC does. Shipped sprint 039, ticket
005; answers this use case's Open Questions note above.

---

### UC-21 — Generate tag images

**Job:** JTBD-05 · **Primary actor:** A-03
**Status:** draft
**Interfaces:** CLI, Python

**Goal.** The developer generates printable AprilTag and ArUco images for
building or repairing a physical playfield.

**Preconditions.** None. **This is a purely local utility — it needs no
daemon, no camera, and no network.** The one command in this file that
works entirely offline.

**OpenCV is in the base install.** ArUco dictionary generation needs
OpenCV, and this command must work on any install — including someone
who installed only to talk to a remote daemon. Rather than gate it behind
the `[daemon]` extra or ship pre-rendered dictionaries, **OpenCV is a
base dependency of `aprilcam`**. This reverses
[specification.md](../specification.md) §9's decision to keep `taggen`
behind the extra, and relaxes the v1 goal of a cv2-free base install.
The vision *boundary* is unaffected: the daemon remains the sole vision
authority, and no client-side component does pixel work
([specification.md](../specification.md) §2). OpenCV merely being
importable is not permission to use it outside the daemon.

**Main success path.**
1. `aprilcam tags generate` with a family, an id or id range, and an
   output target.
2. Images are written, ready to print at a known physical size.

**Postconditions.** Image files exist. Nothing else changes.

**Failure modes.**
- Id outside the family's range → error naming the valid range.
- Unwritable output path → error before generating.
- Requested physical size unachievable at the target print resolution →
  warn rather than silently produce an unusable tag.

**Open questions.**
- PDF output for multi-tag sheets, as v1 had?
- Is `tags generate` the right name given v1 had both `taggen` and
  `arucogen`? Drafted as one command with a family option.

#### Scenarios

**UC-21.S-1 — Generate the playfield's ArUco set** · _happy path_
`aprilcam tags generate` for ArUco 4x4 ids 1–10 produces the ten marker
images `main-playfield` specifies, at a size printable to match the
surveyed field.

**UC-21.S-2 — Generate a robot's AprilTag** · _happy path_
AprilTag 36h11 id 7 is generated for mounting on a robot, matching the
`size` parameter registered in UC-12.

**UC-21.S-3 — Id out of range** · _failure path_
An ArUco 4x4 id beyond the dictionary's range is requested. The command
errors, naming the valid range, and writes nothing.

---

### UC-22 — Run the MCP server

**Job:** JTBD-01, JTBD-02, JTBD-03 · **Primary actor:** A-02 · **Also serves:** A-03
**Status:** draft
**Interfaces:** CLI

**Goal.** An AI agent gets access to AprilCam's perception surface by
running `aprilcam mcp` as an MCP stdio server.

**Preconditions.** None at startup. **The MCP server does not need a
daemon named when it starts** — it discovers and connects to daemons as
its tools are called, and may reach any of them.

**Late binding is the point.** An agent session outlives any one daemon
connection: a daemon may be restarted (discarding pushed config per
UC-01), or the agent may work with a local daemon and a field Pi in the
same session. Binding a daemon at startup would make the server useless
across a restart, so the server starts unbound and resolves per call.

**Main success path.**
1. `aprilcam mcp` starts, speaking MCP over stdio. No daemon is
   contacted yet and startup does not fail for lack of one.
2. The agent calls a tool; the server resolves a daemon — explicitly
   named, or the local one — and proxies the request.
3. The agent works through the perception surface (UC-06, UC-09, UC-10,
   UC-08) with handles and tool calls, never file paths (A-02's
   constraint).

**Postconditions.** None beyond the session.

**Failure modes.**
- No daemon reachable when a tool is called → tool-level error the agent
  can act on, **not** a server crash. The server stays up; the next call
  may succeed.
- Daemon restarts mid-session → subsequent calls reconnect. The agent is
  not required to restart its session.
- Agent requests an uncalibrated world position → the flagged
  uncalibrated result of UC-09.S-5, never fabricated coordinates.

**Open questions.**
- If several daemons are discovered and the agent names none, which is
  used? Local-if-present is the obvious rule; ambiguity among multiple
  remotes needs an answer.
- Does the server expose daemon selection as a tool parameter, or as a
  separate connect tool holding a session default?

#### Scenarios

**UC-22.S-1 — Agent perceives the field** · _happy path_
`aprilcam mcp` starts with no daemon argument. The agent lists cameras,
requests a deskewed frame, and asks for AprilTag 7's world position —
each call resolving to the local daemon. The agent never sees a file
path.

**UC-22.S-2 — Start with no daemon running** · _happy path_
`aprilcam mcp` starts successfully with nothing running. The first tool
call returns an error saying no daemon is reachable. The operator starts
one (UC-13.S-3); the next call succeeds **in the same session**.

**UC-22.S-3 — Daemon restarts mid-session** · _failure path_
Mid-session the daemon is restarted. The in-flight call fails with a
connection error; the following call reconnects and succeeds. Per UC-01,
any RPC-pushed configuration is gone and the daemon is back on its
on-disk config — which the agent may observe as changed camera settings.

---

### UC-25 — Keep the camera list current, and bootstrap configs

**Job:** JTBD-05 · **Primary actor:** A-03
**Status:** draft
**Interfaces:** CLI, Python, MCP

> Placed here, ahead of UC-23/UC-24, because it comes **first in the
> workflow** — probe, then dump, then push. Numbered 25 because
> identifiers are permanent and never renumbered
> ([README.md](README.md) §4).

**Goal.** The developer forces the daemon to re-interrogate its hardware
and bring its camera list up to date, then uses the resulting
configuration documents as the starting point for every camera config
that will ever exist.

**Preconditions.** Daemon running (UC-01). Cameras physically attached.

**The daemon keeps its own camera list current; probe forces the issue.**
Two mechanisms, and both are needed:

1. **Passive hotplug watching (continuous).** The daemon listens for
   devices arriving and disappearing and updates its camera list on its
   own. Attaching a camera to a running daemon should Just Work — you
   should not have to tell the daemon that something you just plugged in
   exists.
2. **Active probe (on demand).** `aprilcam camera probe` tells the daemon
   to go look **harder** than passive watching can: enumerate every
   backend, re-run the interrogation, and refresh what it knows.

**Why passive watching is not sufficient.** Hotplug events cover USB
devices well, but not everything announces itself that way. Raspberry Pi
/ libcamera cameras in particular need **active interrogation** —
enumerating them means reading `/sys` for labelled loopback devices or
shelling out to the `cam` tool, neither of which is triggered by a device
event. v1 compounded this by caching a non-empty camera list *for the
process lifetime*, on the assumption that "cameras are fixed hardware."
That assumption is exactly what a long-running daemon plus hotplug
breaks. So probe exists to say: **discard what you think you know and
find out again.**

The division of labour:

| | Passive watching | `camera probe` |
|---|---|---|
| Runs | continuously | when asked |
| Catches | USB attach/detach | everything, including libcamera/Pi |
| Cost | ~free | subprocesses, backend enumeration |
| Caching | updates incrementally | **invalidates and re-derives** |

**Bootstrapping: every camera configuration starts as a probe and a
dump.** This is the origin of the configuration lifecycle, and the answer
to "where does the first config file come from?" — you never hand-write
one from scratch:

```
1. PROBE   daemon re-interrogates hardware, derives a stable name for each
           camera, and holds an in-memory config with everything it
           introspected plus every settable key, blank
2. DUMP    write that document to a local file            (UC-24)
3. EDIT    fill in exposure, gain, playfield link, mount position
4. PUSH    send it back; applied immediately, in memory   (UC-23)
5. repeat 3–4 until the image is right                    (UC-01.S-4a)
6. INSTALL copy the winning file to the configuration directory
```

Steps 2–4 are a **loop**, and that is the point: the probe output is a
template you iterate on against live video (UC-16.S-2), not a
one-shot guess.

**Every camera configuration starts as a probe and a dump.** This is the
origin of the configuration lifecycle, and the answer to "where does the
first config file come from?" — you never hand-write one from scratch:

```
1. PROBE   daemon finds attached cameras, derives a stable name for each,
           and holds an in-memory config with everything it introspected
           plus every settable key, blank
2. DUMP    write that document to a local file            (UC-24)
3. EDIT    fill in exposure, gain, playfield link, mount position
4. PUSH    send it back; applied immediately, in memory   (UC-23)
5. repeat 3–4 until the image is right                    (UC-01.S-4a)
6. INSTALL copy the winning file to the configuration directory
```

Steps 2–4 are a **loop**, and that is the point: the probe output is a
template you iterate on against live video (UC-16.S-2), not a
one-shot guess.

**Names are derived and stable, not arbitrary.** The daemon assigns each
camera a readable slug from its identity chain — libcamera id, then
serial / AVFoundation unique id, then vid:pid@location, falling back to a
device-name-plus-resolution slug. This is v1's proven scheme and it
yields names like `arducam-ov9782-usb-camera` and `hd-usb-camera`. The
properties that matter:

- **Stable across reboots and re-plugs** — a config keeps matching its
  camera, which is the whole point of the persistent registry (UC-01).
- **Readable** — you can tell which physical camera a config belongs to
  without a lookup table.
- **Unique** — two identical camera models are disambiguated by the
  hardware identifiers earlier in the chain.

**Main success path.**
1. `aprilcam camera probe` (`-d` for remote) tells the daemon to
   re-interrogate. The CLI does no enumeration itself — it forces the
   daemon to, per the vision boundary.
2. The daemon discards its cached view, enumerates every backend
   (including the libcamera/Pi paths passive watching misses), and
   resolves each camera's stable name.
3. Each camera now has an in-memory configuration — the self-describing
   document of UC-03.S-1: introspected facts plus every settable key
   present and blank.
4. The command reports the refreshed list, and **what changed** —
   appeared, disappeared, unchanged.
5. The developer dumps one to a file (UC-24), edits it, and pushes it
   back (UC-23).

**Postconditions.** Every attached camera has a stable name in the
persistent registry and an in-memory configuration document that can be
dumped. Nothing is written to disk by the daemon.

**Failure modes.**
- No cameras attached → empty result, not an error (UC-01.S-2).
- Camera present but unopenable → reported with its name and flagged
  unusable, never silently omitted. You still get a name, so a config
  can be prepared before the hardware is fixed.
- Identity chain degrades to the last-resort name slug — two identical
  cameras with no serial numbers → their derived names could collide.
  The daemon must disambiguate rather than silently merge two cameras
  into one registry entry.
- **Probe while cameras are streaming** → must not disturb live
  subscribers. Re-enumeration cannot mean dropping streams, which makes
  probe more delicate than it first appears: it has to refresh knowledge
  without disrupting capture.
- **A configured camera disappears** → its config stays in the registry
  (a config outliving its hardware is normal — the camera may be
  re-plugged). The camera is flagged absent, not deleted.
- Backend interrogation hangs (a wedged `cam` subprocess) → time out and
  report a partial result rather than hanging the daemon. The daemon
  serving live streams must not be blocked by a probe.

**Open questions.**
- Does probing re-derive names for cameras already in the registry, and
  what happens if a name comes out different than last time? A changed
  name silently orphans an installed config. *(Resolved 2026-08-21: no
  — reconnect never renames. `camera/registry.py`'s `resolve()` keeps a
  camera's persisted `slug` fixed for the life of its `hardware_key`
  record; a fresh probe only refreshes `device_name`/`last_seen` in
  place, even when the newly-resolved identity proposes a different
  slug. This is deliberately the safe answer to the exact risk this
  question names — never renaming is what *prevents* a name change from
  silently orphaning an installed config, not a source of that risk.
  Decision made by ticket 020-002; see `resolve()`'s own docstring
  ("Reconnect does not rename") for the full rationale and the
  disambiguation behaviour that still applies on *first sight* of a
  new `hardware_key`.)*
- Does the probe report the *ranges* each setting accepts, not just which
  settings exist? Without ranges the edit step in step 3 is guesswork,
  and UC-03's hard-error-on-bad-value rule makes guessing expensive.
- Is passive hotplug watching cheap enough to run always, or should it be
  configurable? On a Pi with a fixed camera it earns nothing.
- Should the daemon probe once automatically at startup, so a fresh
  daemon knows about libcamera devices without being asked? Drafted as:
  yes — otherwise UC-01's "daemon knows its own cameras" claim is false
  for Pi cameras until someone runs probe.

#### Scenarios

**UC-25.S-1 — Bootstrap the first config for a new camera** · _happy path_
A freshly attached `arducam-ov9782-usb-camera` on a daemon with no
configuration at all. `aprilcam camera probe` reports it with its derived
name, its resolutions, and the settings its hardware exposes. Dumping it
(UC-24.S-3) yields a document with `name:
"arducam-ov9782-usb-camera"` and every settable key blank. The developer
sets a low manual exposure and `playfield: main-playfield`, pushes it
(UC-23.S-2), and watches the image change live (UC-16.S-2). The full
lifecycle from bare hardware to working config, with nothing
hand-written.

**UC-25.S-2 — Probe two cameras and configure them independently** · _happy path_
Both fixture cameras are attached. The probe names them
`arducam-ov9782-usb-camera` and `hd-usb-camera`. Each gets its own dumped
document with its own settable keys — the HD camera's key set differs
from the Arducam's, because different hardware exposes different controls
(UC-03's per-camera rule).

**UC-25.S-3 — Names survive a re-plug** · _happy path_
A probed and configured camera is unplugged and re-plugged into a
different USB port. A second probe derives the **same** name, so the
installed config still matches it. This is the property that makes
configuration-by-name workable at all.

**UC-25.S-4 — Camera attached but unopenable** · _failure path_
A camera is present but cannot be opened. The probe reports it with its
derived name and an explicit unusable flag rather than omitting it, so
the developer can see that the system noticed the hardware — the
difference between "not plugged in" and "plugged in and broken."

**UC-25.S-5 — Hotplug needs no probe** · _happy path_
A daemon is running with one camera. The developer plugs in
`hd-usb-camera`. Without any command being run, `aprilcam camera list`
shows it within a few seconds — the daemon noticed the device arriving.
Unplugging it flags the camera absent, again with no command. This is the
common case, and it must not require `probe`.

**UC-25.S-6 — Raspberry Pi camera needs a probe** · _happy path_
A daemon runs on the field Pi with a libcamera camera attached. Because
libcamera devices are found by reading `/sys` or shelling out to the
`cam` tool rather than by device events, passive watching alone may not
surface it. `aprilcam camera probe -d pi-field.local` forces active
interrogation and the camera appears. **This is the case that justifies
probe existing** — with USB-only hardware, hotplug watching would make
the command unnecessary.

**UC-25.S-7 — Probe during a live stream** · _failure path_
A subscriber is watching `arducam-ov9782-usb-camera`'s deskewed stream
(UC-07) when `aprilcam camera probe` runs. The probe refreshes the camera
list without dropping the stream or interrupting frames. If a backend
cannot be interrogated without disturbing an open camera, the probe skips
it and says so rather than killing the stream.

---

### UC-23 — Push a configuration document to a daemon

**Job:** JTBD-05 · **Primary actor:** A-03
**Status:** draft
**Interfaces:** CLI, Python, MCP

**Goal.** The developer pushes a playfield definition or a camera config
to a daemon by naming **only the file** — the document says what it is,
so the command line does not have to repeat it.

**Preconditions.** Daemon running on the target. The file exists locally
and is valid.

**Configuration documents are self-naming.** Every configuration document
carries its own `name`, so the push commands take a file and nothing
else:

```
aprilcam playfield config ./main-playfield.json
aprilcam camera config    ./exposure-B.json
```

The daemon reads the name from inside the document and applies it to that
playfield or camera. There is no redundant
`aprilcam playfield config main-playfield ./main-playfield.json`.

**Schema change for v2 — cameras must self-name.** Playfield definitions
already carry `name` (`"main-playfield"`, the first key). **Camera configs
today do not** — their identity comes from the containing directory
(`cameras/arducam-ov9782-usb-camera/config.json`), and the file's
`device_name` is a hardware descriptor, not the slug. For the symmetric
command above to work, **v2 camera configs gain a `name` field** holding
the camera slug. This also makes a camera config portable: a scratch file
in `/tmp` is as pushable as one in the config directory, which the
experiment workflow (UC-01.S-4a) depends on.

**Ephemeral, per UC-01.** A pushed document lives in daemon memory only
and is discarded on restart. Permanence means installing the file in the
configuration directory — locally, or `scp` to a remote host.

**Main success path.**
1. `aprilcam playfield config <file>` or `aprilcam camera config <file>`,
   with `-d` for a remote daemon.
2. The CLI reads the document, validates it locally enough to report
   obvious problems well, and pushes it whole (UC-02 / UC-03).
3. The daemon validates and adopts it — applying camera settings to
   hardware immediately.
4. The command reports what was applied, and to which named target.

**Postconditions.** The daemon's live configuration reflects the pushed
document. Nothing is written to disk on either machine.

**Failure modes.**
- File missing or unreadable → error before contacting the daemon.
- Malformed JSON or schema-invalid → rejected whole, previous
  configuration intact (UC-02.S-2). Never partially applied.
- **Document has no `name`** → error. The command cannot guess the
  target, and guessing from the filename would be a foot-gun: a file
  copied to `backup.json` would push to a camera called `backup`.
- Named target does not exist on that daemon — a camera slug matching no
  enumerated camera → error naming the mismatch and listing what the
  daemon does have.
- Camera config contains a setting the hardware rejects → whole push
  fails, per UC-03's hard-error rule.
- `--all` → refused. Pushing configuration changes state, so it is
  single-daemon by the fan-out rule.

**Open questions.**
- Should the CLI verify the pushed document round-trips (fetch it back
  and compare) or trust the daemon's acknowledgement?
- Is `config` the right verb for a push? It reads as a noun. `push`,
  `set`, or `apply` are candidates — `set` matches the underlying
  `SetPlayfield` / `SetCameraConfig` RPCs.
- Does pushing a playfield that an existing calibration was computed
  against require a force flag, or just mark calibrations stale
  (UC-02's open question)?

#### Scenarios

**UC-23.S-1 — Push a playfield by filename alone** · _happy path_
Daemon running with no playfield. `aprilcam playfield config
./main-playfield.json` reads `name: "main-playfield"` from the document,
pushes it, and reports that `main-playfield` was applied with its 25
features. `aprilcam playfield tags` now works. No name was typed on the
command line.

**UC-23.S-2 — Push a camera config from a scratch file** · _happy path_
The exposure experiment of UC-01.S-4a: `/tmp/exposure-B.json` carries
`name: "arducam-ov9782-usb-camera"`. `aprilcam camera config
/tmp/exposure-B.json` applies it to that camera immediately, despite the
file living nowhere near the configuration directory. `aprilcam camera
view` shows the change live.

**UC-23.S-3 — Push to a remote daemon** · _happy path_
`aprilcam playfield config ./main-playfield.json -d pi-field.local`
pushes across the network. The Pi holds it in memory; the developer's
laptop file is unchanged and the Pi's disk is untouched.

**UC-23.S-4 — Document with no name** · _failure path_
A camera config lacking its `name` field is pushed. The command errors,
saying the document does not identify its target, and does **not** infer
a name from the filename. Nothing is sent to the daemon.

**UC-23.S-5 — Named camera not on this daemon** · _failure path_
A config naming `global-shutter-camera` is pushed to a daemon that has
only `arducam-ov9782-usb-camera` and `hd-usb-camera`. The push fails,
naming the unknown camera and listing the two the daemon actually has.

---

### UC-24 — Dump a daemon's live configuration

**Job:** JTBD-05 · **Primary actor:** A-03
**Status:** draft
**Interfaces:** CLI, Python, MCP

**Goal.** The developer reads back a daemon's live configuration — to
inspect it, or to capture a winning experiment as a file that can be
installed permanently.

**Preconditions.** Daemon running; the named target exists on it.

**The round trip that makes ephemeral config workable.** Because pushes
are discarded on restart (UC-01), the only way to keep a good
configuration is to write it to disk. The dump emits the live document in
**exactly the on-disk format** — same schema the daemon loads at startup,
including the `name` field — so the output can be redirected to a file,
installed, and re-loaded verbatim.

**Main success path.**
1. `aprilcam camera config <camera> --dump` or `aprilcam playfield config
   <name> --dump`, with `-d` for remote.
2. The document is written to stdout in on-disk format.
3. The developer redirects it to a file and installs it — locally, or by
   `scp` to a remote daemon host.

**Postconditions.** None on the daemon — a read. Whatever the developer
redirected now exists.

**Failure modes.**
- Named target unknown → error listing what the daemon has.
- No daemon reachable → connection error.
- Camera has never been configured → dump the introspected-plus-blank
  document of UC-03.S-1 rather than erroring. That document is a useful
  starting template.

**Open questions.**
- Is `--dump` a flag on `config`, or a separate `get`/`show` verb? A flag
  keeps the noun-per-subsystem shape; a verb reads better.
- Should the dump record that it came from a live push rather than disk —
  provenance as a comment or field? JSON has no comments, and a
  provenance field would then be pushed back on the next install.

#### Scenarios

**UC-24.S-1 — Capture a winning experiment** · _happy path_
Completing the loop from UC-01.S-4a and UC-03.S-5: exposure **D** lives
only in daemon memory. Dumping `arducam-ov9782-usb-camera`'s config
yields a document with `name` set and every setting at its live value.
Redirecting it into the configuration directory and restarting produces a
daemon whose disk-loaded config is exactly **D**. Round trip closed:
**dump → install → restart → identical config.**

**UC-24.S-2 — Dump from a remote daemon and install there** · _happy path_
`aprilcam camera config arducam-ov9782-usb-camera --dump -d
pi-field.local > cam.json`, then `scp cam.json` to the Pi's configuration
directory. The daemon never wrote its own config; the operator did.

**UC-24.S-3 — Dump an unconfigured camera as a template** · _happy path_
`hd-usb-camera` has never been configured. The dump returns the
self-describing document of UC-03.S-1 — introspected facts plus every
settable key present and blank — which the developer fills in and pushes
back with UC-23.

**UC-24.S-4 — Unknown target** · _failure path_
Dumping a camera slug the daemon does not have fails, listing the
cameras it does have.

---

### UC-29 — Determine a camera's physical position and height

**Job:** JTBD-06, JTBD-01 · **Primary actor:** A-03
**Status:** draft
**Interfaces:** CLI

**Goal.** An operator who has calibrated a camera against a playfield
(UC-15) also fixes the one input `mount_z`'s parallax correction (UC-12)
has always needed and never had — the camera's own ground position
(nadir) and optical height. `aprilcam camera locate` *solves* both from
surveyed elevated AprilTag references instead of asking anyone to
tape-measure them.

**Preconditions.** Camera calibrated and linked to a playfield (UC-15).
At least 2 AprilTag references physically placed on stands at surveyed
playfield feature positions, each stand's height measured. None of the
reference tags is currently registered (UC-12) with a positive
`mount_z` — that registration's own (possibly stale) correction would
corrupt the raw reading the solve needs.

**Main success path.**
1. `aprilcam camera locate arducam-ov9782-usb-camera --reference
   15:dot-northeast-orange:17.2 --reference 14:dot-southwest-orange:15.0`
   (`-d` for remote; `--reference TAG:SLUG:HEIGHT_CM`, repeatable, 2
   minimum, 3+ recommended for redundancy).
2. The command resolves each `SLUG` to a surveyed position via the
   camera's linked playfield (exact match only, no fuzzy lookup), then
   drives one detection per reference through the daemon (`GetTag`) —
   the only pixel-touching step, and it happens entirely inside the
   daemon, never in the CLI.
3. The collected (surveyed, observed, height) triples are fit with a
   least-squares solve (`calibration.geometry.solve_camera_geometry`)
   for the camera's ground nadir and optical height.
4. The command reports the solved nadir, height, and RMS residual, and
   warns if the references are too near-collinear with the estimated
   nadir to trust.
5. A good fit is written into the camera's `camera_position` config, on
   disk and pushed live (`SetCameraConfig`) — the daemon picks it up on
   its very next detection burst, no restart (UC-24's own round trip).
   A poor fit (high residual or near-collinear) is reported but refused
   without `--force`. `--dry-run` reports without ever writing,
   regardless of fit quality.

**Postconditions.** On a written solve, `camera_position` is set for the
camera and every subsequent detection burst's `mount_z` correction
(UC-12) uses it. `--dry-run`, or a refused poor fit without `--force`,
leaves the camera's configuration untouched.

**The real fixture (worked example, camera 3 / `main-playfield` from
README.md §3).** Two AprilTags on stands at surveyed dot positions: tag
15 at `dot-northeast-orange` (surveyed (50, 30) cm, stand height 17.2
cm), tag 14 at `dot-southwest-orange` (surveyed (-50, -30) cm, stand
height 15.0 cm). Raw, uncorrected readings were (57.38, 34.90) cm and
(-57.15, -33.40) cm — 8.86 cm and 7.92 cm of real, measured parallax
error at only a 15-17 cm mount height. `camera locate` solved nadir
(3.057, -2.799) cm, height 128.230 cm, RMS residual 0.1585 cm; applying
that geometry brings both readings to within ~0.2 cm of their surveyed
ground truth. The 128.230 cm solved height is **not** the physical
sensor plane (measured separately at 127 cm) — it is the effective
optical centre the pinhole model needs. Sprint 029's own sprint.md named
unmodelled lens distortion as the most plausible explanation and
chartered sprint 030 to re-verify it once a distortion model existed;
see the next paragraph for what that re-verification actually found.

**Sprint 030 ticket 003 re-verification (2026-08-20), against the real
camera, with a distortion model now available (tickets 001/002).**
**Falsifiable check 2 (optical height) stayed blocked**: the elevated
reference stands for tags 14/15 are still removed (the same precedent
029-004 hit), and a fresh `--dry-run` reproduced the documented "not
detected" refusal rather than a stale success — so the solved height
could not be re-run or re-measured. The `camera_position` above is
also now of **uncertain provenance for an unrelated reason**: the
camera was physically re-aimed between sprint 029 and this ticket, to
recover main-playfield visibility lost to an environmental fault
(`clasi/issues/camera-marked-tests-failing-en-masse-suspected-v1-daemon-contention.md`).
Ground nadir/height are properties of the camera's fixed mounting
point, not its pointing direction, so they survive a pure pan/tilt
re-aim but not a physical relocation; the lens is wide enough
(fx/width ≈ 0.4-0.5, roughly 90-100° horizontal FOV) to see the whole
field from one fixed point without much tilt, which favours "re-aimed
in place" over "relocated," but this is inference, not a
re-measurement — treat the value above as unverified until a fresh
`camera locate` run confirms or updates it.

**Falsifiable check 1 (radial consistency), adapted to this camera**
(the original seven 46.5mm AprilTags live on a different camera/
playfield that cannot currently get its own intrinsics fit — see the
knowledge-base entry below): main-playfield's own ten identically-sized
surveyed ArUco markers stand in. Across six repeated live bursts, the
correlation between each marker's apparent world size and its pixel
radius² ranged -0.52 to +0.40 (mean magnitude ≈ 0.15) — whether or not
the fitted lens model was applied, and never reaching the n=10
significance threshold (≈0.63) — nowhere near the documented -0.96.
That is evidence against a large real distortion hiding behind the
sanity bound's small `k1`. It is not, however, evidence that the
stored `k1` is a precisely known constant: the same repeated bursts
show the fitted `fx`/`k1` pair itself swings noticeably run to run
(`fx` roughly 504-661px, `k1` roughly 0.0069-0.0135) while the held-out
reprojection residual stays flat (~2.7-3.2px) throughout — the same
fx/depth ridge ticket 002's own investigation found for a near-nadir
single coplanar view, softened by this camera's post-re-aim tilt but
not eliminated. Full methodology, numbers, and the contention/exposure
story behind why this took two tries:
`docs/knowledge/2026-08-20-camera-3-distortion-reverification.md`.

**Failure modes.**
- Fewer than 2 `--reference` args → refused before any connection is
  attempted.
- A named feature slug not found on the camera's linked playfield →
  refused (exact-slug match only — an operator's typo must be a hard
  error, never a near-miss).
- A reference tag currently registered with a positive `mount_z` →
  refused before any detection is attempted (contamination guard).
- A reference tag not detected → fails fast at the first missing
  reference, never a partial solve over whatever *was* seen.
- A poor fit (RMS residual or condition number over threshold) →
  reported but not written, unless `--force`.
- `--all` → refused; solving one camera's geometry from another
  daemon's references has no sensible meaning.

**Open questions.** Are the provisional quality-gate thresholds
(`RMS_RESIDUAL_THRESHOLD_CM = 1.0`, `CONDITION_NUMBER_THRESHOLD =
447.2`) right for cameras or mount heights very different from this
fixture? Evidence-based and revisit-if-real-data-disagrees, the same
posture as `CONFIRMATION_STREAK` — ticket 029-004's real-hardware pass
is the first data point checked against them, and did not disagree
(real fixture RMS residual 0.1585 cm, condition number 67.44, both
comfortably inside threshold).

#### Scenarios

**UC-29.S-1 — The real fixture, solved and applied** · _happy path_
The worked example above, run live: `camera locate` solves nadir
(3.057, -2.799) cm, height 128.230 cm, RMS residual 0.1585 cm, and
writes `camera_position` on disk and live, without `--force` — the fit
is well within both quality thresholds.

**UC-29.S-2 — Ill-conditioned references** · _failure/warning path_
Two references placed diametrically opposite the camera's true nadir —
collinear with it by construction, since camera nadir, a tag's true
footprint, and its apparent position all lie on one ray. The solve does
not reproduce the ~20 cm-off, 150x-worse-residual failure a naive
line-intersection approach hit on this exact shape; it reports a
materially closer answer and/or a condition number the CLI flags as a
near-collinearity warning.

**UC-29.S-3 — Fewer than 2 references** · _failure path_
`aprilcam camera locate arducam-ov9782-usb-camera --reference
15:dot-northeast-orange:17.2` (only one) is refused with a clear error
before any connection is attempted.

**UC-29.S-4 — Contaminated reference** · _failure path_
Tag 15 is currently registered (UC-12) with `mount_z=5.0`. `camera
locate` refuses before attempting any detection, naming the tag and its
stale registration as the problem.

**UC-29.S-5 — Poor fit, no `--force`** · _failure path_
A near-collinear reference pair solves with a condition number over
threshold. The CLI reports the solve (nadir, height, RMS) but refuses to
write it. Re-running with `--force` writes it anyway. `--dry-run` never
writes, with or without `--force`.

---

## Traceability

This file covers 14 use cases and 55 scenarios (UC-20.S-4 added by
sprint 039, ticket 006, documenting the `--follow`/`--json` continuous
mode -- no existing use case or scenario number was renumbered or
reused). Tests are filled for
every use case named in sprint 035's Success Criteria (13, 14, 15, 18,
19, 20, 22, 25, 29); UC-16, UC-17, UC-21, UC-23, and UC-24 remain
unassigned — out of scope for sprint 035's layered test system, named
here as a deliberate gap rather than a silent omission. Every node ID
below was confirmed to resolve via
`uv run pytest --collect-only -q "<node id>"` (sprint 035 ticket 018;
`get_use_case_coverage()` itself is broken — always reports
`total_use_cases: 0` because it reads an unpopulated `clasi/usecases.md`
index rather than this file — so it was not used as the audit).

**The configuration lifecycle** spans four use cases in workflow order:
**UC-25 probe** → **UC-24 dump** → edit → **UC-23 push** → loop → install
on disk. UC-25.S-1 walks the whole chain end to end.

| Use case | Job | Actor(s) | Scenarios | Fan-out | Tests |
|---|---|---|---|---|---|
| UC-13 Daemon discovery/lifecycle | JTBD-05 | A-04, A-03 | S-1 ✓, S-2 ✓, S-3 ✓, S-4 ✗, S-5 ✗ | list/status only | `tests/aprilcam/cli/test_daemon_cli.py::test_start_reports_already_running_naming_the_holder`, `tests/aprilcam/cli/test_daemon_cli.py::test_restart_on_a_stopped_daemon_still_starts_it`, `tests/aprilcam/cli/test_cli_integration.py::test_daemon_and_camera_round_trip_against_real_hardware` |
| UC-14 List cameras | JTBD-05 | A-03 | S-1 ✓, S-2 ✓, S-3 ✓, S-4 ✗ | yes | `tests/aprilcam/cli/test_camera_cli.py::test_list_reports_connection_failure_with_typed_message`, `tests/aprilcam/daemon/test_lifecycle_integration.py::test_start_stop_and_restart_stability_against_real_cameras` |
| UC-15 Calibrate | JTBD-06, 01 | A-03 | S-1 ✓, S-2 ✗, S-3 ✗, S-4 ✗ | no | `tests/aprilcam/cli/test_calibrate_cli.py::test_calibrate_degenerate_geometry_failure_prints_the_named_reason`, `tests/aprilcam/cli/test_calibrate_cli.py::test_calibrate_refuses_all_before_ever_connecting`, `tests/aprilcam/daemon/test_service.py::test_calibrate_with_no_playfield_link_raises_before_touching_camera_backend` |
| UC-16 Raw video | JTBD-02 | A-03 | S-1 ✓, S-2 ✓, S-3 ✗ | no | — |
| UC-17 Raw frame | JTBD-02 | A-03, A-02 | S-1 ✓, S-2 ✓, S-3 ✓, S-4 ✗ | yes | — |
| UC-18 Playfield video | JTBD-02, 01 | A-03 | S-1 ✓, S-2 ✗ | no | `tests/aprilcam/view/test_viewer.py::test_run_playfield_view_draws_apriltag_1_at_the_image_centre_for_a_centred_world_position`, `tests/aprilcam/view/test_viewer.py::test_run_playfield_view_surfaces_calibration_stale_as_a_status`, `tests/aprilcam/cli/test_playfield_cli.py::test_view_uncalibrated_camera_points_at_camera_calibrate` |
| UC-19 Playfield frame | JTBD-02 | A-03, A-02 | S-1 ✓, S-2 ✗ | yes | `tests/aprilcam/cli/test_playfield_cli.py::test_config_dump_h_prints_help_not_a_missing_hostname_error`, `tests/aprilcam/daemon/test_service.py::test_get_frame_deskewed_uncalibrated_never_touches_the_camera_backend` |
| UC-20 List tags | JTBD-01 | A-03, A-02 | S-1 ✓, S-2 ✓, S-3 ✗, S-4 ✓ | yes | `tests/aprilcam/cli/test_camera_cli.py::test_tags_prints_uncalibrated_flag_and_pixel_never_a_world_looking_number`, `tests/aprilcam/cli/test_camera_cli.py::test_tags_with_nothing_detected_prints_an_empty_result_not_an_error`, `tests/aprilcam/daemon/test_detection_hardware.py::test_camera_tags_cli_output_is_legible_against_real_hardware`, `tests/aprilcam/cli/test_camera_cli.py::test_tags_follow_prints_a_timestamped_row_per_tag_per_frame_until_interrupt`, `tests/aprilcam/cli/test_camera_cli.py::test_tags_follow_json_emits_one_parseable_json_object_per_line`, `tests/aprilcam/cli/test_camera_cli.py::test_tags_follow_all_is_refused_before_any_connection` |
| UC-21 Generate tags | JTBD-05 | A-03 | S-1 ✓, S-2 ✓, S-3 ✗ | n/a (local) | — |
| UC-22 MCP server | JTBD-01, 02, 03 | A-02, A-03 | S-1 ✓, S-2 ✓, S-3 ✗ | n/a | `tests/aprilcam/mcp/test_integration.py::test_golden_path_against_a_fake_daemon`, `tests/aprilcam/mcp/test_integration.py::test_daemon_restart_mid_session_fails_then_recovers`, `tests/aprilcam/mcp/test_integration.py::test_no_daemon_at_startup_then_recovers_once_one_starts` |
| UC-23 Push config | JTBD-05 | A-03 | S-1 ✓, S-2 ✓, S-3 ✓, S-4 ✗, S-5 ✗ | no | — |
| UC-24 Dump config | JTBD-05 | A-03 | S-1 ✓, S-2 ✓, S-3 ✓, S-4 ✗ | yes | — |
| UC-25 Probe / hotplug | JTBD-05 | A-03 | S-1 ✓, S-2 ✓, S-3 ✓, S-4 ✗, S-5 ✓, S-6 ✓, S-7 ✗ | yes | `tests/aprilcam/camera/test_discovery.py::test_probe_reports_present_usable_cameras_matching_registry_resolution`, `tests/aprilcam/camera/test_discovery.py::test_probe_reports_present_but_unopenable_camera_never_omitted`, `tests/aprilcam/cli/test_camera_cli.py::test_list_all_reports_discovery_failed_distinctly`, `tests/aprilcam/daemon/test_detection_hardware.py::test_camera_tags_by_cli_number_and_slug_agree_on_real_hardware` |
| UC-29 Locate a camera's position | JTBD-06, 01 | A-03 | S-1 ✓, S-2 ✗, S-3 ✗, S-4 ✗, S-5 ✗ | no | `tests/aprilcam/cli/test_locate_cli.py::test_locate_dry_run_never_writes_even_with_a_poor_fit`, `tests/aprilcam/cli/test_locate_cli.py::test_locate_fail_fast_stops_at_the_first_undetected_reference`, `tests/aprilcam/daemon/test_camera_geometry_hardware.py::test_locate_reports_missing_references_or_reproduces_the_documented_fixture`, `tests/acceptance/test_inventory.py::test_where_surveyed_apriltag` |

✓ happy path · ✗ failure path

**Coverage.** Every use case has at least one happy path and one failure
path.

### Decisions resolved 2026-07-29

| Question | Resolution |
|---|---|
| Which short flag selects the daemon? | **`-d` / `--daemon`.** `-h` stays `--help`. |
| Which commands may fan out? | **Read-only only** — list, status, tags, image. Never calibrate, view, or lifecycle. |
| Does v1's implicit multi-host listing survive? | **No.** `camera list` targets one daemon; multi-daemon is opt-in via `--all`, with every row labelled. |
| Calibrate without a playfield? | **Error.** Nothing to calibrate; not attempted. |
| Is `camera view` processed? | **No.** Camera settings applied, nothing else — the counterpart to `playfield view`. |
| Does `aprilcam mcp` bind a daemon at startup? | **No.** Starts unbound, resolves per tool call, survives daemon restarts. |
| Is OpenCV in the base install? | **Yes.** `tags generate` needs it and must work everywhere. Reverses [specification.md](../specification.md) §9; the vision *boundary* is unchanged. |
| How is a config push targeted? | **By the document's own `name`** — the command takes a file and nothing else. |
| Do camera configs self-name? | **They will in v2** — a `name` field is added. Today identity comes from the containing directory. |
| Where does a camera's first config come from? | **Probe then dump.** Never hand-written. UC-25 → UC-24 → edit → UC-23 → install. |
| Does the daemon watch for camera hotplug? | **Yes, continuously.** Plugging a camera into a running daemon needs no command. |
| Then why keep `probe`? | **libcamera / Raspberry Pi cameras** are found by active interrogation (`/sys`, the `cam` tool), not device events. Probe also invalidates caches — v1 cached the camera list for the whole process lifetime. |
| Are camera names random or derived? | **Derived and readable** — v1's identity chain (libcamera id → serial → vid:pid@location → name slug). Stable across reboots and re-plugs. |

### Not yet covered

- **Tag parameter registration** (UC-12: size, mount X/Y/Z, yaw) has no
  CLI surface yet. v1 had `aprilcam mobile` for this. Note these are
  per-tag *runtime registrations*, not a configuration document — so
  whether they belong in UC-23's push model or need their own command is
  an open design question.
- **Feature queries** (UC-08, "where is the blue dot") have no CLI
  command yet.
- `aprilcam init`, `probe`, `config`, `tool`, `migrate-dirs` from v1 are
  unaddressed — some may simply be dropped.
