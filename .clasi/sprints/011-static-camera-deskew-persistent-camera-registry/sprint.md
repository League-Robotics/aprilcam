---
id: '011'
title: Static-Camera Deskew & Persistent Camera Registry
status: planning-docs
branch: sprint/011-static-camera-deskew-persistent-camera-registry
use-cases:
- SUC-001
- SUC-002
- SUC-003
- SUC-004
- SUC-005
- SUC-006
- SUC-007
issues:
- static-camera-deskew-from-homography.md
- persistent-camera-registry-with-stable-identity-and-enumeration.md
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 011: Static-Camera Deskew & Persistent Camera Registry

## Goals

Make per-camera state survive the real-world failure modes of a fixed-mount
deployment: (1) deskew a fixed camera from its *saved* homography even when
the live ArUco corners are only intermittently detected, and (2) track
cameras by stable hardware identity and an app-assigned enumeration number so
the daemon survives unplug/replug without a restart.

## Problem

Two persisted-state gaps surface in the extreme-angle global-shutter
deployment:

1. **Deskew is gated on live corners, not on saved geometry.** Deskew only
   warps when all four ArUco corner markers are detected in a single live
   frame (`PlayfieldBoundary._order_poly` / `get_polygon`; `display._update_deskew`
   returns early when `get_polygon()` is `None`). At an extreme angle the four
   corners rarely co-appear, so a camera that *calibrates* successfully (the
   saved `calibration.json` has a valid homography) still never deskews.

2. **Camera identity is name-only and indices are transient.** Cameras are
   keyed by OS device name (slugged) and resolved through the transient OpenCV
   index. Two identical cameras collide on one slug; re-enumeration on
   unplug/replug shifts indices, silently rebinding "camera 2" to a different
   device and forcing a daemon restart. There is no persistent record of
   previously-seen-but-now-disconnected cameras and no stable enumeration
   number.

## Solution

1. **Persistent camera registry (foundational).** Add an explicit registry of
   every camera ever seen, keyed by a best-available stable hardware id (USB
   serial / VID:PID+location / AVFoundation uniqueID, with graceful fallback to
   USB location path). Assign each camera a monotonic enumeration number on
   first sight and reuse it on reconnect by unique-id lookup. `OpenCamera` and
   `aprilcam cameras` resolve through the registry; the CLI lists disconnected
   cameras grayed-out but numbered. Preserve/migrate existing
   `data/aprilcam/cameras/<slug>/` directories.

2. **Homography-derived static deskew (builds on persisted geometry).**
   Persist the reference geometry (playfield dimensions plus the calibration-time
   corner pixel positions and static-marker pixel/world positions) into
   `calibration.json`. Derive the deskew source polygon from `H⁻¹ · [world
   corners]` so deskew needs zero live ArUco corners. Seed `Playfield(polygon=…)`
   at load. Add a static-marker fill-in path that holds the known pixel
   positions of static markers (ArUco corners + AprilTag 1) when they flicker,
   while dynamic AprilTags (ID ≠ 1) are always live. Add a movement-invalidation
   check that drops the static assumption and warns when live static-marker
   positions disagree with stored positions beyond a threshold. Optional
   undistortion before the warp.

## Success Criteria

- A fixed camera with a saved calibration deskews with **no** ArUco corner
  detected in the live stream (verified by physically covering a corner).
- The extreme-angle global-shutter camera produces a flat top-down view.
- Dynamic (robot-mounted) AprilTags are tracked frame-by-frame, never frozen.
- Moving the camera after calibration is detected and surfaced, not silently
  deskewed with a stale transform.
- Two identical-model cameras yield two distinct registry entries with distinct
  enumeration numbers.
- Unplug + replug of a known camera reuses its number and data dir with no
  daemon restart.
- `aprilcam cameras` shows connected and previously-seen-disconnected cameras,
  the latter grayed out, all retaining enumeration numbers.
- Existing `data/aprilcam/cameras/<slug>/` data is preserved or migrated.

## Scope

### In Scope

- New persistent camera-registry module + on-disk registry record.
- Stable hardware-id capture with documented fallback chain.
- Monotonic enumeration assignment and reuse-on-reconnect.
- `OpenCamera` and `aprilcam cameras` resolving through the registry.
- Persisting reference geometry (corners + static markers + W×H) into the
  calibration record's `to_dict`/`from_dict`.
- Homography-derived deskew transform and `Playfield` polygon seeding at load.
- Static-marker fill-in and movement-invalidation logic.
- Optional pre-warp undistortion using saved intrinsics.
- Data-dir migration for existing slug-keyed camera directories.
- Unit/integration tests via `uv run pytest`.

### Out of Scope

- ROS 2 bridge (separate pending issue, explicitly excluded).
- Hot-watching for new-camera hotplug events beyond reconcile-on-open/list
  (daemon may still reload for a *genuinely new* camera).
- Metric (cm-scaled) top-down warp variant — pixel-space polygon warp is the
  baseline; metric warp is an open question, not committed.
- Multi-camera compositing changes.

## Test Strategy

- **Registry/identity**: unit tests over identity extraction (mocked
  `system_profiler` / `cv2-enumerate-cameras` output), enumeration assignment
  and reuse, collision of two identical names, and migration of an existing
  slug dir. Tests gated `needs_daemon` where they import daemon modules.
- **Deskew geometry**: unit tests that compute the corner polygon from a known
  homography and assert the warp matches the live-polygon path on a synthetic
  frame; a regression case from `global-shutter-camera/calibration.json`.
- **Fill-in / invalidation**: unit tests holding a static marker's stored
  position across a "not-detected" frame, and tripping the invalidation
  threshold when stored vs. live disagree.
- All tests run under `uv run pytest`; OpenCV-dependent tests marked
  `needs_cv2`.

## Architecture Notes

- Registry/identity is foundational and lands first; deskew geometry builds on
  the persisted per-camera record.
- Reuse the existing `data/aprilcam/cameras/<id>/` layout — extend it, do not
  replace it.
- The static-marker set is a configurable default: `aruco_corners + apriltag:1`.
- No circular dependency: registry sits in/near `camera/`; calibration-geometry
  persistence extends the existing `calibration` record; deskew consumes the
  persisted record via `PlayfieldBoundary` / `Playfield`.

## GitHub Issues

(None linked.)

## Definition of Ready

Before tickets can be created, all of the following must be true:

- [x] Sprint planning documents are complete (sprint.md, use cases, architecture)
- [x] Architecture review passed
- [ ] Stakeholder has approved the sprint plan

## Tickets

| # | Title | Depends On |
|---|-------|------------|
| 001 | Persistent camera registry: hardware identity + record schema | — |
| 002 | Enumeration numbers + reconnect reuse + data-dir migration | 001 |
| 003 | Wire registry into OpenCamera and `aprilcam cameras` (grayed-out offline) | 002 |
| 004 | Persist reference geometry into the calibration record | — |
| 005 | Homography-derived deskew transform + seed Playfield polygon at load | 004 |
| 006 | Static-marker fill-in + movement-invalidation | 005 |
| 007 | Optional pre-warp undistortion + static-camera mode wiring | 005, 006 |

Tickets execute serially in the order listed.
