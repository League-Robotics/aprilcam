---
id: '001'
title: Merge libcamera and cv2 camera enumeration in identity.py
status: open
use-cases:
- SUC-001
depends-on: []
github-issue: ''
issue: usb-cameras-ignored-on-a-linux-host-that-also-has-libcamera-loopbacks.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Merge libcamera and cv2 camera enumeration in identity.py

## Description

`src/aprilcam/camera/identity.py`'s `resolve_all()` (around line 715)
returns early with only libcamera-resolved identities whenever
`_probe_libcamera()` finds any loopback, so cv2-enumerated USB/UVC
cameras on the same Linux host are never resolved. Fix this by unifying
the two enumeration sources on one index domain and merging instead of
choosing.

**Root cause and fix, precisely:**

1. `_probe_libcamera()` (around line 555) currently keys its returned
   `Dict[int, _LibcameraEntry]` by `LibcameraDevice.position` (0-based
   enumeration order — see `backends/libcamera.py`'s
   `list_loopback_cameras`). Change it to key by
   `LibcameraDevice.device_index` instead — the real `/dev/videoN`
   number. On Linux this is the *same* index domain
   `_enumerate_cv2()` already uses (confirmed by sprint 041's
   backend-offset stripping: `aprilcam-imx296-88000` enumerates as cv2
   index `70`, matching `/dev/video70`). Re-keying by device index means
   the same physical loopback shows up under the *same* integer key from
   both enumeration sources — a precondition for merging without
   collision, since two live V4L2 devices can never share one
   `/dev/videoN`.
2. `resolve_all()` (around line 715): replace the `if libcamera_entries:
   return {...}` early return with a union over
   `set(libcamera_entries) | set(enum_entries)`, resolving each index
   through the existing `resolve_identity()` call (pass
   `enum_entries`, `profiler_entries`, and `libcamera_entries` through,
   plus `name=enum_entries[idx].name if idx in enum_entries else None`
   for indices that came only from the cv2 side). Gather each source
   table once, same as today (no per-camera `system_profiler` shell-out).
3. `resolve_identity()` (around line 591) needs **no logic change** —
   its existing "check `libcamera_entries.get(index)` first, else fall
   through to the cv2/profiler chain" order already implements
   loopback-wins dedup once both sources share the device-index domain.
   Do not add a second dedup pass elsewhere; the union + existing
   priority order is sufficient and is the intended design (see
   sprint.md's Architecture → Design Rationale for alternatives
   considered and why they were rejected).
4. This re-keying also closes a latent, previously-unexploited
   collision: `OpenCVBackend.open()`
   (`backends/opencv_backend.py:265`) and `WorkerBackend`
   (`backends/worker_backend.py:372`) both call
   `resolve_identity(self._index)` directly with a real cv2 index, for
   a post-open anti-substitution check. Before this fix, that call's
   internal `_probe_libcamera()` lookup could wrongly match an unrelated
   loopback whose `position` happened to equal the caller's cv2 index.
   No code change is needed at either call site — verify only, once the
   re-keying lands, that this can no longer happen (see Testing).
5. Docstring-only fix: `backends/__init__.py`'s
   `resolve_capture_backend()` docstring (around line 299) currently
   says the libcamera branch's `index` parameter is "the enumeration
   position ... not a `/dev/videoN` device index." After this ticket
   it *is* the device index (though that branch already ignores the
   passed `index` and re-derives it via `hardware_key` matching against
   a fresh `list_loopback_cameras()` call, so this is a comment
   correction, not a behavior change). Update the docstring to match.
6. Update `camera/identity.py`'s module docstring where it currently
   describes `resolve_all`'s either/or shape and `_probe_libcamera`'s
   position-keying, so the docstring matches the new merge behavior
   (see especially the "Fallback chain" and "Root-cause fix" sections
   already in that module's docstring, which set the precedent for
   this kind of inline documentation of a resolved bug).

Do not touch `camera/discovery.py`, `camera/registry.py`, or
`CameraIdentity`'s field set — none of them need to change (see
sprint.md's Architecture → Impact on Existing Components for why each
is unaffected).

