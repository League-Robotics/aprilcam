# AprilCam v2 Development Plan

**Owner:** Eric Busboom · **Last reviewed:** 2026-07-30 · **Status:** active — the build-order reference for v2 development

> **This document answers *in what order*. It does not decide *what*.**
> The requirements live in [use-cases/](use-cases/) — 27 use cases and
> 100 scenarios, which are the acceptance criteria every stage below is
> ultimately tested against. Where this plan and a use case disagree, the
> **use case wins** and this plan is the thing to fix.
>
> Reconciled against the use-case set on 2026-07-30; the stage structure
> is unchanged, the per-stage detail was updated where five sessions of
> decisions had overtaken it.

This is the order in which we build and test the v2 system described in
[specification.md](specification.md), [design.md](design.md), and
[use-cases/](use-cases/). The
method is strictly **subsystem by subsystem, bottom-up**: each stage
produces a component with its own independent test suite. This is a
brownfield rebuild done **in place, in a worktree** — every stage says
what is **reused** from v1, what is **rewritten**, and what is **new**.

> **Build-order revision (2026-07-31, stakeholder): vertical slices.**
> Delivery order is no longer stage-by-stage bottom-up. The daemon and
> the CLI are built **together**, sprint by sprint — every sprint that
> gives the daemon a capability also delivers that capability's CLI
> surface, so each sprint ends with something a person can run (and the
> CLI/Python parity rule is exercised from day one instead of at Stage
> 11). The **MCP server comes after** the daemon + CLI basics; the
> viewer stays last. The stages below remain the catalog of *what* —
> reuse/rewrite decisions, tests, exit criteria — and sprints draw from
> them; per-stage test detail still applies to whichever slice of a
> stage a sprint takes.
>
> Sprint sequence. **020–024 are done and merged**; 025 onward is the
> remaining plan, re-derived 2026-08-03 from what is actually built.
>
> 1. ✅ **020 — Daemon bring-up + basic camera CLI.** Lifecycle
>    (flock singleton, control plane), camera identity/registry/
>    discovery, raw capture, minimum client library, `daemon
>    start|stop|status`, `camera list|probe|image`.
> 2. ✅ **021 — Detection + tag queries.** Vision pipeline, AprilTag
>    36h11 + ArUco 4x4, ring buffer, `camera tags`, camera-by-number
>    addressing.
> 3. ✅ **022 — Calibration, deskew, live view.** Playfield definitions,
>    homography, world coordinates, deskew to a fixed bounding box,
>    `camera calibrate`, `playfield image`, `camera view`/`playfield
>    view`.
> 4. ✅ **023 — Config service + calibration trust.** Boot-time config
>    load (config now survives restart), real hardware settings via
>    `uvc-util`, `--dump`/`--reset`, reprojection threshold, reachable
>    calibration staleness.
> 5. ✅ **024 — Annotation store + detection trust.** Daemon-held
>    world-coordinate annotations with client rendering, `playfield
>    annotations`, and presence-based temporal confirmation that
>    eliminates phantom tag detections.
>
> **Remaining, in dependency order:**
>
> 6. **025 — Complete the daemon's query surface.** The last
>    non-streaming capabilities that are *declared but unimplemented*:
>    `find_feature` (UC-08, "where is the blue dot"), the mobile-tag
>    registry `register_tag`/`unregister_tag`/`list_tag_parameters`
>    (UC-12 — the difference between reporting where a *tag* is and
>    where the *robot* is), and `get_guide` (UC-27, packaged
>    instructions). **Deliberately before the MCP server**: all three
>    are tools the MCP server must expose, and building MCP on an
>    incomplete daemon means doing the wire layer twice.
> 7. **026 — MCP server.** `src/aprilcam/mcp/` and `aprilcam mcp`
>    (UC-22) — the project's stated purpose, and the only major
>    subsystem that does not exist at all. Perception, configuration,
>    and annotation tools over the now-complete daemon.
> 8. **027 — Streams.** `stream_images`/`stream_tags` (UC-07, UC-11),
>    the two remaining unimplemented `Daemon` methods. The viewer stops
>    polling at ~2 Hz, and the ring buffer becomes wall-clock bounded
>    rather than query-driven — closing sprint 021's recorded scope
>    boundary. Robot control loops need this; an MCP agent does not,
>    which is why it sits after 026.
> 9. **028 — Tag generation + daemon discovery.** `tags/` has zero
>    implementation files: `aprilcam tags generate` (UC-21) is how a
>    playfield gets built in the first place. Plus real mDNS and
>    `aprilcam daemon list` (UC-13) — discovery is currently stubbed
>    local-only, so `--all` fan-out can never find a second daemon.
> 10. **029 — Hardening, cleanup, and the design-doc set.** The
>    accumulated issue pool: the v1/v2 control-socket collision, the
>    stale pidfile and orphaned-daemon processes, `BrokenPipeError` on
>    piped CLI output, the deferred libcamera capture backend (needed
>    before any Pi-over-the-field deployment), the broken
>    `testdev/capture_matrix.py` fixture regenerator, per-subsystem
>    `DESIGN.md` bootstrap (`clasi design validate` currently fails),
>    and stage 12's package restructure. Also the `camera view` vs
>    `playfield view` signposting, which has already cost real
>    debugging time.
>
> **Not scheduled, deliberately**: multi-camera on one playfield (the
> cardinality is designed for it and nothing blocks it, but no use case
> demands it yet), the ROS bridge (adapters.md §8 future), and
> streamable-HTTP MCP transport.

