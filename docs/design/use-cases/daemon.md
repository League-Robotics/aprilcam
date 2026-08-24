# Use Cases — Daemon

**Status:** IN PROGRESS — drafted 2026-07-29, awaiting review
**Index:** [README.md](README.md) — jobs, actors, conventions, worked example

Bring-up and the query/stream surface of `aprilcamd`: starting it,
configuring it, calibrating a camera, and getting images, tags, and
feature positions out of it.

UC-01 … UC-05 are a **sequence**: each depends on the one before it, and
together they take a daemon from "just started, knows nothing" to
"calibrated and answering questions." UC-06 … UC-12 are the steady-state
surface, all of which assume UC-05 is satisfied.

The worked example throughout — cameras `arducam-ov9782-usb-camera` and
`hd-usb-camera`, playfield `main-playfield` — is defined in
[README.md](README.md) §3.
---

### UC-01 — Start the daemon and configure it

**Job:** JTBD-05 · **Primary actor:** A-04 · **Also serves:** A-03
**Status:** draft
**Interfaces:** CLI (`aprilcam daemon`), Python

**Goal.** The operator starts `aprilcamd` with no configuration
whatsoever and gets a running daemon that has discovered its attached
cameras and is ready to be configured over RPC.

**Preconditions.** No daemon running. Cameras may or may not be attached.

**Main success path.**
1. Operator starts the daemon with no config arguments.
2. The daemon takes its singleton lock and opens its control plane
   (unix socket + TCP, mDNS-advertised).
3. The daemon enumerates attached cameras and assigns each a persistent
   enumeration number keyed to its stable identity. Enumeration is an
   **active interrogation** of every backend, not just a device listing,
   so libcamera/Pi cameras are found (see [cli.md](cli.md) UC-25).
4. The daemon begins **watching for cameras arriving and disappearing**,
   and keeps its list current for as long as it runs. Plugging a camera
   into a running daemon requires no command.
5. The daemon reports itself ready. It knows its cameras exist; it knows
   nothing about what they are looking at — no playfield, no calibration.

**The camera list is live, not a startup snapshot.** A long-running daemon
outlives the hardware configuration it started with. Cameras get
unplugged, moved between ports, and added. The daemon tracks this itself;
`aprilcam camera probe` ([cli.md](cli.md) UC-25) forces a deeper
re-interrogation when passive watching is not enough — notably for
libcamera devices, which are not discovered by device events.

**Configuration model — disk is the source of record, RPC is
ephemeral.** This is the rule the whole configuration story hangs on:

- **On disk** — `/etc/aprilcam` by default, overridable with
  `APRILCAM_CONFIG_DIR` — is the **source of record**. Every daemon start
  reads it fresh. The config root is **human-owned and not writable by
  the daemon**; daemon-written state (calibration, registry) lives
  separately under `/var/lib/aprilcam`. Full layout and cascade:
  [configuration.md](../configuration.md).
- **Over RPC** is a **live, in-memory override**. It takes effect
  immediately and is **never written to disk**.
- **Every daemon start discards all pushed configuration** and reloads
  from disk. A restart is therefore a reset to the on-disk truth.

The rationale is that playfields and cameras change rarely, so
persistence over RPC would be machinery serving a case that barely
happens. What *does* happen often is experimentation — trying several
camera configurations to see which works. RPC push serves exactly that:
cheap, immediate, and deliberately temporary. If an experiment wins, you
make it permanent by putting the file in the configuration directory
(e.g. `scp` to a remote daemon host), not by asking the daemon to
remember.

**Calibration persists — because it is written to a file.** The rule is
*"after a restart, configuration is reloaded from files; dynamically set
configuration is not persisted."* Calibration satisfies the first half:
the daemon **writes** `calibration.json` under `data_dir` when
calibration succeeds (UC-04), and reloads it at startup like any other
file. So a calibrated camera stays calibrated across restarts.

The distinction is not config-versus-calibration, it is **file-backed
versus in-memory**:

| | Written to a file | Survives restart |
|---|---|---|
| Config document on disk | yes, by the operator | **yes** |
| Config pushed over RPC | no | no |
| Calibration | yes, by the daemon | **yes** |
| Tag mount parameters (UC-12) | no — runtime registrations | no |

Calibration is the one thing the daemon writes for itself, and it is
regenerable state (`data_dir`), not an admin-owned document
(`config_dir`) — so re-running calibration overwrites it freely, and
nobody hand-edits it.

**Consequences worth stating plainly.**
- There is no persistence question, no write-back, no
  pushed-vs-stored precedence to reason about. Live state is either
  "what disk said" or "what disk said, then overridden until restart."
- A remote daemon still needs no write access from clients: pushes are
  memory-only, and permanence is an ordinary file copy over SSH.
- Losing a pushed config on restart is **the deal, not a bug** — no
  warning, no trace. The rule "restart = disk state" holds without
  exception, which is what makes it trustworthy.
- To keep a winning experiment, dump the live config (UC-03) and put the
  file on disk.

**Postconditions.** Daemon running; camera registry populated and
persistent across restarts; configuration exactly as disk describes it
(nothing, in the empty-start case); no camera calibrated.

**Failure modes.**
- Another daemon already holds the lock → refuse to start, say which.
- No cameras attached → start successfully with an empty camera list.
  This is *not* an error; configuration can arrive before hardware.
- A camera is present but cannot be opened → it appears in the registry
  and is flagged unusable, rather than being silently omitted.
- Config directory contains a malformed document → refuse to start, or
  start while reporting the bad file? (see open questions)

**Open questions.**
- Precedence when the configuration directory and an explicitly
  pointed-at file both describe the same camera. (Only an issue at
  startup now — the RPC dimension is gone.)
- A malformed file on disk at startup: refuse to start, or start
  degraded and report it? Refusing is safer for a field deployment;
  starting degraded is kinder when one of five cameras has a typo.
- *(Resolved — see "Calibration persists" below.)*

#### Scenarios

**UC-01.S-1 — Empty start with two cameras attached** · _happy path_
Nothing in the config directory. `arducam-ov9782-usb-camera` and
`hd-usb-camera` are plugged in. Daemon starts, reports ready, and lists
exactly those two cameras with stable enumeration numbers. Both report
no playfield link and no calibration. Restarting the daemon yields the
same two enumeration numbers.

**UC-01.S-2 — Empty start with no cameras** · _happy path_
No config, no cameras attached. Daemon starts successfully and reports
an empty camera list. A client can still connect and push a playfield
definition.

**UC-01.S-3 — Second daemon refused** · _failure path_
A daemon is already running. A second `aprilcam daemon` exits non-zero
with a message naming the existing instance, and does not disturb the
running one or its camera registry.

**UC-01.S-4 — Start from disk, override, restart, revert** · _happy path_
The defining round trip for the configuration model.

1. `/etc/aprilcam` holds a camera config for
   `arducam-ov9782-usb-camera` with exposure **A**, and the
   `main-playfield` definition.
2. Daemon starts and loads both from disk. Fetching the camera config
   returns exposure **A**; the playfield is present.
3. Client pushes a complete camera config over RPC with exposure **B**.
   It applies immediately — captured frames visibly change. Fetching the
   config now returns **B**. Nothing on disk has changed.
4. Daemon restarts.
5. Daemon reloads from disk. The pushed config is **gone**: fetching
   returns exposure **A** again, with no warning and no trace of **B**.

The on-disk file was never touched at any point. To make **B**
permanent, the operator writes it to `/etc/aprilcam` — by hand or with
the dump from UC-03.S-5 — and restarts.

**UC-01.S-4a — Variation: exposure sweep** · _happy path_
As S-4, but the client pushes exposure **B**, then **C**, then **D** in
succession, capturing a frame after each to compare. Each push wholly
replaces the previous one — the live config is always exactly the last
document pushed, never an accumulation. **D** wins; the operator dumps
it and copies it to disk. A restart before copying would have lost all
three, which is the accepted trade.

