# Camera (`aprilcam.camera`)

**Owner:** Eric Busboom · **Last reviewed:** 2026-07-29 · **Status:** stable

---

## 1. Purpose

This package answers one question no other subsystem may answer: *which
physical camera is this, and where does its state live?* It owns stable
camera identity across reboots, re-plugs, and OS re-enumeration; the
persistent registry mapping identity → user-facing enumeration number +
per-camera directory; hardware enumeration; and the capture backends
(OpenCV V4L2/AVFoundation, libcamera-via-loopback, GStreamer, screen
capture, video-file playback). The seam exists because OS camera indices
are unstable garbage — everything above this package (daemon, calibration,
CLI display) keys on the identities and directories minted here. It is
**daemon-only** at runtime: only the daemon may open cameras.

## 2. Orientation

`identity.py` resolves the best-available stable `unique_id` for an OpenCV
index via a documented fallback chain, combining `cv2-enumerate-cameras`
with a macOS `system_profiler` backfill. `registry.py` persists
`cameras_dir/registry.json`: `CameraRecord` (unique_id → enum, dir, name,
vid/pid/serial/location, last_seen), `CameraRegistry.resolve()` (reconnect
reuse + first-sight dir adoption), and `resolve_enum_to_index()` (the
user-facing `#N` → live OS index mapping). `camutil.py` is enumeration and
diagnostics: `list_cameras()` (libcamera → cv2-enumerate-cameras → legacy
probe loop), `get_device_name()`, `diagnose_camera_failure()` (lsof/fuser).
`libcam.py` is the Raspberry Pi CSI seam: enumerates labelled v4l2loopback
devices via `/sys` (loopback mode, default) or `cam -l` (gst mode) and
keeps the rest of AprilCam on its index-based camera model. `camera.py`
(`Camera`, lazy-open `cv.VideoCapture` wrapper), `video_camera.py`
(file playback), and `screencap.py` (`ScreenCaptureMSS`, a
VideoCapture-shaped mss wrapper) are the backend classes. `camera_config.py`
is the atomic read/write helper for per-camera `config.json`.

## 3. Constraints and Invariants

- **Identity chain order is contractual** (`identity.py:resolve_identity`):
  libcamera id → `avf:<uniqueID>` / `serial:<usb-serial>` →
  `vidpid:VVVV:PPPP@<location>` → `loc:<location>` → `name:<slug>`.
  `is_fallback=True` marks the last two (port-move / identical-model
  sensitive). Reordering or reformatting these prefixes silently mints new
  identities for every existing camera, orphaning their registry records.
- **A serial-less camera moved to another USB port becomes a new camera.**
  Accepted, documented limitation — but see the open stale-record bug below
  for how it compounds.
- **The registry only holds records for cameras actually resolved at least
  once.** It never fabricates placeholder records from on-disk dirs
  (`adopt_existing_dirs()` is a deliberate no-op — the old behaviour created
  phantom/duplicate entries). Dir adoption happens at *connect time* in
  `resolve()`.
- **Directories are never renamed.** A first-seen camera adopts an existing
  unowned `<slug>` dir (keeping its `calibration.json`); only when the slug
  is owned by another record does it disambiguate to `<slug>-<enum>`.
- **`next_enum` is clamped above every stored enum on load**
  (`registry.py:204-212`) so a corrupt/missing counter can never re-issue a
  number already in use — enum numbers are the user-facing stable handle.
- **All persistence is atomic**: tmp file + flush + fsync + `os.replace`,
  for both `registry.json` and `config.json`. A crash mid-write must never
  leave a partial file.
- **Daemon-boundary rule** (`camera_config.py` docstring): the daemon may
  call `load_camera_config` (hardware settings at open time) but must never
  call `save_camera_config` — `config.json` is developer-owned, written only
  by the MCP `SetCameraConfig` proxy and humans.
