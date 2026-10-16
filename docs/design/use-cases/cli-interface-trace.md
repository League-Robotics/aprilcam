# CLI ↔ Interface Sufficiency Trace

**Status:** draft 2026-07-31, **re-verified 2026-07-31 against the
post-fix interfaces** — all ten deficits below were resolved directly
in the skeletons the same day · **Owner:** Eric Busboom
**Verifies:** [cli.md](cli.md) UC-13 … UC-25 against the interface
skeletons in `src/aprilcam/`
**Related:** [README.md](README.md) · [../adapters.md](../adapters.md) ·
[../configuration.md](../configuration.md) · [../process.md](../process.md)

This document checks, use case by use case, that the interface skeletons
— `src/aprilcam/types.py` plus the `interfaces.py` in `daemon/`,
`client/`, `camera/`, `calibration/`, `config/`, and `playfield/` — are
**sufficient to execute the CLI use cases**. For each command it traces
from the exact command line down through the declared protocols and
types, and asks whether the main success path and every failure scenario
can actually be produced with the interfaces as written. Nothing is
invented along the way: where a call or field is missing, that is
recorded as a deficit, not papered over with a method that ought to
exist.

**Re-verification note (2026-07-31).** After the first pass recorded
ten deficits (D-1 … D-10), the stakeholder chose to resolve them
directly in the skeletons rather than ticket them. This document was
then re-verified against the updated files — `src/aprilcam/errors.py`
and `src/aprilcam/tags/interfaces.py` (both new), plus revisions to
`types.py`, `daemon/interfaces.py`, `client/interfaces.py`,
`config/interfaces.py`, and [cli.md](cli.md) itself. Every DEFICIT
verdict below now reads **COVERED (deficit resolved 2026-07-31)** with
a Resolution line naming what was added; the original deficit
reasoning is kept intact as the record of *why* the interface has that
member. Traces and reasoning prose otherwise describe the pre-fix
state they were written against.

**Verdict legend.**

- **COVERED** — every scenario's observable outcome falls out of
  declared methods and types.
- **COVERED (notes)** — executable, but with client-side synthesis,
  undeclared-but-conventional mechanics (exceptions), or awkwardness
  worth recording.
- **DEFICIT** — at least one scenario or asserted behaviour cannot be
  executed with the interfaces as declared.

**The model, stated once.** Per [../adapters.md](../adapters.md) and the
parity rule in [../process.md](../process.md), every CLI command is a
thin client: `Discovery.connect(host)` — where `host` is the `-d` value
or `None`, and `connect` itself applies the flag > env > file > default
cascade of [../configuration.md](../configuration.md) — yields a
`Daemon` (`src/aprilcam/client/interfaces.py`,
`src/aprilcam/daemon/interfaces.py`), and the command is one or a few
`Daemon` calls plus formatting. The exceptions are the local-only
lifecycle commands, which go through `DaemonLifecycle`
(`src/aprilcam/daemon/interfaces.py`) without any network, and the
offline utilities (`tags generate`). Fan-out `--all` is
`Discovery.find_all()` followed by one `connect()` per discovered
daemon, rows labelled from `DaemonInfo.host`, with a per-daemon
`connect` failure caught and reported as unreachable rather than
failing the whole command; refusing `--all` on a restricted command is
pure CLI-side argument checking and needs no interface at all.

Two cross-cutting observations recur below and are stated here rather
than five times:

- **No error taxonomy exists anywhere in the skeletons** *(as first
  written; resolved 2026-07-31 by the new `src/aprilcam/errors.py`)*.
  Every "clear error naming the host / listing what the daemon has /
  distinguishing failed-from-empty" scenario rests on exceptions, and
  at first-pass time not one exception type was declared.
  Raise-versus-return does give the CLI a mechanical error/empty
  distinction, and message strings can carry names — so most such
  scenarios were *producible* — but the structured content the
  scenarios assert (the daemon's actual camera list in an
  unknown-target error, "discovery failed" as a class) had no typed
  surface. Recorded once as **D-2**.
- **The `playfield` noun names no camera and no playfield** *(as first
  written; resolved 2026-07-31 by a new convention subsection in
  [cli.md](cli.md))*. `playfield view|image|tags` are camera-addressed
  underneath (`Daemon.get_frame` and friends are explicitly
  camera-addressed, with no playfield shortcut, by design), so the CLI
  must resolve playfield → camera itself. That was executable — but
  the selection rule when a daemon holds two playfields, or a
  playfield has two linked cameras, was nowhere defined. Recorded as
  **D-7**.

The command surface also lists `aprilcam playfield annotations`
(list / `--clear [--layer]`), whose use case (UC-28) lives in
[daemon.md](daemon.md) rather than this file's range. For completeness:
it traces cleanly — `Daemon.get_annotations(playfield, layer)` returns
an `AnnotationSet` to print, `Daemon.clear_annotations(playfield,
layer)` clears one layer or everything, clearing an absent layer is a
documented no-op, and fan-out of the list form follows the standard
model — subject only to the playfield-resolution convention (D-7,
resolved 2026-07-31).

---

## UC-13 — Discover and manage daemons

**Verdict:** COVERED (deficit resolved 2026-07-31)

**Entry:** `aprilcam daemon list` · `aprilcam daemon
start|stop|restart|status [-d HOST] [--all]`

**Trace — discovery.**
1. `Discovery.find_all()` → `tuple[DaemonInfo, ...]`
   (`src/aprilcam/client/interfaces.py`)
2. `DaemonInfo.host` / `.port` / `.is_local` / `.reachable` → the list's
   identity columns
3. For each reachable daemon: `Discovery.connect(info.host)` → `Daemon`,
   then `Daemon.list_cameras()` → `tuple[CameraState, ...]` and
   `Daemon.list_playfields()` → `tuple[Playfield, ...]`
   (`src/aprilcam/daemon/interfaces.py`) → the camera-count and
   playfield columns