**UC-01.S-4b — Variation: remote daemon** · _happy path_
As S-4, but the daemon is on a Raspberry Pi over the field and the
client is on the developer's laptop. The push crosses the network and
behaves identically. Making it permanent means `scp` to the Pi's
`/etc/aprilcam` — the daemon never writes config to disk on any client's
behalf, so no remote write path or authentication story is needed.

---

### UC-02 — Push a playfield definition to a running daemon

**Job:** JTBD-05, JTBD-03 · **Primary actor:** A-03 · **Also serves:** A-01, A-02
**Status:** draft
**Interfaces:** CLI, Python, MCP

**Goal.** A client sends the complete `main-playfield` definition to a
running daemon, which adopts it immediately and serves it to any client —
local or remote — as the shared ground truth, until the daemon restarts.

**Preconditions.** Daemon running (UC-01). The definition exists locally,
typically in a repo.

**Ephemeral, per UC-01.** A pushed playfield lives **in memory only**.
The daemon does not write it to `config_dir`, and a restart reloads
whatever the configuration directory says — discarding the push. The
normal way to install a playfield permanently is to put the file in the
configuration directory (locally, or by `scp` to a remote daemon host);
pushing is for trying one out.

**Main success path.**
1. Client reads the local playfield JSON — the same document format in
   use today.
2. Client pushes it whole over RPC. There is no field-level edit and no
   authentication; the local network is trusted.
3. Daemon validates it and adopts it as calibration ground truth and as
   the answer source for feature queries — in memory, without writing to
   disk.
4. Any client can now fetch the definition back verbatim, plus derived
   information (which cameras are linked to it).

**Postconditions.** Daemon holds `main-playfield`: 1 AprilTag, 10 ArUco
tags, 6 rectangles, 8 dots, field 134.3 × 89.3 cm. **It does not survive
a restart** unless it is also on disk. No camera is linked to it yet.

**Failure modes.**
- Malformed or schema-invalid JSON → reject whole, keep any previous
  definition intact. Never partially apply.
- A definition naming duplicate ArUco ids, or a field size inconsistent
  with its corner coordinates → reject with the specific conflict.
- Pushing a definition that replaces one an existing calibration was
  computed against → accept, but mark affected calibrations stale.

**Open questions.**
- Can a daemon hold more than one playfield at once? Cardinality in
  spec §3 says a camera observes exactly one playfield, but does not
  bound how many playfields the daemon may know.
- Is replacing a playfield out from under a live calibration allowed at
  all, or does it require an explicit force?

#### Scenarios

**UC-02.S-1 — Push main-playfield to an empty daemon** · _happy path_
Daemon from UC-01.S-1, knows no playfields. Client pushes
`main-playfield.json`. Daemon accepts. Fetching it back returns all 25
features with identical coordinates — ArUco 1 at (−67, 44.65), ArUco 5
at (67, −44.65), `dot-west-blue` at (−50, 0) — and reports zero linked
cameras. After a daemon restart the playfield is **gone**, because the
config directory is empty and the push was memory-only.

**UC-02.S-1a — Variation: playfield installed on disk** · _happy path_
Same definition, but placed in the configuration directory instead of
pushed. The daemon loads it at startup and it survives every restart.
This is the normal deployment; S-1 is the experiment.

**UC-02.S-2 — Reject malformed definition, keep the old one** · _failure path_
`main-playfield` is already loaded. Client pushes a definition with
ArUco id 3 listed twice at different coordinates. Daemon rejects it,
naming the duplicate id. Fetching the definition still returns the
original, unmodified.

---

### UC-03 — Discover and configure a camera

**Job:** JTBD-05 · **Primary actor:** A-03
**Status:** draft
**Interfaces:** CLI, Python, MCP

**Goal.** The developer asks what cameras exist, asks what is
configurable on one, and pushes back a complete config which the daemon
applies to the hardware immediately.

**Preconditions.** Daemon running; playfield available (UC-02).

**Ephemeral, per UC-01.** A pushed camera config is **in memory only**
and is discarded on restart. This is what makes configuration
experiments cheap: push a config, look at a frame, push another. To keep
one, write it to the configuration directory — see the dump in S-5.

**Main success path.**
1. Client asks for the camera list; daemon returns its persistent
   enumeration.
2. Client asks for one camera's configuration. The daemon returns what
   it can introspect (dimensions, device name) **plus every
   configuration key the client could set — present but empty or blank
   when unset.** The response is self-describing: it tells you what the
   knobs are, not just which ones happen to have values.
3. Client fills in values locally — gain, shutter/exposure, brightness,
   UVC controls, resolution, the playfield link, camera mount position.
4. Client pushes the complete config document back.
5. Daemon applies it to the camera hardware **immediately**, not at next
   open, and holds it in memory until restart.

**Postconditions.** Camera `arducam-ov9782-usb-camera` has the pushed
config in effect, is linked to `main-playfield`, and the hardware
reflects the new settings. Fetching `main-playfield` now reports one
linked camera. Nothing has been written to disk.

**Capturing a winning experiment.** Because pushes are ephemeral, the
daemon offers a **dump**: emit a camera's live in-memory configuration in
exact on-disk format. The operator redirects it to a file and installs it
in the configuration directory — locally or by `scp` to a remote host.
The daemon never writes configuration on a client's behalf; it only hands
back a document the operator chooses to save.

**Every camera config begins as a probe and a dump.** The
introspected-plus-blank document of S-1 is not just a query result — it is
the **template every camera configuration starts from**. Nobody
hand-writes one. The full lifecycle is probe → dump → edit → push →
iterate → install on disk; see [cli.md](cli.md) UC-25.

**Per-camera, never shared.** A configuration document belongs to
exactly one camera. There is no shared, inherited, or class-wide camera
config — two OV9782s side by side are configured independently. The
settable keys a camera reports are **that camera's** keys, since
different hardware supports different controls.

**Self-naming (v2 schema change).** A camera config carries a `name`
field holding the camera slug, so the document identifies its own target.
v1 configs do not — identity came from the containing directory
(`cameras/arducam-ov9782-usb-camera/config.json`), and `device_name` is a
hardware descriptor, not the slug. Self-naming makes a config portable: a
scratch file anywhere on disk is as pushable as one in the configuration
directory, which is what the experiment workflow needs (see
[cli.md](cli.md) UC-23).

**Unsupported values are hard errors.** If a pushed config contains a
setting the camera does not support, or a value the hardware will not
take, the daemon **fails the push with an error** naming the offending
key. It does not apply what it can and shrug at the rest, and it never
reports success for a setting that did not take effect. This is the
behaviour that must be *really well understood*: a config push either
fully applied or it failed and said why. v1's blind success reporting
here is exactly what this rule exists to kill.

**Failure modes.**
- Camera not present → clear error naming the unknown camera.
- Config contains a key this camera does not support → **error, push
  rejected**, offending key named.
- Value out of the range the hardware accepts → **error**, with the
  accepted range.
- A setting that cannot be applied on a live handle without first
  changing a mode (a known OV9782/UVC trait) → the daemon must either
  sequence the modes correctly and succeed, or **fail with an error**.
  Silently not applying is forbidden.
- Config names a playfield the daemon does not hold → reject, or accept
  with an unlinked warning? (see open questions)
- Push while the camera is streaming to subscribers → apply, and
  subscribers see the change mid-stream.

**Open questions.**
- Does linking to an unknown playfield fail, or defer?
- Can a config be pushed for a camera that is not currently attached?
  (Unsupported-key checking needs the hardware to answer, which suggests
  no — or a deferred validation.)

#### Scenarios

**UC-03.S-1 — Fetch a blank config and see every available knob** · _happy path_
Freshly enumerated `arducam-ov9782-usb-camera`, never configured. Client
requests its config. Response includes introspected facts (device name,
frame dimensions) and the full set of settable keys **for this camera** —
gain, exposure, brightness, white balance, resolution, playfield link,
camera position — each present with an empty/blank value. The client can
enumerate what is settable without prior knowledge or documentation.