- **cv2 imports must stay lazy at the package boundary**:
  `camera/__init__.py` lazy-loads `Camera`/`VideoCamera` via `__getattr__`
  so cv2-free clients can import cv2-free submodules (`identity`,
  `registry`, `camera_config`). Adding a top-level `import cv2` to those
  modules breaks every base (non-`[daemon]`) install.
- **Corrupt registry/config files start empty / read as absent, never
  raise** — a bad JSON file must not prevent the daemon from opening
  cameras.
- **Never run GStreamer+libcamera inside the daemon process** — it
  segfaults via gRPC's fork handlers and hangs against the event loop
  (`libcam.py` module docstring). Loopback mode exists precisely to keep
  libcamera out-of-process.

## 4. Design

**Identity resolution.** `resolve_identity()` short-circuits to the
libcamera camera-id when that backend is enabled (fully stable). Otherwise
it merges two sources: `cv2-enumerate-cameras` (index, name, vid/pid, path;
AVFoundation indices are normalised by subtracting the 1200 backend offset)
and, on macOS, `system_profiler SPCameraDataType` parsed into
`{name: (unique_id, vid, pid, serial)}` and backfilled by device-name
match. `resolve_all()` gathers both tables once for all cameras to avoid a
per-camera `system_profiler` shell-out (~seconds each). Never raises; worst
case is the name+resolution slug.

**Registry resolve.** Known `unique_id` → refresh identity fields +
`last_seen`, keep enum and dir (unplug/replug is invisible). New
`unique_id` → assign `next_enum++`, then `_allocate_dir(slug, enum)`:
prefer the bare slug (adopting existing calibration in place), else
`<slug>-<enum>` (appending `-x` until free). `_used_dirs()` counts a dir as
owned if *any* record references it — this is the known bug (see §6): no
liveness check, so a stale record blocks adoption.

**Backends.** Plain USB/built-in cameras: `cv.VideoCapture(index, backend)`
with platform-ordered backend candidates (`default_backends()`: MSMF/DSHOW
on Windows, AVFoundation on macOS, V4L2 on Linux). The daemon's open path
drains 5 warm-up frames before applying UVC controls — AVFoundation resets
controls applied before its capture session settles. UVC settings come from
`config.json`'s `settings.controls` map applied via `uvc-util -s key=value`
(binary searched in `env_dir/bin` then PATH), and are **re-applied on every
reopen** — the OV9782's manual low exposure does not survive a device
reset. (The apply/reopen code lives in `daemon/camera_pipeline.py`; this
package owns the config schema and enumeration it depends on.)

**libcamera (Pi CSI).** Supported mode is `loopback`: an out-of-process
`libcamerasrc → v4l2sink` bridge feeds labelled v4l2loopback devices
(`aprilcam-<model>-<token>`); enumeration reads
`/sys/class/video4linux/video*/name` (no subprocess), and the daemon reads
the device via `daemon/v4l2_reader.py:V4l2CtlCapture` — a `v4l2-ctl`
subprocess streaming raw YUYV, because pip OpenCV's V4L2 backend cannot
read the loopback (every read is a `select() timeout`). The colour
conversion deliberately uses `COLOR_YUV2RGB_YUYV` (not `_BGR_`): the
bridge's YUY2 stream comes out red/blue-swapped under the BGR code, so the
"wrong" constant yields true BGR — do not "fix" it. `V4l2CtlCapture`
implements `isOpened/read/get/set/release` plus `reopen()`, which is the
hook the daemon's self-heal keys on. gst mode (`cv.CAP_GSTREAMER` on a
`libcamerasrc` pipeline) is kept for standalone scripts only. A non-empty
enumeration is cached for process lifetime; an empty one for 3 s (bridge
may not be up yet). Backend selection: `APRILCAM_CAMERA_BACKEND` =
auto/libcamera/v4l2.

