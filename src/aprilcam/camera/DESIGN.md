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
   control) plus the `Camera` object that composes them into one
   physical camera with a uniform lifecycle.

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
  whole-document `apply_config` with rollback, and the
  `effective_config`/`reset`/`stored_config` trio for the config-service
  boundary.
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

### Concurrency

`Camera._read_lock` (a plain, non-reentrant `Lock`) serializes the
entire body of `read()`, including any self-heal reopen it triggers,
so a background stream producer and one-shot RPC reads never interleave
calls into the same backend handle. `CameraDiscovery` uses its own
`threading.Lock` around `_known_hardware_keys`/`_present_slugs`, updated
by both `probe()` and the background `watch()` poll thread.

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
  number=, playfield=, config_loader=, config_path=, on_reopen=)`** —
  `.slug`, `.state -> CameraState`, `.open()`/`.close()` (raise on
  failure), `.read() -> Optional[np.ndarray]` (never raises),
  `.apply_config(config: CameraConfig)` (whole-push, hard-errors on an
  unsupported key, rolls back atomically), `.effective_config() ->
  CameraConfig` (the self-describing blank template), `.reset()`
  (re-applies the on-disk document or an explicit no-op push), plus
  `.stored_config()` beyond the declared interface.
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

## 6. Open Questions / Known Limitations

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
