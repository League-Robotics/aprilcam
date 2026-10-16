# Daemon Interface Trace — Sufficiency Check

**Status:** COMPLETE — 2026-07-31 · **re-verified same day against the
post-fix interfaces**: all 14 deficits were resolved directly in the
skeletons (stakeholder direction) and each resolution has been checked
against the actual files, not taken on report
**Checks:** [daemon.md](daemon.md) UC-01 … UC-12, UC-26 … UC-28 against
`src/aprilcam/*/interfaces.py` + `src/aprilcam/types.py` +
`src/aprilcam/errors.py`
**Related:** [specification.md](../specification.md) §4 ·
[adapters.md](../adapters.md) · [README.md](README.md)

This document verifies, use case by use case, that the interface
skeletons are **sufficient to execute the daemon use cases**. For each
use case it traces the path from the actor's entry point down through
the declared protocols and value types, and asks one question per
scenario: is the observable outcome producible through methods and
fields that actually exist? "The daemon will obviously do that
internally" is not coverage — the test is whether the *client-visible*
interface expresses it. Missing methods are recorded as deficits, never
invented in the trace.

**Verdict legend.**

| Verdict | Meaning |
|---|---|
| **COVERED** | Every scenario's observable outcome falls out of declared calls and types. |
| **COVERED (notes)** | All scenarios executable, but with caveats — typically a failure *mode* (no scenario attached) that the types cannot express, or a contract carried only in a docstring. Caveats still get numbered deficit entries. |
| **DEFICIT** | At least one scenario cannot be executed as declared — a call or field is missing. |

**The entry-point model, stated once.** An actor enters via a CLI
command, an MCP tool call, or a robot program importing the Python
client library. All three sit on the same substrate
([adapters.md](../adapters.md) §4.4): `Discovery.connect(host)` in
`src/aprilcam/client/interfaces.py` returns a `Daemon`
(`src/aprilcam/daemon/interfaces.py`), and every subsequent step is a
method on that protocol or a field of what it returns. Adapters are
translation layers holding no state and doing no vision, so the trace
below starts at the client-library boundary and **every adapter inherits
the same sufficiency or the same deficit**. The one exception is process
lifecycle: `DaemonLifecycle` is local-only by design, entered from the
daemon host, not through `Discovery`.

Deficits are numbered **D-01 … D-14** and cross-referenced; the
deduplicated list is the final section.

**Re-verification, 2026-07-31.** After the first pass, the stakeholder
directed that the deficits be resolved directly in the interface
skeletons rather than ticketed. All 14 were addressed the same day, and
this document was re-verified against the updated files. Formerly
deficient use cases now carry a **Resolution** note naming the member
that closed the gap, and each summary entry records what was added. The
original deficit reasoning is deliberately kept intact below — it is the
record of *why* the interface now has that member. Two residuals are
called out honestly rather than papered over: D-07's stream-staleness
core is resolved but its gap/reset-event and calibration-epoch sub-items
are not (failure-mode level, no scenario blocked — see UC-07/UC-26 and
the summary entry), and D-12's accepted-range detail rides in message
text rather than a structured field.

---

## UC-01 — Start the daemon and configure it

**Verdict:** COVERED (deficit resolved 2026-07-31)

**Resolution.** `CameraState.number` (src/aprilcam/types.py) now carries
the stable registry enumeration — assigned once, never reshuffled —
making S-1's restart-stability check observable (D-02).
`ConfigLoader.load_camera_config` (src/aprilcam/config/interfaces.py)
closes S-4's push path (D-01). S-3's refusal is typed:
`AlreadyRunning(holder)` in src/aprilcam/errors.py (D-12).

**Entry:** `aprilcam daemon` (CLI, local) → `DaemonLifecycle.start()`;
observation by any client via `Discovery.connect()` → `Daemon`

**Trace.**
1. `DaemonLifecycle.start()` — lock, control plane, enumeration
   (daemon/interfaces.py)
2. `Discovery.connect(host)` → `Daemon` (client/interfaces.py)
3. `Daemon.list_cameras()` → `tuple[CameraState, ...]` (types.py)
4. `CameraState.name / .present / .usable / .playfield / .calibrated`
5. Config round trip: `Daemon.get_camera_config(camera)` →
   `CameraConfig`; `Daemon.set_camera_config(config)`
   (config/interfaces.py)
6. `DaemonStatus.config_dir / .state_dir` — the resolved cascade,
   locally (daemon/interfaces.py)

Most of this use case traces cleanly. **S-2** (empty start, no cameras)
is expressible by construction: `list_cameras()` returns an empty tuple
and `start()`'s contract says starting with nothing is success;
`set_playfield` is then available for the config-before-hardware path.
**S-3** (second daemon refused): `start()`'s docstring commits to
refusing and naming the holder; the mechanism is an exception, which
Python can always raise — but see D-12, because *naming* the existing
instance is error content the interface nowhere types. The "camera
present but unopenable" failure mode is covered exactly:
`CameraState.usable` false with `present` true is declared distinguishable
from not-plugged-in. The live-list behaviour (cameras arriving and
disappearing) is `CameraDiscovery.watch()` internally and observable as
successive `list_cameras()` results; `probe_cameras()` is the deep
re-interrogation.

Two things break.

**S-1 asserts enumeration numbers; no type carries one.** The scenario's
observable is "lists exactly those two cameras with **stable enumeration
numbers**... restarting yields the same two enumeration numbers."
`CameraState` (types.py) has `name` — the slug — and six booleans/links.
No index, no number. The number exists in the design
(specification.md §4.2: `registry.json` maps identity → "stable
enumeration number"; `CameraRegistry.resolve()`'s docstring promises
numbers "never shuffle") but it is trapped inside the daemon: `resolve()`
returns the *slug*, and nothing a client can call surfaces the number.
As declared, S-1's restart-stability assertion cannot be checked through
any interface. **D-02.**

