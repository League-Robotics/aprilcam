---
id: '003'
title: Identity-safe camera open/reopen (re-resolve index and verify identity every
  time)
status: open
use-cases: []
depends-on: []
github-issue: ''
issue: camera-reopen-reuses-a-stale-opencv-index-and-can-open-the-wrong-camera.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Identity-safe camera open/reopen (re-resolve index and verify identity every time)

## Description

Close the silent-camera-substitution bug: `Camera._attempt_self_heal`
and `Camera.check_stall()`'s respawn both call `self._backend.close()`
then `self._backend.open()` on the same backend object, and for
`OpenCVBackend` the OpenCV index is fixed at construction —
`daemon/service.py`'s `_camera_for` already re-resolves the index
fresh via `_index_for(record)` on first open, but that re-resolution
never runs again on a later self-heal/stall reopen, so a reopen after
an index shift (a sibling camera leaving/rejoining an overloaded USB
bus) can silently open a *different* physical camera under the
original slot's slug.

Read this sprint's `design/camera-DESIGN.md` overlay's new "Identity-
safe reopen" section before implementing — it specifies the chosen
approach in detail (an injected `index_resolver` on `OpenCVBackend`,
wired from `daemon/service.py`'s existing `_index_for`, plus optional
post-open AVFoundation `uniqueID` verification) and the rationale for
rejecting a `Camera`-level fix instead.

**Scope note**: this ticket covers `OpenCVBackend` only.
`WorkerBackend` has the same latent defect in principle but is gated
off by default (see `camera-DESIGN.md`'s Open Questions) — out of
scope here, left for whichever sprint enables that gate for real.

**Implementation approach**:
1. `camera/backends/opencv_backend.py`: `OpenCVBackend.__init__` gains
   two new optional keyword parameters — `index_resolver:
   Optional[Callable[[], Optional[int]]]` and
   `expected_hardware_key: Optional[str]` — both backward compatible
   (omitted, behavior is identical to today). `.open()`: if
   `index_resolver` is given, call it immediately before opening
   instead of using the constructor-time index; if it returns `None`,
   raise (matching the existing "`CaptureBackend.open()` raises on
   failure, never a sentinel" contract) rather than attempting to open
   nothing. If `expected_hardware_key` is given and the platform can
   verify identity post-open (macOS AVFoundation `uniqueID`, reusing
   `camera/identity.py`'s existing resolution machinery), compare the
   freshly-opened device's identity against it; on mismatch, close the
   just-opened session and raise loudly (log at WARNING naming both the
   expected and observed identity) — never complete the open.
2. `daemon/service.py`'s `_camera_for`: instead of resolving `index`
   once and passing a bare `int` into `resolve_capture_backend`/
   `backend_factory`, pass (or have the backend factory construct) an
   `index_resolver` closure bound to `record.hardware_key` that calls
   the existing `_index_for(record)` helper fresh each time it is
   invoked, plus `expected_hardware_key=record.hardware_key`. This
   reuses `_index_for` — do not write a second implementation of
   "find the current index for this hardware key."
3. Decide (and document your choice in this ticket's own commit/PR,
   since the architecture deliberately left it open) how a `None`
   resolver result interacts with `MAX_REOPENS`'s fixed 10-attempt
   budget — per the stakeholder's fixed contract, "not currently
   enumerated" must not silently retry a stale index, and should not
   necessarily burn the same budget as a present-but-broken camera.
   Either: (a) `Camera._attempt_self_heal` treats a failed-due-to-
   `None`-resolver reopen as budget-neutral (does not increment
   `_reopens`), or (b) leave `_attempt_self_heal`/`check_stall` as-is
   and instead extend `daemon/service.py`'s existing
   `check_stalled_cameras` watchdog pass to poll identity independently
   and only trigger a real reopen attempt once the hardware key
   reappears (mirroring sprint 038's "exhausted workers get a periodic
   retry" precedent for worker-backed cameras). Either satisfies the
   stakeholder-fixed contract; pick one and note why in code comments.

## Acceptance Criteria

- [ ] Every open and reopen of an `OpenCVBackend`-backed camera
      re-resolves the index from `hardware_key` immediately before
      opening — verified by a test that changes what the identity
      source enumerates between two reopen attempts and asserts the
      *new* index is used, not the one from the first open.
- [ ] **Core regression test**: an enumeration snapshot where the
      target camera's hardware key is absent (or has moved to a
      different index than a *different* camera now occupies) between
      an initial open and a triggered reopen — the reopen must not open
      the device now sitting at the old index. This is the exact
      incident from the source issue; the test must fail against
      today's code and pass after the fix.
    - [ ] A hardware key not currently enumerated reports
      `present=False`/`usable=False` from the reopen attempt, never a
      retried stale index.
- [ ] Where identity can be verified post-open (AVFoundation
      `uniqueID`), a deliberately mismatched identity is refused (the
      open raises, is logged, and the camera is not silently
      substituted) — covered by a test injecting a fake post-open
      identity that disagrees with the expected `hardware_key`.
- [ ] `camera/identity.py`'s fallback-chain order and `hardware_key`
      format are unchanged (contractual — reordering mints new
      `hardware_key`s for every camera, per `camera-DESIGN.md` §3).
- [ ] Every existing direct `OpenCVBackend(index)` construction (tests,
      `testdev/capture_matrix.py`) continues to work unmodified — the
      new parameters are additive and optional.
- [ ] `CaptureBackend`'s public five-method surface
      (`open`/`read`/`close`/`frame_size`/is_open) is unchanged — the
      new parameters are `OpenCVBackend`-constructor-only, not part of
      the behavioral interface.

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/camera -m "not camera and not integration"`.
- **New tests to write**: (1) a fake identity-source test proving
  reopen uses a freshly-resolved index, not the original one; (2) the
  core regression test above (device removed/shifted between open and
  reopen); (3) a post-open identity-mismatch test (AVFoundation path);
  (4) a `None`-resolver-result test confirming `present=False` rather
  than an exception escaping uncaught or a stale-index retry.
- **Verification command**: `uv run pytest tests/aprilcam/camera tests/aprilcam/daemon -m "not camera and not integration"`