**UC-03.S-2 — Push a manual-exposure config, applied at once** · _happy path_
Client pushes a complete config: manual exposure at a low value suited
to the OV9782, fixed gain, `playfield: main-playfield`, camera position
recorded. Daemon persists and applies immediately. A frame captured
right after is visibly darker than one captured before. Re-fetching the
config returns the pushed values; fetching `main-playfield` reports this
camera as linked.

**UC-03.S-3 — Unsupported setting is rejected outright** · _failure path_
Client pushes a config to `hd-usb-camera` containing an exposure value
its hardware will not accept on a live handle. The push **fails** with
an error naming the exposure key. No part of the config is applied — the
camera is left exactly as it was, not half-configured. Re-fetching the
config returns the previous state.

**UC-03.S-4 — Two identical cameras configured independently** · _happy path_
Two OV9782 cameras are attached. A config pushed to the first does not
change the second; fetching the second's config still shows its own
values.

**UC-03.S-5 — Dump a live config to install it permanently** · _happy path_
Following the sweep in UC-01.S-4a, exposure **D** is the winner and lives
only in the daemon's memory. The operator dumps
`arducam-ov9782-usb-camera`'s live config and gets a document byte-wise
valid as an on-disk config file — same schema the daemon loads at
startup. Copying it into the configuration directory and restarting
yields a daemon whose loaded-from-disk config is exactly **D**, now
surviving every subsequent restart. Round trip closed: dump → install →
restart → identical config.

---

### UC-04 — Calibrate a camera against the playfield

**Job:** JTBD-06, JTBD-01 · **Primary actor:** A-03 · **Also serves:** A-02
**Status:** draft
**Interfaces:** CLI, Python, MCP

**Goal.** The developer tells the daemon to calibrate the camera. The
daemon finds the ArUco corner markers in its own view, matches them to
their surveyed positions, computes a homography, and reports whether it
succeeded — after which pixel positions can become world positions.

**Preconditions.** Camera configured and linked to `main-playfield`
(UC-03). The physical field is in view with its ArUco markers visible.

**Main success path.**
1. Client triggers calibration for the camera. All vision work happens
   **in the daemon** — the client sends a command, not pixels.
2. Daemon detects ArUco markers across multiple averaged frames.
3. Daemon matches detected ids to surveyed coordinates from the pushed
   definition — ArUco 1 → (−67, 44.65), and so on.
4. Daemon computes the homography, augments it with all surveyed markers
   it found, and records static markers so later deskew does not require
   the corners to stay visible.
5. Daemon reports **explicit success or failure**. On success the client
   can fetch the resulting homography and inspect it — informational,
   not something the client needs in order to work.

**Postconditions.** Camera has a persisted calibration with provenance.
World-coordinate queries and the deskewed view are available. Position
answers are now in cm rather than pixels.

**Acceptance rule — all specified fiducials must be seen.** The
playfield definition is the authority on what the camera must find.
Calibration succeeds only when **every ArUco fiducial the definition
specifies is detected**. For `main-playfield` that is all 10 —
perimeter 1–8 plus internal 9 and 10. Seeing 9 of 10 is a **failure**,
not a degraded success. There is no partial calibration and no
best-effort solve: an incomplete view means the camera cannot see the
field it claims to be looking at, and any homography from it would be
silently wrong somewhere.

**Failure modes.**
- Any specified fiducial not detected → explicit failure naming exactly
  which ids are missing. No calibration stored.
- Detected ids not in the definition (a stray marker on the field) →
  ignored, reported.
- Degenerate geometry (markers near-collinear, or a physically
  implausible solution) → reject rather than store a bad homography.
- Field or camera moved after calibration → the stored calibration is
  now wrong; flagged stale rather than silently trusted.

**Open questions.**
- What is the acceptance threshold *beyond* completeness? All fiducials
  found is necessary; is it sufficient, or is there also a maximum
  reprojection error in cm? A complete-but-sloppy solve is still a bad
  solve.
- Does a failed calibration attempt invalidate an existing good one?
  (Drafted as: no — a failure leaves the previous calibration untouched.)

#### Scenarios

**UC-04.S-1 — Calibrate with every specified fiducial visible** · _happy path_
`arducam-ov9782-usb-camera` configured per UC-03.S-2, viewing the field
with all 10 ArUco fiducials `main-playfield` specifies in view —
perimeter 1–8 plus internal 9 at (−35, 0) and 10 at (35, 0). Client
triggers calibration. Daemon reports success, having matched all 10 ids
to their surveyed coordinates. Client fetches the homography and
inspects it. AprilTag 1 at the field centre now reports world position
(0, 0) ± tolerance.

**UC-04.S-2 — One fiducial occluded is a failure** · _failure path_
Same setup, but ArUco 6 at (0, −44.65) is covered by a robot. Nine of
ten fiducials are detected. Calibration **fails**, naming ArUco 6 as
missing. No calibration is stored; a previously good calibration is left
as it was. The developer moves the robot and retries.

**UC-04.S-3 — Camera badly aimed** · _failure path_
Camera aimed so only ArUco 1 and 2 are in frame. Calibration fails
explicitly, naming the eight missing ids rather than only reporting a
count.

---

### UC-05 — Confirm the system is ready to answer questions

**Job:** JTBD-06 · **Primary actor:** A-03 · **Also serves:** A-01, A-02
**Status:** draft
**Interfaces:** CLI, Python, MCP

**Goal.** Before relying on the system, a client can establish that a
given camera and playfield pair is fully brought up — configured,
calibrated, not stale — and get a clear reason when it is not.

**Preconditions.** Daemon running.

**Main success path.**
1. Client asks the daemon for the status of a camera/playfield pair.
2. Daemon reports: playfield known, camera linked, calibration present,
   calibration fresh, frame not dark, camera readable.
3. Client proceeds to UC-06 onward.

**Postconditions.** None — this is a read.

**Failure modes.** Each negative answer must name *which* precondition
failed and what to do about it, because this is the diagnostic a
developer reaches for when positions look wrong.

**Open questions.** What makes a calibration "stale" — a config change,
a camera reopen, elapsed time, or only an explicit signal?

#### Scenarios

**UC-05.S-1 — Fully ready** · _happy path_
After UC-04.S-1, the client asks for status and gets an affirmative on
every precondition, naming playfield `main-playfield` and the camera's
calibration provenance.

**UC-05.S-2 — Configured but never calibrated** · _failure path_
After UC-03.S-2 but before UC-04, status reports the camera as linked
and configured but uncalibrated, and says explicitly that world
coordinates are unavailable until calibration runs.

---

### UC-06 — Get a single image from a camera

**Job:** JTBD-02 · **Primary actor:** A-02 · **Also serves:** A-03
**Status:** draft
**Interfaces:** CLI, Python, MCP

**Goal.** A client asks the playfield for one image and gets it — raw as
the camera sees it, or the deskewed top-down view.

**Preconditions.** UC-05 satisfied (deskewed requires calibration; raw
does not).

**Addressing.** Images are always requested **from a camera**, never
from a playfield. A playfield may have only one camera in practice, but
the daemon never guesses which camera a request means — the client names
it. The playfield supplies the *geometry* that makes the deskewed view
meaningful; the camera supplies the pixels.

**Main success path.**
1. Client requests a frame from a named camera, choosing raw or
   deskewed.
2. Daemon returns the encoded image plus the metadata needed to map
   world cm ↔ image pixels for the deskewed view.

**Postconditions.** None — a read.

**Failure modes.**
- Deskewed requested on an uncalibrated camera → explicit failure, not a
  raw frame silently substituted.
- Camera read failure → error, with the daemon self-healing the device.
- Frame too dark to be useful → returned, but flagged advisory.
- No camera named → error. There is no default camera and no
  playfield-resolves-the-camera shortcut.

