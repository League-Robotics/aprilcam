---
status: in-progress
sprint: '011'
tickets:
- 011-001
- 011-002
- 011-003
---

# Persistent camera registry with stable identity and enumeration numbers

Track cameras by stable hardware identity and an app-assigned enumeration
number instead of the transient OS index, so the daemon survives camera
re-enumeration without a restart.

## Problem

The daemon currently keys cameras off the OS-assigned OpenCV index (e.g.
"camera 2"). When cameras re-enumerate (something is plugged or unplugged),
those indices shift, so "camera 2" can silently become a different physical
camera — and today the daemon must be restarted to recover. We want the
daemon to track cameras by stable identity, not by transient OS index, so
that unplug/replug of a *known* camera does **not** require a restart.

## Current state (grounded in code)

- Cameras are identified by OS device name via `cv2-enumerate-cameras`
  (`camutil.get_device_name` / `camutil.list_cameras`), then slugged
  (`calibration.device_name_slug` / `camutil.camera_slug`).
- A persistent per-camera data directory already exists, keyed by slug:
  `data/aprilcam/cameras/<slug>/` (holding `calibration.json`,
  `paths.json`, `info.json`). We already persist per-camera state by name —
  this issue makes that registry **explicit** and adds stable identity plus
  enumeration.
- **Gap 1 — identity is name-only.** Two identical cameras (same model
  name, e.g. two `arducam-ov9782`) collide on the same slug and cannot be
  distinguished.
- **Gap 2 — no enumeration number.** There is no stable, app-assigned
  camera number that survives re-enumeration.
- **Gap 3 — listing hides history.** `aprilcam cameras`
  (`cli/cameras_cli.py`) only lists currently-connected devices;
  previously-seen-but-disconnected cameras are invisible.

## Desired behavior

1. Maintain an explicit **persistent registry** of every camera ever seen —
   one record per camera in the data dir (extend the existing
   `data/aprilcam/cameras/<id>/` layout).
2. Capture a **hardware-unique identifier** beyond the name so two identical
   cameras can be told apart. Investigate what's available on macOS:
   AVFoundation `uniqueID` per device; USB serial / `VID:PID` / USB
   location-id via `system_profiler SPUSBDataType` / `SPCameraDataType`; and
   whatever `cv2-enumerate-cameras` exposes (it can surface vid/pid/path on
   some platforms). Fall back gracefully when no serial exists (e.g. USB
   port/location path) and document the limitation.
3. Assign each newly-seen camera a stable **monotonic enumeration number**.
   On reconnect, look the camera up in the registry by its unique id and
   **reuse** its existing enumeration number rather than assigning a new one.
   `OpenCamera` / lookups should resolve via this registry.
4. `aprilcam cameras` lists **all** registered cameras including
   disconnected ones, the disconnected ones **grayed out**, each keeping its
   enumeration number. Connected cameras show their current OS index;
   disconnected ones show as offline but retain identity + number.
5. Result: re-plugging a previously-seen camera requires **no daemon
   restart** (the daemon recognizes it by unique id and resumes its
   enumeration number / data dir). A genuinely new camera may still trigger
   a reload, but unplug/replug of a known camera must not.

## Open questions (resolve during design)

- Best stable unique id on macOS (and a cross-platform story) — serial vs
  USB location path; behavior when the same physical camera moves to a
  different USB port.
- Registry key / dir naming: keep name-slug dirs, or switch to
  `<enumeration>-<slug>` or a uniqueid-based key? How to migrate existing
  `data/aprilcam/cameras/<slug>/` dirs.
- Whether the daemon should hot-detect new cameras (watch for enumeration
  changes) or only reconcile on explicit open/list.

## Acceptance criteria

- Plugging two of the same model camera yields two distinct registry entries
  with distinct enumeration numbers.
- Unplug + replug of a known camera reuses its enumeration number and
  per-camera data dir, with no daemon restart and no manual reindex.
- `aprilcam cameras` shows connected and previously-seen-disconnected
  cameras, disconnected ones grayed out, all retaining enumeration numbers.
- Existing `data/aprilcam/cameras/<slug>/` data is preserved or migrated,
  not orphaned.
