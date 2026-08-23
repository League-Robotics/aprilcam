---
id: '006'
title: Wire V4l2LoopbackBackend for libcamera-identified cameras
status: done
use-cases:
- SUC-006
depends-on: []
github-issue: ''
issue:
- sprint-020-followups-socket-collision-and-cli-polish.md
- hardening-cleanup-and-the-design-doc-set.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Wire V4l2LoopbackBackend for libcamera-identified cameras

## Description

Libcamera camera *enumeration* was ported in sprint 020
(`camera/backends/libcamera.py`'s `list_loopback_cameras`, wired into
`camera/identity.py`'s `_probe_libcamera`, assigning
`hardware_key=f"libcamera:{lc.camera_id}"`). A capture *backend*
(`V4l2LoopbackBackend`, `camera/backends/v4l2_loopback.py`) also
already exists — but it is never selected. Both `camera/discovery.py`
and `daemon/service.py` have exactly one backend-construction call
site each, and both unconditionally `return OpenCVBackend(index)`
regardless of the camera's identity source. `V4l2LoopbackBackend`'s own
module docstring explains why this matters: "an OpenCV backend cannot
read this loopback reliably — every `read()` reports a stale frame."
This is required before any Pi-over-the-field deployment, which is the
deployment the architecture is shaped around.

Wire both call sites to branch on `identity.hardware_key`'s
`"libcamera:"` prefix and construct `V4l2LoopbackBackend(f"/dev/video{n}")`
instead, where `n` is the enumerated device index from
`list_loopback_cameras()`. Every other identity source is unaffected.