**Open questions.** None. *(Resolved 2026-07-29: camera-addressed
always. Playfields may have only one camera, but addressing is by
camera regardless — no implicit resolution.)*

#### Scenarios

**UC-06.S-1 — One deskewed image from a named camera** · _happy path_
Client asks `arducam-ov9782-usb-camera` — calibrated per UC-04.S-1 and
linked to `main-playfield` — for a deskewed frame. Returns an image
whose field fits an 800×800 bounding box aspect-preserved
(134.3 × 89.3 cm → 800 × 532), with metadata giving the derived
px-per-cm. North is up, east is right. Converting world (0, 0) through
the metadata lands on AprilTag 1 in the image.

**UC-06.S-2 — Raw single frame from a named camera** · _happy path_
Client asks `arducam-ov9782-usb-camera` for a raw frame and gets the
undistorted-but-unwarped camera view. Works with no calibration present.

**UC-06.S-3 — Deskewed requested without calibration** · _failure path_
Camera per UC-05.S-2 — configured, uncalibrated. A deskewed frame
request fails explicitly saying calibration is required. No raw frame is
substituted.

---

### UC-07 — Stream video from a camera

**Job:** JTBD-02 · **Primary actor:** A-03 · **Also serves:** A-01
**Status:** draft
**Interfaces:** CLI, Python (MCP: out — an agent takes single frames)

**Goal.** A client subscribes to a continuous stream of images from a
camera and receives frames until it unsubscribes.

**Preconditions.** As UC-06. Streams are **camera-addressed**, as with
single frames.

**Main success path.**
1. Client subscribes to the raw or deskewed stream of a named camera.
2. Daemon allocates a stream endpoint and publishes frames, computing
   the deskewed image once and sharing it with all subscribers.
3. Frame geometry is stable for the life of the stream.
4. Client unsubscribes; if no subscribers remain the daemon drops to
   low-rate idle capture.

**Postconditions.** No lasting state.

**Failure modes.**
- Slow consumer → bounded queue, frames dropped silently, pipeline never
  stalls and other subscribers are unaffected.
- Camera fails mid-stream → daemon self-heals; the stream reports a gap
  rather than dying silently.
- Config pushed mid-stream (UC-03) → settings change, geometry does not.

**Open questions.** Should a mid-stream config change that alters
resolution force a new stream rather than break the stability promise?

#### Scenarios

**UC-07.S-1 — Two subscribers share one computation** · _happy path_
The viewer and a robot program both subscribe to
`arducam-ov9782-usb-camera`'s deskewed stream. Both receive 800×532
frames with identical metadata. The daemon warps each frame once, not
twice.

**UC-07.S-2 — Slow subscriber drops frames** · _failure path_
One subscriber reads far slower than frame rate. It misses frames; the
fast subscriber's rate is unaffected and the capture loop never blocks.

**UC-07.S-3 — Last subscriber leaves** · _happy path_
Both unsubscribe. The daemon stops publishing and reverts to idle
capture without closing the camera.

---

### UC-08 — Ask where a known feature is

**Job:** JTBD-03 · **Primary actor:** A-02 · **Also serves:** A-01, A-03
**Status:** draft
**Interfaces:** CLI, Python, MCP

**Goal.** A client asks for a feature by name — "the blue dot" — and
gets its surveyed world position from the playfield definition, found by
forgiving text search rather than exact slug.

**Preconditions.** Playfield pushed (UC-02). **No calibration needed** —
this answers from the definition, not from vision.

**Main success path.**
1. Client asks for a feature by an approximate name.
2. Daemon does a fuzzy/full-text match against feature slugs, colors,
   types, and cardinal positions in the definition.
3. Daemon returns the matched feature and its surveyed world position.

**Postconditions.** None — a read.

**Failure modes.**
- No match → say so, ideally with near misses.
- Ambiguous match ("the orange dot" matches four) → return all matches
  rather than arbitrarily picking one.
- Feature exists in the definition but has been physically removed from
  the field → the definition answers anyway; it is ground truth, not
  observation. This is a real limitation and should be understood, not
  hidden.

**Open questions.**
- Ranked-best-match or all-matches-above-threshold?
- Colored features are surveyed-position-only now that color *detection*
  is dropped. Is "where is the blue dot" still meaningful if nothing
  verifies it is there? (Stakeholder decision: yes, definition answers.)

#### Scenarios

**UC-08.S-1 — Fuzzy match on a unique feature** · _happy path_
Client asks for "the blue dot" against `main-playfield`. Matches
`dot-west-blue` and returns (−50, 0) cm. Asking for "blue dot west",
"west blue", or the exact slug all reach the same feature.

**UC-08.S-2 — Ambiguous color query returns all candidates** · _failure path_
Client asks for "the orange dot". Four exist —
`dot-northwest-orange` (−50, 30), `dot-northeast-orange` (50, 30),
`dot-southeast-orange` (50, −30), `dot-southwest-orange` (−50, −30).
All four are returned with their positions rather than one being chosen
silently.

**UC-08.S-3 — No such feature** · _failure path_
Client asks for "the orange triangle". `main-playfield` has no
triangles. The daemon reports no match, and does not return the nearest
orange thing as though it were a triangle.

---

### UC-09 — Ask for one tag by its number

**Job:** JTBD-01 · **Primary actor:** A-01 · **Also serves:** A-02
**Status:** draft
**Interfaces:** CLI, Python, MCP

**Goal.** A robot program that knows its own tag id asks where that tag
is and gets its world position and heading — or a clear "not currently
detected."

**Preconditions.** UC-05 satisfied for world coordinates.

**Tag families are distinct in the interface.** A client asks for
"ArUco 5" or "AprilTag 5" and these are **understood as different
things**. The family is part of the request, never inferred from the id.
v1's internal `-(N+1)` ArUco encoding is a daemon implementation detail
that **never appears in the interface** — no client sends or receives a
negative tag id.

**Main success path.**
1. Client asks for a tag by **family and id** — AprilTag 7, or ArUco 5.
2. Daemon returns the most recent detection: world position in cm,
   heading, velocity, and detection freshness.

**Postconditions.** None — a read.

**Failure modes.**
- Tag not currently visible → explicit "not detected", distinguished
  from "detected at (0,0)". These must never be confusable.
- Tag id not in the playfield definition → still answerable if detected;
  a mobile robot tag legitimately is not a surveyed feature.
- Uncalibrated camera → pixel position only, explicitly flagged as
  uncalibrated. Pixels are diagnostic, never presented as world
  coordinates.

**Open questions.**
- How stale is too stale? Does a tag seen 3 s ago come back with a
  timestamp, or as not-detected?

#### Scenarios

**UC-09.S-1 — Locate the robot's own tag** · _happy path_
Robot carrying AprilTag 7 asks `arducam-ov9782-usb-camera` for AprilTag
7, calibrated per UC-04.S-1. Gets a world position in cm within the
134.3 × 89.3 field, a heading with 0 = east and CCW positive, and a
timestamp.

**UC-09.S-2 — Centre tag reads as the origin** · _happy path_
Client asks for AprilTag 1, the surveyed centre tag at (0, 0). Reported
world position is (0, 0) within tolerance — the check that the geometry
conventions hold end to end.

**UC-09.S-3 — Same id, different family, different tag** · _happy path_
`main-playfield` contains **both** AprilTag 1 (centre, (0, 0)) and ArUco
1 (northwest, (−67, 44.65)). A request for AprilTag 1 returns (0, 0); a
request for ArUco 1 returns (−67, 44.65). The id alone is ambiguous; the
family disambiguates, and no negative id appears anywhere in either
request or response.

**UC-09.S-4 — Tag not on the field** · _failure path_
Client asks for AprilTag 99, which is not present. Response is an
explicit not-detected, clearly distinct from a zero position.

**UC-09.S-5 — Uncalibrated camera** · _failure path_
Camera per UC-05.S-2. Asking for AprilTag 7 returns a pixel position
flagged as uncalibrated with no world coordinates, rather than world
coordinates computed from an absent homography.

