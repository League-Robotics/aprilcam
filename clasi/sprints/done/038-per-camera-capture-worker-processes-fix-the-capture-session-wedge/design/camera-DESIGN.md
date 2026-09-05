# camera/

**Owner:** Eric Busboom · **Last reviewed:** 2026-08-21 · **Status:** stable

---

## 1. Purpose

`camera/` answers one question no other subsystem may answer: *which
physical camera is this, stably, and how do I talk to its hardware?* It
owns three things end to end:

1. **Identity resolution** — a fallback chain that derives a name for a
   camera that survives reboots, USB re-plugs, and OS re-enumeration, so
   configuration keyed by that name stays attached to the right hardware.
2. **The persistent registry** — identity -> stable slug + enumeration
   number, the addressing scheme every other subsystem and every human
   operator uses to say "this camera."
3. **Capture backends** — small, swappable adapters (`cv2.VideoCapture`
   over USB/AVFoundation, a `v4l2-ctl` subprocess reader for a Pi's
   v4l2loopback bridge, a `uvc-util` subprocess adapter for real UVC
   control, and — **sprint 038** — a per-camera capture *worker process*
   for the backends whose capture session can wedge in a way only a
   process boundary clears) plus the `Camera` object that composes them
   into one physical camera with a uniform lifecycle.

The seam exists because OS camera indices are unstable garbage: an index
can point at a different physical camera after a reboot, and a camera
can shift index just from being re-plugged. Everything above this
package — `daemon/`'s per-camera pipeline, calibration storage, CLI
display — keys on the identities and slugs minted here rather than on
raw indices.

This is distinct from `daemon/`'s ownership of *using* cameras.
`camera/` answers "which physical camera is this, and give me a frame
from it"; `daemon/` answers "what do I do with the frames it gives me"
(detection, tracking, calibration, streaming, config-service state like
`playfield`/`calibrated`, which `Camera.state` deliberately does not
know about — see §3). Per design.md's architectural principle 1,
`camera/` is **daemon-only** at runtime: nothing outside the daemon
opens a camera, with one sanctioned exception — `testdev/capture_matrix.py`,
which opens cameras directly through this package's own code to
re-capture the test-fixture image matrix, and refuses to run when the
daemon holds the device (design.md §8).

## 2. Orientation

- **`interfaces.py`** — the contract. `CameraIdentity` is a frozen
  `@dataclass(frozen=True, kw_only=True)` (value-shaped); `CameraRegistry`,
  `CameraDiscovery`, and `Camera` are plain base classes with
  `NotImplementedError` bodies (behavioral), per the project's no-`Protocol`
  rule. `camera.identity` imports this exact `CameraIdentity` rather than
  declaring a second one — the module docstring records that two
  differently-shaped classes of the same name used to coexist and were
  merged.
- **`identity.py`** — `resolve_identity()`/`resolve_all()`: the
  fallback-chain algorithm that fills in a `CameraIdentity`. Combines
  `cv2-enumerate-cameras`, macOS `system_profiler SPCameraDataType`, and
  a libcamera enumeration hook. Never imports `cv2` at module top level
  (only lazily inside `_enumerate_cv2`) and never raises — worst case is
  a name+resolution slug.
- **`registry.py`** — `CameraRegistry`: the persistent identity -> slug +
  enumeration-number mapping, stored atomically at
  `state_dir/cameras/registry.json`. Owns first-sight slug assignment
  (with disambiguation), reconnect refresh (never rename), and a
  self-healing pass that merges registry records that a now-fixed
  resolver bug once split in two.
- **`discovery.py`** — `CameraDiscovery`: `probe()` (active,
  non-disturbing re-enumeration, producing a `ProbeReport`) and
  `watch()` (passive polling loop, since no portable OS hotplug event
  source exists). Delegates backend construction to
  `backends.resolve_capture_backend` when it briefly opens a camera to
  test usability.
- **`camera.py`** — `Camera`: the runtime object the daemon holds
  one-of-per-camera. Composes a primary `CaptureBackend` (pixels) with
  an optional `ControlBackend` (settings), and owns self-heal reopen,
  whole-document `apply_config` with rollback, the
  `effective_config`/`reset`/`stored_config` trio for the config-service
  boundary, and — an out-of-process change predating this doc's last
  review, documented here for the first time — applying a per-camera
  flat-field gain map (`FLATFIELD_FILENAME`, `_load_flatfield`/
  `_apply_flatfield`; see §4 "Flat-field gain-map application" below).