**Live capture cannot be verified in this sprint** — there is no
v4l2loopback/Pi hardware in this development environment (the same
constraint sprint 020's own deferral recorded). Scope this ticket's
acceptance criteria to what is mechanically testable (backend-selection
logic given a mocked/fixture identity) and name the hardware-dependent
portion explicitly as future, hardware-gated verification.

## Acceptance Criteria

- [x] Given a mocked/fixture identity resolution whose `hardware_key`
      is `"libcamera:"`-prefixed, both `camera/discovery.py`'s and
      `daemon/service.py`'s backend-construction call sites construct
      `V4l2LoopbackBackend(f"/dev/video{n}")` (with `n` the correct
      enumerated device index), not `OpenCVBackend(index)`.
- [x] Given any other identity source (AVFoundation, UVC/`vid:pid`,
      fallback name-based slug), both call sites are unchanged —
      regression coverage against today's `OpenCVBackend(index)`
      behavior.
- [x] No change to `V4l2LoopbackBackend` itself, `list_loopback_cameras`,
      or `camera/identity.py`'s `_probe_libcamera` — this ticket only
      connects an existing, previously-unwired edge.
- [x] Ticket completion notes explicitly state that live capture
      through a real loopback device remains unverified for lack of
      hardware in this environment — not silently claimed as done.
- [x] `uv run mypy` clean.

## Completion Notes

Implemented via a shared helper, `resolve_capture_backend(hardware_key,
index, *, backend_factory, list_loopback_cameras=None)`, added to
`src/aprilcam/camera/backends/__init__.py`. Both `camera/discovery.py`'s
`_probe_openable` and `daemon/service.py`'s `_camera_for` now call this
helper instead of unconditionally invoking their own `backend_factory`/
`OpenCVBackend(index)`.

**A subtlety discovered during implementation, not previously called
out in the ticket or sprint plan**: the `index` that flows through
`identity_source()`/`resolve_all()` for a libcamera-sourced camera is
`LibcameraDevice.position` (0-based enumeration order,
`camera/identity.py`'s `_probe_libcamera`), **not** the real
`/dev/videoN` device index (`LibcameraDevice.device_index`). Naively
building `f"/dev/video{index}"` from that `index` would have been
wrong. `resolve_capture_backend` instead re-enumerates
`list_loopback_cameras()` and matches on `camera_id` (the raw `/sys`
card label embedded after the `"libcamera:"` prefix in `hardware_key`)
to recover the real `device_index`, then builds
`V4l2LoopbackBackend(f"/dev/video{device_index}")` from that. Both new
call-site tests deliberately use a `position` different from
`device_index` (0 vs. 70) to pin this distinction, not just coincidentally
pass because the two happened to be equal.

If a libcamera-prefixed `hardware_key` has no matching enumerated
device (a stale registry entry, or an enumeration race), the helper
logs a warning and falls back to `backend_factory(index)` rather than
raising — an explicit, documented degradation, not a silent gap; see
the function's docstring. This case is covered by
`test_resolve_capture_backend_falls_back_when_no_matching_loopback_device`
in `tests/aprilcam/camera/test_backends.py`.

`CameraDiscovery.__init__` and `DaemonService.__init__` each gained one
new optional, keyword-only constructor parameter,
`list_loopback_cameras` (defaults to `None`, meaning "use the real,
subprocess-free `/sys` scan"), mirroring the existing
`identity_source`/`backend_factory` override pattern so tests can inject
a fixed device list headlessly. This was necessary for testability but
is additive only — every existing caller and test that does not pass it
is unaffected, confirmed by the full scoped regression run below (no
existing test needed updating).

**Live capture through a real v4l2loopback device remains unverified**
— there is no Raspberry Pi / v4l2loopback hardware in this development
environment, the same constraint sprint 020's own deferral recorded and
sprint.md's Migration Concerns names explicitly for this ticket. What
is verified here is the backend-*selection* logic only: given a mocked
libcamera identity and a mocked `list_loopback_cameras()` result, the
correct backend *class* is constructed with the correct device path.
Whether a real Pi's `libcamerasrc` bridge actually produces bytes
`V4l2LoopbackBackend` can decode is carried forward to whichever future
sprint next brings up real Pi hardware to validate against — not
silently claimed as done here.

**Test results** (`uv run pytest tests/aprilcam/camera/test_discovery.py
tests/aprilcam/camera/test_backends.py tests/aprilcam/daemon/test_service.py
-m "not camera"`): 256 passed, 8 deselected (hardware-gated
`@pytest.mark.camera` tests, correctly skipped in this environment).
`uv run mypy` (project-wide): Success, no issues found in 79 source
files.

## Implementation Plan

**Approach**: locate the exact backend-construction call sites (already
identified during sprint planning: `camera/discovery.py` and
`daemon/service.py`, each with a single `return OpenCVBackend(index)`),
and introduce a shared helper (to avoid duplicating the branch logic in
two places) that takes an identity and an index/device-index and
returns the correct backend instance. Reuse
`camera/backends/libcamera.py`'s `list_loopback_cameras()` to resolve
the enumerated device index for a given libcamera-sourced camera,
consistent with how `camera/identity.py`'s `_probe_libcamera` already
consumes that function.

**Files**:
- `src/aprilcam/camera/discovery.py` — backend-construction call site.
- `src/aprilcam/daemon/service.py` — backend-construction call site
  (currently at/near line 154 per this ticket's planning research).
- Possibly a small shared helper, if the implementer judges the
  duplicated branch logic is better factored into one function both
  call sites use (e.g. in `camera/backends/__init__.py` or
  `camera/identity.py`) — a judgment call left to the implementer,
  consistent with keeping each subsystem's boundary narrow.

**Testing plan**:
- **Existing tests to run**: `uv run pytest tests/aprilcam/camera/
  test_discovery.py tests/aprilcam/camera/test_backends.py
  tests/aprilcam/daemon/test_service.py -v`.
- **New tests to write**: for both call sites, a test with a mocked
  identity carrying a `"libcamera:"`-prefixed `hardware_key` asserting
  `V4l2LoopbackBackend` is constructed with the correct device path; a
  test with a non-libcamera identity asserting `OpenCVBackend` is
  constructed as before (regression).
- **Verification command**: `uv run pytest -m "not camera"` and
  `uv run mypy`. Live hardware verification is explicitly out of scope
  for this sprint (see Description) — record this in completion notes
  rather than attempting a workaround.

**Documentation updates**: none required in `docs/design/` proper — the
`camera/` subsystem `DESIGN.md` does not exist yet; ticket 008
documents this behavior when it bootstraps that file (ticket 008
depends on this ticket for exactly that reason).