Read this before anything else — it is the operating philosophy of the
whole rebuild:

- **We do NOT keep the system running at every stage.** Breaking the
  build is expected. Trying to hold the entire system green through every
  step of a rewrite is enormously expensive and is explicitly *not* a
  goal. You will make breaking changes freely.
- **You own only your current stage.** At any given stage you are
  responsible for testing and keeping working *only the subsystem that
  stage builds* — nothing else. Do not divert effort to repair code that
  a later stage will rewrite or a demolition already condemned. The rest
  of the tree may be broken; that is fine.
- **Delete first, don't preserve.** The v1 test suite is not carried
  forward — Stage 1 deletes it (and all unneeded v1 code). Tests are
  rebuilt from scratch as each subsystem is rebuilt. There are no
  parity-with-v1 oracle tests: once v1 is gutted it is gone, so new code
  is validated against fixtures and known-truth values, never against
  live v1 behaviour.
- **Convert, don't rewrite blindly.** Within a stage, take the v1 code
  for that same subsystem, convert it into what v2 needs, then test that
  converted subsystem. Reuse the good parts; modify as you go.

Two standing rules shape the testing below:

- **Test without hardware wherever possible.** The captured image-matrix
  fixtures (`tests/images/`, regenerable via
  `testdev/capture_matrix.py` — see design.md §8) are the substrate for
  pipeline, detection, calibration, and geometry tests. These fixtures
  survive the Stage 1 demolition. Hardware-touching tests are marked
  (`@pytest.mark.camera`) and skip cleanly on machines without cameras;
  CI runs everything else.
- **Efficiency is a requirement, not a hope.** The camera pipeline is
  expensive. Work happens **on demand**: ArUco corner detection happens
  at camera-open/calibration time, not per frame; AprilTag detection runs
  only when someone is asking (a live stream subscriber or a one-shot
  query); an idle daemon does near-zero work. Stage 8 has explicit CPU
  budget tests.

---

## Stage 0 — Scaffolding and fixtures (mostly done)

**Build:** nothing new — this is the ground we already stand on:
the design doc set (co-located `DESIGN.md`s), the CLASI design gate,
the image-matrix capture tool and first fixtures, `field-state.json`.

**Remaining:** commit everything; fill the HD-USB variation gap
(control-ordering fix, issue `image-matrix-fixture-test-suite.md`);
stakeholder verifies field state (are 36h11 tags on the field?).

**Exit criteria:** fixtures committed; `clasi design validate` ok;
field-state confirmed.

---

## Stage 1 — Demolition: gut what we don't need

**What it is:** the first real act of the rebuild is destruction. Before
any code is converted, we clear the ground so v2 is built without v1's
dead weight, its stale tests, or its now-wrong assumptions sitting next
to us as we work. This happens **first**, not last — the deferred
end-of-project cleanup that v1 plans used is exactly the pattern we're
abandoning.

**Largely done (2026-07-29):** all of v1 — `src/`, `tests/`, `testdev/`,
`proto/`, `scripts/`, `deploy/`, `contrib/` — was moved to `archive/`
rather than deleted, so it remains readable for the convert-don't-rewrite
work below without sitting in the build. The image-matrix fixtures are at
`archive/tests/images/` and the capture tool at
`archive/testdev/capture_matrix.py`; both move back as their stages need
them.

