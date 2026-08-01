---
id: '003'
title: Capture backends and the Camera class
status: done
use-cases:
- UC-17
- UC-25
depends-on:
- '002'
github-issue: ''
issue: daemon-bring-up-with-basic-camera-cli.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Capture backends and the Camera class

## Description

Depends on ticket 002 (a `Camera` reports `CameraState`, which the
registry now knows how to produce identity/enumeration for). Implements
`camera/backends/*.py` (one small adapter per capture technology) and
`camera/camera.py` (the `Camera` Protocol from
`src/aprilcam/camera/interfaces.py`: `open`/`close`/`read`/
`apply_config`/`effective_config`/`reset`/`state`), plus
`camera/discovery.py`'s `CameraDiscovery` Protocol (`probe()` returning
`ProbeReport`, `watch()` for passive hotplug).

This sprint's `Camera.apply_config`/`effective_config` are scoped to
**introspection only** — reporting what a camera exposes and its
current settings — not the config *push* RPC loop (UC-03 is out of
scope this sprint). `effective_config()` still must work because
`CameraState` construction and probe reporting depend on it internally,
but no CLI command or `Daemon` RPC this sprint calls
`apply_config()` with a client-supplied document.

**Reuse from archive** (per the architecture's module table):
- `archive/src/aprilcam/camera/video_camera.py` (97 lines,
  `VideoCamera(Camera)` wrapping `cv2.VideoCapture`) — directly
  reusable, closest existing match to the new `Camera` Protocol.
- `archive/src/aprilcam/daemon/v4l2_reader.py` (184 lines, v4l2-loopback
  reader via `v4l2-ctl` subprocess, with its YUYV workaround) — needs
  modification to conform to the `Camera` Protocol shape as one backend
  among several rather than the daemon's only reader.
- `archive/src/aprilcam/camera/libcam.py` (233 lines, libcamera
  enumeration + GStreamer pipeline builder) — needs modification; only
  in scope if a libcamera/Pi device is expected on this Mac (it is not
  — both fixture cameras are USB/AVFoundation), so this backend may be
  stubbed/deferred if it adds no test coverage this sprint. Record the
  decision in the module docstring either way.
- `archive/src/aprilcam/camera/screencap.py` (108 lines, `mss`-based
  screen capture) — needs modification; low priority, include only if
  trivial, otherwise defer (not needed for UC-17's scenarios on real
  hardware).
- `archive/src/aprilcam/camera/camutil.py` (472 lines — `list_cameras`,
  `camera_slug`, `select_camera_by_pattern`, AVFoundation + v4l2 USB
  enumeration, failure diagnosis) — needs modification; extract the
  backend-enumeration and failure-diagnosis helpers into
  `camera/discovery.py`'s `probe()`.

## Acceptance Criteria

- [x] `Camera.open()` / `.read()` / `.close()` work against a real
      `cv2.VideoCapture`-backed device (`@pytest.mark.camera`, both
      `arducam-ov9782-usb-camera` and `hd-usb-camera` on this Mac).
- [x] `Camera.read()` returns `None` on a read failure rather than
      raising, and repeated failures trigger the documented self-heal
      reopen path (v1's 5-consecutive-failures-then-reopen threshold,
      capped at 10 reopens, per the `Camera.read` docstring).
- [x] `Camera.effective_config()` returns introspected facts (device
      name, frame dimensions) for both real cameras.
- [x] `CameraDiscovery.probe()` returns a `ProbeReport` whose `cameras`
      tuple matches what registry resolution (ticket 002) produces for
      attached hardware, and whose `skipped` tuple is empty when
      nothing was actually skipped (UC-25.S-7 — "empty means genuinely
      complete").
- [x] `CameraDiscovery.probe()` does not disturb a camera mid-read: a
      test opens a camera, starts reading frames in a loop, calls
      `probe()` concurrently, and confirms the read loop is
      uninterrupted (UC-25.S-7, `@pytest.mark.camera`).
- [x] `CameraDiscovery.watch()` begins passive hotplug watching; a
      camera physically unplugged during a `@pytest.mark.camera` test
      run is flagged `present=False` without a `probe()` call
      (UC-25.S-5) — if unplug/replug cannot be automated in CI, this
      criterion is verified manually once by the ticket's implementer
      and recorded in the ticket's completion notes, per the dev
      plan's hardware-testing standing rules. **See Completion Notes:
      the diffing mechanism itself is implemented and fully covered by
      an automated headless test; the literal physical-unplug leg is
      NOT manually verified (an implementing agent cannot unplug
      hardware) and is flagged below for a human to do once.**
- [x] A camera present but unopenable (simulate via a backend that
      raises on `open()`) is reported by `probe()` with its derived
      name and `usable=False`, `present=True` — never omitted
      (UC-25.S-4).

## Completion Notes

- **Implemented**: `camera/backends/__init__.py` (`CaptureBackend`,
  the internal adapter Protocol), `camera/backends/opencv_backend.py`
  (`OpenCVBackend`, the backend both fixture cameras use, exercised
  live against both), `camera/backends/v4l2_loopback.py`
  (`V4l2LoopbackBackend`, converted in full from `v4l2_reader.py`),
  `camera/backends/libcamera.py` (loopback `/sys` enumeration only —
  see below), `camera/camera.py` (`Camera`, `SimpleCameraConfig`),
  `camera/discovery.py` (`CameraDiscovery`). `camera/identity.py`'s
  `_probe_libcamera()` is now wired to the real (empty-on-this-Mac)
  enumeration instead of ticket 002's `{}` stub.
- **Deferred, as the ticket explicitly allows**: `libcam.py`'s "gst
  mode" (`cam`-tool enumeration, GStreamer pipeline builder) and an
  actual libcamera capture backend — no Pi/libcamera hardware here to
  validate either against; the decision is recorded in
  `camera/backends/libcamera.py`'s module docstring. `screencap.py` is
  not ported at all this ticket (lowest priority, not needed for any
  hardware-backed AC) — no stub file created.
- **Simplification recorded, not a silent deviation**: v1 split
  self-heal reopen into an infinite-retry loopback path and a
  10-reopen-capped USB path (the two capture classes had genuinely
  different reopen primitives). Since every `CaptureBackend` now
  exposes the same `open()`/`close()`, this ticket unifies to the
  single 5-failures/10-reopens threshold the acceptance criteria name,
  applied uniformly across all backends — documented in
  `camera/camera.py`'s module docstring, along with the
  budget-exhausted decision (stays `present=True`, `usable=False`
  forever after, i.e. "plugged in and broken", not "unplugged").
- **`apply_config()`**: implemented (ordering, hard-error-on-unknown-
  key, atomic rollback) against `OpenCVBackend`'s generic
  `property_map()`/`settable_names()` — a best-effort, not a verified
  per-camera-model settable-key list (real UVC introspection, e.g.
  v1's `uvc-util`, is deferred to the config-push ticket per the
  sprint architecture's Open Question 1). No CLI/RPC calls it this
  sprint, matching the ticket's introspection-only scope.
- **Open item for the stakeholder/team-lead**: `CameraDiscovery.watch()`'s
  hotplug-diffing mechanism is real and headlessly tested
  (`test_watch_detects_hotplug_and_unplug_without_probe` in
  `test_discovery.py`), but the literal "physically unplug a USB
  camera" leg of UC-25.S-5 was not performed — the implementing agent
  has no way to manipulate hardware. Recommend a human unplugs one of
  the two fixture cameras while `watch()` is running (once a daemon
  process wires it in, ticket 004+) and confirms it flags absent
  within a few seconds with no `probe()` call, recording the result
  here or in ticket 007's end-to-end pass.

## Testing

- **Existing tests to run**: `tests/aprilcam/camera/test_identity.py`,
  `test_registry.py` (ticket 002) — `Camera`/`CameraState` construction
  depends on both.
- **New tests to write**: `tests/aprilcam/camera/test_camera.py` (mocked
  backend for headless open/read/close/self-heal-reopen logic);
  `tests/aprilcam/camera/test_discovery.py` (probe/watch, headless with
  a fake backend for the report-shape assertions, `@pytest.mark.camera`
  for the two real-hardware and non-disturbance criteria above).
- **Verification command**: `uv run pytest tests/aprilcam/camera/ -k "camera or discovery"`
  then `uv run pytest -m camera tests/aprilcam/camera/` on this Mac with
  both cameras attached.