**Trace — lifecycle.**
1. `DaemonLifecycle.start()` / `.stop()` / `.status()`
   (`src/aprilcam/daemon/interfaces.py`); `restart` is `stop()` then
   `start()` — no `restart()` is declared, and the composition is
   equivalent because restart's defining behaviour (pushed config
   discarded, on-disk config reloaded) is the daemon's own restart
   semantics, documented on `Daemon`
2. `DaemonLifecycle.status()` → `DaemonStatus`; `.running`, `.cameras`,
   `.config_dir`, `.state_dir` → what `status` prints

S-1 works: `find_all` lists both daemons, `is_local` marks the
laptop's, and the camera count and playfields come from a per-daemon
`connect` — `DaemonInfo` itself carries neither, so `daemon list` costs
one connection per reachable daemon. That is synthesis, not a gap: an
advertising-but-dead daemon (`reachable` false) is listed without the
extra columns, which is exactly S-1's shape. S-2 falls out of
`find_all`'s documented contract — finding nothing is an empty tuple,
and the CLI exits zero. S-5 is the load-bearing distinction: the
docstring commits to reporting discovery *failure* distinctly from
finding nothing, and the only available mechanism is an exception —
which works, but no `DiscoveryError` (or any error type) is declared
(D-2). S-3 round-trips through `DaemonLifecycle`: `status().running`
false → `start()` → `status().running` true with `.cameras` for the
count → stop/start for restart → `stop()`. `start()`'s docstring
carries the singleton-refusal ("refuses… naming it") for the
start-when-running failure mode, and `stop()` returning `None` is
compatible with either quiet-success reading of stop-when-stopped. S-4
(refuse `start -d remote`) needs no interface: the CLI sees `-d` plus a
lifecycle verb and refuses before touching anything, which is precisely
why `DaemonLifecycle` being local-only is design, not deficit.