---

### UC-10 — Get all detected tags at once

**Job:** JTBD-01, JTBD-04 · **Primary actor:** A-02 · **Also serves:** A-01
**Status:** draft
**Interfaces:** CLI, Python, MCP

**Goal.** A client asks a camera for the tags it currently detects and
gets **all of them**. There is no count, no limit, no pagination.

**Preconditions.** UC-05 satisfied.

**Main success path.**
1. Client asks a camera for its detected tags.
2. Daemon returns **every** current tag record, world-first — the
   complete detection set for the latest frame.

**Postconditions.** None — a read.

**Failure modes.**
- Nothing detected → empty result, not an error.
- Uncalibrated camera → pixel positions only, flagged, as in UC-09.

**Open questions.** None. *(Resolved 2026-07-29: no count parameter —
you get them all. With no truncation there is no ordering question
either. This is a current-state query only; frame history is not part of
this use case.)*

#### Scenarios

**UC-10.S-1 — Three robots on the field** · _happy path_
AprilTags 7, 12, 30 are on the field plus surveyed centre tag 1. Client
asks `arducam-ov9782-usb-camera` for detected tags and receives exactly
4 records — all of them, unbounded — each with world position, heading,
and velocity.

**UC-10.S-2 — Empty field** · _happy path_
No tags in view. The result is an empty list, not an error.

**UC-10.S-3 — Uncalibrated camera** · _failure path_
Camera per UC-05.S-2. All detected tags come back with pixel positions
flagged as uncalibrated and no world coordinates — never world
coordinates fabricated from an absent homography.

---

### UC-26 — Ask where a tag has recently been

**Job:** JTBD-04, JTBD-01 · **Primary actor:** A-02 · **Also serves:** A-01
**Status:** draft
**Interfaces:** CLI, Python, MCP

> Numbered 26 but placed here, next to UC-10, because it is the
> historical counterpart to that current-state query. Identifiers are
> never renumbered ([README.md](README.md) §4).

**Goal.** A client asks for the recent past of tag detections — the last
N frames of records — so it can see trajectories, verify velocities, and
reason about where something was going rather than only where it is.

**Preconditions.** UC-05 satisfied. The daemon has been detecting long
enough to have history.

**Why history is a query, not a client's job.** A polling client sees
only the frames it happened to ask for, at whatever rate it managed —
too coarse to reconstruct a trajectory, and it misses everything between
polls. An MCP agent (A-02) especially cannot poll fast enough for this;
each call costs latency and tokens. The daemon is already computing every
frame, so keeping a bounded window of them costs almost nothing and is
the only place the full-rate record exists.

**The ring buffer.** The daemon keeps a fixed-size ring of per-frame tag
records — v1 used ~300 frames, roughly 10 seconds at 30 fps. It is
**bounded and lossy by design**: old frames fall off the end. This is
recent history for motion reasoning, explicitly **not** a recording
system or a log.

**Main success path.**
1. Client asks a camera for tag history — optionally for one tag
   (family + id, as UC-09), optionally bounded to the last N frames.
2. Daemon returns the buffered records, oldest to newest, each with its
   timestamp, world position, heading, and velocity — mount corrections
   (UC-12) already applied, as everywhere else.
3. Client computes whatever it needs: trajectory, acceleration, or a
   check on the velocity the daemon reported.

**Postconditions.** None — a read. Asking does not change the buffer.

**Failure modes.**
- Buffer not yet full (daemon just started) → return what exists, not an
  error. Partial history is useful.
- Requested more frames than the buffer holds → return everything, and
  say how many were available, rather than silently returning fewer than
  asked and letting the client assume that is all that ever happened.
- Tag absent for part of the window → gaps are **explicit** in the
  returned records. A tag that vanished for 10 frames must be
  distinguishable from one that never moved; interpolating across a gap
  would invent motion that did not occur.
- Uncalibrated camera → pixel positions flagged, as UC-10.S-3.
- Calibration changed mid-window → records computed under the old
  calibration are not comparable with the new ones. Flag rather than
  silently mixing two coordinate systems.

**Open questions.**
- Is the buffer size configurable, and per camera or global? v1 had a
  module constant.
- Does history survive a **camera** restart within a running daemon
  (self-heal reopen, UC-07)? Almost certainly not — but a client seeing
  a gap should be able to tell the difference between "nothing was
  detected" and "the camera was reset."
- Does the buffer hold every frame, or only frames where something
  changed? The tag *stream* is change-driven (UC-11); if the buffer is
  too, then "the last 300 frames" means something different on a quiet
  field than a busy one.

#### Scenarios

**UC-26.S-1 — Reconstruct a robot's path** · _happy path_
A robot carrying AprilTag 7 drives east across `main-playfield` over
about 3 seconds. The client asks for tag 7's history and receives an
ordered series of records with world x increasing monotonically, y
roughly constant, and timestamps spanning the drive — enough to plot the
path and compute acceleration, neither of which UC-09 or UC-10 could
answer.

**UC-26.S-2 — Verify a reported velocity** · _happy path_
The client takes the velocity reported for AprilTag 7 by UC-09 and checks
it against the history: differencing consecutive world positions over
their timestamps yields the same value within tolerance. This is the
check that makes the daemon's derived velocity trustworthy rather than
taken on faith.

**UC-26.S-3 — Buffer holds only recent history** · _happy path_
A robot drove across the field two minutes ago and has been still since.
Its history contains only the recent stationary records; the drive has
fallen off the end of the ring. The client can tell it is seeing a
bounded window, not the whole session.

**UC-26.S-4 — Tag disappeared mid-window** · _failure path_
AprilTag 12 was occluded for about half a second in the middle of the
requested window. The returned records show an explicit gap — not
interpolated positions bridging it, and not a silently shortened list
that would read as continuous motion.

---

### UC-11 — Stream tag positions as they are detected

**Job:** JTBD-04, JTBD-01 · **Primary actor:** A-01
**Status:** draft
**Interfaces:** CLI, Python (MCP: out — agents poll UC-09/UC-10)

**Goal.** A robot program subscribes to tag detections and receives
updates at whatever rate they are detected, without polling.

**Preconditions.** UC-05 satisfied.

**Main success path.**
1. Client subscribes to a named camera's tag stream (camera-addressed, as
   UC-06/UC-07).
2. Daemon publishes tag frames as detections change, rate-capped, with a
   heartbeat so a quiet field is distinguishable from a dead stream.
3. Records are world-first: position in cm, world velocity, speed,
   heading — with UC-12 mount corrections already applied.

**Postconditions.** No lasting state.

**Failure modes.**
- Slow consumer → bounded queue, silent drop, pipeline unaffected.
- No detections for a while → heartbeat proves liveness.
- Calibration goes stale mid-stream → records must not silently continue
  as though world coordinates were still trustworthy.

**Open questions.** On a change-driven stream, what counts as a change —
any sub-pixel jitter, or movement past a threshold? Too sensitive and it
is a full-rate stream; too coarse and slow drift is invisible.

#### Scenarios

**UC-11.S-1 — Follow a moving robot** · _happy path_
Robot with AprilTag 7 drives east across the field. Subscriber to
`arducam-ov9782-usb-camera`'s tag stream receives a series of records
with world x increasing, y roughly constant, heading near 0 (east), and
non-zero world velocity in cm/s.

**UC-11.S-2 — Quiet field heartbeats** · _happy path_
Nothing moves for several seconds. The subscriber still receives
heartbeats and can tell the stream is alive rather than stalled.

**UC-11.S-3 — Calibration goes stale mid-stream** · _failure path_
The camera is bumped mid-stream and its calibration is flagged stale.
Records must not keep flowing as though world coordinates were still
trustworthy — the subscriber is told, rather than quietly receiving
plausible-looking wrong positions. This is the worst failure mode in the
system: silently wrong coordinates are harder to catch than no
coordinates.

---

### UC-12 — Set a tag's physical parameters

