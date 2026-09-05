---
source_file: camera-DESIGN.md
source_hash: 19a73b4f3267c3e05bbcaef960d451532024ce360737f5e0691bd43bb21caafb
---
# Diff: camera-DESIGN.md

Comparison of the sprint overlay copy of `camera-DESIGN.md` against its pristine (seed-commit) canonical version.

```diff
--- camera-DESIGN.md (pristine)
+++ camera-DESIGN.md (current)
@@ -19,8 +19,10 @@
 3. **Capture backends** — small, swappable adapters (`cv2.VideoCapture`
    over USB/AVFoundation, a `v4l2-ctl` subprocess reader for a Pi's
    v4l2loopback bridge, a `uvc-util` subprocess adapter for real UVC
-   control) plus the `Camera` object that composes them into one
-   physical camera with a uniform lifecycle.
+   control, and — **sprint 038** — a per-camera capture *worker process*
+   for the backends whose capture session can wedge in a way only a
+   process boundary clears) plus the `Camera` object that composes them
+   into one physical camera with a uniform lifecycle.
 
 The seam exists because OS camera indices are unstable garbage: an index
 can point at a different physical camera after a reboot, and a camera
@@ -99,6 +101,21 @@
     macOS needs because `cv2.VideoCapture.set()` was found (live, on
     this project's own OV9782 camera) to return `False` for every
     exposure/gain/white-balance control.
+  - `worker_backend.py` (**sprint 038**) — `WorkerBackend`, a
+    `CaptureBackend` that owns no capture device itself. It spawns,
+    monitors, and (on `close()`+`open()`) respawns a
+    `capture_worker.py` child process that owns the real
+    `cv2.VideoCapture`/`OpenCVBackend`, and reads frames from a
+    shared-memory ring the child publishes into. Selected in place of
+    `OpenCVBackend` by `backend_factory` (config/env-gated — see §4);
+    every other backend and `ControlBackend` is untouched.
+- **`capture_worker.py`** (**sprint 038**, `python -m
+  aprilcam.camera.capture_worker`) — the standalone child process
+  module `WorkerBackend` spawns one of per opened camera. Owns exactly
+  one real `OpenCVBackend`/`cv2.VideoCapture`, publishes frames into
+  the shared-memory ring, and answers the parent's control channel
+  (`get_property`/`set_property`/`settable_names`/`frame_size`). See
+  §4 "Per-camera capture worker process (sprint 038)" for the protocol.
 
 ## 3. Constraints and Invariants
 
@@ -150,6 +167,21 @@
   sentinel.** This is the hook `probe()`'s "present but unopenable"
   detection depends on. `read()` conversely never raises for an
   ordinary failure — only `open()` does.
+- **`usable` means "delivered a frame," not merely "opened" (sprint
+  038, closing the original issue's gap 1).** `_probe_openable` now
+  performs `open()`, one bounded frame read, then `close()` — a device
+  whose capture session is open but wedged (reads return `None`
+  indefinitely while `isOpened()`-equivalent state still reports true,
+  the exact signature the process-local wedge above produces) reports
+  `usable=False`, where the previous open()-then-close()-only probe
+  reported it healthy. This is an explicit, accepted latency/cost
+  tradeoff — `probe()` runs over every camera and is now measurably
+  slower per camera — chosen because an assertion of health that was
+  never tested is the single most misleading signal the source issue
+  identified. The bound is short (order of one frame interval, not the
+  daemon's stall-watchdog window above) since a healthy camera's first
+  read after open is fast; a wedged camera's read simply never
+  resolves inside it.
 - **Resolution (`frame_width`/`frame_height`) always routes through the
   primary capture backend, never through `control_backend`** — it is a
   capture property (`CAP_PROP_FRAME_WIDTH`/`HEIGHT`), not a UVC control,
@@ -199,6 +231,68 @@
   via gRPC's fork handlers). The current `libcamera.py` module is
   `/sys`-only enumeration with no subprocess, consistent with this — a
   future capture backend must keep libcamera itself out-of-process.
+- **The AVFoundation capture-session wedge is process-local; no
+  in-process fix can clear it (sprint 038, root-caused 2026-09-04).**
+  Confirmed live: while the daemon's held-open `Camera` read `None`
+  indefinitely from a wedged `cv2.VideoCapture(2, CAP_AVFOUNDATION)`
+  that reported `isOpened() == True`, a **fresh Python process**
+  opening the exact same index through the exact same backend read a
+  real frame immediately. `release()` does not fully tear down
+  AVFoundation's capture-session state within a process, so delays,
+  backoff, a freshly-constructed backend object, or a re-resolved index
+  — every purely in-process reopen strategy — are futile against this
+  specific failure. Only a process boundary clears it. This is *why*
+  `WorkerBackend` exists: it is not a general "more robust capture"
+  redesign, it is the minimum change that turns "reopen" into "a new
+  process," which is the only thing observed to work.
+- **`WorkerBackend.close()`/`open()` implement kill-and-respawn
+  entirely inside themselves; `Camera` needs zero code change (sprint
+  038).** `Camera._attempt_self_heal` already only ever calls
+  `backend.close()` then `backend.open()` — the "self-heal is unified
+  across backends" invariant above. For `WorkerBackend`, `close()`
+  terminates the child process (SIGTERM, then SIGKILL after a bounded
+  grace period) and detaches the shared-memory ring; `open()` spawns a
+  brand-new child process (a fresh process is exactly the fix the
+  finding above calls for) and blocks briefly for its first heartbeat.
+  `MAX_REOPENS`/`FAILURE_THRESHOLD` budgeting, `on_reopen` staleness
+  notification, and the exhausted-budget `usable=False`/`present=True`
+  contract all therefore apply to a worker-backed camera completely
+  unchanged — self-heal does not fork into a backend-specific code
+  path.
+- **A worker crash is an ordinary read failure, never an exception
+  escaping `WorkerBackend.read()` (sprint 038).** If the child process
+  has died (crashed, killed externally, or its heartbeat has gone
+  stale — see below) `read()` returns `None`, exactly like a dropped
+  frame from any other backend. This is what makes "a worker crash
+  does not take the daemon down, and does not disturb any other
+  camera's worker" true by construction: the failure surfaces through
+  the same `Camera._consecutive_failures` counter every backend
+  already reports through, with no new exception type for
+  `Camera`/`DaemonService` to catch.
+- **A stalled worker is detected even with no reader (sprint 038).**
+  `Camera.read()`/self-heal is demand-driven — it only notices a
+  failure when something calls `read()`. A camera nobody is polling
+  (no active stream, no recent RPC) could stay silently wedged
+  indefinitely under that scheme alone. Each worker publishes a
+  monotonically increasing sequence number/heartbeat timestamp into
+  the shared-memory ring header on every successful capture; the
+  daemon's existing per-camera watchdog thread (`daemon/DESIGN.md`'s
+  control-drift watchdog, extended this sprint) polls every held-open
+  worker-backed camera's heartbeat on a fixed interval and drives the
+  same close()/open() respawn if it has not advanced within a bounded
+  staleness window — independent of whether an RPC or a stream happens
+  to be reading that camera at the time.
+- **Exhausted workers get a periodic retry, not permanent death
+  (sprint 038).** Once `MAX_REOPENS` is spent, `Camera.usable` stays
+  `False` exactly as it does today for every backend — but the
+  watchdog above periodically re-attempts a respawn for an exhausted
+  worker-backed camera on a slow interval (independent of the
+  synchronous `_attempt_self_heal` budget, which stays exhausted-once
+  by design for the direct-caller-driven path), because a camera that
+  was unplugged and replugged, or whose worker process was killed for
+  an unrelated reason, should not require a full daemon restart to try
+  again. This is additive to, not a change of, the existing exhausted
+  contract.
 
 ## 4. Design
 
@@ -261,6 +355,71 @@
 fixing the resolver alone does not retroactively merge records minted
 before the fix.
 
+### Per-camera capture worker process (sprint 038)
+
+**New cross-module dependency**: `camera/backends/worker_backend.py` →
+a spawned `python -m aprilcam.camera.capture_worker` child process (a
+new process boundary, not a new Python import edge — `WorkerBackend`
+imports nothing from `capture_worker.py`, it only knows how to spawn
+and speak the wire protocol to it). No existing edge changes direction
+or shape; `daemon/service.py` still only ever sees a `CaptureBackend`.
+
+```mermaid
+graph LR
+    CAM["camera/camera.py<br/>Camera"] -->|open/read/close<br/>get_property/set_property| WB["camera/backends/worker_backend.py<br/>WorkerBackend"]
+    WB -->|spawn/terminate<br/>(multiprocessing.Process)| PROC["python -m aprilcam.camera.capture_worker<br/>(child process)"]
+    WB -->|attach, read latest frame| SHM["multiprocessing.shared_memory<br/>frame ring (few slots, ~6MB/1080p frame)"]
+    WB <-->|get/set property, settable_names,<br/>frame_size, heartbeat| CTRL["control channel<br/>(pipe/socket)"]
+    PROC -->|publish frame + seq/timestamp| SHM
+    PROC <-->|answer control requests| CTRL
+    PROC -->|owns| CV2["cv2.VideoCapture<br/>(AVFoundation/USB/V4L2)"]
+    WATCHDOG["daemon/service.py<br/>stall watchdog (extended)"] -->|poll heartbeat, trigger<br/>close()+open() on stall| CAM
+```
+
+**Shared-memory frame ring.** A short ring (a handful of slots) of
+raw frame buffers under one `multiprocessing.shared_memory.SharedMemory`
+segment, sized for the camera's resolution (~6 MB per 1080p BGR frame —
+this is why the ring is shared memory and not a pipe: a pipe would
+either block the worker on a slow parent or force a full-frame copy
+through kernel buffers every tick at 15-30 Hz). A small header (fixed
+offset, fixed-size fields) carries the current write slot index, a
+monotonically increasing sequence number, a capture timestamp, and the
+frame's shape/dtype, so the parent can always locate and validate the
+newest complete frame without a separate metadata channel. The worker
+writes a new frame into the next slot and only then advances the
+header's sequence number — `WorkerBackend.read()` reads the header,
+copies out (once — no double-copy) the slot the sequence number names,
+and returns `None` if the sequence hasn't advanced since the last read
+(no fresh frame yet) or if the child process is no longer alive. This
+mirrors the same "N subscribers, one computation" shared-buffer
+discipline `daemon/broadcast.py` already uses for stream fan-out, one
+layer lower — here there is exactly one reader (the parent) per ring,
+so no broadcaster is needed, only the sequence-number "is this new"
+check.
+
+**Control channel.** A separate pipe (or local socket) carries small,
+synchronous request/response messages: `get_property`/`set_property`/
+`settable_names`/`frame_size`, plus the settle-read behavior
+`Camera.apply_config` already drives from one layer up (`camera/DESIGN.md`
+§3's "Settle-after-open" constraint) — the worker, not the parent,
+issues the actual settle reads against its own `cv2.VideoCapture`,
+since that capture object only exists inside the child process; the
+parent's `WorkerBackend.apply_config`-facing methods are a thin
+request/response proxy over this channel, not a reimplementation of
+settle logic. The heartbeat is carried in the shared-memory header
+(sequence number + timestamp), not the control channel, so a control
+request never has to compete with the capture loop's own cadence.
+
+**Backend-internal, not `Camera`-visible.** Everything above is fully
+contained inside `WorkerBackend` and `capture_worker.py`. `Camera`,
+`DaemonService._camera_for`, and every other consumer of a
+`CaptureBackend` see the identical five-method surface
+(`is_open`/`open`/`read`/`close`/`frame_size`) plus the optional
+`ControlBackend` surface, unchanged. This is the entire point of the
+`CaptureBackend` seam (§1's "why the seam exists"): the wedge fix is a
+backend-internal concern, not an architectural change to how `Camera`
+uses a backend.
+
 ### Backend selection — the ticket 006/031-006 wiring
 
 `backends.resolve_capture_backend(hardware_key, index, *,
@@ -281,6 +440,21 @@
 throwing on an enumeration miss, and no worse than the unconditional
 `OpenCVBackend(index)` every camera got before this ticket.
 
+**`WorkerBackend` selection (sprint 038).** `resolve_capture_backend`
+itself is unchanged — the libcamera-vs-everything-else branch stays
+exactly as ticket 031-006 built it. What changes is what
+`backend_factory(index)` constructs for the non-libcamera, default
+case: a config/env-gated choice between `_default_backend_factory`
+(`OpenCVBackend(index)`, unchanged default) and a new factory
+constructing `WorkerBackend(index)`. This is a process-wide daemon
+setting (not per-camera), gated so a partially-merged sprint cannot
+change runtime camera behavior until explicitly enabled (sprint.md
+Migration Concerns) — every existing test and every non-macOS platform
+keeps getting `OpenCVBackend` by default. `_probe_openable` and
+`_camera_for` both go through this same `backend_factory`, so a probe
+and a real open agree on which backend a camera gets, exactly as they
+do today for the libcamera branch.
+
 ### `Camera`: composition over backends, self-heal, config push
 
 `Camera` holds exactly one primary `CaptureBackend` (pixels) and an
@@ -306,6 +480,28 @@
 calibration stale, since a reopen correlates with a real-world event
 (someone unplugged/moved the camera) that elapsed time alone cannot
 detect.
+
+**`Camera.check_stall()` — the proactive half of self-heal (sprint
+038).** A new method, called by `daemon/service.py`'s watchdog thread
+on every held-open camera each pass (`daemon/DESIGN.md`'s
+"Stall-detection watchdog" — this is the `Camera`-level half of that
+mechanism). Duck-types an optional `heartbeat_age() -> Optional[float]`
+on the primary backend — seconds since that backend's last known-fresh
+frame, or `None` if the backend has no such concept — the same
+duck-typing discipline `_describe_set_failure`/`get_property` already
+use for a backend-specific capability that not every backend
+implements. `OpenCVBackend`/`V4l2LoopbackBackend` expose no
+`heartbeat_age`, so `check_stall()` is an unconditional no-op for them
+(`getattr(..., None)` returns `None`, never a stale age) —
+`WorkerBackend` is the only backend that implements it, reading the
+shared-memory ring header's sequence number/timestamp (§4). When the
+age exceeds a threshold, `check_stall()` calls the exact same
+`_attempt_self_heal()` this class already uses for a demand-driven
+read-failure streak — no second respawn code path — and returns
+whether it did so, purely for the watchdog's own logging. This keeps
+`Camera` the single place that decides "close and reopen the backend,"
+whether triggered reactively (a caller's `read()` failing enough
+times) or proactively (this method, called with nobody reading).
 
 ### Concurrency
 
@@ -363,6 +559,38 @@
   settle-quirk root cause — investigated and ruled out live: the camera
   that actually failed (`hd-usb-camera`) does not even expose that
   control.
+- **Every capture backend running out-of-process, not just the
+  wedge-prone one (sprint 038)** — rejected as unjustified scope: the
+  wedge is specific to `cv2.VideoCapture`/AVFoundation's own
+  process-local session state (root cause above); `V4l2LoopbackBackend`
+  and `UvcUtilBackend` show no equivalent symptom and gain nothing from
+  the added process-boundary cost. `WorkerBackend` wraps only the
+  backend that actually wedges; the others are untouched.
+- **A pipe instead of shared memory for frame transport (sprint
+  038)** — rejected: a ~6 MB 1080p frame over a pipe at 15-30 Hz either
+  blocks the worker on a slow parent read or forces a full-frame copy
+  through kernel pipe buffers every tick; a shared-memory ring lets the
+  parent read the latest frame with at most one copy, independent of
+  how promptly it polls.
+- **Reworking `Camera._attempt_self_heal` to special-case a worker
+  backend (sprint 038)** — rejected: self-heal already only calls
+  `backend.close()`/`backend.open()` uniformly across every backend
+  (the existing "self-heal is unified across backends" invariant,
+  above). Special-casing would duplicate exactly the failure-counting
+  and budget logic `Camera` already owns, and would break that
+  invariant for no benefit — `WorkerBackend` implements
+  kill-and-respawn entirely inside its own `close()`/`open()` instead.
+- **A first-frame timeout implemented as a new socket-level timeout in
+  `client/streams.py` (sprint 038)** — considered for the `camera view`
+  hang (a separate, client-side gap from the worker-process fix above)
+  and rejected: `view/viewer.py`'s `StreamLoop` already polls a
+  single-slot "latest frame" cell on a fixed cadence on the *caller's*
+  thread, so "no frame received within N seconds of `run()` starting" is
+  already directly observable there with no wire-level change. A socket
+  timeout would also not distinguish "no frame published yet" from "the
+  reader thread's own `EOFError`/`OSError` handling is already in
+  progress" — two states `StreamLoop` can already tell apart today. See
+  `view-DESIGN.md`'s Design Rationale for the chosen alternative.
 
 ## 5. Interfaces
 
@@ -390,7 +618,9 @@
   unsupported key, rolls back atomically), `.effective_config() ->
   CameraConfig` (the self-describing blank template), `.reset()`
   (re-applies the on-disk document or an explicit no-op push), plus