**Delete:**
- **The v1 test suite** — not carried forward, not maintained for parity.
  The only survivors are the **image-matrix fixtures** and the capture
  tooling that regenerates them, the substrate every later stage tests
  against.
- **Subsystems v2 does not use** — the modules the design set already
  marks as removed: legacy `protocol.py`/`client.py`, the `homocal` +
  upper-left-origin table, the web server (`remove-web-server.md`), the
  `ui/` tree, dead compositing paths, and any other v1 code no v2 stage
  reuses. When in doubt, check whether a later stage lists a module under
  **Reused**; if no stage claims it, it goes.

**Do NOT delete:** anything a later stage's **Reused** list names —
those modules are converted in place at their stage, not now. And do not
try to keep the tree importable or the test suite green after this
demolition; it will be broken, and staying broken is expected until each
subsystem is rebuilt at its stage.

**Tests:** none authored here — this stage removes tests, it doesn't add
them. The only check is negative.

**Exit criteria:** the condemned modules and the v1 test suite are gone;
`tests/images/` fixtures and `testdev/capture_matrix.py` remain; the
worktree is committed at a clean "demolished" baseline. The build is
*expected to be broken* — that is not a failure of this stage.

---

## Stage 2 — Camera discovery: identify, enumerate, describe

**What it is:** the subsystem that answers "what cameras exist, which
one is which, and what are they called" — identity resolution (stable
across reboots/replugs), enumeration with device details, the
persistent registry mapping identity → stable enum number + camera
directory.

**Reused:** `camera/identity.py` (identity chain), `camera/registry.py`
(atomic registry), `camera/camutil.py` (enumeration, device names,
failure diagnosis) — this code is good; it gets tests, not a rewrite.

**Fixed here:** the stale-registry-record bug (issue
`stale-registry-record-orphans-camera-from-its-calibration-dir.md`) —
liveness-aware dir allocation.

**Tests (`tests/test_camera_discovery.py`):**
- Enumerate: on a camera-equipped machine, `list_cameras` finds the
  connected devices with correct names (`@camera`).
- Identity stability: identity chain unit tests over recorded
  enumeration tables (no hardware — feed captured `system_profiler` /
  enumeration snapshots as fixtures); same device → same unique_id,
  fallback ordering respected.
- Registry: resolve() is idempotent; enum numbers monotonic and stable;
  dir adoption/disambiguation; the stale-record scenario from the issue
  reproduced and fixed; atomic-write crash safety (write, kill, reload).

**Exit criteria:** all tests green headless; `@camera` tests green on
this Mac (Arducam + HD USB found by name).

---

## Stage 3 — The Camera class: configure, read, own

**What it is:** one class that owns a physical camera end-to-end:
open/close a backend, read frames, **apply a configuration document to
the hardware** (UVC controls with correct ordering — manual modes
before values, learned in the first matrix run), report its effective
state, and hold its calibration when one exists. This is the object
the daemon manages one-of-per-camera, and the object the capture tool
and calibration use directly.

**Reused:** backends (`cv.VideoCapture`, `v4l2_reader.py` loopback
reader with its YUYV workaround, screencap), `_apply_camera_settings`
uvc-util machinery, `camera_config.py` load/save.

**Rewritten:** the scattered open/settings/reopen logic in
`CameraPipeline.start()` consolidates into `Camera`; `CameraPipeline`
(stage 8) becomes a thin loop around it.

**New:** control-ordering fix (auto-exposure → manual before pushing
exposure values; auto-WB off before WB temps — the HD USB finding);
config document contract per spec §4.1 (whole-document, introspected
fields reported, `reset()` re-applies stored config).

**Tests (`tests/test_camera_class.py`):**
- Config store/read round-trip: document in → saved → loaded →
  identical (headless, tmp dirs).
- Config application: mocked uvc-util runner asserts correct commands
  *in correct order* for a settings block; unsupported-control skip
  behavior; reset() re-issues baseline.
- Live (`@camera`): open both real cameras, read frames at configured
  resolution, apply the OV9782 baseline, read controls back, verify;
  re-run the capture matrix — the 12 previously-skipped HD USB
  variations must now apply.
- Recovery: reopen path re-applies settings (mock backend that fails
  N reads).

**Exit criteria:** headless tests green; matrix re-run captures the
full HD USB variation set; both cameras restored.