**Job:** JTBD-01, JTBD-06 · **Primary actor:** A-01 · **Also serves:** A-03
**Status:** draft
**Interfaces:** CLI, Python, MCP

**Goal.** A client describes a tag's size and how it is mounted on its
robot, so that reported positions are **the robot's location, not the
tag's**. This is the point of the use case: nobody cares where a piece
of printed paper is; they care where the machine carrying it is.

**Preconditions.** Daemon running; calibration present for any mount
correction to be meaningful.

**The tag configuration.**

| Parameter | Meaning |
|---|---|
| `size` | Physical edge length of the tag. Has a **default** from the config the daemon loads at startup; overridable per tag at runtime. |
| `mount_x` | Offset from the robot's reference point to the tag, along the robot's own x axis. |
| `mount_y` | Offset along the robot's y axis. |
| `mount_z` | Height of the tag above the playfield plane. Drives the parallax projection. |
| `mount_yaw` | Rotation of the tag relative to the robot's forward direction, so reported heading is the *robot's* heading. |

Mount X, Y, and Z together place the tag relative to the robot's
reference point in three dimensions; yaw orients it. Applying all four
turns an observed tag pose into the robot's pose.

**Main success path.**
1. A default tag size comes from the configuration the daemon loads at
   startup.
2. A client overrides size for a specific tag at runtime, and sets that
   tag's mount X, Y, Z, and yaw.
3. The daemon applies the corrections per frame: it projects the tag
   from its mount height down onto the playfield plane (parallax), then
   applies the X/Y mount offset and yaw to convert tag pose → robot
   pose.
4. Subsequent queries and streams report **the robot's** position and
   heading.

**Postconditions.** Per-tag parameters held by the daemon and applied
per frame. Calibration itself stays robot-agnostic — these are runtime
registrations, never calibration data.

**Lifetime (resolved 2026-07-31).** Registrations are **in-memory only
and do not survive a daemon restart** — v1's persisted `tags.json` is
dropped. The contract for clients: **if you expect your tag to have a
height or mount orientation, send it when you connect** — registering
mounts is part of starting a session, exactly like re-drawing
annotations (UC-28.S-4). A robot program registers its own tag on
startup; it never assumes a previous session's registration is still
there.

**Failure modes.**
- Mount Z set with no calibration → projection is meaningless; reject or
  flag rather than silently reporting an unprojected position.
- Implausible size or mount values (zero/negative size, a height above
  the camera itself) → reject.
- Parameters set for a tag never seen → accept; the tag may appear later.
- Partial mount specification (Z set, X/Y unset) → treated as zero
  offset, not an error. A tag mounted directly over the robot's centre
  is the common case.

**Open questions.**
- Does changing a tag's mount parameters retroactively affect buffered
  history, or only new frames?
- What is the robot's "reference point" — geometric centre, drive-axle
  centre, or client-defined? The offsets are meaningless without a
  stated convention, and this is exactly the kind of thing that silently
  mirrors or shifts positions if left implicit.
- Is mount X/Y expressed in the robot's frame (rotating with the robot)
  or the world frame? It must be the robot's frame for yaw to compose
  correctly, but it should be stated.

#### Scenarios

**UC-12.S-1 — Default size from startup config** · _happy path_
Daemon starts with a configured default tag size. A detected AprilTag
with no per-tag override is reported using that default.

**UC-12.S-2 — Tall robot's tag projects down to the field** · _happy path_
AprilTag 7 sits 20 cm above the field on a robot, viewed from an oblique
angle near the field edge. With `mount_z` unset, the reported position is
offset away from the camera. With `mount_z` = 20 cm, the reported
position moves to the robot's actual footprint — measurably closer to
ground truth.

**UC-12.S-3 — Off-centre tag reports the robot's centre** · _happy path_
AprilTag 12 is mounted 8 cm forward and 3 cm left of its robot's
reference point, 15 cm up: `mount_x` = 8, `mount_y` = 3, `mount_z` = 15.
The reported position is the robot's reference point, not the tag — so
as the robot rotates in place, the reported position stays put while the
observed tag sweeps a circle around it.

**UC-12.S-4 — Rotated tag reports the robot's heading** · _happy path_
AprilTag 30 is mounted turned 90° from its robot's forward direction:
`mount_yaw` = 90°. The robot drives east. Reported heading is 0 (east) —
the robot's heading — not the 90° the tag itself presents.

**UC-12.S-5 — Mount height without calibration** · _failure path_
Camera per UC-05.S-2, uncalibrated. Setting `mount_z` is rejected or
flagged as inert, rather than accepted and quietly ignored.

---

### UC-27 — Ask the system how to use itself

**Job:** JTBD-01, JTBD-02, JTBD-03 · **Primary actor:** A-02 · **Also serves:** A-01
**Status:** draft
**Interfaces:** MCP, CLI, Python

**Goal.** An agent or robot programmer asks the system for its own
instructions — conventions, coordinate frame, workflow, idioms — and gets
them back as text, without a human pasting documentation into the
conversation.

**Preconditions.** None. **Instructions must be available before
anything is configured** — an agent's first question may well be "what is
this and how do I use it?"

**Why the system carries its own instructions.** Actor A-02 works
entirely through tool calls and cannot read repository files. Everything
it needs to know — that world coordinates are cm with +x east and +y
north, that headings are 0 = east CCW positive, that a tag id needs a
family, that calibration precedes world coordinates — is knowledge the
system already has and the agent does not. Shipping that knowledge *with*
the system means it can never drift from a separately maintained document
that nobody updated.

v1 did this: `AGENT_GUIDE.md` and `ROBOT_API_GUIDE.md` shipped inside the
package, served through `guides.py` and an MCP tool. v2 keeps the idea.

**What the instructions cover.** At minimum the things that are
non-obvious and expensive to get wrong:

- the world frame and its conventions (the §7 invariants)
- the family + id tag addressing rule (UC-09)
- the bring-up order: playfield → camera config → calibrate → query
- what "uncalibrated" results mean and why they are not world
  coordinates
- which questions are answered from the surveyed definition versus from
  vision (UC-08)
- that mount parameters and annotations are in-memory: a client that
  expects its tag to have a height or orientation registers the mount
  **at session start, every session** (UC-12), and re-draws its
  annotations on reconnect (UC-28)

**Main success path.**
1. Client asks for the guide, optionally naming which one (agent-facing
   or robot-API).
2. The system returns its text.
3. The agent proceeds without a human having explained anything.

**Postconditions.** None — a read.

**Failure modes.**
- Named guide does not exist → error listing the guides that do.
- Guide requested before any daemon is running → **must still work**. The
  instructions are packaged with the software, not held by the daemon, so
  a client with no daemon can still learn how to get one running.

**Open questions.**
- Are the guides served by the **MCP server** (packaged with it, no
  daemon needed) or by the **daemon** (one copy, but requires a daemon)?
  Packaging them with each component that serves them is more robust and
  matches v1; it risks divergence if a stale MCP server serves stale
  instructions against a newer daemon.
- Should the guide be **generated** in part — the actual tool list, the
  actual playfields, this daemon's real camera names — rather than purely
  static text? A guide that names the real cameras is far more useful,
  but only a daemon can produce it, which conflicts with the
  no-daemon-needed property above.
- Does the CLI expose this too (`aprilcam guide`), or is it MCP-only?
  Drafted as: available everywhere, since a human reading the agent guide
  is exactly how the conventions get reviewed.

#### Scenarios

**UC-27.S-1 — Agent orients itself before doing anything** · _happy path_
A fresh agent session with `aprilcam mcp` running and **no daemon
started**. The agent's first tool call asks for the guide and receives
the conventions and the bring-up order. It then follows that order —
start a daemon, push a playfield, configure, calibrate — without a human
explaining the sequence.

**UC-27.S-2 — Conventions answer a geometry question** · _happy path_
An agent has a tag at world (50, 30) and needs to know which way is
north. The guide states +y north, +x east, A1-centred, heading 0 = east
CCW positive — so the agent does not have to infer the frame from
observed data, which is exactly how sign errors get baked in.