## Acceptance Criteria

- [ ] `_probe_libcamera()` keys its dict by `LibcameraDevice.device_index`.
- [ ] `resolve_all()` merges both enumeration sources on a mixed host;
      no early return that drops one source.
- [ ] A mixed-host fixture (one loopback + one UVC camera, injected via
      `enum_entries`/`libcamera_entries`) resolves both, each exactly
      once — the loopback under `libcamera:...`, the UVC camera under
      its normal fallback-chain identity.
- [ ] A loopback device present in *both* injected sources under the
      same `/dev/videoN` resolves once, under its libcamera identity —
      never a second `loc:`/`vidpid:` record for the same device.
- [ ] A loopback-only fixture and a UVC-only fixture each resolve
      exactly as they did before this change (regression check against
      existing test expectations, updated only for the position→
      device_index key change in fixtures).
- [ ] Registry numbering is stable: a registry pre-populated with
      libcamera cameras already numbered (e.g. `imx296` #1, `imx296-2`
      #2) keeps those numbers when `resolve_all()`'s output gains a USB
      camera entry.
- [ ] `OpenCVBackend`/`WorkerBackend`'s post-open `resolve_identity(index)`
      call no longer risks matching an unrelated loopback merely because
      of numeric coincidence — add or extend a test that constructs a
      libcamera fixture and a cv2 fixture whose indices would have
      collided under the old `position`-keyed scheme, and asserts the
      cv2 index resolves to the cv2 identity.
  - Old `position`-based fixtures updated to `device_index`, or
    removed, throughout.
  - `_probe_libcamera`, `resolve_all`, and `backends/__init__.py`
    docstrings updated to describe the merge behavior accurately.
- [ ] `uv run mypy` over `src/aprilcam` passes with no new errors
      (sprint 041's close failed on an `Optional`-narrowing mypy error —
      this ticket runs mypy itself rather than deferring to
      `close_sprint`).

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/camera/test_identity.py tests/aprilcam/camera/test_discovery.py -m "not camera and not integration"`
- **New tests to write** (all offline, in `tests/aprilcam/camera/test_identity.py` unless noted):
  - Mixed host: injected `enum_entries` with a USB/UVC entry and a
    loopback entry at the same `/dev/videoN`, plus `libcamera_entries`
    for that loopback keyed by the same device index — assert
    `resolve_all()` returns two identities, the loopback one carrying
    `hardware_key` prefixed `libcamera:`, the USB one carrying its
    normal fallback-chain `hardware_key`.
  - Loopback-only: only `libcamera_entries` populated, `enum_entries`
    empty or irrelevant — assert output unchanged from pre-fix
    behavior.
  - UVC-only: `libcamera_entries` empty, `enum_entries` populated —
    assert output unchanged from pre-fix behavior.
  - Loopback in both sources, no duplicate: `enum_entries` contains an
    entry whose `path` is the loopback's `/dev/videoN`, and
    `libcamera_entries` contains the same device index — assert exactly
    one identity is returned for that index, not two.
  - Registry numbering stability: build a `CameraRegistry` (or fixture
    matching its resolve/records behavior) pre-populated with two
    libcamera cameras at numbers #1/#2, run `resolve_all()` with those
    two loopbacks plus a new USB camera, and assert the two existing
    numbers are unchanged and the USB camera gets the next number —
    this may belong in `test_discovery.py` if that is where
    registry-numbering behavior is currently tested; follow the
    existing test file's convention for where registry-numbering
    assertions live.
  - Collision regression: a fixture where a cv2 index and a (pre-fix)
    libcamera `position` would have been numerically equal, confirming
    `resolve_identity()` resolves the cv2-side camera correctly (see
    Acceptance Criteria).
- **Verification command**: `uv run pytest -m "not camera and not integration"` (scoped to `tests/aprilcam/camera/` is acceptable for this ticket per the source-code rule — the full suite runs once at `close_sprint`), followed by `uv run mypy src/aprilcam`.