- **`backends/`** — one small adapter per capture technology, plus the
  shared `resolve_capture_backend()` branch point:
  - `__init__.py` — `CaptureBackend` (open/read/close/frame_size) and
    `ControlBackend` (get_property/set_property/settable_names/
    describe_set_failure) base classes, and `resolve_capture_backend()`.
  - `opencv_backend.py` — `OpenCVBackend`, wrapping `cv2.VideoCapture`
    for USB/AVFoundation/V4L2 hardware; also the generic (best-effort,
    not verified-per-model) `property_map()` OpenCV settings surface.
  - `v4l2_loopback.py` — `V4l2LoopbackBackend`, a `v4l2-ctl
    --stream-to=-` subprocess reader for a Raspberry Pi's v4l2loopback
    bridge device, because pip OpenCV's V4L2 backend cannot read that
    loopback reliably.
  - `libcamera.py` — **enumeration only**: `list_loopback_cameras()`
    reads `/sys/class/video4linux/video*/name` for labelled
    v4l2loopback devices, no subprocess. No libcamera *capture* backend
    exists yet.
  - `uvc_util.py` — `UvcUtilBackend`, a `ControlBackend`-only adapter
    shelling out to the `uvc-util` CLI — the real per-control mechanism
    macOS needs because `cv2.VideoCapture.set()` was found (live, on
    this project's own OV9782 camera) to return `False` for every
    exposure/gain/white-balance control.
  - `worker_backend.py` (**sprint 038**) — `WorkerBackend`, a
    `CaptureBackend` that owns no capture device itself. It spawns,
    monitors, and (on `close()`+`open()`) respawns a
    `capture_worker.py` child process that owns the real
    `cv2.VideoCapture`/`OpenCVBackend`, and reads frames from a
    shared-memory ring the child publishes into. Selected in place of
    `OpenCVBackend` by `backend_factory` (config/env-gated — see §4);
    every other backend and `ControlBackend` is untouched.
- **`capture_worker.py`** (**sprint 038**, `python -m
  aprilcam.camera.capture_worker`) — the standalone child process
  module `WorkerBackend` spawns one of per opened camera. Owns exactly
  one real `OpenCVBackend`/`cv2.VideoCapture`, publishes frames into
  the shared-memory ring, and answers the parent's control channel
  (`get_property`/`set_property`/`settable_names`/`frame_size`). See
  §4 "Per-camera capture worker process (sprint 038)" for the protocol.

## 3. Constraints and Invariants

- **Daemon-only, with one sanctioned exception.** Nothing outside the
  daemon opens a camera except `testdev/capture_matrix.py`, which uses
  this package's own code and refuses to run while the daemon holds the
  device.
- **Identity fallback-chain order is contractual.** libcamera id ->
  `avf:<uniqueID>` / `serial:<usb-serial>` -> `vidpid:VVVV:PPPP@<loc>` ->
  `loc:<loc>` -> `name:<slug>`. Reordering or reformatting these prefixes
  silently mints new `hardware_key`s for every existing camera,
  orphaning their registry records. `is_fallback=True` marks the last
  two tiers (port-move / identical-model sensitive) — this is an
  accepted, documented limitation: a serial-less camera moved to a
  different USB port resolves as a new camera.
- **Two keys, two jobs — never conflate them.** `hardware_key` answers
  "is this the same physical camera I saw before" (the registry's
  primary key); `slug` answers "what should a human call this" (always
  device-name-derived, regardless of which chain tier resolved it).
- **A slug always contains at least one non-digit character**
  (`device_name_slug`'s guard, prefixing an all-digit result with
  `camera-`). This is load-bearing: `daemon/service.py`'s `_camera_for`
  parses an all-digits token unambiguously as a persistent registry
  number and anything else as a slug, which only works because a slug
  can never itself be all-digits.
- **Reconnect never renames.** A camera's persisted `slug` never changes
  on reconnect even if its freshly-resolved identity proposes a
  different one — only `device_name`/`last_seen` refresh in place.
  Silently renaming would orphan whatever config file is installed
  under the old slug.
- **Enumeration numbers are assigned once and never reshuffled or
  reissued**, even across a corrupt/missing counter (`_next_number` is
  clamped above every stored number on load) or a `forget()`. Gaps are
  structural, not an error.
- **Atomic writes for `registry.json`**: tmp + fsync + `os.replace`. A
  crash between those steps leaves the previous file untouched.
- **`camera/identity.py` must never import `cv2` at module top level** —
  only lazily inside `_enumerate_cv2` — so cv2-free clients can still
  import `identity`/`registry` without the `[daemon]` extra.
- **Non-disturbance (UC-25.S-7).** `CameraDiscovery.probe()` never opens
  a backend for a camera the daemon already holds open — it takes an
  `active_slugs` callback and reports those cameras present/usable from
  what is already known, listing them in `ProbeReport.skipped` instead.
- **No OS hotplug event source exists.** `watch()` polls the same
  non-opening identity enumeration `probe()` uses and diffs hardware
  keys — it does not (and cannot) subscribe to a device-arrived/departed
  callback.
- **A `CaptureBackend.open()` raises on failure; it never returns a
  sentinel.** This is the hook `probe()`'s "present but unopenable"
  detection depends on. `read()` conversely never raises for an
  ordinary failure — only `open()` does.
- **`usable` means "delivered a frame," not merely "opened" (sprint
  038, closing the original issue's gap 1).** `_probe_openable` now
  performs `open()`, one bounded frame read, then `close()` — a device
  whose capture session is open but wedged (reads return `None`
  indefinitely while `isOpened()`-equivalent state still reports true,
  the exact signature the process-local wedge above produces) reports
  `usable=False`, where the previous open()-then-close()-only probe
  reported it healthy. This is an explicit, accepted latency/cost
  tradeoff — `probe()` runs over every camera and is now measurably
  slower per camera — chosen because an assertion of health that was
  never tested is the single most misleading signal the source issue
  identified. The bound is short (order of one frame interval, not the
  daemon's stall-watchdog window above) since a healthy camera's first
  read after open is fast; a wedged camera's read simply never
  resolves inside it.
- **Resolution (`frame_width`/`frame_height`) always routes through the
  primary capture backend, never through `control_backend`** — it is a
  capture property (`CAP_PROP_FRAME_WIDTH`/`HEIGHT`), not a UVC control,
  regardless of which `ControlBackend` is wired for everything else.
- **Settle-after-open before the first real UVC push.** Pushing UVC
  controls immediately after `backend.open()` races AVFoundation's
  asynchronous capture-session init on macOS (observed failing 6/10
  cold trials with no drain, 0/22 with a 5-frame drain). `Camera` drains
  5 frames the first time `apply_config` has something real to write
  after a fresh open. Skipping this reintroduces an intermittent,
  hard-to-reproduce failure that was already root-caused once.
- **UVC control ordering matters.** Auto-exposure must be pushed manual
  before an exposure value sticks; auto-white-balance off before a WB
  temperature. `apply_config` orders `_GATE_KEYS` first, unconditionally.
- **`apply_config` is whole-push atomic.** Every key is validated before
  any hardware write; a mid-push failure rolls back every key already
  applied this call, through that key's own backend. It must never
  report success for a setting that did not take effect — `uvc-util`'s
  `set_property` reads back after writing and self-repairs (writes the
  pre-call value back) rather than merely reporting a mismatch, because
  `uvc-util` can silently clamp an out-of-range value while still
  exiting 0.
- **Self-heal budget: 5 consecutive read failures trigger a reopen,
  capped at 10 reopens**, budget restored on any healthy read. Once
  exhausted, the camera stops attempting further reopens and
  `state.usable` goes and stays `False` while `state.present` stays
  `True` — deliberately the "plugged in and broken" case, not
  "unplugged," since a stopped self-heal loop is not evidence the
  hardware is gone.
- **`Camera._read_lock` guards only `read()`** (including any self-heal
  reopen it triggers), not `open()`/`close()`/`apply_config()`. This
  exists so a background stream producer and one-shot RPC reads never
  interleave calls into the same backend handle; widening it would be
  unnecessary serialization since nothing else calls those concurrently
  with a background reader.
- **`OpenCVBackend` cannot read a v4l2loopback device reliably** — every
  `read()` reports a stale/timed-out frame — which is why
  `V4l2LoopbackBackend` exists at all. Do not "fix" its
  `COLOR_YUV2RGB_YUYV` conversion constant: the bridge's YUY2 stream
  comes out red/blue-swapped under the `_BGR_` variant, so the
  seemingly-wrong `_RGB_` constant is the one that yields true BGR.
- **`uvc-util` invocation is always an argument list, `shell=False`**,
  never a shell string — a config value reaching a subprocess command
  line must not become an injection surface, even on a trusted network.
- **Never run GStreamer+libcamera inside the daemon process** (carried
  forward from v1's `archive/src/aprilcam/camera/DESIGN.md`: it segfaults
  via gRPC's fork handlers). The current `libcamera.py` module is
  `/sys`-only enumeration with no subprocess, consistent with this — a
  future capture backend must keep libcamera itself out-of-process.
- **The AVFoundation capture-session wedge is process-local; no
  in-process fix can clear it (sprint 038, root-caused 2026-09-04).**
  Confirmed live: while the daemon's held-open `Camera` read `None`
  indefinitely from a wedged `cv2.VideoCapture(2, CAP_AVFOUNDATION)`
  that reported `isOpened() == True`, a **fresh Python process**
  opening the exact same index through the exact same backend read a
  real frame immediately. `release()` does not fully tear down
  AVFoundation's capture-session state within a process, so delays,
  backoff, a freshly-constructed backend object, or a re-resolved index
  — every purely in-process reopen strategy — are futile against this
  specific failure. Only a process boundary clears it. This is *why*
  `WorkerBackend` exists: it is not a general "more robust capture"
  redesign, it is the minimum change that turns "reopen" into "a new
  process," which is the only thing observed to work.
- **`WorkerBackend.close()`/`open()` implement kill-and-respawn
  entirely inside themselves; `Camera` needs zero code change (sprint
  038).** `Camera._attempt_self_heal` already only ever calls
  `backend.close()` then `backend.open()` — the "self-heal is unified
  across backends" invariant above. For `WorkerBackend`, `close()`
  terminates the child process (SIGTERM, then SIGKILL after a bounded
  grace period) and detaches the shared-memory ring; `open()` spawns a
  brand-new child process (a fresh process is exactly the fix the
  finding above calls for) and blocks briefly for its first heartbeat.
  `MAX_REOPENS`/`FAILURE_THRESHOLD` budgeting, `on_reopen` staleness
  notification, and the exhausted-budget `usable=False`/`present=True`
  contract all therefore apply to a worker-backed camera completely
  unchanged — self-heal does not fork into a backend-specific code
  path.
- **A worker crash is an ordinary read failure, never an exception
  escaping `WorkerBackend.read()` (sprint 038).** If the child process
  has died (crashed, killed externally, or its heartbeat has gone
  stale — see below) `read()` returns `None`, exactly like a dropped
  frame from any other backend. This is what makes "a worker crash
  does not take the daemon down, and does not disturb any other
  camera's worker" true by construction: the failure surfaces through
  the same `Camera._consecutive_failures` counter every backend
  already reports through, with no new exception type for
  `Camera`/`DaemonService` to catch.
- **A stalled worker is detected even with no reader (sprint 038).**
  `Camera.read()`/self-heal is demand-driven — it only notices a
  failure when something calls `read()`. A camera nobody is polling
  (no active stream, no recent RPC) could stay silently wedged
  indefinitely under that scheme alone. Each worker publishes a
  monotonically increasing sequence number/heartbeat timestamp into
  the shared-memory ring header on every successful capture; the
  daemon's existing per-camera watchdog thread (`daemon/DESIGN.md`'s
  control-drift watchdog, extended this sprint) polls every held-open
  worker-backed camera's heartbeat on a fixed interval and drives the
  same close()/open() respawn if it has not advanced within a bounded
  staleness window — independent of whether an RPC or a stream happens
  to be reading that camera at the time.
- **Exhausted workers get a periodic retry, not permanent death
  (sprint 038).** Once `MAX_REOPENS` is spent, `Camera.usable` stays
  `False` exactly as it does today for every backend — but the
  watchdog above periodically re-attempts a respawn for an exhausted
  worker-backed camera on a slow interval (independent of the
  synchronous `_attempt_self_heal` budget, which stays exhausted-once
  by design for the direct-caller-driven path), because a camera that
  was unplugged and replugged, or whose worker process was killed for
  an unrelated reason, should not require a full daemon restart to try
  again. This is additive to, not a change of, the existing exhausted
  contract.

## 4. Design

### Data structures and ownership

- **`CameraIdentity`** (`interfaces.py`, frozen dataclass) — the
  resolver's output: `slug`, `device_name`, `hardware_key`, `source`,
  `is_fallback`, plus raw per-source diagnostics (`vid`, `pid`,
  `serial`, `location`, `avfoundation_unique_id`).
- **`CameraRecord`** (`registry.py`, frozen dataclass) — the persisted
  registry entry: `hardware_key`, `slug`, `number`, `device_name`,
  `last_seen`. Keyed by `hardware_key` in `CameraRegistry._records`.
- **`CameraState`** (`types.py`) — the runtime status `Camera.state`
  reports: `present`/`usable`/`playfield`/`calibrated`/
  `calibration_stale`/`frame_dark`/`number`. `Camera` itself always
  reports `playfield=None`, `calibrated=False`, `calibration_stale=False`
  — it has no notion of the config-service link or calibration store,
  which stay daemon-level concerns; `DaemonService.list_cameras`
  overlays the real values.
- **`CameraConfig`** (`config/interfaces.py`) — the whole-document
  hardware-settings contract `apply_config`/`effective_config` speak:
  `name`, `playfield`, `settings: dict[str, Any]` (every settable key,
  blank when unset), `camera_position`.

### Identity resolution algorithm

`resolve_identity(index, ...)` tries, in order: (1) a libcamera hit at
that index (`hardware_key=f"libcamera:{camera_id}"`, never a fallback);
(2) AVFoundation `uniqueID` or a USB serial number backfilled from
`system_profiler` by device-name match; (2b) on macOS, `location` alone
trusted directly as the AVFoundation uniqueID (`cv2-enumerate-cameras`'s
own `path` *is* that value on macOS — a root-cause determinism fix,
ticket 021-007 — removing the dependency on `system_profiler`'s
name-match succeeding); (3) `vid:pid@location`; (4) `location` alone,
marked fallback; (5) `name+resolution`, last resort, marked fallback.
`resolve_all()` gathers each enumeration source table once (avoiding a
per-camera `system_profiler` shell-out) and resolves every currently
enumerated index. Every source function degrades to an empty result on
any failure — the module never raises for an unsupported platform or a
missing tool.

### Registry: slug assignment, disambiguation, and self-healing

`CameraRegistry.resolve(identity)` keys on `hardware_key`. A known key
refreshes `device_name`/`last_seen` and returns the existing slug
unchanged. A new key gets the next monotonic `number` and the
identity's proposed slug — disambiguated with a `-2`, `-3`, ... suffix
only if a *different* `hardware_key` already owns that slug (two
identical camera models on different ports), never merged into that
camera's entry.

Because the pre-021-007 resolver could mint two different `hardware_key`
formats (`avf:<id>` vs. `vidpid:<vid>:<pid>@<id>`) for the *same*
physical camera across different runs, `_load()` runs
`_reconcile_location_duplicates()` on every construction: it groups
existing records by the location fragment embedded in their
`hardware_key` and merges any group of 2+, keeping the lowest-numbered
(earliest-assigned) record and dropping the rest. This self-heals an
already-dirty `registry.json` without a separate migration script —
fixing the resolver alone does not retroactively merge records minted
before the fix.

### Per-camera capture worker process (sprint 038)

**New cross-module dependency**: `camera/backends/worker_backend.py` →
a spawned `python -m aprilcam.camera.capture_worker` child process (a
new process boundary, not a new Python import edge — `WorkerBackend`
imports nothing from `capture_worker.py`, it only knows how to spawn
and speak the wire protocol to it). No existing edge changes direction
or shape; `daemon/service.py` still only ever sees a `CaptureBackend`.

```mermaid
graph LR
    CAM["camera/camera.py<br/>Camera"] -->|open/read/close<br/>get_property/set_property| WB["camera/backends/worker_backend.py<br/>WorkerBackend"]
    WB -->|spawn/terminate<br/>(multiprocessing.Process)| PROC["python -m aprilcam.camera.capture_worker<br/>(child process)"]
    WB -->|attach, read latest frame| SHM["multiprocessing.shared_memory<br/>frame ring (few slots, ~6MB/1080p frame)"]
    WB <-->|get/set property, settable_names,<br/>frame_size, heartbeat| CTRL["control channel<br/>(pipe/socket)"]
    PROC -->|publish frame + seq/timestamp| SHM
    PROC <-->|answer control requests| CTRL
    PROC -->|owns| CV2["cv2.VideoCapture<br/>(AVFoundation/USB/V4L2)"]
    WATCHDOG["daemon/service.py<br/>stall watchdog (extended)"] -->|poll heartbeat, trigger<br/>close()+open() on stall| CAM
```

**Shared-memory frame ring.** A short ring (a handful of slots) of
raw frame buffers under one `multiprocessing.shared_memory.SharedMemory`
segment, sized for the camera's resolution (~6 MB per 1080p BGR frame —
this is why the ring is shared memory and not a pipe: a pipe would
either block the worker on a slow parent or force a full-frame copy
through kernel buffers every tick at 15-30 Hz). A small header (fixed
offset, fixed-size fields) carries the current write slot index, a
monotonically increasing sequence number, a capture timestamp, and the
frame's shape/dtype, so the parent can always locate and validate the
newest complete frame without a separate metadata channel. The worker
writes a new frame into the next slot and only then advances the
header's sequence number — `WorkerBackend.read()` reads the header,
copies out (once — no double-copy) the slot the sequence number names,
and returns `None` if the sequence hasn't advanced since the last read
(no fresh frame yet) or if the child process is no longer alive. This
mirrors the same "N subscribers, one computation" shared-buffer
discipline `daemon/broadcast.py` already uses for stream fan-out, one
layer lower — here there is exactly one reader (the parent) per ring,
so no broadcaster is needed, only the sequence-number "is this new"
check.

**Control channel.** A separate pipe (or local socket) carries small,
synchronous request/response messages: `get_property`/`set_property`/
`settable_names`/`frame_size`, plus the settle-read behavior
`Camera.apply_config` already drives from one layer up (`camera/DESIGN.md`
§3's "Settle-after-open" constraint) — the worker, not the parent,
issues the actual settle reads against its own `cv2.VideoCapture`,
since that capture object only exists inside the child process; the
parent's `WorkerBackend.apply_config`-facing methods are a thin
request/response proxy over this channel, not a reimplementation of
settle logic. The heartbeat is carried in the shared-memory header
(sequence number + timestamp), not the control channel, so a control
request never has to compete with the capture loop's own cadence.

**Backend-internal, not `Camera`-visible.** Everything above is fully
contained inside `WorkerBackend` and `capture_worker.py`. `Camera`,
`DaemonService._camera_for`, and every other consumer of a
`CaptureBackend` see the identical five-method surface
(`is_open`/`open`/`read`/`close`/`frame_size`) plus the optional
`ControlBackend` surface, unchanged. This is the entire point of the
`CaptureBackend` seam (§1's "why the seam exists"): the wedge fix is a
backend-internal concern, not an architectural change to how `Camera`
uses a backend.

### Backend selection — the ticket 006/031-006 wiring

`backends.resolve_capture_backend(hardware_key, index, *,
backend_factory, list_loopback_cameras=None)` is the **single shared
branch point** both `discovery.py`'s `_probe_openable` and
`daemon/service.py`'s `_camera_for` call, so the `"libcamera:"`-prefix
decision lives in exactly one place. For every non-libcamera
`hardware_key` it is a pass-through to `backend_factory(index)`
(`OpenCVBackend(index)` by default) — unchanged behavior. For a
libcamera-sourced `hardware_key`, `index` is the enumeration *position*
(0-based, not a `/dev/videoN` node), so this function re-enumerates
`list_loopback_cameras()`, matches on `camera_id` (the raw `/sys` card
label), and constructs `V4l2LoopbackBackend(f"/dev/video{device.device_index}")`
using the resolved real device index. If no enumerated device matches
(a stale registry entry, or a race), it logs a warning and falls back
to `backend_factory(index)` rather than raising — consistent with every
other function in this identity/discovery family degrading instead of
throwing on an enumeration miss, and no worse than the unconditional
`OpenCVBackend(index)` every camera got before this ticket.

**`WorkerBackend` selection (sprint 038).** `resolve_capture_backend`
itself is unchanged — the libcamera-vs-everything-else branch stays
exactly as ticket 031-006 built it. What changes is what
`backend_factory(index)` constructs for the non-libcamera, default
case: a config/env-gated choice between `_default_backend_factory`
(`OpenCVBackend(index)`, unchanged default) and a new factory
constructing `WorkerBackend(index)`. This is a process-wide daemon
setting (not per-camera), gated so a partially-merged sprint cannot
change runtime camera behavior until explicitly enabled (sprint.md
Migration Concerns) — every existing test and every non-macOS platform
keeps getting `OpenCVBackend` by default. `_probe_openable` and
`_camera_for` both go through this same `backend_factory`, so a probe
and a real open agree on which backend a camera gets, exactly as they
do today for the libcamera branch.

### `Camera`: composition over backends, self-heal, config push

`Camera` holds exactly one primary `CaptureBackend` (pixels) and an
optional `ControlBackend` (settings). `_backend_for_key()` routes
`frame_width`/`frame_height` to the primary backend unconditionally and
everything else to `_settings_backend()` (`control_backend` if given,
else the primary backend itself — this is what every non-macOS platform
and every headless test still does). `apply_config` resolves accessors
per key up front, snapshots previous values, orders `_GATE_KEYS` first,
runs the settle-after-open drain if this is the first real push
following a fresh open, then applies each key — rolling back everything
already applied this call through that key's own backend on any
failure.

Self-heal is unified across backends (a deliberate simplification of
v1, whose loopback and USB captures had asymmetric reopen policies
because their capture classes had genuinely different primitives): every
`CaptureBackend` exposes the same `open()`/`close()` pair, so "reopen"
is uniformly close-then-open regardless of backend, and the single v1
threshold (5 failures -> reopen, capped at 10) applies everywhere.
`on_reopen` is an optional constructor-injected callback fired only on a
*successful* reopen — `daemon/service.py` wires it to mark a camera's
calibration stale, since a reopen correlates with a real-world event
(someone unplugged/moved the camera) that elapsed time alone cannot
detect.

**`Camera.check_stall()` — the proactive half of self-heal (sprint
038).** A new method, called by `daemon/service.py`'s watchdog thread
on every held-open camera each pass (`daemon/DESIGN.md`'s
"Stall-detection watchdog" — this is the `Camera`-level half of that
mechanism). Duck-types an optional `heartbeat_age() -> Optional[float]`
on the primary backend — seconds since that backend's last known-fresh
frame, or `None` if the backend has no such concept — the same
duck-typing discipline `_describe_set_failure`/`get_property` already
use for a backend-specific capability that not every backend
implements. `OpenCVBackend`/`V4l2LoopbackBackend` expose no
`heartbeat_age`, so `check_stall()` is an unconditional no-op for them
(`getattr(..., None)` returns `None`, never a stale age) —
`WorkerBackend` is the only backend that implements it, reading the
shared-memory ring header's sequence number/timestamp (§4). When the
age exceeds a threshold, `check_stall()` calls the exact same
`_attempt_self_heal()` this class already uses for a demand-driven
read-failure streak — no second respawn code path — and returns
whether it did so, purely for the watchdog's own logging. This keeps
`Camera` the single place that decides "close and reopen the backend,"
whether triggered reactively (a caller's `read()` failing enough
times) or proactively (this method, called with nobody reading).

### Concurrency

`Camera._read_lock` (a plain, non-reentrant `Lock`) serializes the
entire body of `read()`, including any self-heal reopen it triggers,
so a background stream producer and one-shot RPC reads never interleave
calls into the same backend handle. `CameraDiscovery` uses its own
`threading.Lock` around `_known_hardware_keys`/`_present_slugs`, updated
by both `probe()` and the background `watch()` poll thread.

### Flat-field gain-map application (out-of-process, documented here for the first time; capture side is sprint 037)

`Camera` optionally accepts a `flatfield_path` at construction (wired by
`daemon/service.py`'s `_flatfield_path_for(slug)`, `<state_dir>/cameras/
<slug>/flatfield.npy`, right next to `calibration.json` — a measured
artifact tied to this camera's current mounting and lighting, like a
calibration, not an operator-authored setting). `_load_flatfield` reads
it (if present) on every `open()`/`reset()` — never on construction alone
— accepting a finite, non-negative float `(H, W)` or `(H, W, C)` array
and rejecting anything else by name, never fatally: a malformed or
unreadable map is logged and ignored, since a camera must never become
unopenable over an optional correction artifact. `_apply_flatfield`
multiplies every frame `read()` returns by the loaded map and saturates
back to the frame's own dtype; a map whose `(H, W)` no longer matches the
frame's (a resolution change since capture) is skipped with one
rate-limited warning rather than silently mis-corrected. This class has
**no notion of the map's provenance or staleness** — `captured_at`,
`stale`, and gain-range reporting are daemon-level state (sprint 037's
`FlatFieldInfo`, held in `DaemonService._flatfields`), the identical
division of responsibility §3/§4 already establish for `calibrated`/
`calibration_stale` on `CameraState`. `Camera` itself changes not at all
for sprint 037's capture workflow — install/replace/clear are exactly the
same "file present or not, reloaded on `open()`/`reset()`" mechanism the
apply side already shipped with; the daemon just gained a way to *produce*
the file instead of an operator hand-running a script.

### Rejected/changed alternatives, per the code's own record

- **Generic `cv2.VideoCapture.set()` as the settings surface** — kept as
  the *only* settings path pre-ticket-023-001, but real-hardware
  validation against this project's own OV9782 camera found it returns
  `False` for every exposure/gain/white-balance control on macOS
  AVFoundation. `UvcUtilBackend` (a `ControlBackend`, not a
  `CaptureBackend`) replaces it exclusively when `control_backend` is
  given, matching v1's own already-proven `uvc-util` answer
  (`archive/camera_setup.sh`).
- **A sleep-based settle fix** — considered and explicitly not chosen;
  plain frame reads (matching v1's own mechanism) cost nothing a caller
  wasn't already going to spend and were what v1 itself proved worked.
- **Retrying a readback mismatch** — explicitly rejected: `uvc-util`
  silently clamping an out-of-range value is not transient, so only a
  raw CLI rejection (`-s` exits nonzero) is retried; an accepted-but-
  wrong write goes straight to repair-and-reject, once.
- **The stakeholder's `auto-exposure-priority` hypothesis** for the
  settle-quirk root cause — investigated and ruled out live: the camera
  that actually failed (`hd-usb-camera`) does not even expose that
  control.
- **Every capture backend running out-of-process, not just the
  wedge-prone one (sprint 038)** — rejected as unjustified scope: the
  wedge is specific to `cv2.VideoCapture`/AVFoundation's own
  process-local session state (root cause above); `V4l2LoopbackBackend`
  and `UvcUtilBackend` show no equivalent symptom and gain nothing from
  the added process-boundary cost. `WorkerBackend` wraps only the
  backend that actually wedges; the others are untouched.
- **A pipe instead of shared memory for frame transport (sprint
  038)** — rejected: a ~6 MB 1080p frame over a pipe at 15-30 Hz either
  blocks the worker on a slow parent read or forces a full-frame copy
  through kernel pipe buffers every tick; a shared-memory ring lets the
  parent read the latest frame with at most one copy, independent of
  how promptly it polls.
- **Reworking `Camera._attempt_self_heal` to special-case a worker
  backend (sprint 038)** — rejected: self-heal already only calls
  `backend.close()`/`backend.open()` uniformly across every backend
  (the existing "self-heal is unified across backends" invariant,
  above). Special-casing would duplicate exactly the failure-counting
  and budget logic `Camera` already owns, and would break that
  invariant for no benefit — `WorkerBackend` implements
  kill-and-respawn entirely inside its own `close()`/`open()` instead.
- **A first-frame timeout implemented as a new socket-level timeout in
  `client/streams.py` (sprint 038)** — considered for the `camera view`
  hang (a separate, client-side gap from the worker-process fix above)
  and rejected: `view/viewer.py`'s `StreamLoop` already polls a
  single-slot "latest frame" cell on a fixed cadence on the *caller's*
  thread, so "no frame received within N seconds of `run()` starting" is
  already directly observable there with no wire-level change. A socket
  timeout would also not distinguish "no frame published yet" from "the
  reader thread's own `EOFError`/`OSError` handling is already in
  progress" — two states `StreamLoop` can already tell apart today. See
  `view-DESIGN.md`'s Design Rationale for the chosen alternative.

## 5. Interfaces

### Exposes

For `daemon/` (currently `daemon/service.py`, the sole runtime consumer)
and `testdev/capture_matrix.py` (the sanctioned offline exception):

- **`registry.CameraRegistry(cameras_dir)`** — `.resolve(identity:
  CameraIdentity) -> str` (idempotent; registers if new), `.forget(slug)`
  (raises `UnknownCamera` if absent), `.get(slug)`, `.get_by_number(n)`,
  `.records()` (ordered by number), `__len__`/`__contains__`.
- **`discovery.CameraDiscovery(registry, *, active_slugs=, backend_factory=,
  identity_source=, watch_interval=, list_loopback_cameras=)`** —
  `.probe() -> ProbeReport` (full re-enumeration; never disturbs an
  active-slug camera; a probe-open failure reports `usable=False`,
  never omits the camera), `.watch()` (idempotent background hotplug
  polling), `.known_present() -> FrozenSet[str]`, `.stop_watch()`.
- **`camera.Camera(slug, backend, *, control_backend=, device_name=,
  number=, playfield=, config_loader=, config_path=, on_reopen=,
  flatfield_path=)`** —
  `.slug`, `.state -> CameraState`, `.open()`/`.close()` (raise on
  failure), `.read() -> Optional[np.ndarray]` (never raises),
  `.apply_config(config: CameraConfig)` (whole-push, hard-errors on an
  unsupported key, rolls back atomically), `.effective_config() ->
  CameraConfig` (the self-describing blank template), `.reset()`
  (re-applies the on-disk document or an explicit no-op push), plus
  `.stored_config()` and — **sprint 038** — `.check_stall() -> bool`
  (proactive respawn-if-stalled, a no-op returning `False` for any
  backend with no `heartbeat_age`) beyond the declared interface.
- **`identity.resolve_identity(index, ...)` / `resolve_all(...)`** —
  never raises; `hardware_key` always populated.
- **`backends.resolve_capture_backend(hardware_key, index, *,
  backend_factory, list_loopback_cameras=None) -> CaptureBackend`** —
  the shared libcamera-vs-everything-else branch point.
- **`backends.opencv_backend.OpenCVBackend`**,
  **`backends.v4l2_loopback.V4l2LoopbackBackend`**,
  **`backends.uvc_util.UvcUtilBackend`**, and
  **`backends.libcamera.{LibcameraDevice, list_loopback_cameras,
  loopback_label_prefix}`**.
- **`backends.worker_backend.WorkerBackend(index)`** (**sprint 038**) —
  a `CaptureBackend`; `.open()` spawns a fresh `capture_worker` child
  process, `.close()` terminates it (SIGTERM then SIGKILL after a
  bounded grace period), `.read()` returns the latest complete frame
  from the shared-memory ring or `None` if none is fresh or the child
  is not alive, `.frame_size()`/`.get_property()`/`.set_property()`/
  `.settable_names()` proxy to the child over the control channel.
  Selected by `backend_factory` in place of `OpenCVBackend`, config/
  env-gated (see §4).
- **`capture_worker.main()`** (**sprint 038**, `python -m
  aprilcam.camera.capture_worker`) — the child process entry point.
  Not part of this package's public Python API (nothing imports it);
  invoked only as a subprocess by `WorkerBackend.open()`.
- **`interfaces.CameraIdentity`** and the `CameraRegistry`/
  `CameraDiscovery`/`Camera` base classes every concrete class above
  derives from.

### Consumes

By reference only — `camera/` depends on these but does not own them:

- **`config.interfaces.CameraConfig` / `ConfigLoader`** — the whole-
  document hardware-settings contract `Camera.apply_config`/
  `effective_config`/`reset` speak; `ConfigLoader.load_camera_config`
  is what `Camera._on_disk_config` reads through when a `config_loader`
  and `config_path` are injected.
- **`config.directories.Directories.state_dir`** — where
  `CameraRegistry`'s `cameras/registry.json` lives (regenerable, never
  hand-edited; two-directory model in `docs/design/configuration.md`).
- **`aprilcam.errors`** — `UnknownCamera`, `UnsupportedSetting`,
  `CameraNotPresent` — the typed failures this package's registry and
  `Camera.apply_config` raise; `daemon/service.py` is what turns
  `CameraNotPresent` into an addressing distinction ("registered but not
  present" vs. "never registered").
- **`aprilcam.types`** — `CameraState`, `ProbeReport`, `ProbeSkip`.
- **External tools, all best-effort with graceful degradation on
  absence/failure**: `cv2` / `cv2-enumerate-cameras` (daemon extra),
  `system_profiler` (macOS only), `v4l2-ctl` (Linux/Pi loopback reads
  and format queries), `uvc-util` (macOS UVC control — its absence is
  the one case that raises loudly at `UvcUtilBackend` construction
  rather than degrading, per the "no silent fallback to a control path
  already found not to work" rule).

## Migration Concerns (sprint 038)

None affecting data or the wire: no on-disk schema change (the ring
and control channel are runtime-only, torn down with the process, and
`registry.json`/`calibration.json`/`config.json` are all untouched),
no RPC/proto change (`daemon/service.py` still sees a plain
`CaptureBackend`), no client-visible behavior change for any camera
that keeps `OpenCVBackend`. `WorkerBackend` is selected by a config/env
gate, defaulting to the unchanged `OpenCVBackend` path, so tickets can
land incrementally (the worker module and its tests first, the
backend-selection wiring last) without any intermediate state changing
what a running daemon actually does until the gate is deliberately
flipped. The one operational-deployment concern: a worker-backed
camera now has a second OS process per open camera, which changes
process-table/resource-monitoring expectations (documented in Open
Questions below) — not a migration in the data/schema sense, but worth
naming for whoever operates the daemon.

## 6. Open Questions / Known Limitations

- **The flat-field apply side (§4) shipped out-of-process and went undocumented in this file until sprint 037's planning pass caught the drift.** No functional gap — the code and its own docstrings were always complete — but a reminder that an out-of-process change is still expected to update the owning subsystem's `DESIGN.md`, and this one didn't at the time.
- **No libcamera capture backend exists yet.** `backends/libcamera.py`
  and `backends/__init__.py` both say so explicitly: only `/sys`-based
  enumeration is ported; v1's `cam`-tool/GStreamer "gst mode" and an
  actual capture backend reading a resolved loopback device are
  deferred to "whichever sprint next brings up real Pi hardware" —
  there is no Pi/libcamera device in this dev environment to validate
  against. Ticket 006/031-006 wires the *branch point*
  (`resolve_capture_backend`) end to end, but it is exercised only
  against injected fake device lists in tests, never a real `/sys` tree
  with a real bridge behind it.
- **`V4l2LoopbackBackend` is unverified against real frames.** Its own
  module docstring states this directly: "there is no Linux/
  v4l2loopback hardware on this Mac to validate against." Only the
  pure format-parsing/frame-reshaping logic is unit-tested, against a
  faked subprocess — whether `v4l2-ctl --stream-to=-` actually produces
  correct bytes on a real loopback device is carried forward
  unverified.
- **`resolve_capture_backend`'s fallback-on-miss is not expected to
  actually work.** When a libcamera `hardware_key` has no matching
  enumerated loopback device, the code falls back to
  `backend_factory(index)` (plain `OpenCVBackend`) — but the entire
  reason this ticket exists is that `OpenCVBackend` cannot read a
  loopback device reliably. The fallback exists only so the failure is
  logged rather than silent; it is documented as no better than what
  every camera got before ticket 006.
- **Only the macOS avf/vidpid duplicate-registration bug has automatic
  self-healing.** The general accepted limitation — a serial-less
  camera moved to a different USB port mints an entirely new
  `hardware_key` and a new registry entry, silently orphaning whatever
  config was installed under the old slug — is not itself fixed;
  `_reconcile_location_duplicates` only recognizes the specific
  cross-tier location-fragment collision ticket 021-007 root-caused, not
  every way a fallback-tier identity can drift.
- **`camera probe`'s presentation currently disagrees with `camera
  list`** for calibration/playfield/skip state — filed 2026-08-21 as
  `clasi/issues/camera-probe-reports-everything-uncalibrated-and-renders-skipped-cameras-as-data.md`.
  The root cause is one layer up in `daemon/service.py`
  (`probe_cameras()` does not apply the same calibration/playfield
  overlay `list_cameras()` does, and a skipped camera is rendered as a
  full data row above its own skip notice) — `camera/`'s own
  `ProbeReport.skipped` contract is honored at the data level; the bug
  is in how the daemon presents that data, not in what this package
  reports.
- **What happens when the self-heal reopen budget is exhausted was an
  open question `interfaces.py`'s `Camera.read` docstring names
  explicitly** ("mark dead? keep trying?") — `camera.py` now answers it
  (stop trying; `usable` stays `False`; `present` stays `True`) but the
  interface docstring itself still frames it as unresolved, so a reader
  of `interfaces.py` alone would not know the concrete class has already
  decided this.
- **Whether the wedge correlates with UVC control writes during an
  active stream is still not established (sprint 038, carried forward
  from the source issue).** This sprint fixes the wedge structurally
  via process isolation rather than chasing that correlation — a
  process boundary clears the symptom regardless of its trigger, so
  the open question no longer blocks the fix, but it remains genuinely
  open for anyone curious whether an ordering bug in the daemon (not
  AVFoundation itself) is also involved.
- **A worker-backed camera doubles the daemon's OS-process footprint
  (sprint 038).** N held-open cameras now means N+1 processes (daemon
  plus one worker each) instead of one. Nothing in this sprint adds
  process-table monitoring beyond what `client/lifecycle.py`'s existing
  orphan-process scan already does (and that scan does not know a
  capture worker's command-line pattern yet) — a future sprint may want
  to teach it to recognize and report orphaned capture workers
  specifically, rather than only orphaned `aprilcam.daemon` processes.
- **Worker respawn latency is not bounded by an SLA in this sprint.**
  `WorkerBackend.open()` waits for the child's first heartbeat before
  returning, but no explicit timeout on that wait is specified here —
  left as an implementation-time decision (a ticket's job), not an
  architectural one, since it only affects how quickly a respawn
  completes, not whether the design is sound.