-  `.stored_config()` beyond the declared interface.
+  `.stored_config()` and — **sprint 038** — `.check_stall() -> bool`
+  (proactive respawn-if-stalled, a no-op returning `False` for any
+  backend with no `heartbeat_age`) beyond the declared interface.
 - **`identity.resolve_identity(index, ...)` / `resolve_all(...)`** —
   never raises; `hardware_key` always populated.
 - **`backends.resolve_capture_backend(hardware_key, index, *,
@@ -401,6 +631,19 @@
   **`backends.uvc_util.UvcUtilBackend`**, and
   **`backends.libcamera.{LibcameraDevice, list_loopback_cameras,
   loopback_label_prefix}`**.
+- **`backends.worker_backend.WorkerBackend(index)`** (**sprint 038**) —
+  a `CaptureBackend`; `.open()` spawns a fresh `capture_worker` child
+  process, `.close()` terminates it (SIGTERM then SIGKILL after a
+  bounded grace period), `.read()` returns the latest complete frame
+  from the shared-memory ring or `None` if none is fresh or the child
+  is not alive, `.frame_size()`/`.get_property()`/`.set_property()`/
+  `.settable_names()` proxy to the child over the control channel.
+  Selected by `backend_factory` in place of `OpenCVBackend`, config/
+  env-gated (see §4).
+- **`capture_worker.main()`** (**sprint 038**, `python -m
+  aprilcam.camera.capture_worker`) — the child process entry point.
+  Not part of this package's public Python API (nothing imports it);
+  invoked only as a subprocess by `WorkerBackend.open()`.
 - **`interfaces.CameraIdentity`** and the `CameraRegistry`/
   `CameraDiscovery`/`Camera` base classes every concrete class above
   derives from.
@@ -430,6 +673,24 @@
   the one case that raises loudly at `UvcUtilBackend` construction
   rather than degrading, per the "no silent fallback to a control path
   already found not to work" rule).
+
+## Migration Concerns (sprint 038)
+
+None affecting data or the wire: no on-disk schema change (the ring
+and control channel are runtime-only, torn down with the process, and
+`registry.json`/`calibration.json`/`config.json` are all untouched),
+no RPC/proto change (`daemon/service.py` still sees a plain
+`CaptureBackend`), no client-visible behavior change for any camera
+that keeps `OpenCVBackend`. `WorkerBackend` is selected by a config/env
+gate, defaulting to the unchanged `OpenCVBackend` path, so tickets can
+land incrementally (the worker module and its tests first, the
+backend-selection wiring last) without any intermediate state changing
+what a running daemon actually does until the gate is deliberately
+flipped. The one operational-deployment concern: a worker-backed
+camera now has a second OS process per open camera, which changes
+process-table/resource-monitoring expectations (documented in Open
+Questions below) — not a migration in the data/schema sense, but worth
+naming for whoever operates the daemon.
 
 ## 6. Open Questions / Known Limitations
 
@@ -484,3 +745,25 @@
   interface docstring itself still frames it as unresolved, so a reader
   of `interfaces.py` alone would not know the concrete class has already
   decided this.
+- **Whether the wedge correlates with UVC control writes during an
+  active stream is still not established (sprint 038, carried forward
+  from the source issue).** This sprint fixes the wedge structurally
+  via process isolation rather than chasing that correlation — a
+  process boundary clears the symptom regardless of its trigger, so
+  the open question no longer blocks the fix, but it remains genuinely
+  open for anyone curious whether an ordering bug in the daemon (not
+  AVFoundation itself) is also involved.
+- **A worker-backed camera doubles the daemon's OS-process footprint
+  (sprint 038).** N held-open cameras now means N+1 processes (daemon
+  plus one worker each) instead of one. Nothing in this sprint adds
+  process-table monitoring beyond what `client/lifecycle.py`'s existing
+  orphan-process scan already does (and that scan does not know a
+  capture worker's command-line pattern yet) — a future sprint may want
+  to teach it to recognize and report orphaned capture workers
+  specifically, rather than only orphaned `aprilcam.daemon` processes.
+- **Worker respawn latency is not bounded by an SLA in this sprint.**
+  `WorkerBackend.open()` waits for the child's first heartbeat before
+  returning, but no explicit timeout on that wait is specified here —
+  left as an implementation-time decision (a ticket's job), not an
+  architectural one, since it only affects how quickly a respawn
+  completes, not whether the design is sound.
```
