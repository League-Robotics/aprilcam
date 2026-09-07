---
id: '004'
title: Honest probe()/list_cameras() health for active (held-open) cameras
status: open
use-cases: []
depends-on:
- '003'
github-issue: ''
issue: camera-probe-reports-a-held-open-camera-as-usable-without-checking-it.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Honest probe()/list_cameras() health for active (held-open) cameras

## Description

`camera/discovery.py`'s `CameraDiscovery.probe()` currently
short-circuits any slug in `active_slugs()` to
`CameraState(..., usable=True)` and a fixed `ProbeSkip(reason="in use
by a live subscriber")`, without ever checking whether the held-open
`Camera` is actually healthy. `daemon/service.py`'s `list_cameras()`
already computes real health for the same cameras from the same
`Camera.state` — this ticket makes `probe()` consult that same source
of truth instead of assuming.

Depends on ticket 003 landing first: both touch
`daemon/service.py`'s `CameraDiscovery` construction site and the same
area of `_camera_for`'s surrounding code, and sequencing avoids merge
churn — there is no functional dependency (this ticket's fix works
whether or not 003 has landed), but do this one second.

Read this sprint's `design/camera-DESIGN.md` overlay's "Honest
active-camera health for `probe()`/`list_cameras()`" section before
implementing — it specifies the constructor-signature change
(`active_slugs: Callable[[], AbstractSet[str]]` ->
`active_camera_state: Callable[[str], Optional[CameraState]]`) and why
a per-slug state accessor was chosen over duplicating health logic in
`daemon/service.py`.

**Implementation approach**:
1. `camera/discovery.py`: change `CameraDiscovery.__init__`'s
   `active_slugs` parameter to `active_camera_state: Callable[[str],
   Optional[CameraState]]`. In `probe()`, for each currently-enumerated
   identity, call `active_camera_state(slug)` instead of checking slug
   membership in a set; `None` means "not held open" (falls through to
   today's `_probe_openable` path, unchanged); a real `CameraState`
   means "held open — report *its* `usable` value," and pick a
   `ProbeSkip.reason` that distinguishes healthy (`"in use by a live
   subscriber"`, unchanged text) from stalled (a new, clearly-worded
   string, e.g. `"in use by a live subscriber (stalled)"`).
2. `daemon/service.py`: wherever `CameraDiscovery` is constructed, pass
   `active_camera_state=lambda slug: (self._cameras[slug].state if slug
   in self._cameras else None)` in place of the old `active_slugs`
   lambda. This is the one production call site — update it and every
   test fake in the same change, no straddling period.
3. Update every existing `CameraDiscovery(...)` test construction site
   using the old `active_slugs` parameter name.

## Acceptance Criteria

- [ ] `CameraDiscovery`'s constructor takes `active_camera_state`
      instead of `active_slugs`; every production and test call site is
      updated in this ticket, none left on the old signature.
- [ ] **Core regression test**: a simulated stalled active camera (a
      fake/stub `CameraState` with `usable=False` returned from
      `active_camera_state` for a held-open slug) makes `probe()`
      report `usable=False` for that slug — this must fail against
      today's code (which hardcodes `True`) and pass after the fix.
- [ ] A healthy active camera still reports `usable=True` — no
      regression on the common case.
- [ ] `ProbeSkip.reason` text distinguishably differs between
      healthy-in-use and stalled-in-use — a test asserts the two
      strings are not equal and each names its condition.
- [ ] `daemon/service.py`'s `list_cameras()` and `probe_cameras()` now
      agree on an active camera's `usable` value (both read from the
      same `Camera.state`) — a test calling both against the same
      daemon state for a stalled active camera gets the same `usable`
      value from each.
- [ ] `CameraDiscovery.probe()`'s non-disturbance invariant (UC-25.S-7)
      is unchanged — verify no test needs to open a real backend for an
      active slug; the health read is against already-known state.

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/camera/test_discovery.py tests/aprilcam/daemon/test_service.py -m "not camera and not integration"` (adjust to actual paths).
- **New tests to write**: a `probe()` test with a fake
  `active_camera_state` returning a stalled `CameraState` for one
  active slug and a healthy one for another, asserting the report and
  skip-reason text differ correctly; a `daemon/service.py`-level test
  comparing `list_cameras()` and `probe_cameras()` output for the same
  stalled active camera.
- **Verification command**: `uv run pytest tests/aprilcam/camera tests/aprilcam/daemon -m "not camera and not integration"`