**S-4/S-4a/S-4b push a config the client cannot construct.** The defining
round trip — fetch exposure A, push B, restart, revert to A — needs the
client to *build* a `CameraConfig` document to pass to
`set_camera_config()`. `CameraConfig` is a protocol of read-only
properties; `ConfigDocument.to_dict()` serializes *out* but has no
declared inverse, and no loader, factory, or concrete document class
exists anywhere in the skeleton. The dump direction (S-4a's "operator
dumps it") works: `get_camera_config()` → `to_dict()` → file. The push
direction — the actual experiment workflow the use case exists for — has
no declared path from a document on disk (or edited in memory) to the
argument `set_camera_config()` requires. Concrete implementation classes
will obviously exist, but the exercise is the declared surface, and the
declared surface is one-directional. **D-01.** (S-4b, the remote
variant, adds nothing new: `connect(host)` carries the push across the
network, and the restart is an SSH-side operator action, correctly
outside `DaemonLifecycle`'s local-only scope.)

The ephemerality contract itself — pushed config gone after restart,
disk state restored — is fully observable: `get_camera_config()` before
and after, compared. No persistence machinery needs to exist in the
interface, which is the point of the design.

**Deficits.**
1. **D-02** — `CameraState` has no enumeration-number field; UC-01.S-1's
   stable-numbers assertion is unobservable through any client call.
2. **D-01** — no declared way to construct a `CameraConfig` (or any
   `ConfigDocument`) client-side; `set_camera_config` in S-4 cannot be
   given an argument. `to_dict()` has no inverse.
3. **D-12** — S-3's "message naming the existing instance" is untyped
   error content (cross-cutting).

---

## UC-02 — Push a playfield definition to a running daemon

**Verdict:** COVERED (deficit resolved 2026-07-31)

**Resolution.** `ConfigLoader.load_playfield(path)`
(src/aprilcam/config/interfaces.py) supplies the missing step 1: file →
`PlayfieldDefinition`, with client-side `MalformedDocument` rejection
before any RPC and the `load(x.to_dict()) == x` round-trip contract
(D-01). Rejection identity is typed — `MalformedDocument` /
`UnknownPlayfield(requested, available)` in src/aprilcam/errors.py
(D-12).

**Entry:** `aprilcam playfield push` / MCP `set_playfield` / Python

**Trace.**
1. Client reads `main-playfield.json` → **needs a
   `PlayfieldDefinition`** — no declared constructor (D-01)
2. `Daemon.set_playfield(definition)` (daemon/interfaces.py)
3. `Daemon.get_playfield("main-playfield")` → `Playfield`
   (playfield/interfaces.py)
4. `Playfield.definition` → `PlayfieldDefinition.features()` →
   `Feature.slug / .position / .kind / .colour` (config/interfaces.py)
5. `Playfield.linked_cameras()` → derived link info
6. Staleness side effect: `Daemon.get_calibration(camera)` →
   `Calibration.stale`, and `CameraState.calibration_stale`

The read-back half of S-1 is fully covered: `get_playfield` returns the
`Playfield`, whose `definition` yields all 25 features with surveyed
`WorldPoint` positions — ArUco 1 at (−67, 44.65), `dot-west-blue` at
(−50, 0) — and `linked_cameras()` returns the empty tuple. "Verbatim" is
checkable through `to_dict()`, whose contract is exact on-disk format.
Post-restart disappearance is observable as `list_playfields()` empty /
`get_playfield` failing (error identity: D-12). S-1a (installed on disk)
differs only in who put the file where; the query surface is identical.

S-2 (reject duplicate ArUco id, keep the old definition) is expressible
in outcome: `set_playfield`'s contract is reject-whole-keep-previous,
and re-fetching proves the original intact. The rejection itself is an
exception "naming the duplicate id" — content with no type (D-12).

The failure mode the task list flags — *"pushing a definition that
replaces one an existing calibration was computed against → accept, but
mark affected calibrations stale"* — checks out on inspection rather
than on faith: `set_playfield`'s docstring commits to marking the
calibration stale, and staleness is genuinely client-observable through
**two** declared paths, `Calibration.stale`
(calibration/interfaces.py, served by `Daemon.get_calibration`) and
`CameraState.calibration_stale` (types.py, served by `list_cameras`).
Observable, therefore covered.

What is not covered is step 1. As with UC-01.S-4, the push half of the
main path requires a `PlayfieldDefinition` object the client has no
declared way to make. The daemon-side methods are sufficient; the
client-side vocabulary is one-directional (D-01). Since the main path
and both S-1 and S-2 begin with a push, the use case cannot be executed
end to end as declared.

**Deficits.**
1. **D-01** — no declared path from playfield JSON on disk to the
   `PlayfieldDefinition` argument of `set_playfield`; blocks S-1, S-1a
   (indirectly — the daemon loads from disk itself, but the push
   variant is the scenario), and S-2.
2. **D-12** — rejection content ("naming the duplicate id") untyped.

---

## UC-03 — Discover and configure a camera

**Verdict:** COVERED (deficit resolved 2026-07-31)

**Resolution.** The edit→push half of the lifecycle now exists:
`ConfigLoader.load_camera_config` (src/aprilcam/config/interfaces.py)
turns a dumped-and-edited file back into the `CameraConfig` that
`set_camera_config` requires (D-01). `Daemon.reset_camera_config(camera)`
is the RPC face of spec §4.1's `ResetCameraConfig` (D-10). S-3's
rejection is typed as `UnsupportedSetting(key)` in
src/aprilcam/errors.py (D-12) — the accepted *range* still rides in
message text rather than a structured field, noted in the summary.

**Entry:** `aprilcam camera config` probe → dump → edit → push
([cli.md](cli.md) UC-23) / MCP `get_camera_config` · `set_camera_config`

**Trace.**
1. `Daemon.list_cameras()` → persistent enumeration
2. `Daemon.get_camera_config(camera)` → `CameraConfig`
   (config/interfaces.py) — `settings` reports **every settable key,
   blank when unset** (the self-describing template, S-1)
3. Client edits values locally → **no declared mutation or
   construction path** (D-01)
4. `Daemon.set_camera_config(config)` — targeted by the document's own
   `name`; unsupported settings fail the whole push
5. Verify: `Daemon.get_frame(camera)` (visibly darker frame),
   `get_camera_config` re-fetch, `get_playfield(...).linked_cameras()`
6. Dump: `get_camera_config(camera)` → `ConfigDocument.to_dict()` —
   exact on-disk format (S-5)

S-1 is covered by contract: `CameraConfig.settings`'s docstring is
precisely the "every settable key, present but blank" promise, and the
introspected facts ride alongside. S-4 (two identical cameras
independent) is structural — configs are fetched and pushed per camera
slug, and `CameraConfig` is documented per-camera-never-shared. S-5's
dump round trip is exactly `to_dict()`'s stated contract
("dump → install → restart → identical config").

S-2 and S-3 break on the same edit-and-push gap as UC-01/UC-02.
`CameraConfig` exposes `playfield` and `settings` as read-only
properties; there are no setters, no builder, and no parse of an edited
document. A client can *fetch* the blank template (S-1) and can *dump*
it (S-5), but the middle of the probe → dump → **edit → push** lifecycle
— the whole point of the use case — has no declared vocabulary (D-01).
S-3's *outcome* (push fails naming the exposure key, camera untouched,
re-fetch returns previous state) is otherwise well-specified in
`set_camera_config` and `Camera.apply_config` docstrings; but "naming
the offending key" and "with the accepted range" are structured error
content that — unlike calibration's `CalibrationResult.missing_ids` —
has no typed home (D-12). The contrast is instructive: calibration got a
result object because its failure content matters; config push failure
content matters just as much (it is the "must be *really well
understood*" behaviour) and got nothing.

One capability the specification names is absent outright:
**`ResetCameraConfig`**. Spec §4.1 lists `SetCameraConfig /
GetCameraConfig / ResetCameraConfig` as the configuration service, and
`Camera.reset()` exists — but `Camera` is daemon-internal
(camera/interfaces.py is marked daemon-only; no client can reach it).
There is no `Daemon` method to discard a live override and re-apply the
on-disk config short of restarting the whole daemon. No UC-03 scenario
exercises reset, so this does not block a scenario here, but it is a
spec-declared RPC with no client-visible expression. **D-10.**

**Deficits.**
1. **D-01** — no edit/construct path for `CameraConfig`; blocks S-2 and
   S-3 (both begin with pushing a document the client composed).
2. **D-10** — `ResetCameraConfig` (spec §4.1) has no `Daemon` method;
   `Camera.reset()` is unreachable from clients.
3. **D-12** — offending-key / accepted-range error content untyped;
   S-3's "naming the exposure key" is docstring-only.

---

## UC-04 — Calibrate a camera against the playfield

**Verdict:** COVERED (deficits resolved 2026-07-31)

**Resolution.** `Calibration.calibrated_at` and `Calibration.playfield`
(src/aprilcam/calibration/interfaces.py) supply the provenance the
postcondition asserts (D-04), and `CalibrationResult.reason` names why a
complete-but-degenerate solve failed rather than reporting failure with
every id present (D-05).

**Entry:** `aprilcam calibrate` / MCP `calibrate_playfield` / Python

**Trace.**
1. `Daemon.calibrate(camera)` → `CalibrationResult`
   (calibration/interfaces.py) — command, not pixels; all vision
   daemon-side
2. `CalibrationResult.succeeded / .matched_ids / .missing_ids /
   .reprojection_error_cm`; since sprint 045 also `.method`,
   `.method_reason`, `.confidence`, `.corner_count`,
   `.reprojection_error_px`, `.scale_agreement`, `.angular_spread_deg`,
   `.camera_height_cm / .camera_nadir / .camera_tilt_deg`, `.notes`,
   `.helpful_missing_ids` and the leave-one-out fields
3. `Daemon.get_calibration(camera)` → `Optional[Calibration]` —
   `to_world` / `to_pixel` / `stale`
4. Verify: `Daemon.get_tag(camera, TagId(APRILTAG, 1))` →
   `TagRecord.world` ≈ (0, 0)

This is the best-covered failure surface in the skeleton, because the
failure content was given a type. S-1: `calibrate` returns a result with
`succeeded` true and `matched_ids` covering all 10 specified fiducials
(`PlayfieldDefinition.required_aruco_ids()` is the authority, and its
docstring carries the all-or-nothing rule); the centre-tag check falls
out of `get_tag`. S-2: `succeeded` false, `missing_ids == (6,)` — the
by-id-not-by-count reporting the scenario demands is exactly what
`missing_ids`'s docstring argues for; and "previously good calibration
left as it was" is observable by `get_calibration` still returning the
old mapping (`Calibrator.calibrate`'s contract states a failed attempt
leaves an existing calibration untouched). S-3: `missing_ids` with eight
entries. The stray-marker failure mode (detected ids not in the
definition, "ignored, reported") is *reportable* only via `matched_ids`'s
complement — there is no `extra_ids` field — a caveat, not a blocker,
since no scenario asserts it.

**Sprint 045 update.** The all-or-nothing rule is now a per-marker policy
(`PlayfieldDefinition.required_marker_ids()` / `helpful_marker_ids()`; the
old `required_aruco_ids()` still lists every id). S-2 and S-3 hold when the
markers are `required`, which is the default. S-4 (helpful marker missing)
is `succeeded` true with `helpful_missing_ids == (9,)`. S-5 and S-6 are
`succeeded` true with `confidence` `reduced` / `low` and `notes`. S-7 is
`succeeded` false with `reason` naming the gate and
`leave_one_out_worst_id` the swapped marker. S-8 is `method ==
"centre-point"` with `method_reason`. `Calibration.camera_geometry()`
exposes the pose-derived camera position that UC-12's parallax correction
prefers over `camera_position`.

Two notes keep this from clean COVERED. First, S-1's "client fetches the
homography and inspects it": `Calibration` exposes the *mapping*
(`to_world` / `to_pixel` — probe points through it) but not the
homography's values, and not when or against what it was computed. As an
"informational, not needed to work" assertion this is arguably
satisfiable by probing, but the postcondition "persisted calibration
**with provenance**" has no field anywhere — which becomes a hard
blocker one use case later (UC-05.S-1). **D-04.** Second, the
degenerate-geometry failure mode (all markers found, solution physically
implausible, rejected): expressible only as `succeeded` false with
`missing_ids` empty — the client knows it failed *despite* completeness
but not why. `CalibrationResult` has no reason/message field. **D-05.**
Neither breaks a listed scenario, hence the verdict.

**Deficits.**
1. **D-04** — `Calibration` carries no provenance (timestamp, source
   playfield, solve parameters); asserted by UC-04's postcondition and
   UC-05.S-1's observable.
2. **D-05** — `CalibrationResult` has no failure reason beyond
   `missing_ids`; the degenerate-geometry rejection is expressible only
   as an unexplained failure.

---

## UC-05 — Confirm the system is ready to answer questions

**Verdict:** COVERED (deficit resolved 2026-07-31)

**Resolution.** `Daemon.status()` (src/aprilcam/daemon/interfaces.py)
makes `DaemonStatus` the remote-readable *service* view —
`DaemonLifecycle.status()` stays the local process view — so UC-05's
remote actors get readiness over the wire in one call (D-03), and spec
§4.1 now lists `GetStatus` among the one-shot queries. S-1's provenance
is answerable: `Calibration.calibrated_at` / `Calibration.playfield`
via `Daemon.get_calibration` (D-04). Remediation prose remains
adapter-composed from the per-precondition fields, as originally judged
acceptable.

**Entry:** `aprilcam status` / MCP status query / robot program preflight

**Trace.**
1. `Discovery.connect(host)` → `Daemon` (remote-capable — required,
   since this UC serves A-01 and A-02)
2. `Daemon.list_cameras()` → `CameraState` — `.playfield` (linked),
   `.calibrated` (present), `.calibration_stale` (fresh),
   `.frame_dark` (not dark), `.usable` / `.present` (readable)
3. `Daemon.get_playfield(state.playfield)` — playfield known
4. `Daemon.get_calibration(camera)` → provenance — **no such field**

First, the suspect flagged in review: `DaemonStatus` — the protocol
*named* for UC-05 — hangs off `DaemonLifecycle.status()`, which is
local-only by explicit design. A robot program on a laptop or an MCP
agent can never reach it. Verified honestly, this is less damning than
it looks: `DaemonStatus` only exposes `running`, `cameras`,
`config_dir`, `state_dir` — it is a *lifecycle* diagnostic, not the
readiness answer — and `CameraState`, which **is** remotely reachable
via `Daemon.list_cameras()`, turns out to carry five of the six checks
the main path enumerates directly as fields, with the sixth ("playfield
known") one `get_playfield` call away. So a client can *compose*
readiness remotely in two calls. S-2's observable — linked and
configured but uncalibrated, world coordinates unavailable — falls out
of `calibrated == False` with `playfield` set; the remediation sentence
("until calibration runs") is adapter-composable from that boolean.
Reasonable coverage for S-2, with the note that "ask the daemon for the
status of a pair" is in reality "assemble it yourself from two queries,"
and each negative answer's "what to do about it" is adapter prose, not
interface contract.

S-1, however, cannot be executed. Its observable is an affirmative on
every precondition "naming playfield `main-playfield` **and the
camera's calibration provenance**." Provenance exists nowhere:
`Calibration` (calibration/interfaces.py) exposes `to_world`,
`to_pixel`, `stale` — no timestamp, no source-playfield identity, no
record of what the solve was computed against. The one assertion that
distinguishes "calibrated" from "calibrated against *this* field, at
*this* time" — precisely the trust question JTBD-06 exists for — has no
field to read. **D-04.**

**Deficits.**
1. **D-04** — no calibration provenance anywhere in the client-visible
   surface; blocks S-1's observable outright.
2. **D-03** — no remote readiness/status query on `Daemon`; readiness is
   composable from `CameraState` but the per-precondition diagnostic
   with remediation that the UC demands ("each negative answer must name
   *which* precondition failed and what to do about it") is a
   client-side reconstruction with no interface contract behind it.
   `DaemonStatus` is unreachable for the remote actors this UC serves.

---

## UC-06 — Get a single image from a camera

**Verdict:** COVERED (deficits resolved 2026-07-31)

**Resolution.** `ImageFrame.frame_dark` (src/aprilcam/types.py) carries
the darkness advisory in-band, tied to the frame in hand (D-13). The
anchor invariant is now stated as contract in `ImageFrame`'s docstring:
a deskewed frame spans exactly the field rectangle, image (0, 0) is the
field's NW corner at world (−width/2, +height/2), y flips once inside
the mapping (D-14). S-3's failure is typed as `NotCalibrated`
(src/aprilcam/errors.py, D-12). `get_frame` also gained an
`encoding="jpeg"/"png"` parameter (lossless one-shot capture — beyond
this UC's scenarios, no verdict impact).

**Entry:** `aprilcam frame` / MCP `get_frame` / Python

**Trace.**
1. `Daemon.get_frame(camera, deskewed=True)` → `ImageFrame`
   (daemon/interfaces.py, types.py)
2. `ImageFrame.data / .width / .height / .encoding / .deskewed /
   .px_per_cm / .world_extent`
3. `ImageFrame.tags` → embedded `TagFrame` (detection-nearest);
   `ImageFrame.annotations` → embedded `AnnotationSet` (deskewed only)

Camera-addressing is structural: `get_frame` takes a camera slug and
nothing else can be passed — the no-default-camera rule is enforced by
the signature, not by discipline. S-2 (raw frame, no calibration) is the
default call. S-1 (deskewed 800×532 with px-per-cm metadata): `deskewed`
true, `world_extent == (134.3, 89.3)`, `px_per_cm` derived-and-reported
per the `Deskewer` contract (calibration/interfaces.py — fixed bounding
box, aspect-preserved, north up east right, exactly one y-flip). S-3
(deskewed without calibration fails explicitly, no raw substitute):
`get_frame`'s docstring commits to exactly this; the mechanism is an
exception, whose "calibration is required" identity is untyped (D-12)
but whose observable — failure rather than a frame — is expressible.

Two notes. First, S-1's conversion check — "converting world (0, 0)
through the metadata lands on AprilTag 1" — is only well-defined because
`main-playfield`'s origin is the field centre. `px_per_cm` and
`world_extent` give the *scale* of the mapping but not its *anchor*: no
field of `ImageFrame` states the world coordinate of the image origin.
The conversion every client (and `Renderer`) must perform assumes
origin-at-centre, an invariant that holds for the
`apriltag-center-a1` convention but is stated nowhere in `ImageFrame`'s
contract. **D-14.** Second, the "frame too dark → returned, but flagged
advisory" failure mode has no scenario but also no expression: the flag
lives on `CameraState.frame_dark`, a *different query* about the camera
in general, not about the frame that was just returned. A frame cannot
carry its own advisory. **D-13.** Neither blocks S-1 … S-3.

**Deficits.**
1. **D-14** — `ImageFrame` metadata lacks a world anchor for the
   pixel↔world conversion; correctness rests on an unstated
   centred-origin assumption.
2. **D-13** — the dark-frame advisory has no home on `ImageFrame`;
   `CameraState.frame_dark` is not tied to a specific returned frame.
3. **D-12** — S-3's explicit-failure identity untyped.

---

## UC-07 — Stream video from a camera

**Verdict:** COVERED (deficit resolved 2026-07-31)

**Resolution.** `ImageStream` and `TagStream` moved into
src/aprilcam/daemon/interfaces.py, next to the methods that now return
them: `stream_images` returns `ImageStream`, whose `close()` is the
declared unsubscribe S-3 needed (D-08). `ImageFrame.calibration_stale`
gives stream items an in-band staleness carrier (D-07). **Residual,
stated plainly:** the reported-gap failure mode (camera fails
mid-stream) still has no dedicated event — a gap is inferable only from
timestamps, since `ImageFrame` has no frame index and no reset marker.
Failure-mode level, no scenario blocked; carried in the D-07 summary
entry.

**Entry:** `aprilcam view` / Python `stream_images`

**Trace.**
1. `Daemon.stream_images(camera, deskewed=True)` →
   `Iterator[ImageFrame]` (daemon/interfaces.py)
2. per-item: `ImageFrame` as UC-06, geometry stable for stream life
3. unsubscribe: `ImageStream.close()` (client/interfaces.py) —
   **unreachable**: nothing returns an `ImageStream`

S-1 (two subscribers, one warp) asserts an internal economy whose
observable half — both receive 800×532 frames with identical metadata —
is fully expressible; the compute-once half is `stream_images`'s
documented contract and correctly unobservable. S-2 (slow subscriber
drops, fast one unaffected) is likewise expressible as observed
behaviour: the slow consumer's iterator yields fewer frames; nothing in
the type needs to change. Worth noting for gap *detection*, though:
`ImageFrame` has no `frame_index` (`TagFrame` does), so a client can
infer drops only from timestamps.

S-3 breaks on a typing mismatch between the two halves of the skeleton.
The client library declares `ImageStream` — a subscription whose
`close()` unsubscribes, with an explicit warning that holding one open
costs the daemon real work — but **no declared method returns one**.
`Daemon.stream_images` returns a bare `Iterator[ImageFrame]`, which has
no `close`. The scenario's action ("both unsubscribe") and the UC's step
4 have no declared expression: a client can only abandon the iterator
and hope generator finalization reaches the daemon, which is exactly the
implicitness `ImageStream`'s docstring argues against. `TagStream` is
orphaned identically (see UC-11). **D-08.**

The mid-stream failure mode — "camera fails mid-stream → self-heal; the
stream **reports a gap** rather than dying silently" — has no scenario
here but no expression either: an `Iterator[ImageFrame]` can yield
frames or raise; there is no gap/reset event an item can carry. This is
the image-side face of D-07 (argued fully at UC-11).

**Deficits.**
1. **D-08** — `ImageStream`/`TagStream` declare the unsubscribe contract
   but are unreachable; `Daemon.stream_*` return bare iterators. Blocks
   S-3 as declared.
2. **D-07** — no in-band stream event vocabulary; the reported-gap
   failure mode is inexpressible (shared with UC-11, UC-26).

---

## UC-08 — Ask where a known feature is

**Verdict:** COVERED (deficit resolved 2026-07-31)

**Resolution.** `Daemon.find_feature(playfield, query) ->
tuple[Feature, ...]` (src/aprilcam/daemon/interfaces.py) is the RPC face
of `FeatureQuery.find` — spec §4.1's `WhereIs` — carrying the same
contract: all matches on ambiguity, empty on no match, answered from the
surveyed definition with no camera or calibration (D-06). All three
scenarios now trace daemon-answered, matching in one place instead of N
adapters.

**Entry:** `aprilcam where "the blue dot"` / MCP `where` / Python

**Trace.**
1. `Discovery.connect(host)` → `Daemon`
2. **no method** — `Daemon` has no feature-search call
3. the machinery exists, unreachable: `FeatureQuery.find(query)` →
   `tuple[Feature, ...]` (playfield/interfaces.py)

The suspect checks out as a genuine hole. `FeatureQuery` is a complete
and well-argued protocol: `find` returns all matches on ambiguity
(S-2's four orange dots, never an arbitrary pick), empty on no match
(S-3, explicitly not the nearest orange thing), and fuzzy-matches slug,
colour, kind, and cardinal (S-1's four phrasings reaching
`dot-west-blue`). Every scenario's observable is expressible **by this
protocol** — but no client can get one. `Daemon` has no
`find_feature`/`where` method; `Playfield` (the other candidate host)
exposes `name`, `definition`, `linked_cameras()` and nothing else.
Spec §4.1 names `WhereIs` among the one-shot RPCs, and spec §4.7 gives
the MCP server a `where` tool — both currently have nothing to call.

A client could approximate by fetching
`get_playfield(name).definition.features()` and matching locally, but
that fails the trace's own standard twice over: the main path says "the
**daemon** does a fuzzy/full-text match," and pushing matching into
clients means the CLI, the MCP server, and every robot program each
reimplement it — the duplication test that adapters.md §2(b) says moves
a capability *into* the daemon. The empty-vs-ambiguous distinction (S-2
vs S-3) would then live in N implementations instead of one contract.

**Deficits.**
1. **D-06** — `FeatureQuery` is unreachable: no `Daemon` method exposes
   feature search (spec §4.1 `WhereIs`). Blocks S-1, S-2, S-3 as
   daemon-answered queries.

---

## UC-09 — Ask for one tag by its number

**Verdict:** COVERED

**Entry:** robot program `get_tag` / MCP `get_tag` / `aprilcam tag`

**Trace.**
1. `Daemon.get_tag(camera, TagId(TagFamily.APRILTAG, 7))` →
   `Optional[TagRecord]` (daemon/interfaces.py, types.py)
2. `TagRecord.world` (cm) / `.heading_rad` / `.world_velocity` /
   `.speed` / `.timestamp`
3. uncalibrated: `.world is None`, `.pixel` set, `.calibrated == False`

This use case is what the type system was visibly built for, and every
scenario traces clean. Family-and-id addressing is structural: `TagId`
requires a `TagFamily`, so "AprilTag 5" and "ArUco 5" are different
values by construction and S-3 (same number, both families on
`main-playfield`, different answers) is two distinct calls; the
`TagFamily` docstring pins the v1 `-(N+1)` encoding as
never-in-interface, so no negative id can appear. S-4 (not detected):
`get_tag` returns `None`, and both the method's and `TagRecord.world`'s
docstrings argue exactly the not-confusable-with-(0, 0) point the
scenario asserts — `None` vs a record is a type-level distinction, not a
sentinel. S-1/S-2: `world`, `heading_rad` (0 = east, CCW positive per
the types.py header conventions), `timestamp` for freshness. S-5
(uncalibrated): `world` is `None`, `pixel` is the only representation,
`calibrated` false — pixels flagged as diagnostic, never dressed as
world coordinates. The not-in-definition failure mode (a mobile robot's
tag) needs nothing special: detection does not consult the definition.

The open question (how stale is too stale) is answerable as drafted —
the record carries `timestamp` and the client judges — so nothing is
blocked on resolving it.

**Deficits.** None.

---

## UC-10 — Get all detected tags at once

**Verdict:** COVERED

**Entry:** MCP `get_tags` / `aprilcam tags` / robot program

**Trace.**
1. `Daemon.get_tags(camera)` → `TagFrame` (daemon/interfaces.py)
2. `TagFrame.tags` → `tuple[TagRecord, ...]` — all of them, unbounded
3. `TagFrame.timestamp / .frame_index` — the frame the set belongs to

The resolved decisions are enforced by the signature: no count
parameter, no pagination, tuple of every record. S-1 (four tags, each
with world position, heading, velocity) is `TagFrame.tags` with the same
`TagRecord` fields UC-09 traced. S-2 (empty field): an empty `tags`
tuple inside a real `TagFrame` — with `timestamp` and `frame_index`
still present, "nothing detected" is a successful answer about a
specific frame, not an error and not an absence. S-3 (uncalibrated):
per-record `world is None` / `pixel` / `calibrated` false, exactly as
UC-09.S-5 — the flag rides on every record, so a mixed impossible state
(some records world, some pixel, unexplained) is at least representable
enough to be truthful, and the fabricated-world-coordinates failure the
scenario forbids is unrepresentable when `world` is `None`.

**Deficits.** None.

---

## UC-26 — Ask where a tag has recently been

**Verdict:** COVERED (notes; staleness deficit resolved 2026-07-31)

**Resolution.** `TagFrame.calibration_stale` (src/aprilcam/types.py)
now flags stale-calibration frames within a returned window (D-07,
staleness core). **Residual, stated plainly:** a *recalibration*
mid-window (old solve → fresh new solve, neither stale) still has no
epoch marker, and a camera self-heal reset remains indistinguishable
from an empty field — both are failure-mode / open-question level with
no scenario blocked, carried in the D-07 summary entry.

**Entry:** MCP `get_tag_history` / `aprilcam history` / robot program

**Trace.**
1. `Daemon.get_tag_history(camera, tag=TagId(APRILTAG, 7), frames=90)`
   → `tuple[TagFrame, ...]` (daemon/interfaces.py)
2. per-frame: `TagFrame.timestamp / .frame_index`, `TagFrame.tags` →
   `TagRecord.world / .heading_rad / .world_velocity / .timestamp`

The shape of the return type does quiet work here. Because history is a
tuple of **whole frames** rather than a flat list of records, S-4's
central demand — a gap must be explicit, never interpolated, never a
silently shortened list reading as continuous motion — is satisfied
structurally: an occluded half-second is a run of `TagFrame`s that
*exist* (timestamps and indices present) but contain no record for
AprilTag 12. The gap is in the data, not inferred from it. S-1
(trajectory: x increasing, timestamps spanning the drive) and S-2
(differencing positions over timestamps to verify the daemon's reported
velocity) are arithmetic over declared fields. S-3 (bounded window): the
drive from two minutes ago is simply not in the returned tuple, and the
window's extent is evident from the first record's timestamp — the
client can see it holds seconds, not the session. The buffer-shorter-
than-requested failure mode ("return everything, and say how many were
available") is satisfied by the return value itself: asking for 500 and
receiving 300 frames *is* the statement that 300 were available, since
the contract is return-everything.

Three notes. Ordering (oldest to newest, per the main path) is asserted
by the use case but not stated in `get_tag_history`'s docstring — a
one-line contract gap, not a type gap. The camera-reset-vs-nothing-
detected distinction (open question) has no expression: a self-heal
reopen and an empty field produce identical frame runs — this is the
history-side face of D-07's missing event vocabulary. And the
calibration-changed-mid-window failure mode ("flag rather than silently
mixing two coordinate systems") has no flag to raise: records carry
`calibrated` (a boolean) but no calibration identity or epoch, so
records solved under two different homographies are indistinguishable.
Also D-07's territory. No listed scenario exercises either, hence the
verdict.

**Deficits.**
1. **D-07** — no epoch/event signal in `TagFrame`/`TagRecord`: a
   mid-window recalibration or camera reset is invisible in returned
   history (shared with UC-11).

---

## UC-11 — Stream tag positions as they are detected

**Verdict:** COVERED (deficit resolved 2026-07-31)

**Resolution.** `TagFrame.calibration_stale` (src/aprilcam/types.py) is
the in-band carrier S-3 required — its docstring states the argument
exactly: a stream has no side channel, so if the flag is not in the
payload the promise cannot be kept — and `stream_tags`'s contract now
names it (D-07). `stream_tags` returns `TagStream`
(src/aprilcam/daemon/interfaces.py), so `close()` is reachable (D-08).

**Entry:** robot program `stream_tags` / `aprilcam tags --follow`
(MCP: out of scope by design)

**Trace.**
1. `Daemon.stream_tags(camera)` → `Iterator[TagFrame]`
   (daemon/interfaces.py)
2. per-item: `TagFrame.tags` → world-first `TagRecord`s, mount
   corrections applied (UC-12)
3. unsubscribe: `TagStream.close()` (client/interfaces.py) —
   **unreachable**, as UC-07

S-1 (follow a moving robot: x increasing, heading near 0, non-zero
world velocity) is the same record vocabulary as UC-09/UC-10, delivered
by iteration — covered. S-2 (quiet field heartbeats): a heartbeat is
representable as a `TagFrame` carrying the current (unchanged) state
with a fresh timestamp; the subscriber distinguishes alive-and-quiet
from dead by receiving items at all. Nothing marks a frame *as* a
heartbeat, but the scenario's observable — liveness is distinguishable
from a stall — is producible. Covered, thinly.

S-3 is the deficit, and the use case itself calls it the worst failure
mode in the system. The camera is bumped mid-stream; calibration is
flagged stale; "the subscriber is told, rather than quietly receiving
plausible-looking wrong positions." Walk the types: the stream yields
`TagFrame` — `tags`, `timestamp`, `frame_index`. `TagRecord` carries
`calibrated`, but stale is *not* uncalibrated: a stale calibration still
produces world coordinates, so `world` stays populated and `calibrated`
stays true — the records keep flowing looking exactly like trustworthy
data, which is precisely what the scenario forbids. The staleness signal
exists in the system (`Calibration.stale`, `CameraState.calibration_stale`)
but only as *poll-side* state on other queries; nothing in the stream
item vocabulary can carry it, and an `Iterator[TagFrame]` has no
out-of-band channel. `stream_tags`'s docstring promises "the subscriber
is told" — a promise the declared types cannot keep. The same hole
swallows UC-07's reported-gap failure mode and UC-26's epoch problem:
the streams have no event vocabulary at all. **D-07.** The adapters
document independently demands this signal survive into the ROS topic
(adapters.md §4.3), which it cannot until it exists at the source.

**Deficits.**
1. **D-07** — no staleness/event signalling in stream items: nothing in
   `TagFrame` (or any declared stream mechanism) can tell a subscriber
   that calibration went stale mid-stream. Blocks S-3.
2. **D-08** — `TagStream.close()` unreachable; `stream_tags` returns a
   bare iterator (shared with UC-07).

---

## UC-12 — Set a tag's physical parameters

**Verdict:** COVERED (deficits resolved 2026-07-31)

**Resolution.** `Daemon.unregister_tag(tag)`
(src/aprilcam/daemon/interfaces.py) removes a registration outright —
absent key a no-op — so the session registry no longer grows
monotonically and "never registered" is reachable again (D-11). S-5's
rejection is typed as `NotCalibrated` (src/aprilcam/errors.py, D-12).

**Entry:** robot program `register_tag` at session start / MCP
mobile-tag registration / `aprilcam tag register`

**Trace.**
1. `Daemon.register_tag(TagId(APRILTAG, 7), MountParameters(mount_z=20.0))`
   (daemon/interfaces.py, types.py)
2. `MountParameters.size_cm / .mount_x / .mount_y / .mount_z /
   .mount_yaw_rad`
3. observe the effect: `Daemon.get_tag(camera, tag)` →
   `TagRecord.world / .heading_rad / .yaw_rad` — corrections already
   applied
4. `Daemon.list_tag_parameters()` → `dict[TagId, MountParameters]`

The parameter vocabulary matches the use-case table one-for-one, and
`MountParameters` is a concrete dataclass — unlike config documents,
clients can construct it, so the register path actually executes. The
defaults encode the resolved semantics: `size_cm` `None` means the
startup default (S-1 — the observable is a correctly-sized detection,
which is position correctness, fine); partial specification is zero
offset by field default, the not-an-error the failure modes require.
S-2 (mount_z parallax), S-3 (off-centre tag reporting the robot's
reference point, stationary under rotation), S-4 (mount_yaw yielding the
robot's heading): each is register-then-observe through `get_tag`, whose
`TagRecord` contract states corrections are already applied — and the
corners deliberately are *not* (they outline the tag as seen), so S-3's
"observed tag sweeps a circle while the reported position stays put" is
even directly visible in `world_corners` vs `world`. S-5 (mount height
on an uncalibrated camera "rejected or flagged as inert"): rejection is
an exception — the scenario offers two acceptable outcomes and the
interface can express one of them (untyped, D-12); "flagged as inert"
has no representation, which is acceptable since either satisfies.
Registration-before-first-sighting is free: `register_tag` takes a
`TagId`, not an observation.

The reviewed suspect — unregistering — verifies as a real but
scenario-unblocking gap. There is no `unregister_tag` and no clear. A
client can overwrite with `MountParameters()` (all defaults), which is
observationally equivalent for position reporting, but
`list_tag_parameters()` can then never return to the never-registered
state — within a session the dict grows monotonically, and "explicitly
defaulted" is indistinguishable from "registered as default." The
in-memory lifetime (restart wipes, re-register every session) needs no
interface support by design. **D-11**, note-level.

**Deficits.**
1. **D-11** — no way to unregister a mount registration; overwrite-with-
   defaults approximates but never removes the entry.
2. **D-12** — S-5's rejection identity untyped.

---

## UC-27 — Ask the system how to use itself

**Verdict:** COVERED (deficit resolved 2026-07-31)

**Resolution.** The `Guides` protocol (src/aprilcam/client/interfaces.py)
serves the packaged instructions with **no daemon** — needing nothing
but the package, which unwinds S-1's circularity (D-09).
`Guides.available()` enumerates the guides, and `Guides.get(name)`
raises `UnknownGuide(requested, available)` (src/aprilcam/errors.py),
naming the guides that exist as S-3 asserts (D-12).
`Daemon.get_guide` remains as the over-the-wire serving point.

**Entry:** MCP guide tool (first call of a fresh agent session) /
`aprilcam guide` / Python

**Trace.**
1. **S-1's precondition: no daemon started.**
2. `Discovery.connect()` → fails or has nothing to find
3. `Daemon.get_guide(name)` — the only guide accessor, on the one
   protocol that requires a running daemon

The content contract is fine: `get_guide` returns text, the default
(`name=None`) serves the unnamed case, and what the guide must cover is
specified in the use case, not the interface. S-2 (conventions
answering a geometry question) is a property of the text. The problem
is *where the method lives*. The use case is emphatic twice over:
"instructions must be available **before anything is configured**," and
the failure modes escalate it to "guide requested before any daemon is
running → **must still work**. The instructions are packaged with the
software, **not held by the daemon**." S-1 then operationalizes it: MCP
server running, *no daemon started*, first tool call gets the guide and
learns the bring-up order — including how to get a daemon running at
all. But `get_guide` is a method on `Daemon`, reachable only through
`Discovery.connect()` to a live daemon. As declared, the guides are held
by exactly the component the use case says must not be their sole home,
and S-1 is unexecutable: the agent needs the guide to learn how to
start the daemon it must start to fetch the guide. The open question
(daemon-served vs packaged, possibly both) is real, but the current
skeleton implements only the arm the failure mode rules out as
insufficient. **D-09.**

S-3 (unknown guide → error listing the guides that exist) adds a second,
smaller gap: there is no way to enumerate guides — no `list_guides`, and
the available-names content would live only in an untyped exception
message (D-12).

**Deficits.**
1. **D-09** — guides are reachable only through a connected `Daemon`;
   no package-level accessor exists in the client (or MCP) surface.
   Blocks S-1 outright.
2. **D-12** — S-3's "names the available guides" is untyped error
   content; no enumeration call exists.

---

## UC-28 — Draw on the playfield for everyone to see

**Verdict:** COVERED (deficit resolved 2026-07-31)

**Resolution.** S-5's rejection content is typed:
`UnknownPlayfield(requested, available)` (src/aprilcam/errors.py)
carries the playfields the daemon holds as a structured field (D-12) —
the only caveat this UC had.

**Entry:** robot program / MCP `draw_marker` · `draw_path` ·
`list_annotations` / CLI list · clear

**Trace.**
1. `Daemon.put_annotation("main-playfield",
   Annotation(id="goal", layer="planner", shape=Marker(at=WorldPoint(50.0, 30.0))))`
   (daemon/interfaces.py, types.py)
2. `Daemon.replace_layer(playfield, layer, annotations)` — atomic swap
3. `Daemon.get_annotations(playfield, layer=None)` → `AnnotationSet`
   (`.annotations / .revision / .timestamp`)
4. embedded delivery: `ImageFrame.annotations` on deskewed frames;
   rendered client-side by `Renderer.render(frame, layers=...)`
   (client/interfaces.py)
5. `Daemon.remove_annotation(...)` / `Daemon.clear_annotations(...)`

The authoring vocabulary is complete and — pointedly, unlike config
documents — constructible: `Annotation`, the `AnnotationShape` union
(`Marker`, `PathShape`, `Polygon`, `Circle`, `Text`), and `Style` are
concrete dataclasses, so a client with no filesystem and no state can
build and push them, which is the use case's premise. S-1 traces end to
end: put a marker and a path into layer `planner`; every subsequent
deskewed frame from a linked camera embeds the set (`ImageFrame`
contract, spec §9 #8); the viewer renders via `Renderer` with no action
of its own; an MCP agent fetching a deskewed frame receives the same two
annotations embedded. S-2: `replace_layer` is declared atomic —
frames embed old set or new, never a mix — and `AnnotationSet.revision`
makes the transition observable as a single bump. S-3: `get_annotations`
is the structured read, exactly "the drawings, not the pixels." S-4:
the restart contract (empty set, revision 0) is written into
`get_annotations`'s docstring, and the re-draw ritual mirrors UC-12 by
design. S-6: last-write-wins on (layer, id) is `put_annotation`'s
documented semantics; the resulting set holds one `target`. The
clear-nonexistent-layer no-op is `clear_annotations`'s stated contract.

S-5 (unknown playfield rejected, naming the playfields that exist) is
expressible in outcome — the put fails, and `list_playfields()` can
supply the names — but the rejection's content is once more an untyped
exception (D-12). The open questions (TTL semantics, limits,
empty-vs-omitted embed) are carried honestly as open in the type
docstrings (`Annotation.ttl_seconds`, `AnnotationSet`, `ImageFrame`)
rather than silently decided; none blocks a scenario.

**Deficits.**
1. **D-12** — S-5's playfields-that-exist error content untyped
   (cross-cutting).

---

## Deficit summary

Deduplicated, numbered as cross-referenced above. Each entry is
self-contained: what is missing, which use cases it blocks or degrades,
and a one-line proposed fix. Ordered roughly by severity: scenario
blockers first, then failure-mode and contract gaps.

**All 14 entries were resolved in the skeletons on 2026-07-31**
(stakeholder direction: fix directly rather than ticket) and each
resolution below is verified against the actual files. Entries retain
their original text as the rationale record; the **Resolved** line
states what was added. D-07 and D-12 carry explicit residuals.

1. **D-01 — No client-side construction path for configuration
   documents.** `ConfigDocument.to_dict()`
   (src/aprilcam/config/interfaces.py) has no declared inverse: no
   loader, factory, or concrete class turns a JSON file (or an edited
   dict) into the `PlayfieldDefinition` that `Daemon.set_playfield`
   requires or the `CameraConfig` that `Daemon.set_camera_config`
   requires, and the protocols' read-only properties preclude editing a
   fetched one. Blocks UC-01.S-4/4a/4b, UC-02.S-1/S-2, UC-03.S-2/S-3 —
   every push path. Fix: declare the load half in config/interfaces.py
   (e.g. a `load_document(dict) -> ConfigDocument` function or concrete
   document dataclasses, mirroring how `Annotation` is constructible).
   **Resolved 2026-07-31:** `ConfigLoader` protocol
   (src/aprilcam/config/interfaces.py) — `load_camera_config(path)` /
   `load_playfield(path)`, client-side `MalformedDocument` rejection
   before any RPC, `load(x.to_dict()) == x` round-trip contract.

2. **D-07 — No staleness or event signalling in streams and history.**
   `Iterator[TagFrame]` / `Iterator[ImageFrame]` items carry no
   calibration-stale flag, no epoch, and no gap/reset event;
   `TagRecord.calibrated` cannot express stale (stale calibrations still
   produce world coordinates). Blocks UC-11.S-3 — named the worst
   failure mode in the system — and leaves UC-07's reported-gap and
   UC-26's recalibrated-mid-window / camera-reset failure modes
   inexpressible; adapters.md §4.3 requires the signal to survive into
   ROS. Fix: add `calibration_stale` (and a calibration epoch or reset
   marker) to `TagFrame` and `ImageFrame`, or introduce a stream-item
   event union.
   **Resolved 2026-07-31 (staleness core):** `TagFrame.calibration_stale`
   and `ImageFrame.calibration_stale` (src/aprilcam/types.py) are the
   in-band carriers; UC-11.S-3 — the blocking scenario — is now
   expressible, and `stream_tags`'s contract names the flag.
   **Residual, not resolved:** no calibration epoch and no gap/reset
   event were added — UC-07's reported-gap and UC-26's
   recalibrated-mid-window / camera-reset distinctions remain
   inexpressible. Failure-mode / open-question level; no scenario
   blocked.

3. **D-06 — Feature search is unreachable.**
   `FeatureQuery.find(query)` (src/aprilcam/playfield/interfaces.py)
   fully expresses UC-08's semantics, but no `Daemon` method (and no
   `Playfield` property) exposes it; spec §4.1 names the `WhereIs` RPC.
   Blocks UC-08.S-1/S-2/S-3 as daemon-answered queries; client-side
   reimplementation would duplicate matching across every adapter. Fix:
   add `Daemon.find_feature(playfield: str, query: str) ->
   tuple[Feature, ...]` (or return a `FeatureQuery` from
   `Daemon.get_playfield`).
   **Resolved 2026-07-31:** `Daemon.find_feature(playfield, query)`
   (src/aprilcam/daemon/interfaces.py), the RPC face of
   `FeatureQuery.find`, carrying the all-matches / empty-on-no-match
   contract.

4. **D-04 — Calibration provenance does not exist.** `Calibration`
   (src/aprilcam/calibration/interfaces.py) exposes `to_world` /
   `to_pixel` / `stale` only — no timestamp, no source-playfield
   identity, no inspectable solve parameters. Blocks UC-05.S-1
   ("naming ... the camera's calibration provenance") and undercuts
   UC-04's "persisted calibration with provenance" postcondition and
   S-1's fetch-and-inspect step. Fix: add provenance properties
   (`calibrated_at`, `playfield`, and an inspectable
   homography/parameter view) to `Calibration`.
   **Resolved 2026-07-31:** `Calibration.calibrated_at` (solve
   timestamp) and `Calibration.playfield` (the definition it was solved
   against) in src/aprilcam/calibration/interfaces.py. Inspection of the
   mapping itself remains via `to_world`/`to_pixel` probing, as
   originally judged sufficient for UC-04.S-1's informational
   assertion.

5. **D-09 — Guides are held only by the daemon.** `get_guide` exists
   solely on `Daemon`, but UC-27 requires the instructions packaged with
   the software and available with **no daemon running**; UC-27.S-1
   (fresh agent, no daemon, first call fetches the guide) is
   unexecutable — the agent needs the guide to learn how to start the
   daemon it must connect to. Fix: declare a package-level guide
   accessor (plus enumeration, per D-12) in the client or MCP surface;
   keeping `Daemon.get_guide` as a second serving point is compatible.
   **Resolved 2026-07-31:** `Guides` protocol
   (src/aprilcam/client/interfaces.py) — `get(name)` and `available()`,
   readable with no daemon; unknown names raise
   `UnknownGuide(requested, available)`. `Daemon.get_guide` retained as
   the wire-serving point.

6. **D-08 — Stream handle types are orphaned.** `ImageStream.close()` /
   `TagStream.close()` (src/aprilcam/client/interfaces.py) define the
   unsubscribe contract, but nothing returns either type —
   `Daemon.stream_images` / `stream_tags` return bare `Iterator`s with
   no close. Blocks UC-07.S-3 (explicit unsubscribe → idle capture) as
   declared; degrades UC-11 teardown identically. Fix: have
   `Daemon.stream_images` / `stream_tags` return `ImageStream` /
   `TagStream` (or declare client-library factory methods that wrap
   them).
   **Resolved 2026-07-31:** `ImageStream`/`TagStream` moved into
   src/aprilcam/daemon/interfaces.py, next to the methods that now
   return them — unsubscription argued there as a daemon-visible event,
   not client bookkeeping; client/interfaces.py points to the new home.

7. **D-02 — Camera enumeration numbers are unobservable.** UC-01.S-1
   asserts stable enumeration numbers across restarts;
   `CameraState` (src/aprilcam/types.py) carries only the slug, and
   `CameraRegistry.resolve` returns the slug — the number
   (spec §4.2 `registry.json`) never reaches a client. Blocks
   UC-01.S-1's stability check. Fix: add an enumeration `number` field
   to `CameraState` — or amend UC-01 to slugs-only if numbers are
   registry-internal.
   **Resolved 2026-07-31:** `CameraState.number`
   (src/aprilcam/types.py) — stable registry enumeration, assigned once
   and never reshuffled; `None` only for a camera not yet registered.

8. **D-03 — No remote readiness query.** `DaemonStatus` hangs off
   local-only `DaemonLifecycle`, unreachable for UC-05's remote actors
   (A-01, A-02); readiness is composable client-side from
   `CameraState` + `get_playfield`, but the UC's composed diagnostic —
   each negative answer naming the failed precondition and what to do —
   has no interface contract. Degrades UC-05 (S-1 also blocked by
   D-04). Fix: add `Daemon.get_status(camera: str)` returning a
   readiness structure with per-precondition results.
   **Resolved 2026-07-31:** `Daemon.status() -> DaemonStatus`
   (src/aprilcam/daemon/interfaces.py) — the remote-readable service
   view, distinct from `DaemonLifecycle.status()`'s local process view;
   spec §4.1 now lists `GetStatus`. Per-precondition data rides in
   `DaemonStatus.cameras` (`CameraState`, now including `number`),
   with provenance via D-04's `Calibration` fields; remediation prose
   stays adapter-composed, as originally judged acceptable.

9. **D-12 — No error taxonomy anywhere.** Failure scenarios across the
   set assert distinct, content-bearing errors — the offending config
   key and accepted range (UC-03.S-3), the lock-holder identity
   (UC-01.S-3), the duplicate feature id (UC-02.S-2),
   calibration-required (UC-06.S-3), the playfields that exist
   (UC-28.S-5), the guides that exist (UC-27.S-3) — but no exception
   type is declared in any interfaces.py; every failure identity lives
   only in docstring prose. `CalibrationResult` shows the alternative:
   structured failure content as a type. Degrades UC-01, UC-02, UC-03,
   UC-06, UC-12, UC-27, UC-28. Fix: a small typed exception module
   (e.g. `aprilcam.errors`) whose classes carry the structured fields
   the scenarios name.
   **Resolved 2026-07-31:** src/aprilcam/errors.py — `AprilCamError`
   base; `DiscoveryFailed`; `ConnectionFailed(host, reason)`;
   `UnknownTarget(requested, available)` with `UnknownCamera` /
   `UnknownPlayfield` / `UnknownGuide`; `NotCalibrated`;
   `UnsupportedSetting(key)`; `MalformedDocument`; `InvalidTagId`;
   `AlreadyRunning(holder)` — with the explicit contract that empty
   results never raise. **Residual:** UC-03's accepted-*range* detail is
   message text on `UnsupportedSetting`, not a structured field; minor,
   no scenario asserts the range as data.

10. **D-10 — `ResetCameraConfig` has no client-visible method.**
    Spec §4.1 names it as the third leg of the configuration service;
    `Camera.reset()` exists but camera/interfaces.py is daemon-only.
    Discarding a live override short of a daemon restart is
    inexpressible remotely. No UC-01…12 scenario blocks on it, but the
    spec-declared RPC surface is incomplete (and cli.md's config
    lifecycle will want it). Fix: add
    `Daemon.reset_camera_config(camera: str)`.
    **Resolved 2026-07-31:** `Daemon.reset_camera_config(camera)`
    (src/aprilcam/daemon/interfaces.py) — the RPC face of
    `Camera.reset()`; the disk document wins again, immediately.

11. **D-05 — `CalibrationResult` cannot say why a complete solve
    failed.** The degenerate-geometry rejection (UC-04 failure mode) is
    representable only as `succeeded` false with `missing_ids` empty —
    failure with no reason. Fix: add a `reason`/`message` property to
    `CalibrationResult`.
    **Resolved 2026-07-31:** `CalibrationResult.reason`
    (src/aprilcam/calibration/interfaces.py) — names the problem when
    ids alone don't (collinear markers, singular homography).

12. **D-11 — Mount registrations cannot be removed.** Only
    `register_tag` and `list_tag_parameters` exist; overwriting with a
    default `MountParameters()` approximates a reset but the entry
    persists for the session, so "never registered" is unreachable and
    the listing grows monotonically. No UC-12 scenario blocks on it.
    Fix: add `Daemon.unregister_tag(tag: TagId)` (or document
    overwrite-with-defaults as the sanctioned reset).
    **Resolved 2026-07-31:** `Daemon.unregister_tag(tag)`
    (src/aprilcam/daemon/interfaces.py); absent key is a no-op.

13. **D-13 — Frames cannot carry their own advisories.** UC-06's
    "too dark → returned, but flagged advisory" failure mode has no
    field: `CameraState.frame_dark` is a separate query about the
    camera, not about the returned frame. Fix: an advisory flag (or
    flags field) on `ImageFrame`.
    **Resolved 2026-07-31:** `ImageFrame.frame_dark`
    (src/aprilcam/types.py) — in-band, tied to the frame in hand;
    `CameraState.frame_dark` remains the camera-level trend flag.

14. **D-14 — The pixel↔world conversion has no anchor.** `ImageFrame`
    publishes `px_per_cm` and `world_extent` (scale) but not the world
    coordinate of the image origin; the conversion UC-06.S-1 and
    `Renderer` depend on is well-defined only under an unstated
    origin-at-field-centre assumption. Fix: add the image origin's
    world coordinate to `ImageFrame`, or state the centred-origin
    invariant as contract in its docstring.
    **Resolved 2026-07-31** (the state-the-invariant arm): `ImageFrame`'s
    docstring now carries the anchor invariant as contract — a deskewed
    frame spans exactly the field rectangle, image (0, 0) is the field's
    NW corner at world (−width/2, +height/2) with
    `world_extent = (width, height)`, and world→pixel flips y exactly
    once, inside the mapping. Scale plus this stated anchor is the whole
    conversion.
