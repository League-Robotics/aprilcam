---
source_file: camera-DESIGN.md
source_hash: a666d6227dc364fb19e726b289ee7747d9617f6824c843a83c9f60893f1309ad
---
# Diff: camera-DESIGN.md

Comparison of the sprint overlay copy of `camera-DESIGN.md` against its pristine (seed-commit) canonical version.

```diff
--- camera-DESIGN.md (pristine)
+++ camera-DESIGN.md (current)
@@ -503,6 +503,136 @@
 whether triggered reactively (a caller's `read()` failing enough
 times) or proactively (this method, called with nobody reading).
 
+### Identity-safe reopen (sprint 039)
+
+**The bug this closes.** Before this sprint, `_attempt_self_heal` and
+`check_stall()`'s respawn both call `self._backend.close()` then
+`self._backend.open()` on the *same* backend object — and for
+`OpenCVBackend`, the OpenCV index is baked in at construction time,
+inside `daemon/service.py`'s `_camera_for` (§4's "Data structures and
+ownership"). `_camera_for` already re-resolves the index fresh every
+open via `_index_for(record)` (`daemon/DESIGN.md`'s `_camera_for`
+docstring: "re-resolves fresh every call ... rather than caching"), but
+that re-resolution ran only once, at the *first* open. Every later
+self-heal or stall-triggered reopen skipped it entirely and reused
+whatever index the backend object was born with — exactly the incident
+the source issue observed: a sibling camera's departure/return shifted
+AVFoundation's positional indices enough that a reopen silently served
+a different physical camera's frames under the original slot's slug.
+
+**The fix: an injected index-resolution hook, not a `Camera`-level
+change.** `OpenCVBackend` (the only backend whose addressing unit — an
+OS-assigned positional index — is unstable across replugs) gains an
+optional constructor-injected `index_resolver: Callable[[],
+Optional[int]]`. When given, `.open()` calls it immediately before
+opening, instead of reusing the index passed at construction; when
+absent (a bare `OpenCVBackend(index)`, as every existing test and
+`testdev/capture_matrix.py` construct it), behavior is unchanged —
+purely additive. `daemon/service.py`'s `_camera_for` wires this
+resolver bound to `record.hardware_key`, reusing the identical
+`_index_for` helper that already backs first-open resolution — so the
+same re-resolution logic now backs every reopen too, not a second,
+parallel implementation of "find the current index for this hardware
+key." If the resolver returns `None` (the hardware key is not currently
+enumerated), `.open()` raises — the existing "`CaptureBackend.open()`
+raises on failure, never a sentinel" contract (§3) — which
+`_attempt_self_heal`/`check_stall`'s existing `except Exception`
+handling already treats as a failed reopen attempt, requiring no new
+error-handling path in `Camera` itself.
+
+**Post-open identity verification, where the backend can tell.** On
+macOS, `OpenCVBackend` additionally accepts an optional
+`expected_hardware_key`; after a successful `cv2.VideoCapture` open, it
+re-resolves the just-opened index's identity (the same
+`cv2-enumerate-cameras`/`avfoundation_unique_id` machinery
+`identity.py` already uses) and compares its `uniqueID` against the
+expected key. A mismatch closes the just-opened session and raises
+loudly (logged at WARNING with both the expected and observed
+identity) rather than completing the open — turning the exact silent-
+substitution failure mode observed in production into an explicit,
+loud reopen failure. Every other identity source (no reliable
+per-index identity read after open) skips this check — it degrades to
+"index was freshly resolved, not verified post-open," which is still
+strictly safer than today's "never re-resolved at all."
+
+**Not-currently-enumerated is a distinct state from
+budget-exhausted.** `MAX_REOPENS`'s existing 10-attempt budget
+(§3) models "plugged in and broken" — a camera that is present but
+keeps failing to open. A hardware key absent from the current
+enumeration is a different condition (unplugged, or mid-bus-event) that
+should not burn through the same fixed budget the way a genuinely
+broken-but-present camera does, per the stakeholder's explicit
+decision: "the watchdog keeps retrying the identity lookup, never a
+stale index." This sprint's tickets decide the exact mechanics (whether
+`_attempt_self_heal` special-cases a `None` resolver result as
+budget-neutral, or whether `daemon/service.py`'s existing
+`check_stalled_cameras` watchdog pass is the layer that keeps polling
+identity independently of `Camera`'s own exhausted-budget state,
+mirroring sprint 038's "exhausted workers get a periodic retry, not
+permanent death" precedent) — see Open Questions below; the contract
+this sprint guarantees regardless of that choice is "never a stale
+index," not a specific budget-accounting scheme.
+
+**Why not have `Camera` itself re-resolve identity.** Considered and
+rejected: `Camera` today has zero dependency on `camera/identity.py` or
+`CameraRegistry` — it is handed an already-resolved backend and knows
+nothing about hardware keys. Giving `Camera` that dependency directly
+would mean every `Camera` construction site (including tests) now
+needs a registry/identity source wired in, and would duplicate
+`daemon/service.py`'s existing `_index_for` re-resolution logic inside
+`camera/camera.py` instead of reusing it. Injecting the resolver as a
+callable keeps `Camera` and `OpenCVBackend` exactly as ignorant of
+*how* an index is found as they are today — they just call a function
+that hands one back — while `daemon/service.py` (which already owns
+the registry and identity source) is the only thing that knows how.
+
+### Honest active-camera health for `probe()`/`list_cameras()` (sprint 039)
+
+**The bug this closes.** `CameraDiscovery.probe()` (§4/`discovery.py`)
+takes an `active_slugs: Callable[[], AbstractSet[str]]` callback and,
+for any slug it names, unconditionally reports `CameraState(...,
+usable=True)` and a `ProbeSkip(reason="in use by a live subscriber")` —
+never consulting whether that held-open `Camera` is actually healthy.
+A camera whose stall watchdog has already given up (`usable=False`,
+per `Camera.state`) still read back `usable=True` from `probe()`,
+because "held open" and "healthy" were conflated.
+
+**The fix: a health accessor replaces a membership set.**
+`CameraDiscovery`'s constructor parameter changes from `active_slugs:
+Callable[[], AbstractSet[str]]` to `active_camera_state: Callable[[str],
+Optional[CameraState]]` — called once per currently-enumerated slug;
+`None` means "not held open" (falls through to the existing
+`_probe_openable` path, unchanged), a real `CameraState` means "held
+open, and here is its actual health." `probe()` reports that
+`CameraState`'s own `usable` value instead of a hardcoded `True`, and
+`ProbeSkip.reason` becomes one of two distinguishable strings —
+`"in use by a live subscriber"` (healthy) vs. `"in use by a live
+subscriber (stalled)"` (unhealthy) — so an operator reading `probe`
+output can tell the two apart without cross-referencing daemon logs.
+`daemon/service.py` wires this to `self._cameras.get(slug)`'s own
+`.state`, the identical `CameraState` `list_cameras()` already
+constructs from the same held-open `Camera` — one source of truth for
+"is this camera actually working," read by both call sites instead of
+`list_cameras()` computing it correctly while `probe()` assumed it.
+
+**Why not have `daemon/service.py` compute `usable` itself and pass a
+bool.** Considered and rejected: `camera/discovery.py`'s `ProbeReport`
+is the documented source of truth for present/usable state (§1's "which
+physical camera is this" ownership); having `daemon/service.py`
+independently decide "usable" for the active-slug case and hand
+`CameraDiscovery` a pre-computed bool would create two divergent
+places computing the same concept for the inactive vs. active case.
+Passing the `CameraState` itself keeps that single source of truth
+inside `camera/` — `daemon/service.py` only ever hands over `Camera`'s
+own already-computed state, never re-derives it.
+
+**This is unrelated to `CameraDiscovery`'s non-disturbance rule
+(§3, UC-25.S-7).** `probe()` still never opens a backend for an active
+slug — it reads `Camera.state`, which the daemon already maintains
+continuously (heartbeat polling, self-heal budget) whether or not
+anyone calls `probe()`. No new camera access is introduced; only what
+`probe()` does with information the daemon already has changes.
+
 ### Concurrency
 
 `Camera._read_lock` (a plain, non-reentrant `Lock`) serializes the
@@ -691,6 +821,20 @@
 process-table/resource-monitoring expectations (documented in Open
 Questions below) — not a migration in the data/schema sense, but worth
 naming for whoever operates the daemon.
+
+## Migration Concerns (sprint 039)
+
+No on-disk schema change, no RPC/proto change. `OpenCVBackend`'s new
+`index_resolver`/`expected_hardware_key` constructor parameters are
+both optional and default to today's behavior (a fixed index, no
+post-open identity check) — every existing direct construction (tests,
+`testdev/capture_matrix.py`) is unaffected until `daemon/service.py`
+explicitly wires them. `CameraDiscovery`'s `active_slugs` ->
+`active_camera_state` constructor-parameter rename is a breaking
+signature change with exactly one production call site
+(`daemon/service.py`'s `DaemonService.__init__`/wherever it constructs
+`CameraDiscovery`) plus test fakes — updated in the same ticket, no
+straddling period where both signatures need to work.
 
 ## 6. Open Questions / Known Limitations
 
