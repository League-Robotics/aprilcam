---
id: '001'
title: 'Persistent camera registry: hardware identity + record schema'
status: done
use-cases:
- SUC-005
depends-on: []
github-issue: ''
issue: persistent-camera-registry-with-stable-identity-and-enumeration.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Persistent camera registry: hardware identity + record schema

## Description

Foundational layer for the persistent camera registry. Introduce a stable
hardware-identity resolver and the on-disk registry record schema. No behavior
change to `OpenCamera` or the CLI yet (that is ticket 003).

Today camera identity is name-only (`camutil.get_device_name` →
`calibration.device_name_slug`), so two identical-model cameras collide on one
slug and there is no hardware-unique id (issue Gap 1). This ticket adds:

1. **`camera/identity.py`** — a single resolver returning the best-available
   stable id for a device, with a documented fallback chain:
   AVFoundation `uniqueID` / USB `serial` → `VID:PID + USB location-id` →
   USB `location path` (port) → `name+resolution` slug (last resort). Sources:
   `cv2-enumerate-cameras` (vid/pid/path where exposed) and, on macOS,
   `system_profiler SPCameraDataType` / `SPUSBDataType`. This module is the
   only place that shells out to `system_profiler`; it must not import OpenCV
   at module top and must degrade gracefully (return the fallback id, never
   raise) on unsupported platforms.
2. **Extend `camera/camutil.py` `CameraInfo`** with optional identity fields
   (`unique_id`, `vid`, `pid`, `serial`, `location`) and populate them in
   `list_cameras` from the resolver when available. Absent fields leave
   behavior unchanged.
3. **`camera/registry.py`** — the `CameraRegistry` record schema and load/save
   of `data/aprilcam/cameras/registry.json` (an index mapping
   `unique_id → {enum, dir, name, vid, pid, serial, location, last_seen}`).
   This ticket provides the schema, atomic read/write, and an `upsert(record)`
   API. Enumeration assignment and reconnect reuse are ticket 002.

## Acceptance Criteria

- [x] `camera/identity.py` exposes a resolver returning a stable `unique_id`
  plus component fields, with the documented fallback chain and a recorded
  reason for the chosen source.
- [x] When no serial/uniqueID is available, the resolver returns the USB
  location-path id and marks it as a fallback; the limitation is documented in
  the module docstring.
- [x] The resolver never raises on unsupported platforms or missing tools; it
  returns the name+resolution slug as the last-resort id.
- [x] `CameraInfo` carries the new optional identity fields; `list_cameras`
  populates them when available and is unchanged when they are absent.
- [x] `CameraRegistry` reads/writes `registry.json` atomically and round-trips
  a record through `upsert` + reload.
- [x] Two distinct devices (mocked) produce two distinct `unique_id`s even when
  their device names are identical.
- [x] `uv run pytest` passes.

## Implementation Plan

- **Approach**: Build identity resolution as a pure leaf module, then the
  registry as a thin persistence layer over it. Keep `system_profiler` parsing
  isolated and mockable.
- **Files to create**: `src/aprilcam/camera/identity.py`,
  `src/aprilcam/camera/registry.py`.
- **Files to modify**: `src/aprilcam/camera/camutil.py` (extend `CameraInfo`,
  populate identity in `list_cameras`).
- **Testing plan**: see Testing below; mock all subprocess/enumeration sources.
- **Docs**: module docstrings document the fallback chain and the
  location-path limitation.

## Testing

- **Existing tests to run**: `uv run pytest tests -k camutil`, then full
  `uv run pytest`.
- **New tests to write**:
  - identity resolver over mocked `cv2-enumerate-cameras` and `system_profiler`
    outputs: serial path, VID:PID+location path, location-path fallback, and
    last-resort slug; assert distinct ids for two identical-name devices.
  - resolver returns a value (no raise) when `system_profiler` is missing or on
    a non-darwin platform (monkeypatch `sys.platform`).
  - `CameraRegistry` round-trip: `upsert` then reload yields the same record;
    atomic write leaves no `.tmp` behind.
  - Mark OpenCV-importing tests `needs_cv2`.
- **Verification command**: `uv run pytest`