**UC-27.S-3 — Unknown guide requested** · _failure path_
A client asks for a guide name that does not exist. The response names
the available guides rather than returning empty text that would read as
"there are no instructions."

---

### UC-28 — Draw on the playfield for everyone to see

**Job:** JTBD-07 · **Primary actor:** A-01 · **Also serves:** A-02, A-03
**Status:** draft
**Interfaces:** Python, MCP, CLI

**Goal.** A service draws paths, markers, and symbols on the playfield in
world coordinates, and every other consumer — the viewer, other robots,
an agent — sees them, without any client maintaining files or state of
its own.

**Preconditions.** A daemon holding the named playfield definition.
Calibration is *not* required to author annotations — they are world
coordinates against the definition — but rendering them on an image
requires a deskewed frame, which requires calibration.

**Why the daemon holds them.** One service draws, every service sees:
the daemon is the rendezvous point (spec principle 3), so shared
drawings live there. They are **runtime state like the mobile-tag
registry (UC-12), not configuration**: in-memory, record-level, gone on
restart, never written to disk. The daemon stores and embeds; it never
renders (spec §9 #8).

**The model.** An annotation is keyed **(playfield, layer, id)** — id
unique within its client-chosen layer (`planner`, `robot-3`). Shapes:
marker, path, polygon, circle, text; geometry always world-cm; style
hints are presentation and may be ignored by a renderer. Operations:
upsert one, remove one, atomically replace a whole layer, clear by
layer or everything, fetch the current set. There is **no ownership**
— last write wins on a key. Every published deskewed frame embeds the
playfield's current set (with a monotonic revision); raw frames do not
(UC-16 — raw means raw, and a raw frame carries no world→pixel
mapping).

**Main success path.**
1. A client puts an annotation into a layer on a named playfield.
2. The daemon stores it and bumps the set's revision.
3. Every subsequent deskewed frame from any camera linked to that
   playfield embeds the updated set.
4. Consumers render it locally (viewer panels, client `Renderer`), or
   fetch the structured set directly.

**Postconditions.** The annotation is visible to every consumer of the
playfield until removed, replaced, or the daemon restarts.

**Failure modes.**
- Unknown playfield named on a put → the put is rejected, naming the
  playfields the daemon holds (open below: reject vs hold).
- Clear of a layer that does not exist → defined as a no-op, not an
  error (clearing to a known-empty state is the caller's actual goal).
- Daemon restart → all annotations gone. Not a failure but the deal:
  clients re-draw on reconnect, same as mobile-tag registrations
  (UC-12).

**Open questions.**
- **TTL.** Without ownership, a crashed client's layer lingers until
  restart. An optional per-annotation `ttl_seconds` is drafted — but
  measured from put or last upsert? Does upsert reset it? Pruned lazily
  at read/embed time or actively? Is expiry observable?
- **Limits.** Maximum annotations per playfield/layer and points per
  path — daemon memory and per-frame wire cost both depend on it; and
  at the limit, reject-with-error or evict?
- **Embed policy.** Do deskewed frames embed an empty set (explicit
  "nothing drawn") or omit it? The `ImageFrame` contract's three-way
  distinction (absent vs empty) depends on the answer.
- **Unknown playfield: reject or hold?** Rejecting is drafted, but the
  UC-12 analogy cuts the other way — mount params are accepted for
  tags not yet seen.
- **Layer names.** Do client layers share a namespace with the
  viewer's built-in layers (`tags`, `features`)? Reserved names or a
  prefix convention?
- **Playfield replacement.** Does `SetPlayfield` for the same name
  clear that playfield's annotations? A changed definition can
  invalidate their coordinates.
- **CLI surface.** Parity (process.md) requires at least
  list/clear from the shell; whether the CLI also *authors*
  (draw a marker from a script) is undecided.
- **No annotation stream in v2** — frames are the publish channel and
  `GetAnnotations` the query; a change-driven `stream_annotations` is
  recorded as out of scope, like the MCP tag-stream exclusion.

#### Scenarios

**UC-28.S-1 — Planner draws a goal, viewer sees it** · _happy path_
A robot program puts marker `goal` at (50.0, 30.0) and path `route`
through (0, 0) → (25.0, 10.0) → (50.0, 30.0) into layer `planner` on
`main-playfield`. The viewer, subscribed to the deskewed stream from
`arducam-ov9782-usb-camera`, renders both on the next frame without any
action on the viewer's side. An MCP agent fetching a deskewed frame gets
the same two annotations embedded in it.

**UC-28.S-2 — Atomic layer redraw** · _happy path_
The planner recomputes and replaces layer `planner` in one call with a
new marker and a 40-point path. No consumer ever sees a half-updated
layer: frames embed either the old set or the new one, never a mix.

**UC-28.S-3 — Agent reads the drawings, not the pixels** · _happy path_
An MCP agent calls `list_annotations` for `main-playfield` and receives
the structured set — layer `planner`, marker `goal` at (50.0, 30.0), the
path — without fetching an image. This is how a second robot reads the
first robot's plan.

**UC-28.S-4 — Restart wipes the slate** · _happy-path variation_
The daemon restarts. `list_annotations` on `main-playfield` returns an
empty set at revision 0. The planner, on reconnect, re-puts its layer —
the same re-registration ritual as UC-12 mount parameters.

**UC-28.S-5 — Unknown playfield** · _failure path_
A client puts a marker on playfield `practice-field`, which the daemon
does not hold. The put is rejected with an error naming the playfields
that exist (`main-playfield`) — not silently held for a playfield that
may never arrive.

**UC-28.S-6 — Two writers, one key** · _happy-path variation_
Two services both put layer `shared`, id `target` — one at (10.0, 5.0),
one at (12.0, 6.0). Last write wins; the set holds exactly one `target`
with the later position. No error, no merge — documented semantics, not
a race bug.

---

## Traceability

> The coverage check: every use case has at least one happy-path and one
> failure-path scenario; every scenario eventually names a test.

Tests are unassigned — the next stage of the process. This file covers 15
use cases and 57 scenarios (including variations).

| Use case | Job | Actor(s) | Scenarios | Subsystems | Tests |
|---|---|---|---|---|---|
| UC-01 Start daemon / config model | JTBD-05 | A-04, A-03 | S-1 ✓, S-2 ✓, S-3 ✗, S-4 ✓ (+4a, 4b) | daemon, camera, config | — |
| UC-02 Push playfield | JTBD-05, 03 | A-03, A-01, A-02 | S-1 ✓ (+1a), S-2 ✗ | daemon, playfield | — |
| UC-03 Configure camera | JTBD-05 | A-03 | S-1 ✓, S-2 ✓, S-3 ✗, S-4 ✓, S-5 ✓ | daemon, camera, config | — |
| UC-04 Calibrate | JTBD-06, 01 | A-03, A-02 | S-1 ✓, S-2 ✗, S-3 ✗ | daemon, calibration, vision | `tests/image_matrix/test_calibration_robustness.py::test_calibration_robustness`, `tests/image_matrix/test_world_accuracy.py::test_world_accuracy` |
| UC-05 Confirm readiness | JTBD-06 | A-03, A-01, A-02 | S-1 ✓, S-2 ✗ | daemon | — |
| UC-06 Single image from camera | JTBD-02 | A-02, A-03 | S-1 ✓, S-2 ✓, S-3 ✗ | daemon, calibration | `tests/image_matrix/test_geometry.py::test_geometry_east_west_and_north_up` |
| UC-07 Stream video from camera | JTBD-02 | A-03, A-01 | S-1 ✓, S-2 ✗, S-3 ✓ | daemon, client | — |
| UC-08 Where is a feature | JTBD-03 | A-02, A-01, A-03 | S-1 ✓, S-2 ✗, S-3 ✗ | playfield, daemon | — |
| UC-09 One tag by id | JTBD-01 | A-01, A-02 | S-1 ✓, S-2 ✓, S-3 ✓, S-4 ✗, S-5 ✗ | daemon, vision, calibration | `tests/image_matrix/test_recall.py::test_recall` |
| UC-10 All detected tags | JTBD-01, 04 | A-02, A-01 | S-1 ✓, S-2 ✓, S-3 ✗ | daemon, vision | `tests/image_matrix/test_recall.py::test_recall`, `tests/image_matrix/test_calibration_robustness.py::test_calibration_robustness` |
| UC-11 Stream tags | JTBD-04, 01 | A-01 | S-1 ✓, S-2 ✓, S-3 ✗ | daemon, vision, client | — |
| UC-12 Tag parameters | JTBD-01, 06 | A-01, A-03 | S-1 ✓, S-2 ✓, S-3 ✓, S-4 ✓, S-5 ✗ | daemon, calibration | — |
| UC-26 Tag history | JTBD-04, 01 | A-02, A-01 | S-1 ✓, S-2 ✓, S-3 ✓, S-4 ✗ | daemon, vision | `tests/image_matrix/test_recall.py::test_recall`, `tests/image_matrix/test_synthetic_degradation.py::test_severe_degradation_documented_boundary` |
| UC-27 Packaged instructions | JTBD-01, 02, 03 | A-02, A-01 | S-1 ✓, S-2 ✓, S-3 ✗ | mcp, guides | — |
| UC-28 Draw on the playfield | JTBD-07 | A-01, A-02, A-03 | S-1 ✓, S-2 ✓, S-3 ✓, S-4 ✓, S-5 ✗, S-6 ✓ | daemon, client, mcp | — |

✓ happy path · ✗ failure path

**Coverage.** Every use case now has at least one happy path and one
failure path, satisfying the rule in §5. Every job JTBD-01…07 is covered
by at least one use case.

**Not yet covered.** No use case describes *stopping* the system, tearing
down a camera, or removing a playfield — only UC-03's mid-stream
reconfigure touches change-while-running. Tag detection *history* (a
ring buffer of past frames, which v1 had) is deliberately absent: UC-10
is current-state only. If history is wanted it needs its own use case.

### Decisions resolved 2026-07-29

| Question | Resolution |
|---|---|
| UC-03 — is config shared across cameras? | **Per-camera only.** No shared or class-wide config. |
| UC-03 — unsupported setting? | **Hard error, whole push rejected.** Never a partial apply, never blind success. |
| UC-04 — how many fiducials for success? | **All that the playfield definition specifies.** 9 of 10 is a failure. |
| UC-06 — playfield- or camera-addressed? | **Camera-addressed**, always. No implicit resolution even with one linked camera. |
| UC-09 — do ArUco and AprilTag ids collide? | **No** — family is part of the request. `-(N+1)` never leaves the daemon. |
| UC-10 — how many tags returned? | **All of them.** No count, no limit, no pagination. |
| UC-12 — mount parameters in scope? | **Yes** — size, mount X, Y, Z, and yaw. Goal is the robot's pose, not the tag's. |
| UC-01 — does pushed config persist? | **No.** Disk is the source of record; RPC push is an in-memory override discarded on every restart. |
| UC-01 — is the camera list a startup snapshot? | **No.** The daemon watches for cameras arriving and disappearing for as long as it runs. |
| UC-01 — does calibration survive a restart? | **Yes.** The daemon writes `calibration.json` and reloads it. The rule is file-backed vs in-memory, not config vs calibration. |
| Is tag history kept? | **Yes** — bounded ring buffer, queryable (UC-26). It was in the original requirements and nearly lost. |
| Can agents get instructions from the system? | **Yes** — packaged guides (UC-27), as v1 had. |
| UC-01 — warn when live config diverges from disk? | **No.** Silent revert. "Restart = disk state" holds without exception. |
| UC-03 — can a push be partial? | **No** — whole document only, same rule as playfields. |
| UC-03 — how is an experiment made permanent? | **Dump the live config** and install the file on disk (`scp` for a remote daemon). The daemon never writes config for a client. |

### Decisions resolved 2026-07-31

| Question | Resolution |
|---|---|
| Is drawing a service or a library? | **Both, split** (supersedes the 2026-07-29 "no overlay RPC" decision): the daemon *stores* annotations (UC-28); clients *render* them. The daemon never rasterizes. |
| Do annotations survive a daemon restart? | **No.** In-memory runtime state, like UC-12 mount parameters. Clients re-draw on reconnect. |
| How are annotations organized? | **Named layers**, client-chosen; key is (playfield, layer, id); clear/replace by layer or id; no ownership — last write wins. |
| Do raw frames embed annotations? | **No.** Annotations are world-only and raw frames carry no world→pixel mapping (UC-16). Deskewed frames embed the current set. |
| UC-12 — do mount parameters survive a restart? | **No.** v1's persisted `tags.json` is dropped; the registry is in-memory like annotations. A client expecting its tag to have a height or orientation registers the mount at session start, every session. |

### Decisions resolved 2026-08-03

| Question | Resolution |
|---|---|
| UC-12 — what is the robot's "reference point"? | **Client-defined, not daemon-mandated.** The daemon cannot inspect a robot's physical geometry, so it applies whatever `mount_x`/`mount_y`/`mount_z`/`mount_yaw` it is given mechanically, with no notion of what physical point they are measured from. The packaged guide (UC-27) recommends centre of rotation as a sensible default, but never enforces it. |
| UC-12 — is mount X/Y in the robot's frame or the world frame? | **The robot's own frame, `+x` forward, `+y` left, rotating with `mount_yaw`** — carried forward from v1's `x_mm` forward / `y_mm` left convention unchanged, rather than inventing a new one. See `calibration.geometry.apply_mount_offset`'s docstring for the composed math and worked example. |
| UC-12 — do mount-parameter changes retroactively affect buffered history? | **No.** A registration or un-registration only affects frames detected after the change; buffered `TagFrame`s keep whatever correction (or none) was in effect when they were built. Recomputing history would require storing raw, pre-correction detections, which the ring buffer does not do. |

### Decisions resolved 2026-08-12

| Question | Resolution |
|---|---|
| UC-12 — where does `mount_z`'s parallax projection get the camera's own ground position and height from? | **`CameraConfig.camera_position` (sprint 029 ticket 001).** A real, optional per-camera field (`nadir: WorldPoint`, `height_cm: float`), sourced either by hand-authoring or by `aprilcam camera locate` (ticket 003, UC-29) solving it from surveyed elevated references. `DaemonService._camera_geometry_for` reads it directly; a camera nobody has run `locate` against (or hand-authored a `camera_position` for) still falls back to the exact pre-029 stopgap — ground position `(0, 0)`, height `0.0`, `project_tag_to_field_plane`'s own "nothing to correct" sentinel — so an unconfigured camera's behaviour is unchanged. Closes the gap `calibration.geometry`'s module docstring named since sprint 025 and the linked issue (`camera-geometry-for-real-parallax-correction.md`) asked for. Hardware-verified on camera 3 / `main-playfield`: real, measured parallax error of 8.86 cm and 7.92 cm at 15-17 cm mount heights corrects to ~0.2 cm once `camera_position` is configured (ticket 004's real-hardware acceptance pass). |
| UC-12's Failure-modes bullet — "Implausible ... mount values (... a height above the camera itself) → reject" — is this enforced? | **Yes, as of sprint 029 ticket 001** — a real, tested guard, not an aspirational note. `daemon.detection._apply_mount_correction` skips the correction (reports the raw, uncorrected position) whenever a configured camera's height is known (`camera_height_cm > 0.0`) and a registered `mount.mount_z >= camera_height_cm`, rather than extrapolating a position past the camera. Before this sprint the condition could never be evaluated at all — no camera height was ever known — so the bullet described intent, not behaviour. Hardware-checked once (ticket 004): an unconfigured camera's `mount_z` remains a documented no-op, unaffected by this sprint. |

