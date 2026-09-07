---
id: '003'
title: Identity-safe camera open/reopen (re-resolve index and verify identity every
  time)
status: done
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

**Scope note (corrected during team-lead review, post-first-pass):**
this ticket's original text scoped `WorkerBackend` out as "gated off by
default" — that was **wrong**.
`camera/backends/default_capture_backend_factory` selects
`WorkerBackend` *unconditionally* on macOS (not gated), which is
exactly the backend the source incident hit in production (the daemon
log showed a stall-watchdog respawn spawning at a shifted `--index`
that served a different physical camera's frames). The first pass of
this ticket wired `index_resolver`/`expected_hardware_key` onto
`OpenCVBackend` only, leaving the real incident's backend unfixed. This
ticket now covers **both** `OpenCVBackend` and `WorkerBackend` — see
"Implementation notes" below for `WorkerBackend`'s specific design
(post-open identity verification is parent-side there, since the
process that opens the device is the spawned child, not this class).

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

- [x] Every open and reopen of an `OpenCVBackend`-backed camera
      re-resolves the index from `hardware_key` immediately before
      opening — verified by a test that changes what the identity
      source enumerates between two reopen attempts and asserts the
      *new* index is used, not the one from the first open.
- [x] **Corrected scope**: the same is true of a `WorkerBackend`-backed
      camera — `WorkerBackend.open()` calls `index_resolver()` fresh
      immediately before building the spawn command, on every open
      including a kill-and-respawn, and uses its freshly-returned index
      to spawn `capture_worker` — verified by a test that changes the
      resolved index between two open attempts and asserts the *new*
      index is the one spawned, not the one from the first open
      (`tests/aprilcam/camera/backends/test_worker_backend.py::test_open_resolves_index_fresh_before_every_spawn_not_a_stale_one`).
- [x] **Core regression test**: an enumeration snapshot where the
      target camera's hardware key is absent (or has moved to a
      different index than a *different* camera now occupies) between
      an initial open and a triggered reopen — the reopen must not open
      the device now sitting at the old index. This is the exact
      incident from the source issue; the test must fail against
      today's code and pass after the fix. Covered for both backends
      (`OpenCVBackend` via a fake `cv2.VideoCapture`; `WorkerBackend` via
      a fake `subprocess.Popen` that records the spawned `--index`).
    - [x] A hardware key not currently enumerated reports
      `present=False`/`usable=False` from the reopen attempt, never a
      retried stale index — for both backends (`Camera` catches
      `CameraNotEnumeratedError` generically, not per-backend-type;
      `WorkerBackend.open()` raises it identically to `OpenCVBackend`,
      and never calls `subprocess.Popen` when the resolver returns
      `None`).
- [x] Where identity can be verified post-open (AVFoundation
      `uniqueID`), a deliberately mismatched identity is refused (the
      open raises, is logged, and the camera is not silently
      substituted) — covered by a test injecting a fake post-open
      identity that disagrees with the expected `hardware_key`, for
      both `OpenCVBackend` (checked directly against the just-opened
      `cv2.VideoCapture`'s index) and `WorkerBackend` (checked
      parent-side, after the spawned worker's first-frame report — see
      Implementation notes).
- [x] `camera/identity.py`'s fallback-chain order and `hardware_key`
      format are unchanged (contractual — reordering mints new
      `hardware_key`s for every camera, per `camera-DESIGN.md` §3).
- [x] Every existing direct `OpenCVBackend(index)`/`WorkerBackend(index)`
      construction (tests, `testdev/capture_matrix.py`) continues to
      work unmodified — the new parameters are additive and optional on
      both classes.
- [x] `CaptureBackend`'s public five-method surface
      (`open`/`read`/`close`/`frame_size`/is_open) is unchanged — the
      new parameters are constructor-only on `OpenCVBackend` and
      `WorkerBackend`, not part of the behavioral interface.
