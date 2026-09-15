---
id: '001'
title: Fix Linux backend-offset dedup and filter internal Pi V4L2 nodes in camera/identity.py
status: open
use-cases: [SUC-001]
depends-on: []
github-issue: ''
issue: linux-enumeration-lists-pi-isp-nodes-as-cameras-with-duplicate-rows.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Fix Linux backend-offset dedup and filter internal Pi V4L2 nodes in camera/identity.py

## Description

`camera/identity.py::_enumerate_cv2()` has two bugs on Linux, both
confirmed by reading the vendored `cv2_enumerate_cameras` source
(see sprint.md's Problem section for the full trace):

1. **Duplicate rows.** `enumerate_cameras()` (called with the default
   `CAP_ANY`) yields one `CameraInfo` per entry in
   `linux_backend.supported_backends = (CAP_GSTREAMER=1800,
   CAP_V4L2=200)`, combining `index = real_index + backend_id`.
   `_enumerate_cv2()`'s offset-stripping (`idx = raw - avf_offset if
   raw >= avf_offset else raw`) only accounts for macOS's single
   `CAP_AVFOUNDATION` backend, so on Linux both backend-tagged entries
   for the same physical `/dev/videoN` survive as two different,
   never-collapsed indices in the output dict — this is the entire
   "every row printed twice" bug from the linked issue, and it affects
   every Linux camera, not just Pi internal nodes.
2. **Junk nodes admitted.** `cv2_enumerate_cameras`'s own
   `device_can_capture_video()` check does not exclude Raspberry Pi
   RP1/PiSP internal V4L2 nodes (`rp1-cfe-*`, `pispbe-*`, `rpivid`) —
   they genuinely report `V4L2_CAP_VIDEO_CAPTURE`. v1's
   `archive/src/aprilcam/camera/camutil.py::_list_v4l2_usb_cameras`
   already solved this by checking the node's V4L2 driver name via
   the `/sys/class/video4linux/videoN/device/driver` symlink and
   keeping only real capture-source drivers.

Fix both in `camera/identity.py`, so that enumeration never proposes a
Pi-internal node as a camera candidate at all, and never reports the
same physical Linux device twice.

## Acceptance Criteria

- [ ] `_enumerate_cv2()` recovers the correct real device index for
      both `CAP_V4L2`- and `CAP_GSTREAMER`-tagged entries on Linux
      (not just `CAP_AVFOUNDATION` on macOS), so a physical device
      enumerated under both backends collapses to one dict entry keyed
      by its real index.
- [ ] A new Linux-only node classifier excludes candidates whose V4L2
      driver (read via `/sys/class/video4linux/videoN/device/driver`)
      is a known Pi media-pipeline-internal driver (`rp1-cfe`,
      `pispbe`, `rpivid`, `bcm2835-codec`); a narrow, documented
      name-prefix backstop (`rp1-cfe-`, `pispbe-`) applies when the
      driver symlink cannot be read.
- [ ] The classifier fails open: an unreadable driver symlink or an
      unrecognized driver name keeps the candidate rather than
      excluding it. A real USB/UVC camera (`uvcvideo` driver) is never
      excluded.
- [ ] Excluded candidates never appear in `_enumerate_cv2()`'s output
      dict, so no downstream code (`resolve_all`, `CameraDiscovery`)
      ever sees, resolves, opens, or registers an identity for them.
- [ ] macOS behavior (`_macos_camera_profiles`, the `CAP_AVFOUNDATION`
      offset path) is unchanged — no new code path runs on `sys.platform
      == "darwin"`.
- [ ] The sprint-040 libcamera loopback path is unaffected:
      `resolve_all()`'s early return when `_probe_libcamera()` yields
      entries means `_enumerate_cv2()` is not called at all in that
      case, and no test in this ticket should need to touch
      `backends/libcamera.py`.
- [ ] All new logic is covered by injected-fixture unit tests (fake
      `/sys/class/video4linux` trees and fake `cv2_enumerate_cameras`-
      shaped `CameraInfo` lists via the existing `enum_entries`
      injection point) — no real hardware, no real `cv2_enumerate_cameras`
      call, in the test suite.

## Implementation Plan

**Approach**: Work entirely inside `camera/identity.py`.

1. In `_enumerate_cv2()`, replace the single `avf_offset` check with a
   small helper that, given a raw combined index and the platform's
   `supported_backends` tuple (importable from
   `cv2_enumerate_cameras.linux_backend`/`macos_backend`/etc., or
   simply hardcoded per platform to avoid importing the vendored
   module's private symbols at every call site — implementer's
   judgment, documented either way), returns the real device index and
   which backend produced it. Keep the function's existing "never
   raises, degrades to empty on any failure" contract.
2. Add a module-private predicate, e.g.
   `_is_pi_internal_node(driver: Optional[str], name: Optional[str]) ->
   bool`, and a small `/sys` reader (mirroring the style of
   `backends/libcamera.py::list_loopback_cameras`'s `/sys` handling —
   no subprocess, tolerant of missing files) that resolves a
   `/dev/videoN` path's driver via
   `/sys/class/video4linux/videoN/device/driver` (realpath, basename).
   Only invoke this reader when `sys.platform.startswith("linux")` (or
   equivalent); it must be a no-op / return "unknown" everywhere else.
3. Wire the predicate into the Linux enumeration loop inside
   `_enumerate_cv2()` (or a thin wrapper it calls before building each
   `_EnumEntry`), so an excluded candidate is skipped — not filtered
   later by a caller.
4. Update the module docstring's "Fallback chain" / relevant sections
   to record this addition, following the existing convention of
   documenting *why*, not just *what* (see the existing "Root-cause
   fix" docstring section for the house style).

**Files to modify**:
- `src/aprilcam/camera/identity.py`

**Files to create**:
- A new test file if the existing `tests/` structure doesn't already
  have a natural home for `identity.py` Linux-enumeration tests (check
  first — there is very likely already a
  `tests/camera/test_identity.py` or similar to extend).

**Testing plan**:
- Fake a `cv2_enumerate_cameras.enumerate_cameras()`-shaped result
  list containing, for one physical `/dev/video5`, two `CameraInfo`
  entries at `index=205` (V4L2) and `index=1805` (GStreamer, given
  `CAP_GSTREAMER=1800`) with identical name/vid/pid/path — assert
  `_enumerate_cv2()` (or `resolve_all`) returns exactly one entry
  keyed at index 5.
- Fake a `/sys/class/video4linux/videoN/device/driver` symlink tree
  (via `tmp_path`, matching `list_loopback_cameras`'s test style) for
  `rp1-cfe-fe-image0` (driver `rp1-cfe`), `pispbe-input` (driver
  `pispbe`), a real `imx296` loopback-style node, and a `uvcvideo`-
  driven USB camera node — assert only the driver-verified real nodes
  survive.
- Test the name-prefix backstop directly by simulating an unreadable
  driver symlink for an `rp1-cfe-csi2-ch0`-named node.
- Test fail-open: an unknown driver name (e.g. `some-future-driver`)
  and a missing `/sys` entry both keep the candidate.
- Verify existing `identity.py` tests (macOS profiler tiers, fallback
  chain, libcamera short-circuit) still pass unchanged.

**Documentation updates**: Extend `camera/identity.py`'s module
docstring per the Implementation Plan above. No `docs/design/` overlay
changes are expected for this ticket (see sprint.md's Architecture
section — no data-model or cross-module change).

## Testing

- **Existing tests to run**: the full `camera/identity.py` test file
  (find it under `tests/` — likely `tests/camera/test_identity.py` or
  equivalent), scoped per `.claude/rules/source-code.md` (this is
  ticket-scoped work, not a sprint close).
- **New tests to write**: as described in the Testing plan above —
  Linux dedup across backend offsets, driver-based exclusion, name-
  prefix backstop, fail-open behavior.
- **Verification command**: `uv run pytest tests/camera/test_identity.py -m "not camera and not integration"`
  (adjust the path once the actual test file is located; do not run
  the full suite for this ticket — see `.claude/rules/source-code.md`).