**Enumeration** (`camutil.list_cameras`). With libcamera enabled: the real
CSI cameras plus any `uvcvideo`-driver USB cameras from `/sys` (one capture
node per physical device — uvcvideo exposes a metadata node too), indices
being the `/dev/videoN` numbers. Otherwise `cv2-enumerate-cameras` with
identity backfill; last resort a probe loop that briefly opens each index
(explicitly probe-only — sustained capture is the daemon pipeline's job)
with stderr silenced.

## 5. Interfaces

### Exposes

- `identity.resolve_identity(index, name=…) → CameraIdentity` and
  `resolve_all() → {index: CameraIdentity}`. Contract: never raises;
  `unique_id` always populated; `reason` names the source; `is_fallback`
  flags non-hardware ids.
- `registry.CameraRegistry(cameras_dir)` — `.resolve(identity) →
  CameraRecord` (the only sanctioned way to mint/refresh records),
  `.get/.records/.upsert/.save/.load`, `.next_enum`.
  `registry.resolve_enum_to_index(enum_no, registry, live_identities)` →
  live OS index, raising `CameraSelectError` with a printable message.
  `registry.dir_slug(name)` mirrors `calibration.device_name_slug` so
  adoption matches legacy dirs — keep them in sync.
- `camutil.list_cameras(...) → [CameraInfo]`, `get_device_name(index)`,
  `select_camera_by_pattern`, `diagnose_camera_failure(index)`
  (`{exists, blocking_processes}`), `default_backends()`, `camera_slug`.
- `libcam.backend_enabled()`, `list_cameras()`, `camera_for_index()`,
  `loopback_index()`, `gst_pipeline()`, `capture_mode()`.
- `camera_config.load_camera_config(dir) → dict | None` (never raises),
  `save_camera_config(dir, dict)` (atomic; daemon must not call),
  `parse_camera_config(d)` (pure validation for RPC blobs).
- `Camera` (lazy-open VideoCapture wrapper, context manager),
  `VideoCamera` (file playback for tests), `ScreenCaptureMSS`.

### Consumes

- `cv2` / `cv2-enumerate-cameras` — capture and enumeration (daemon extra).
- `aprilcam.errors` — `CameraError`, `CameraNotFoundError`.
- `system_profiler`, `v4l2-ctl`, `ffmpeg`, `uvc-util`, `cam` — external
  tools, all best-effort with graceful absence.
- `aprilcam.calibration.device_name_slug` — slug parity (owned by
  calibration; mirrored in `dir_slug`).

## 6. Open Questions / Known Limitations

- **Stale-record dir orphaning (confirmed bug)**: `_used_dirs()` has no
  liveness filter, so a camera re-plugged at a new USB location (new
  `unique_id` because it has no serial) gets `<slug>-<enum>` even when the
  bare-slug dir's only claimant is a dead record — orphaning its
  calibration, playfield link, and `camera_position` (breaking parallax).
  The `resolve()` docstring's "another *live* record" qualifier is not
  implemented. Full analysis and fix direction in
  `clasi/issues/stale-registry-record-orphans-camera-from-its-calibration-dir.md`.
  Related weakness: identity could survive a re-plug when vid+pid(+serial)
  match instead of minting a new location-based id.
- **`CameraRegistry(adopt=)`** is a retained-for-compat no-op parameter;
  `adopt_existing_dirs()` likewise. Candidates for deletion once call sites
  are cleaned.
- **`camera.py:Camera` is barely used by the live daemon path** — the
  pipeline opens `cv.VideoCapture` directly. `Camera`/`VideoCamera` mainly
  serve tests and the legacy second pipeline in `core/`.
- **v2 disposition (spec §4.2)**: this package survives essentially intact
  as the daemon-only identity + backend layer — identity chain, registry,
  backends, and UVC re-apply are all explicitly retained. The registry
  stale-record fix and the whole-document `SetCameraConfig`/
  `ResetCameraConfig` flow (spec §4.1) are the changes that land here; the
  camera config document additionally gains the playfield link and mount
  position as first-class, daemon-served content (spec §5).