What does *not* trace is **remote status**. The UC drafts "yes,
`status` accepts `-d`", and the fan-out table allows `daemon status
--all` — but `DaemonStatus` is reachable only through
`DaemonLifecycle.status()`, which is local-only by that protocol's own
contract. `Discovery.connect(host)` yields a `Daemon`, and `Daemon` has
**no status or readiness method**. The CLI can approximate: a
successful `connect` implies running, and `list_cameras()` gives the
camera states — but `config_dir` and `state_dir`, which local `status`
reports and which exist precisely so "the cascade is never a mystery,"
are unobtainable for a remote daemon, and "running: no" for a remote
daemon collapses into an undifferentiated connection error. `daemon
status -d pi-field.local` and `daemon status --all` cannot print what
`daemon status` prints.

**Deficits.**
1. **No remote-readable status on `Daemon`** — `DaemonStatus` hangs off
   local-only `DaemonLifecycle`; `daemon status -d HOST` and the
   fan-out-table-sanctioned `daemon status --all` cannot produce the
   status surface (notably `config_dir`/`state_dir`) remotely. (D-1)
2. Discovery-failed vs found-nothing, and every other error in this UC,
   rides on undeclared exceptions. (D-2)

**Resolution (2026-07-31).** `Daemon.status() -> DaemonStatus` was
added (`src/aprilcam/daemon/interfaces.py`, under self-description),
explicitly the remote-readable service view alongside
`DaemonLifecycle.status()`'s local process view — so `daemon status -d
HOST` is `connect(host).status()`, and `daemon status --all` is one
`status()` per connected daemon, `config_dir`/`state_dir` included.
(Spec §4.1's query list gained `GetStatus` to match.) The error side
(D-2) is resolved by `src/aprilcam/errors.py`: `DiscoveryFailed` is
the failed-vs-empty distinction of S-5 as a type, `ConnectionFailed`
carries the host, and `AlreadyRunning` names the holder for the
start-when-running refusal.

---

## UC-14 — List cameras on a daemon

**Verdict:** COVERED (deficit resolved 2026-07-31)

**Entry:** `aprilcam camera list [-d HOST] [--all]`

**Trace.**
1. `Discovery.connect(host)` → `Daemon`
   (`src/aprilcam/client/interfaces.py`)
2. `Daemon.list_cameras()` → `tuple[CameraState, ...]`
   (`src/aprilcam/daemon/interfaces.py`)
3. `CameraState.name` / `.playfield` / `.calibrated` / `.present` /
   `.usable` (`src/aprilcam/types.py`) → the columns

Most of this UC is the `CameraState` type doing exactly what it was
designed for. The linked-playfield and calibrated columns of S-1 are
`playfield` and `calibrated`; "currently readable" is `usable`;
the unopenable-but-listed failure mode is the documented
`present`-true/`usable`-false state, and `list_cameras`'s docstring
explicitly promises absent and unusable cameras are listed, never
omitted. No cameras is an empty tuple, not an error. S-2 is the same
call against `connect("pi-field.local")`; the no-daemon failure is a
connection error from `connect` (undeclared type, D-2 — the "suggest
`aprilcam daemon list`" text is CLI-side). S-3/S-4 follow the fan-out
model from the preamble: `find_all()`, one `connect` per daemon, rows
labelled with `DaemonInfo.host`, a per-daemon connect failure reported
as unreachable without failing the rest.

The gap is the very first column S-1 names: the **stable enumeration
number**. `CameraState` carries `name` and five booleans-and-a-link —
no enumeration number, no index. The registry that owns
identity → enum-number (`cameras/registry.json`,
[../configuration.md](../configuration.md); `CameraRegistry` in
`src/aprilcam/camera/interfaces.py` is daemon-internal) never crosses
the wire. "Listed with its stable enumeration number and identity"
cannot be printed from what `list_cameras` returns.

**Deficits.**
1. **`CameraState` has no enumeration-number field**, so
   UC-14.S-1's "with their enumeration numbers" is unprintable.
   (D-3)

**Resolution (2026-07-31).** `CameraState.number` was added
(`src/aprilcam/types.py`): the stable registry enumeration, assigned
once and never reshuffled, `None` only for a not-yet-registered
camera. The unknown-target error shape also gained a type
(`UnknownCamera` carrying `requested` and `available`,
`src/aprilcam/errors.py`), closing this UC's D-2 exposure.

---

## UC-15 — Calibrate a camera from the command line

**Verdict:** COVERED (notes)

**Entry:** `aprilcam camera calibrate <camera> [-d HOST]`

**Trace.**
1. `Discovery.connect(host)` → `Daemon`
2. `Daemon.calibrate(camera)` → `CalibrationResult`
   (`src/aprilcam/daemon/interfaces.py`,
   `src/aprilcam/calibration/interfaces.py`)
3. `CalibrationResult.succeeded` / `.matched_ids` / `.missing_ids` →
   what the command reports; since sprint 045 the pose fields
   (`.method`, `.confidence`, `.corner_count`, `.reprojection_error_px`,
   `.reprojection_error_cm`, `.scale_agreement`, `.angular_spread_deg`,
   `.camera_height_cm`/`.camera_nadir`/`.camera_tilt_deg`, `.notes`,
   `.helpful_missing_ids`, `.leave_one_out_cm` for `--verbose`) →
   the report lines of S-5 and S-6. `--with-intrinsics` no longer exists.

The result type is well matched to the scenarios. S-1: `succeeded`
true, `matched_ids` lists the ten ids to report, and the follow-up
`aprilcam camera list` observation comes from `CameraState.calibrated`.
S-3: `succeeded` false with `missing_ids == (6,)` — the type's own
docstring makes the "which marker to uncover" reporting the point of
the field — and leaving prior calibration intact is `Calibrator`'s
documented behaviour, observable afterwards via `Daemon.
get_calibration`. S-4 (`--all` refused) is CLI-side argument checking.

S-2, the no-playfield precondition, deserves care because the UC
insists it is a precondition error, *not* a calibration failure — a
different observable from S-3. The interfaces support the distinction
two ways: `Calibrator.calibrate`'s docstring classifies no-playfield as
a precondition error (so `Daemon.calibrate` raises rather than
returning a failed result — undeclared exception, D-2), and the CLI can
equally pre-check `Daemon.get_camera_config(camera).playfield is None`
and refuse before triggering anything, which also satisfies "no frames
are captured." Either way the outcome is producible. Camera-unreadable
is likewise an exception.

**Notes, not deficits.** The main path's "reports progress" has no
channel — `calibrate` is one blocking call — but no scenario asserts
progress, so this stays a note. The undeclared exception types are
D-2.

---

## UC-16 — Watch live raw video from a camera

**Verdict:** COVERED (deficit resolved 2026-07-31)

**Entry:** `aprilcam camera view <camera> [-d HOST]`

**Trace.**
1. `Discovery.connect(host)` → `Daemon`
2. `Daemon.stream_images(camera, deskewed=False)` →
   `Iterator[ImageFrame]` (`src/aprilcam/daemon/interfaces.py`)
3. `ImageFrame.data` / `.encoding` (`src/aprilcam/types.py`) → decoded
   and blitted to a window, frame by frame

Raw-means-raw is enforced by the types, which is the elegant part:
a raw `ImageFrame` carries `annotations=None` by contract and no
world→pixel metadata, so the CLI *could not* draw annotations on it
even by mistake, and since UC-16 draws no overlays at all the
`Renderer` is not even involved. S-2 (watch a pushed config take
effect) is this stream running while UC-23's `set_camera_config` fires
— nothing extra needed. S-3 (headless) is a CLI-local environment
check before subscribing; no interface implicated. Camera-unreadable →
error rather than a black window is an exception from the subscribe
call (D-2). The dark-frame advisory is *reachable* but awkward:
`ImageFrame` carries no dark flag, so surfacing it mid-view means
polling `list_cameras()` for `CameraState.frame_dark` alongside the
stream — producible, so a note, though it is worth asking whether the
flag belongs on the frame.

The deficit is the unsubscribe step, which is on the main path ("the
developer closes it; the CLI unsubscribes") and load-bearing —
`ImageStream`'s docstring is explicit that holding a stream open costs
the daemon real work. The client library declares `ImageStream` and
`TagStream` (`src/aprilcam/client/interfaces.py`) with exactly the
needed `close()` semantics — but **nothing in the declared surface
returns them**. The object the trace actually yields is
`Daemon.stream_images`'s bare `Iterator[ImageFrame]`, and `Iterator`
declares no `close()`. Generator objects happen to support `close()`
in CPython, but that is an implementation assumption, not the
contract; the two stream protocols are orphaned as written.

**Deficits.**
1. **`ImageStream`/`TagStream` are unreachable** — declared with the
   unsubscribe contract the UC needs, but no declared method returns
   them; `Daemon.stream_images`/`stream_tags` return bare `Iterator`s
   with no declared close/unsubscribe path. (D-4)

**Resolution (2026-07-31).** `ImageStream` and `TagStream` were
**moved** to `src/aprilcam/daemon/interfaces.py`, next to the methods
that now return them — `stream_images` returns `ImageStream`,
`stream_tags` returns `TagStream` — on the argument that
unsubscription is a daemon-visible event, not client bookkeeping
(`src/aprilcam/client/interfaces.py` keeps a pointer comment). The
main path's close step now has a declared `close()`. The dark-frame
note also improved beyond what was asked: `ImageFrame.frame_dark`
(`src/aprilcam/types.py`) carries the advisory in-band, tied to the
frame in hand, replacing the poll-`CameraState` workaround.

---

## UC-17 — Capture a single raw frame

**Verdict:** COVERED (deficit resolved 2026-07-31)

**Entry:** `aprilcam camera image <camera> [-o FILE] [-d HOST]`

**Trace.**
1. `Discovery.connect(host)` → `Daemon`
2. `Daemon.get_frame(camera, deskewed=False)` → `ImageFrame`
3. `ImageFrame.data` → raw bytes to stdout · base64-encoded to stdout ·
   written to a file

The three destinations are pure adapter work over one field, and the
failure modes are almost all CLI-local: unwritable path checked before
the call, binary-to-a-terminal refusal is an isatty check (S-4),
camera-unreadable is an exception from `get_frame` with nothing yet
written (D-2). S-2 and S-3 need nothing beyond `data` and `base64`.

S-1 is where it breaks. The scenario writes a **lossless PNG** as a
test fixture — the UC argues losslessness matters precisely because
JPEG artifacts would be baked into ground truth. But `get_frame`
declares **no way to request an encoding**, and `ImageFrame.encoding`
is purely descriptive: the daemon serves whatever it serves. If that is
JPEG, the CLI can transcode to PNG (Pillow, cv2-free — the sanctioned
render-time pixel step), but a PNG of a JPEG is not a lossless frame,
and the fixture scenario is silently corrupted while appearing to
succeed. The same hole swallows the unsupported-format failure mode
("error listing the supported ones") — with no format parameter there
is no supported-format set to name.

**Deficits.**
1. **No encoding selection or losslessness guarantee on
   `Daemon.get_frame`** — no format/encoding parameter, no documented
   default; UC-17.S-1's lossless fixture is not producible as
   specified, and the unsupported-format error has no anchor. Also
   touches UC-19 (see there). (D-5)

**Resolution (2026-07-31).** `Daemon.get_frame` gained an `encoding`
keyword (`"jpeg"` default, `"png"` for lossless — the docstring cites
UC-17.S-1's fixture rationale directly), with `ImageFrame.encoding`
reporting what was actually produced; an unsupported value now has a
parameter to be rejected against. Deliberate scope limit worth noting:
encoding selection is **one-shot capture only** — streams stay JPEG,
per-frame loss being the accepted trade there — which UC-17 never
needed anyway.

---

## UC-18 — Watch the live calibrated playfield

**Verdict:** COVERED (deficit resolved 2026-07-31)

**Entry:** `aprilcam playfield view [-d HOST]`

**Trace.**
1. `Discovery.connect(host)` → `Daemon`
2. `Daemon.list_playfields()` → `Playfield.linked_cameras()`
   (`src/aprilcam/playfield/interfaces.py`) → resolve the camera slug
   (the command names neither playfield nor camera — D-7)
3. `Daemon.stream_images(camera, deskewed=True)` →
   `Iterator[ImageFrame]`, each frame carrying `tags: TagFrame` and
   `annotations: AnnotationSet` embedded
4. `Renderer.render(frame, layers=…)` → PNG bytes
   (`src/aprilcam/client/interfaces.py`) → blitted to a window

The happy path is genuinely well provisioned — this is the flow the
frame-embedding design exists for. One deskewed frame is
self-sufficient: `world_extent`/`px_per_cm` give the world→pixel
mapping, the embedded `TagFrame`'s `world_corners` and `yaw_rad` let
`Renderer` draw boundary quads and hats with no second query, north-up
comes from the `Deskewer` contract, and S-1's moving-robot observation
is just successive frames. The layer-panel model is literally
`Renderer.render`'s `layers` parameter. S-2 (uncalibrated) is covered
by `get_frame`/`stream_images`' documented explicit failure on a
deskewed request against an uncalibrated camera — never a raw
substitute, exactly the UC's demand (exception type undeclared, D-2).
No-playfield → `linked_cameras()` resolution finds nothing → error.
Headless → CLI-local, as UC-16.S-3.

Two failure modes do not trace. **Stale-mid-view** (explicitly
required: "surfaced in the view, not silently ignored," citing
UC-11.S-3): `stream_tags`' docstring promises the subscriber is told,
but no declared type can carry the message — `ImageFrame`, `TagFrame`,
and `TagRecord` have no staleness field. `TagRecord.calibrated` means
uncalibrated, a different state from stale (the types distinguish them
deliberately in `CameraState`: `calibrated` and `calibration_stale`
are separate fields). The only staleness carriers are
`CameraState.calibration_stale` and `Calibration.stale` — poll-only
surfaces, invisible to a stream subscriber. The promise has no
mechanism. And the **unsubscribe step** shares UC-16's orphaned-stream
deficit (D-4).

**Deficits.**
1. **Staleness has no carrier in stream payloads** — `stream_tags`
   promises mid-stream stale notification and UC-18 requires it in the
   view, but no field on `ImageFrame`/`TagFrame`/`TagRecord` can
   express it. (D-6)
2. Unsubscribe path — shared with UC-16. (D-4)
3. Playfield → camera resolution rule undefined when multiple
   playfields or linked cameras exist. (D-7)

**Resolution (2026-07-31).** All three: `TagFrame.calibration_stale`
and `ImageFrame.calibration_stale` (`src/aprilcam/types.py`) are the
in-band staleness carriers — `stream_tags`' docstring now names the
field its promise rides on, so stale-mid-view is renderable from the
payload itself (D-6). `stream_images` returns the relocated
`ImageStream` with its `close()` (D-4, see UC-16). And
[cli.md](cli.md) gained a "Playfield commands resolve their camera"
convention: client-side resolution from `CameraState`'s
camera→playfield links, exactly one linked present camera → implicit,
zero or several → error naming the linked cameras, `--camera` to
disambiguate (D-7). A bonus for the render step: `ImageFrame`'s new
anchor-invariant paragraph pins deskewed `(0, 0)` to the field's NW
corner, making the `Renderer`'s world→pixel conversion fully
determined rather than scale-plus-guessed-origin.

---

## UC-19 — Capture a single deskewed playfield frame

**Verdict:** COVERED (notes)

**Entry:** `aprilcam playfield image [-o FILE] [-d HOST] [--all]`

**Trace.**
1. `Discovery.connect(host)` → `Daemon`
2. `Daemon.list_playfields()` → `Playfield.linked_cameras()` → camera
   slug (D-7)
3. `Daemon.get_frame(camera, deskewed=True)` → `ImageFrame`
4. `ImageFrame.data` → file / stdout / base64, as UC-17

S-1's 800×532 north-up frame is the `Deskewer` bounding-box contract
made observable through `ImageFrame.width`/`.height`; writing
`field.png` may involve a client-side transcode given D-5, but unlike
UC-17.S-1 this scenario asserts no losslessness, so the observable
outcome — a PNG of the deskewed frame — is producible. S-2
(uncalibrated) is `get_frame`'s documented explicit failure with
nothing written and no raw frame substituted; the remaining failure
modes are UC-17's, traced there. Fan-out follows the standard model.
The open question about world↔pixel sidecar metadata is answerable
when it is decided: `px_per_cm` and `world_extent` are already on the
frame, so the CLI has the data to emit — only the output format is
undecided.

**Notes.** D-5 (encoding) touches the `-o file.png` ergonomics;
D-7 (playfield resolution) applies as in UC-18. Neither blocks a
scenario as written here.

---

## UC-20 — List the tags on the playfield

**Verdict:** COVERED (notes)

**Entry:** `aprilcam playfield tags [-d HOST] [--all]`

**Trace.**
1. `Discovery.connect(host)` → `Daemon`
2. `Daemon.list_playfields()` → `Playfield.linked_cameras()` → camera
   slug (D-7)
3. `Daemon.get_tags(camera)` → `TagFrame`
4. Per `TagRecord`: `tag.family` / `tag.number` / `world` /
   `heading_rad` / `world_velocity` / `speed` / `calibrated` → the rows

The record type answers every column the UC names: family and id from
`TagId` (never a negative-id encoding, per the type's own contract),
world position in cm, heading, velocity — with mount corrections
already applied by documented contract, satisfying the UC-12
reference. All-tags-no-limit is `get_tags`' explicit contract, as is
empty-frame-not-error for S-1's "exactly 4" and the nothing-detected
failure mode. S-3 (uncalibrated) is the type system at its best:
`world` is `None`, `pixel` is populated, `calibrated` is `False` — the
CLI *cannot* print world coordinates from an absent homography because
there is nothing to print, and the explicit flag is right there. S-2
fans out per the standard model, labelled from `DaemonInfo.host`.

**Notes.** D-7: the command names no playfield and no camera, and
`get_tags` is camera-addressed; with one playfield and one linked
camera the resolution is forced, but two linked cameras (a playfield
legitimately has "zero or more") leaves which-camera — or a merge —
undefined. The machine-readable-output open question is CLI
formatting; the data is fully structured already.

---

## UC-21 — Generate tag images

**Verdict:** COVERED (deficit resolved 2026-07-31)

**Entry:** `aprilcam tags generate <family> <ids> [-o TARGET]`

**Trace.** There is nothing to trace. The command is offline by design
— no `Discovery`, no `Daemon` — and the interface skeleton set
contains **no generation interface at all**: `src/aprilcam/vision/`
has no interfaces (daemon-internal, correctly), and no `tags/`
subsystem interface exists despite [../specification.md](../specification.md)
§4.10 naming one. Grepping the seven interface files for anything
generation-shaped finds nothing.

Every scenario is therefore unexecutable against the declared surface:
S-1 (ArUco 4×4 ids 1–10 at printable size), S-2 (AprilTag 36h11 id 7
matched to a registered `size_cm`), and S-3 — which is the sharpest
loss, because id-range validation ("error naming the valid range")
requires the interface to expose each family's valid range, and no
type does. The physical-size-vs-print-resolution warning likewise has
no home. `TagFamily` in `src/aprilcam/types.py` names the families,
and that is the entire extent of the support.

**Deficits.**
1. **No tag-generation interface exists** — no protocol for
   family/dictionary selection, id-range validation, physical sizing,
   or output; all of UC-21 (S-1, S-2, S-3) is blocked. (D-8)

**Resolution (2026-07-31).** `src/aprilcam/tags/interfaces.py` (new)
declares `TagGenerator`: `valid_ids(family) -> range` publishes each
dictionary's range so the CLI validates before generating and S-3's
error names the range via `InvalidTagId` (`src/aprilcam/errors.py`,
carrying family, id, and valid range); `generate(family, ids, *,
size_cm, out_dir, dpi=300)` ties output to physical print size —
S-1's surveyed corner set and S-2's UC-12-registered robot-tag size —
with the unachievable-size warning and the
unwritable-path-fails-before-generating rule in its contract.
Dictionaries are fixed system-wide (36h11 / 4x4), so family alone
selects one, matching the one-command-with-a-family-option draft.

---

## UC-22 — Run the MCP server

**Verdict:** COVERED (notes)

**Entry:** `aprilcam mcp`

**Trace.**
1. Startup: **no interface call at all** — which is the point. Late
   binding means the server holds a `Discovery` and touches nothing
   until a tool call arrives.
2. Per tool call: `Discovery.connect(named_host_or_None)` → `Daemon` —
   `connect(None)` is the local daemon by contract, giving the
   local-if-unnamed default.
3. Tool bodies are the same `Daemon` calls as the rest of this file:
   `list_cameras()`, `get_frame(camera, deskewed=True)` (data
   base64-encoded by the adapter — handles and inline bytes, never
   file paths, satisfying A-02's constraint), `get_tag(camera,
   TagId(APRILTAG, 7))` → `TagRecord.world`.

The three scenarios are all consequences of per-call binding, and the
declared surface supports it: nothing in `Discovery` or `Daemon`
requires a connection at construction time, so S-2 (start with nothing
running, first call errors at tool level, later call succeeds in the
same session) and S-3 (restart mid-session; next call reconnects) are
both just `connect` being called fresh each time — the failed call
surfaces `connect`'s exception as a tool error (D-2 as to type; MCP's
own error envelope does the rest, which is adapter concern). The
server staying up through a failed tool call is adapter behaviour, not
interface. The uncalibrated-world-position failure mode is
`TagRecord`'s `world=None`/`calibrated=False` contract — fabrication
is unrepresentable, not merely discouraged.

**Notes.** The UC's own open questions (default among multiple
remotes; parameter-vs-connect-tool) are genuinely open and the
interfaces don't prejudge them — either answer is expressible with
`connect(host)`. The daemon-restart observable "changed camera
settings" is visible through `get_camera_config`. No deficits.

---

## UC-25 — Keep the camera list current, and bootstrap configs

**Verdict:** COVERED (deficit resolved 2026-07-31)

**Entry:** `aprilcam camera probe [-d HOST] [--all]`

**Trace.**
1. `Discovery.connect(host)` → `Daemon`
2. `Daemon.probe_cameras()` → `tuple[CameraState, ...]`
   (`src/aprilcam/daemon/interfaces.py`; daemon-internally
   `CameraDiscovery.probe`, `src/aprilcam/camera/interfaces.py`)
3. `CameraState.name` / `.present` / `.usable` → the refreshed list
4. For the bootstrap chain: `Daemon.get_camera_config(name)` →
   `CameraConfig.settings` ("every settable key, blank") → dump
   (UC-24) → edit → push (UC-23)

The scenarios that live in `CameraState` trace well. S-4 (present but
unopenable, reported with its derived name and an explicit unusable
flag, never omitted) is the exact `present`/`usable` contract the type
documents, citing this scenario. S-5 (hotplug, no command) is
daemon-internal `CameraDiscovery.watch()`, observed through
`list_cameras` — nothing needed from the CLI. S-6 is the same
`probe_cameras` call against `connect("pi-field.local")`; that active
interrogation reaches libcamera devices is the daemon's documented
obligation. S-3 (name stability across re-plug) is `CameraIdentity`'s
contract, observed as the same `name` in successive probe results.
S-1/S-2's "settings its hardware exposes" and per-camera key sets are
not on `CameraState` but are reachable by following each probed camera
with `get_camera_config` — the self-describing template contract on
`CameraConfig.settings`. Configured-camera-disappears is
`present=False` with the config still dumpable. Empty result, not an
error: tuple semantics.

Two asserted behaviours have no carrier. Main-path step 4 requires the
command to report **what changed** — appeared, disappeared, unchanged.
The CLI can synthesize a diff (`list_cameras()` before,
`probe_cameras()` after), which is producible if inelegant — a note.
But S-7's contract cannot be synthesized: when a backend cannot be
interrogated without disturbing an open camera, probe "skips it **and
says so**", and a wedged subprocess "times out into a **partial
result**" — both promised in `CameraDiscovery.probe`'s own docstring,
and neither expressible in `tuple[CameraState, ...]`. There is no
skipped-backends field, no partial flag, no per-backend notes.
The docstring writes a check the return type cannot cash.

**Deficits.**
1. **Probe's return type cannot report skipped backends or
   partial/timeout results** — `probe_cameras()` →
   `tuple[CameraState, ...]` has no channel for UC-25.S-7's
   "skips it and says so" or the documented timeout-to-partial
   behaviour; a probe-result type is missing. (D-10)

**Resolution (2026-07-31).** `Daemon.probe_cameras()` now returns
`ProbeReport` (`src/aprilcam/types.py`): the camera states plus
`skipped: tuple[ProbeSkip, ...]` naming each backend the probe could
not interrogate and why — `skipped` empty meaning the probe really
was complete, which is S-7's "says so" made structural. The
appeared/disappeared report of main-path step 4 remains a client-side
diff against a prior `list_cameras()` — as noted above, producible
and never blocking. (An adjacent gain: `Daemon.reset_camera_config`
now ends an experiment without a daemon restart, rounding out the
probe→dump→edit→push loop this UC anchors.)

---

## UC-23 — Push a configuration document to a daemon

**Verdict:** COVERED (deficit resolved 2026-07-31)

**Entry:** `aprilcam camera config <file>` · `aprilcam playfield
config <file>` `[-d HOST]`

**Trace.**
1. CLI reads the file and constructs a `CameraConfig` /
   `PlayfieldDefinition` — **no declared construction surface exists**
   (see deficit)
2. `Discovery.connect(host)` → `Daemon`
3. `Daemon.set_camera_config(config)` /
   `Daemon.set_playfield(definition)` — targeted by the document's own
   `ConfigDocument.name`, no separate target argument, exactly the
   self-naming contract (`src/aprilcam/config/interfaces.py`)
4. Report: "applied to `<name>`" from the document's `name`

The daemon-side half is well matched to the scenarios. Self-naming is
structural: `set_camera_config` and `set_playfield` take only the
document, so the redundant-name command shape is unrepresentable.
S-5 (unknown camera named) → the push fails; the "listing the two the
daemon actually has" observable is composable client-side from
`list_cameras()` even if the error itself is an undeclared exception
(D-2). Hardware-rejects-a-setting → the whole push fails per
`set_camera_config`'s documented all-or-nothing rule
(`Camera.apply_config` carries the never-half-configured guarantee
daemon-side). Malformed-rejected-whole with previous state intact is
`set_playfield`'s documented contract. Ephemerality is `Daemon`'s
class-level contract. S-3 is the same trace via `connect` with a host.
`--all` refusal is CLI-side.

The client-side half is the hole. Step 1 has no interface:
`ConfigDocument` declares `to_dict()` but **no inverse** — no
`from_dict`, no loader, nothing that takes file bytes and yields a
`CameraConfig` or `PlayfieldDefinition`. Since both are Protocols,
*something* must construct concrete instances from the file the user
named, and that something is exactly where two asserted behaviours
live: the main path's "validates it locally enough to report obvious
problems well," and S-4's no-`name` rejection — which must happen with
**nothing sent to the daemon**, so it cannot be delegated to
`set_camera_config`'s server-side validation. As declared, the
missing-name error has no home, and neither does file-missing /
malformed-JSON ("error before contacting the daemon").

**Deficits.**
1. **No config-document loading/construction surface** —
   `ConfigDocument.to_dict()` has no declared inverse; the CLI cannot
   get from a file to a pushable `CameraConfig`/`PlayfieldDefinition`
   through any declared call, and UC-23.S-4's client-side
   missing-`name` rejection (plus local validation of unreadable /
   malformed files) has nowhere to live. (D-9)

**Resolution (2026-07-31).** `ConfigLoader`
(`src/aprilcam/config/interfaces.py`) is the declared inverse of
`to_dict`: `load_camera_config(path)` and `load_playfield(path)`
construct the pushable documents, with unparseable JSON or a missing
`name` raising `MalformedDocument` (`src/aprilcam/errors.py`)
**client-side, before any RPC** — S-4's nothing-sent-to-the-daemon
observable, verbatim. The round-trip contract is now stated
bidirectionally (`load(x.to_dict()) == x`), binding this UC to
UC-24.S-1's dump-install-restart symmetry. S-5's error shape gains
`UnknownCamera(requested, available)`, so the "listing the two the
daemon actually has" observable is typed rather than composed.

---

## UC-24 — Dump a daemon's live configuration

**Verdict:** COVERED (notes)

**Entry:** `aprilcam camera config <camera> --dump` · `aprilcam
playfield config <name> --dump` `[-d HOST] [--all]`

**Trace — camera.**
1. `Discovery.connect(host)` → `Daemon`
2. `Daemon.get_camera_config(camera)` → `CameraConfig`
3. `CameraConfig.to_dict()` → serialized to stdout

**Trace — playfield.**
1. `Discovery.connect(host)` → `Daemon`
2. `Daemon.get_playfield(name)` → `Playfield`
   (`src/aprilcam/playfield/interfaces.py`)
3. `Playfield.definition` → `PlayfieldDefinition.to_dict()` → stdout

The round trip the UC calls load-bearing is exactly what
`ConfigDocument.to_dict()` promises: "serialize in exact on-disk
format… loadable verbatim, including `name`," with UC-24.S-1 cited in
the docstring. S-1's dump-install-restart-identical observable is
producible on that contract; S-2 is the same trace through
`connect("pi-field.local")` with the `scp` half outside the software
entirely. S-3 (unconfigured camera dumps the introspected-plus-blank
template rather than erroring) rests on the `CameraConfig.settings`
contract — "every settable key, blank when unset," the template every
config starts from — with the behaviour anchored daemon-side on
`Camera.effective_config`. S-4 (unknown target → error listing what
the daemon has) is an exception from `get_camera_config` (D-2), with
the listing composable from `list_cameras()`. Fan-out of `--dump`
follows the standard model.

**Notes.** `Daemon.get_camera_config` itself has a bare signature and
no docstring — its unconfigured-camera behaviour is inherited from
type contracts rather than stated at the call site; worth a line when
the skeleton is fleshed out, but the contract chain is unambiguous.
D-2 applies to S-4's error shape.

---

## Deficit summary

The deduplicated list, in the order first encountered. Each entry is
self-contained for ticketing. **All ten were resolved directly in the
skeletons on 2026-07-31** (stakeholder decision: fix, don't ticket);
each entry carries its resolution, verified against the updated files
rather than taken on report.

1. **D-1 — No remote-readable daemon status.** `DaemonStatus` is
   obtainable only via `DaemonLifecycle.status()`
   (`src/aprilcam/daemon/interfaces.py`), which is local-only by
   contract; `Daemon` has no status/readiness method. Blocks UC-13's
   drafted `daemon status -d HOST` and the fan-out table's `daemon
   status --all` (`config_dir`/`state_dir` and a clean
   running-report are unobtainable remotely). *Fix:* add a status
   query to `Daemon` returning the `DaemonStatus` surface (or a
   remote-safe subset).
   **Resolved 2026-07-31:** `Daemon.status() -> DaemonStatus` added
   (`src/aprilcam/daemon/interfaces.py`), documented as the
   remote-readable service view; spec §4.1 gained `GetStatus`.
2. **D-2 — No error taxonomy anywhere in the skeletons.** Every
   error-shaped scenario — discovery-failed vs found-nothing
   (UC-13.S-5), connection error naming the host (UC-14),
   precondition vs calibration failure (UC-15.S-2), unknown target
   listing alternatives (UC-23.S-5, UC-24.S-4), deskew-on-uncalibrated
   (UC-18.S-2, UC-19.S-2) — rides on undeclared exceptions.
   Mechanically producible via raise-vs-return plus message strings,
   but the structured distinctions the scenarios assert have no typed
   surface. Touches UC-13…UC-20, UC-22…UC-25. *Fix:* declare an
   exception module (discovery failure, connection failure,
   unknown-target carrying the known set, precondition violation,
   push rejection) as part of the client contract.
   **Resolved 2026-07-31:** `src/aprilcam/errors.py` (new) —
   `AprilCamError` base; `DiscoveryFailed` (distinct from empty by
   type); `ConnectionFailed(host, reason)`; `UnknownTarget(requested,
   available)` with `UnknownCamera`/`UnknownPlayfield`/`UnknownGuide`;
   `NotCalibrated`; `UnsupportedSetting(key)`; `MalformedDocument`;
   `InvalidTagId(family, id, valid)`; `AlreadyRunning(holder)`. The
   module's own contract: distinctions are types, not message strings,
   and no adapter parses text.
3. **D-3 — `CameraState` lacks the stable enumeration number.**
   UC-14.S-1 lists cameras "with their enumeration numbers"; the
   registry's identity → enum-number mapping never crosses the wire
   (`src/aprilcam/types.py`). Blocks UC-14. *Fix:* add an
   enumeration-number field to `CameraState`.
   **Resolved 2026-07-31:** `CameraState.number: Optional[int]` —
   stable registry enumeration, never reshuffled, `None` only for an
   unregistered camera.
4. **D-4 — `ImageStream`/`TagStream` are orphaned.** Declared in
   `src/aprilcam/client/interfaces.py` with the close-to-unsubscribe
   contract, but nothing returns them; `Daemon.stream_images`/
   `stream_tags` return bare `Iterator`s with no declared close path.
   Blocks the unsubscribe step of UC-16 and UC-18. *Fix:* have the
   client-side stream calls return `ImageStream`/`TagStream` (or fold
   `close()` into the daemon protocol's return type).
   **Resolved 2026-07-31:** both protocols moved to
   `src/aprilcam/daemon/interfaces.py`, and
   `Daemon.stream_images`/`stream_tags` now return them —
   unsubscription framed as a daemon-visible event; the client module
   keeps a pointer comment.
5. **D-5 — No frame-encoding selection on `Daemon.get_frame`.** No
   format parameter and no documented default encoding; UC-17.S-1's
   lossless-PNG fixture is not producible as specified (a client-side
   transcode of a lossy frame silently fakes it), and the
   unsupported-format error has no anchor. Blocks UC-17; touches
   UC-19. *Fix:* an encoding parameter on `get_frame` (or a
   documented lossless default) plus a defined supported set.
   **Resolved 2026-07-31:** `get_frame(camera, *, deskewed=False,
   encoding="jpeg")`, with `"png"` documented as lossless for
   fixtures; one-shot capture only — streams remain JPEG by stated
   trade.
6. **D-6 — Staleness has no carrier in stream payloads.**
   `stream_tags` promises the subscriber is told when calibration goes
   stale; UC-18 requires it surfaced in the live view — but
   `ImageFrame`/`TagFrame`/`TagRecord` have no staleness field
   (`calibrated` is a different state; `CameraState.calibration_stale`
   is poll-only). Blocks UC-18's stale-mid-view failure mode (and
   UC-11.S-3 by reference). *Fix:* add a `calibration_stale` field to
   `TagFrame` (and/or `ImageFrame`).
   **Resolved 2026-07-31:** both — `TagFrame.calibration_stale` and
   `ImageFrame.calibration_stale` (`src/aprilcam/types.py`), with
   `stream_tags`' docstring naming the field as the in-band carrier.
   `ImageFrame.frame_dark` was added in the same spirit (per-frame
   darkness advisory).
7. **D-7 — Playfield → camera resolution rule undefined.** `playfield
   view|image|tags|annotations` name no playfield and no camera; the
   daemon surface is camera-addressed by design, so the CLI resolves
   via `list_playfields()` → `linked_cameras()` — well-defined only
   when exactly one of each exists. Touches UC-18, UC-19, UC-20 (and
   the annotations command). *Fix:* a design decision in cli.md
   (error on ambiguity, or an optional playfield/camera argument), not
   necessarily a new interface.
   **Resolved 2026-07-31:** [cli.md](cli.md) gained a "Playfield
   commands resolve their camera" convention — client-side resolution
   from `CameraState` links; exactly one linked present camera is
   implicit, zero or several errors naming the linked cameras, with
   `--camera` to disambiguate. No new interface needed, as proposed.
8. **D-8 — No tag-generation interface exists.** `aprilcam tags
   generate` has no corresponding protocol anywhere — no
   family/dictionary selection, no id-range validation (UC-21.S-3's
   "error naming the valid range"), no sizing or output surface;
   `TagFamily` alone is not an interface. Blocks all of UC-21. *Fix:*
   add `src/aprilcam/tags/interfaces.py` (per spec §4.10's `tags/`
   subsystem) with a generator protocol exposing per-family id ranges,
   physical sizing, and output targets.
   **Resolved 2026-07-31:** `src/aprilcam/tags/interfaces.py` (new) —
   `TagGenerator.valid_ids(family) -> range` and `generate(family,
   ids, *, size_cm, out_dir, dpi=300)`, with `InvalidTagId` in
   `aprilcam.errors` carrying the valid range for UC-21.S-3.
9. **D-9 — No config-document loading surface.**
   `ConfigDocument.to_dict()` has no declared inverse; the CLI cannot
   construct a pushable `CameraConfig`/`PlayfieldDefinition` from a
   file through any declared call, and the client-side rejections
   UC-23 asserts (missing `name` with nothing sent to the daemon,
   S-4; unreadable/malformed file before contact) have no home.
   Blocks UC-23; the probe→dump→edit→push loop of UC-25.S-1 is
   blocked at its push step by the same gap. *Fix:* declare a loader
   (`from_dict`/`load_file`) in `src/aprilcam/config/interfaces.py`
   that validates `name` presence and schema shape.
   **Resolved 2026-07-31:** `ConfigLoader` protocol
   (`src/aprilcam/config/interfaces.py`) — `load_camera_config(path)`
   / `load_playfield(path)`, raising `MalformedDocument` client-side
   before any RPC, with the bidirectional round-trip contract
   `load(x.to_dict()) == x` stated.
10. **D-10 — Probe cannot report skipped backends or partial
    results.** `Daemon.probe_cameras()` returns only
    `tuple[CameraState, ...]`, but `CameraDiscovery.probe`'s own
    contract requires "skip it and say so" (UC-25.S-7) and
    timeout-to-partial reporting — neither expressible in the return
    type. Blocks UC-25.S-7. *Fix:* a probe-result type wrapping the
    camera states plus per-backend skipped/timed-out notes (which
    would also give the main path's appeared/disappeared report a
    home instead of a client-side diff).
    **Resolved 2026-07-31:** `ProbeReport` / `ProbeSkip`
    (`src/aprilcam/types.py`); `Daemon.probe_cameras()` returns
    `ProbeReport`, `skipped` empty meaning genuinely complete. The
    optional appeared/disappeared half of the proposal was not taken —
    that report remains a client-side diff, which was noted as
    producible and never blocking.