---

## Stage 4 — The image pipeline: raw frame → detection-ready image

**What it is:** a well-defined, explicitly staged transformation from a
raw camera frame to the image(s) detection needs: grayscale,
illumination flattening (the highpass divide), optional CLAHE/sharpen,
optional downscale-with-coordinate-rescale, optional undistort. Each
stage is a pure function `image → image` with declared parameters; the
pipeline is a named, inspectable sequence — not ad-hoc calls buried in
a detector.

**Reused:** the preprocessing internals of `core/detector.py`
(flattening, CLAHE, proc_width scaling) — extracted into
`vision/pipeline.py` as discrete stages.

**Tests (`tests/test_image_pipeline.py`) — NO cameras, fixtures only:**
- Each stage: deterministic output on matrix fixture images (hash or
  statistical properties: flattening normalizes the illumination
  gradient — variance of block means drops; grayscale shape/dtype).
- Coordinate integrity: downscale stage's rescale-back maps a known
  point to within ε of its original position (geometry discipline
  starts here).
- Pipeline composition: a configured sequence applied to a fixture
  produces the expected image properties for that sequence (declared per
  stage above), and is deterministic — same fixture in, byte-identical
  image out on repeat runs. This is a self-contained correctness test,
  not a comparison against v1 (v1 is gone by now).