- [x] `daemon/service.py`'s `_camera_for` wires
      `index_resolver=lambda: self._index_for(record)` and
      `expected_hardware_key=record.hardware_key` into whichever backend
      `resolve_capture_backend` actually returns — `OpenCVBackend` *and*
      `WorkerBackend` — matching the platform-default selection
      (`WorkerBackend` unconditionally on macOS). Verified by two
      `test_service.py` tests: one with an injected `backend_factory`
      returning a bare `WorkerBackend`, one exercising the real
      unforced `default_capture_backend_factory` selection on a
      `sys.platform`-patched-to-`"darwin"` service.
- [x] `camera/discovery.py`'s `_probe_openable` needs no equivalent
      wiring — confirmed by reading it: it constructs a fresh backend
      (and a fresh, immediately-discarded `Camera`) on every single
      probe call, so there is no persistent backend object for a stale
      index to survive in, unlike `_camera_for`'s held-open `Camera`.

## Implementation notes (recorded per the ticket's own instruction)

**Not-currently-enumerated vs. `MAX_REOPENS` budget (Open Questions /
implementation approach item 3):** chose **option (a)**.
`camera/backends/__init__.py` gains a new
`CameraNotEnumeratedError(RuntimeError)`, raised by
`OpenCVBackend.open()` specifically when an injected `index_resolver`
returns `None` (as opposed to a bare `RuntimeError` for an ordinary
"enumerated but failed to open"). `Camera._attempt_self_heal` (and
`Camera.retry_if_exhausted`) catch this distinctly: the reopen-budget
increment is undone (`_reopens -= 1`, so the attempt is budget-neutral)
and `_present` is set `False` (a resolver reporting "not found" is
positive evidence the hardware itself is gone, unlike an ordinary
failure of unknown cause, which leaves `present` unchanged/`True` per
the existing "plugged in and broken" contract). This keeps the
distinction entirely inside `camera/` (`Camera`/`OpenCVBackend`), adding
no new responsibility or signature change to
`daemon/service.py`'s `check_stalled_cameras` watchdog pass — option
(b) was not needed since option (a) already satisfies "never burns the
same fixed budget as a present-but-broken camera" and "the watchdog
keeps retrying the identity lookup" (an unplugged camera's `_reopens`
never advances, so `check_stall`/read-triggered self-heal keep trying
indefinitely without ever tripping `_exhausted`).

