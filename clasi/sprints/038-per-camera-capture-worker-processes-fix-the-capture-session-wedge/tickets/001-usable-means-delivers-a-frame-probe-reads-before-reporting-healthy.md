---
id: '001'
title: usable means delivers a frame (probe reads before reporting healthy)
status: open
use-cases:
- SUC-003
depends-on: []
github-issue: ''
issue: camera-capture-session-wedge-is-silent-and-needs-a-daemon-restart.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# usable means delivers a frame (probe reads before reporting healthy)

## Description

`camera/discovery.py::CameraDiscovery._probe_openable` currently reports
`usable=True` from `open()` succeeding and then an immediate `close()` —
it never reads a frame. This is the single most misleading signal in
the capture-session-wedge issue: a camera whose AVFoundation session is
wedged (opens fine, `isOpened()` reports true, but every read returns
`None` forever) still probes healthy. Fix `_probe_openable` to attempt
one bounded frame read between `open()` and `close()`, and report
`usable=False` if that read doesn't return a real frame within the
bound. This ticket is fully independent of the worker-process work
(tickets 003-006) — it applies to whichever backend a camera happens to
use today, worker or not — and closes gap 1 from
`clasi/issues/camera-capture-session-wedge-is-silent-and-needs-a-daemon-restart.md`
on its own.

See `clasi/sprints/038-.../design/camera-DESIGN.md` (the sprint's
overlay copy) §3 ("`usable` means 'delivered a frame,' not merely
'opened'") for the accepted latency/cost tradeoff this introduces:
`probe()` runs over every camera and gets measurably slower per camera.

## Acceptance Criteria

- [ ] `_probe_openable` performs `open()`, one bounded frame read, then
      `close()`, in that order, for every non-libcamera backend path
      (the libcamera branch's own device resolution is unaffected).
- [ ] A fake `CaptureBackend` whose `open()` succeeds but `read()`
      always returns `None` causes `_probe_openable` to report
      `usable=False` — where it previously reported `True`.
- [ ] A fake `CaptureBackend` whose `read()` returns a real frame
      promptly still reports `usable=True`, unchanged from before.
- [ ] The read bound is a named constant (not a magic number inline),
      documented with the tradeoff it accepts.
- [ ] No change to `CameraDiscovery.probe()`'s non-disturbance
      guarantee (UC-25.S-7) — a camera the daemon already holds open
      (`active_slugs`) is still never opened by this probe path at all.
- [ ] Existing `camera/discovery.py` tests continue to pass; add new
      tests rather than loosening existing assertions.

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/camera/ -m "not camera and not integration"`
- **New tests to write**: a fake backend whose `read()` always returns
  `None` after a successful `open()`; a fake backend whose `read()`
  returns a valid frame immediately; a timing-independent test asserts
  the bounded read does not hang the test suite (inject a short bound).
- **Verification command**: `uv run pytest tests/aprilcam/camera/ -m "not camera and not integration"`

## Implementation Plan

**Approach**: Add a small, named read-timeout constant near
`_probe_openable` in `camera/discovery.py`. After `camera.open()`
succeeds, call `camera.read()` once (or in a short bounded loop/timeout
if a single call could itself block indefinitely on some backend —
match whatever bound is cheapest to reason about given `Camera.read()`'s
existing non-blocking-per-call contract). Treat a `None` result as
`usable=False`; a real frame as `usable=True`. Preserve the existing
try/except around `open()` for the "present but unopenable" case
unchanged.

**Files to modify**:
- `src/aprilcam/camera/discovery.py` (`_probe_openable`)

**Testing plan**: New unit tests in `tests/aprilcam/camera/test_discovery.py`
(or wherever existing `_probe_openable`/`probe()` tests live) using a
fake/injectable backend, no real camera hardware.

**Documentation updates**: None per-ticket — the sprint's already-
approved `design/camera-DESIGN.md` overlay content is applied to the
canonical `src/aprilcam/camera/DESIGN.md` at sprint close (see the
`close-sprint` skill), not hand-edited mid-sprint.