- Effectiveness matrix (informational): each pipeline variant ×
  each exposure-sweep fixture → detection count table (feeds stage 5
  tuning; today's `testdev/detect_pipeline.py` becomes a real test).

**Exit criteria:** stage-level tests green on all fixtures.
Pure-function property: same input → same output, no globals, no I/O.

---

## Stage 5 — Tag detection: ArUco and AprilTag interfaces

**What it is:** two detector interfaces over the pipeline's output —
`ArucoDetector` (4×4 corner/reference markers) and `AprilTagDetector`
(36h11 et al.) — plus the LK optical-flow tracker that carries
AprilTags between full detections. Detection returns *raw pixel-space
hits*; world conversion is calibration's job (stage 6). This is the
last layer where pixels are a legitimate output.

**Reused:** `core/detector.py` detection cores, aruco_compat shim,
tracker logic from `core/aprilcam.py`.

**Tests (`tests/test_tag_detection.py`) — fixtures only:**
- Recall per fixture: baseline images detect the field-state tag set
  (both cameras); the exposure sweep documents the washout boundary as
  assertions (`exp-40` washed → reduced recall is *expected*; a change
  that shifts the boundary fails the test).
- ArUco/AprilTag separation: each interface reports only its family;
  the `-(N+1)` ArUco id mapping preserved.
- Corner subpixel accuracy: known-position corners on fixtures stable
  within ε across repeated runs.
- Tracker: synthetic frame pairs (fixture + shifted copy) — tracked
  corners follow the shift; total-loss falls back to detect.

**Exit criteria:** recall table generated and asserted for every
fixture; detection runs at documented ms/frame on the dev machine
(baseline number recorded for stage 8 budgets).

---

## Stage 6 — Calibration and world coordinates

**What it is:** open-time ArUco detection → homography → the pixel↔world
mapping, per spec §4.4: multi-frame corner averaging, surveyed-marker
augmentation, optional distortion correction, static-marker recording,
parallax + mobile-tag mount correction, A1-centred world frame. **ArUco
corners are detected here — at open/calibrate time — and then not
again**; the stored geometry (static markers, corner pixels) carries
deskew from then on. Low-rate drift *validation* (stored markers
re-checked opportunistically from frames already being processed, on
the order of seconds) replaces per-frame corner detection; drift sets
`calibration_stale`, it never silently re-homographies.

**Reused:** `calibration/calibration.py` workflow, `geometry.py` (pure
leaf), playfield-def registry. **Removed:** legacy `homocal` +
upper-left-origin table.

**Tests (`tests/test_calibration.py`) — fixtures + saved calibrations:**
- Calibrate from fixture images (a fixture-backed "camera" that replays
  baseline frames): produces homography; surveyed static tags map to
  their world positions within 1.5 cm.
- **Geometry convention suite** (the sacrosanct tests, design.md §6):
  +x east, +y north, A1 at origin, north-up on the deskewed image,
  exactly one y-flip in the mapping, heading 0 = east CCW — asserted
  from fixtures with known east/north tag relationships. These tests
  never get relaxed; they are the "up stays up" guarantee.
- Invariants: tid `-(N+1)`; calibration.json-before-config.json write
  order (crash-injection test); config-owned key overlay; stale
  detection on playfield mismatch.
- Parallax: synthetic cases — tag at h with known camera height →
  corrected position matches hand-computed value; mobile-tag mount
  offset/yaw → robot centre.
- Round-trip: world → deskewed-pixel → world identity within ε.

**Exit criteria:** geometry suite green (permanently); calibration from
fixtures maps the surveyed static markers to their known world
positions within tolerance (1.5 cm) — validated against ground-truth
survey values, not against any v1 calibration output.

---

## Stage 7 — Deskew and the published image products

**What it is:** the deskewed playfield image (spec §4.1): field warped
aspect-preserved into the 800×800 default bounding box, uniform scale,
metadata (frame dims + field cm dims) making world↔pixel exact. Built
directly on stage 6's geometry.

**Tests (`tests/test_deskew.py`) — fixtures only:**
- Output ≤ box, aspect preserved, metadata consistent
  (px_per_cm = frame_w / field_w within rounding).
- A surveyed tag's world position → metadata conversion → lands on the
  tag in the deskewed fixture image (visual truth test, assertable via
  local template/corner check).
- North-up: a tag known to be north of another appears *above* it in
  the deskewed image (the cheap mirror-catcher).
- Per-playfield box override honored.

**Exit criteria:** deskewed fixture images visually verified once by
the stakeholder, then locked by the automated checks.

---

## Stage 8 — Daemon assembly: lifecycle, scheduling, streams

**What it is:** the daemon proper, composing stages 2–7: singleton
lifecycle, gRPC control plane, per-camera loop around the `Camera`
class, stream producers, the **annotation store** (UC-28 — in-memory,
playfield-addressed, layer-keyed, embedded into published deskewed
frames; built here alongside the mobile-tag registry it mirrors), and
the **demand-driven scheduler** — the efficiency heart:

- No image subscribers → no JPEG encode; no deskew subscribers → no
  warp; nobody asking for tags (no tag stream subscriber, no recent
  one-shot query) → **no AprilTag detection at all**; fully idle →
  ~1 fps keepalive reads only.
- Someone subscribes/asks → that product (and only that product) starts
  computing, rate-capped as today (change-driven tag publish, 20 Hz
  cap, heartbeat).
- ArUco corner detection: only at open/calibrate/explicit recalibrate
  (stage 6's contract), plus the low-rate drift check.

**Reused:** `daemon/server.py` lifecycle, `grpc_server.py`, stream
plumbing (bounded queues, silent drop), mDNS. **Removed:** dead
`protocol.py`/`client.py`.

**Tests (`tests/test_daemon.py` + `tests/test_daemon_efficiency.py`):**
- Lifecycle: lock singleton, `--force` takeover, clean shutdown
  (exists today — keep).
- Streams: fake-camera daemon (fixture replay backend) → subscribe,
  frames/tags arrive, framing correct, slow consumer drops without
  stalling others.
- **Efficiency budget (`@camera` + headless with fake camera):**
  - idle daemon (camera open, zero subscribers): detection functions
    called 0 times over 10 s; CPU below a recorded budget.
  - one tag-stream subscriber: detection ≤ configured fps; CPU within
    budget; unsubscribe → detection stops within grace period.
  - one-shot `get_tags` with no stream: exactly one detection burst.
  These are regression-pinned numbers on this Mac, not absolutes.
- Restart/reload: calibration hot-reload; boot_id changes; **calibration
  survives a restart** (it is file-backed under the state dir — UC-01),
  while pushed config does not.
- **Tag history (UC-26):** the ring buffer answers a history query;
  partial history before the buffer fills is returned, not an error;
  a tag absent for part of the window shows an explicit gap rather than
  interpolated positions.
- **Camera hotplug (UC-25.S-5):** a camera attached to a running daemon
  appears without any command; detached is flagged absent, not deleted.
- **Annotation store (UC-28):** put/get round-trip; restart wipes the
  store (S-4); the set embedded in a deskewed frame matches the store
  at that revision; (layer, id) upsert is last-write-wins (S-6);
  `replace_layer` is atomic — no frame embeds a half-updated layer
  (S-2); raw frames embed no annotations.

**Exit criteria:** a machine running the daemon with two cameras open
and no clients sits at near-idle CPU (stakeholder-verified once,
budget-pinned thereafter).

---

## Stage 9 — Configuration service

**What it is:** the whole-document config plane (spec §4.1/§4.5):
`SetPlayfield`/`GetPlayfield` (with derived camera links),
`SetCameraConfig`/`GetCameraConfig`/`ResetCameraConfig`. Issues
`daemon-playfield-config-service.md`,
`daemon-camera-config-service-reset.md`.

**Config model (UC-01, UC-23, UC-24 — changed since this plan was
first written):** pushed documents are **in-memory only and discarded on
restart**. Disk is the source of record; the daemon never writes config.
Directory layout and cascade: [configuration.md](configuration.md).

**Tests:** round-trip push/serve verbatim; derived links correct
(camera↔playfield cardinality); reset re-applies to hardware
(`@camera`); invalid documents rejected whole with actionable errors
(never partially applied — UC-02.S-2, UC-03.S-3); **daemon restart
discards pushed documents and reloads from disk** (UC-01.S-4 — the
defining round trip); documents are self-naming and a nameless document
is rejected (UC-23.S-4); dump output re-loads verbatim (UC-24.S-1).

**Exit criteria:** a playfield def pushed from a client machine is
served back identically plus links; calibration consumes the pushed
def (not a hand-placed file); the UC-01.S-4 cycle — disk **A** → push
**B** → restart → **A** — passes.

---

## Stage 10 — Client library + renderer

**What it is:** discovery, `DaemonControl`, stream consumers (now
including deskewed), world-first models (pixel fields demoted to
diagnostics — issue `world-coordinates-first-tag-contract.md`), the
annotation-authoring RPC wrappers, and the Pillow **renderer** — it
rasterizes a frame's *embedded* tags (boundary + hat) and annotations
to PNG via the frame's world→pixel metadata, plus caller-supplied
local-only extras. *(Reshaped 2026-07-31, spec §9 #8: issue
`replace-paths-service-with-client-drawing-library.md` is superseded —
authoring moved into the daemon; a successor issue should be cut
rather than repurposing it.)*

**Tests:** models from recorded proto messages (no daemon); consumer
framing against a fake socket server; rendering: render a fixture
`ImageFrame` whose embedded set holds a marker at a surveyed tag's
world position → pixel neighborhood check; unchanged revision →
renderer skips re-rasterizing; **no-cv2-import test** — every client
module is imported and `cv2` must be absent from `sys.modules`
afterwards. *(Changed: OpenCV is now a base dependency (UC-21), so
"fresh venv without OpenCV" is no longer possible. The boundary is
unchanged — we now test the discipline directly rather than relying on
packaging to enforce it.)*

**Exit criteria:** a 20-line example script — connect, put a marker
and a path into a layer via the daemon, fetch one deskewed frame,
render its embedded tags + annotations, save PNG — works against the
live daemon.

---

## Stage 11 — MCP server (slim) and CLI

**What it is:** the pure-daemon-client MCP server (issue
`slim-mcp-server-to-pure-daemon-client.md`) — perception + config
tools, no cv2 anywhere — and CLI updates (`calibrate` as RPC
orchestrator, `web` removed — issues `move-calibration-execution-into-daemon.md`,
`remove-web-server.md`, `fix-calibrate-joint-nameerror.md` mooted).

**Tests:** every MCP tool against a fake daemon (existing pattern) —
including the drawing tools (`draw_marker`, `draw_path`, `draw_polygon`,
`draw_circle`, `draw_text`, `remove_annotation`, `clear_layer`,
`list_annotations`, spec §4.7); no-cv2 import sweep (as stage 10 —
`cv2` absent from `sys.modules` after importing the MCP server);
packaged-guide tool returns instructions with **no daemon running**
(UC-27.S-1); end-to-end (`@camera`): agent-style session — list, open,
calibrate via RPC, get_tags in world coords, get tag history (UC-26),
draw a marker and read it back (UC-28.S-3), get deskewed frame,
set/get playfield.

**CLI scope (UC-13 … UC-25, UC-28):** `-d`/`--daemon` targeting with
`-h` reserved for help; `--all` fan-out on read-only commands only
(annotation writes are restricted); `camera probe`; `camera`/
`playfield config` push and `--dump`; `playfield annotations` list and
`--clear`.

**Exit criteria:** full MCP session against a daemon on this Mac, with
no client module having imported cv2; the CLI covers every use case in
[use-cases/cli.md](use-cases/cli.md).

---

## Stage 12 — Viewer + package restructure + cleanup

**What it is:** `aprilcam view` as basic deskewed-stream subscriber
(issue `viewer-subscribes-to-deskewed-stream.md`), then the directory
moves and dead-code deletion **last**, when the code that moves is
final (issue `package-restructure-v2-layout.md`): `server/`→`mcp/`,
`core/` dissolves, `view/` extracted, `ui/` deleted, legacy modules
removed, DESIGN.md files move with their code.

**Tests:** viewer smoke (headless render of one frame from fake
daemon, including one named annotation layer toggled on and off —
UC-28, UC-18); full suite green after restructure; `clasi design
validate` green against the new layout; import-graph check (nothing
imports deleted modules).

**Exit criteria:** the whole test pyramid green; design doc set valid;
stakeholder runs the viewer on the live field.

---

## Order rationale & issue map

Demolition (1) comes first so v2 is built on cleared ground, not next to
v1's dead weight. From there each stage depends only on the ones above
it. Detection (5) before calibration (6) because calibration *uses*
detection; calibration before deskew (7) because deskew is pure geometry
consumption; the daemon (8) is assembly, not invention; services (9–11)
are thin over a tested core; restructure (12) moves only finished code.
Note the one deletion that is *not* in Stage 1: the package restructure
and final dead-code sweep stay in Stage 12, because they move code that
only becomes final at the end — Stage 1 removes what v2 never touches,
Stage 12 tidies what v2 leaves behind.

| Stage | Issues consumed |
|---|---|
| 0 | image-matrix-fixture-test-suite (fixtures part) |
| 1 | remove-web-server (deletion side), plus deletion of v1 test suite and all modules no later stage reuses |
| 2 | stale-registry-record-orphans-camera… |
| 3 | daemon-camera-config-service-reset (class-level), image-matrix HD-USB fix |
| 4–5 | image-matrix-fixture-test-suite (suite part), calibration-json-missing-world-scale… (pipeline block) |
| 6 | move-calibration-execution-into-daemon (math side), world-coordinates-first-tag-contract (geometry tests) |
| 7 | daemon-publish-deskewed-playfield-stream (product side) |
| 8 | daemon-publish-deskewed-playfield-stream (streams), world-coordinates-first-tag-contract (contract) |
| 9 | daemon-playfield-config-service, daemon-camera-config-service-reset (RPC side) |
| 10 | replace-paths-service-with-client-drawing-library *(superseded 2026-07-31 by spec §9 #8 — daemon annotation store + client renderer; cut a successor issue rather than repurposing it)* |
| 11 | slim-mcp-server-to-pure-daemon-client, move-calibration-execution-into-daemon (client side), remove-web-server, fix-calibrate-joint-nameerror |
| 12 | viewer-subscribes-to-deskewed-stream, package-restructure-v2-layout |

CLASI mapping: stages become sprints (one or two stages per sprint);
each stage's tests are that sprint's acceptance evidence; the design
overlay seeds from the affected `DESIGN.md`s per the design gate.

## Stage → use-case coverage

Which use cases each stage is ultimately accountable for. A stage is not
done until its use cases' scenarios pass.

| Stage | Use cases |
|---|---|
| 2 Camera discovery | UC-25 (probe, hotplug, naming) |
| 3 Camera class | UC-03 (config document, hard-error on unsupported) |
| 4–5 Pipeline + detection | UC-09, UC-10 (detection surface) |
| 6 Calibration | UC-04 (all-fiducials rule), UC-05 |
| 7 Deskew | UC-06, UC-19 |
| 8 Daemon assembly | UC-01, UC-07, UC-11, UC-26, UC-12, UC-28 (store + embed) |
| 9 Config service | UC-02, UC-23, UC-24 |
| 10 Client library | *(chapter not yet written — see below)* |
| 11 MCP + CLI | UC-08, UC-13 … UC-22, UC-27 |
| 12 Viewer + restructure | UC-18 |

**Known gap:** [use-cases/](use-cases/) has no chapter for the **client
library**, which Stage 10 builds and which every adapter sits on. The
process doc requires every capability to be reachable from the Python
module, so this chapter should be written before Stage 10 — and probably
before Stage 8, since the daemon's contract is what it wraps.