**Production wiring (`daemon/service.py`'s `_camera_for`):** since
`resolve_capture_backend`'s `backend_factory: Callable[[int],
CaptureBackend]` contract is shared with `camera/discovery.py`'s
`_probe_openable` and with every test's injected `backend_factory`
(overwhelmingly `lambda index: FakeBackend()`), `_camera_for` does not
change that shared signature. Instead, after
`resolve_capture_backend(...)` returns, `_camera_for` checks
`isinstance(capture_backend, OpenCVBackend)` **and** (added by this
correction) `isinstance(capture_backend, WorkerBackend)`, rebuilding
whichever one matches with `index_resolver=lambda: self._index_for(record)`
and `expected_hardware_key=record.hardware_key`. Constructing either
backend performs no I/O (the worker spawn happens only in
`WorkerBackend.open()`, called by `Camera.open()` further down this
same method), so discarding the first instance is cheap and
behaviourally inert for `V4l2LoopbackBackend`/test fakes, which remain
untouched by either branch.

**Corrected scope: `WorkerBackend` was wrongly left out of this
ticket's first pass.** `camera/backends/default_capture_backend_factory`
selects `WorkerBackend` *unconditionally* on macOS (`sys.platform ==
"darwin"`), not gated behind an env var the way the original Scope
note assumed — the production incident (daemon log: a stall-watchdog
respawn spawning `capture_worker --index 4` and serving a different
physical camera's frames) happened on exactly this path. The first
pass wired `index_resolver`/`expected_hardware_key` onto
`OpenCVBackend` only, leaving the real default backend on this
project's development machine unfixed. `WorkerBackend` now gets the
identical additive, optional constructor contract
(`camera/backends/worker_backend.py`'s module docstring has the full
"Identity-safe reopen" section):

- *Index re-resolution*: identical mechanics to `OpenCVBackend` --
  `index_resolver()` is called at the very top of `open()`, before the
  spawn command is built, on every open including a
  `Camera._attempt_self_heal`/`check_stall`-triggered respawn (which
  only ever call `close()`/`open()` on this same object). `None`
  raises `CameraNotEnumeratedError` before anything is spawned.
- *Post-open identity verification is parent-side, not child-side.*
  Two designs were possible: (a) teach the spawned worker child an
  `--expected-hardware-key` flag and have *it* verify and refuse to
  report ready on a mismatch, or (b) have this class (the parent) call
  `camera/identity.py`'s `resolve_identity()` itself, right after the
  child reports its first frame. **Chose (b)**:
  `resolve_identity()` is a plain enumeration-based function (no open
  capture handle needed -- the same call `OpenCVBackend.open()` already
  makes), so it works identically regardless of which process calls
  it; (a) would duplicate that machinery across a process boundary and
  add a wire-protocol field for no extra verification power (the
  worker has no more reliable way to answer "what's at this index
  right now" than the parent does). (b) still catches the race this
  ticket cares about -- enumeration shifting again between
  `index_resolver`'s pre-spawn resolution and the child's first-frame
  report -- because `resolve_identity()` re-resolves at call time. A
  mismatch calls `self.close()` (kills the worker process, unlinks the
  shared-memory ring) before raising, so nothing is left running or
  leaked.
- Guarded by the identical `_can_verify_identity()` restriction as
  `OpenCVBackend` (macOS + `avf:`-tier `expected_hardware_key` only --
  the other fallback tiers are legitimately port-move-sensitive and
  would produce false-positive mismatches).

`camera/discovery.py`'s `_probe_openable` needs no equivalent change:
read closely, it builds a fresh backend (via `resolve_capture_backend`)
and a fresh `Camera` on every single probe call, both discarded right
after (`finally: camera.close()`) -- there is no persistent backend
object across separate probe calls for a stale index to survive in,
unlike `_camera_for`'s cached, held-open `Camera`. This is unchanged by
either this ticket's first pass or this correction.

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/camera -m "not camera and not integration"`.
- **New tests written** (`tests/aprilcam/camera/backends/test_opencv_backend.py`,
  already present from the first pass): (1) a fake identity-source test
  proving reopen uses a freshly-resolved index, not the original one;
  (2) the core regression test (device removed/shifted between open and
  reopen); (3) a post-open identity-mismatch test (AVFoundation path);
  (4) a `None`-resolver-result test confirming `present=False` rather
  than an exception escaping uncaught or a stale-index retry.
- **New tests written this correction**
  (`tests/aprilcam/camera/backends/test_worker_backend.py`): the
  `WorkerBackend` equivalents of all four above --
  `test_open_resolves_index_fresh_before_every_spawn_not_a_stale_one`
  and `test_index_resolver_absent_preserves_todays_fixed_index_behaviour`
  (fake `subprocess.Popen`, no real worker process, records the spawned
  `--index`), `test_open_raises_camera_not_enumerated_when_resolver_returns_none`
  (asserts `subprocess.Popen` is never called), and
  `test_post_open_identity_mismatch_is_refused_and_kills_the_worker` /
  `test_post_open_identity_match_completes_the_open` /
  `test_identity_check_skipped_off_darwin` /
  `test_identity_check_skipped_when_no_expected_hardware_key_given`
  (real `--synthetic` worker, no camera hardware, monkeypatched
  `resolve_identity`). Plus two `tests/aprilcam/daemon/test_service.py`
  wiring tests:
  `test_camera_for_wires_identity_safe_index_resolver_into_worker_backend`
  (injected `backend_factory` returning a bare `WorkerBackend`) and
  `test_camera_for_default_wiring_reaches_worker_backend_on_darwin`
  (the real, unforced `default_capture_backend_factory` selection with
  `sys.platform` patched to `"darwin"`).
- **Verification command**: `uv run pytest tests/aprilcam/camera tests/aprilcam/daemon -m "not camera and not integration"`
  -- 756 passed on the run that produced this correction (one unrelated
  pre-existing timing flake, `test_streaming.py::test_image_slow_subscriber_drops_frames_without_affecting_a_fast_subscriber`,
  was seen once under full-suite load and confirmed to pass in isolation
  both with and without this ticket's changes -- not touched by this
  diff, which never modifies `daemon/streaming.py`).