@@ -767,3 +911,26 @@
   left as an implementation-time decision (a ticket's job), not an
   architectural one, since it only affects how quickly a respawn
   completes, not whether the design is sound.
+- **`WorkerBackend` is not covered by sprint 039's identity-safe reopen
+  fix.** The fix targets `OpenCVBackend` specifically, since that is
+  the backend whose index is both unstable across replugs and the one
+  implicated in the observed incident. `WorkerBackend` also bakes an
+  index in at construction and would, in principle, suffer the same
+  stale-index-on-respawn defect if a worker-backed camera's index ever
+  shifted between opens — but `WorkerBackend` is selected only behind
+  an off-by-default config/env gate (§4's "Backend selection"), so this
+  is a latent gap in an unreleased path, not a live one, and is left
+  for whichever sprint flips that gate on for real hardware.
+- **The interaction between a not-currently-enumerated hardware key and
+  `MAX_REOPENS`'s fixed budget is a ticket-level decision, not resolved
+  here.** The "Identity-safe reopen" section above states the required
+  outcome (retried by identity, never a stale index, never treated
+  identically to a present-but-broken camera burning through the same
+  10-attempt budget) but leaves open exactly which layer implements the
+  distinction — `Camera._attempt_self_heal` treating a `None` resolver
+  result as budget-neutral, versus `daemon/service.py`'s existing
+  `check_stalled_cameras` watchdog pass independently polling identity
+  on its own interval regardless of `Camera`'s exhausted state (the
+  sprint 038 "exhausted workers get a periodic retry" precedent). Both
+  satisfy the stakeholder-fixed contract; the ticket implementing this
+  fix chooses one.
```
