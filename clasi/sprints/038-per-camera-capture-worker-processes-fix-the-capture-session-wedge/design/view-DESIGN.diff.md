---
source_file: view-DESIGN.md
source_hash: e6d674f79b7e783b780f5b0477f8f13b6f8f61d2752b16123ac540b2d68d1e7f
---
# Diff: view-DESIGN.md

Comparison of the sprint overlay copy of `view-DESIGN.md` against its pristine (seed-commit) canonical version.

```diff
--- view-DESIGN.md (pristine)
+++ view-DESIGN.md (current)
@@ -45,7 +45,11 @@
   cadence (0.05s), pumps the window, and calls `Window.update_tags(frame.tags)`
   unconditionally alongside `show`/`set_status` — safe for both view
   kinds because `Window.update_tags`'s no-op default absorbs the call
-  for any window without a panel.
+  for any window without a panel. **Sprint 038**: `run()` also tracks
+  elapsed time since it started with `_latest` still unset; if a
+  bounded `first_frame_timeout_s` elapses with no frame ever received,
+  it raises `FirstFrameTimeout` instead of continuing to pump forever
+  (see §3/§4).
 - **`run_raw_view`** / **`run_playfield_view`** — the two public entry
   points. Each opens a window, subscribes to `daemon.stream_images`
   with the matching `deskewed` flag, builds a view-specific `convert`
@@ -143,6 +147,7 @@
 
 ## 3. Constraints and Invariants
 
+- **A stream that never delivers a first frame fails with a bounded, diagnosable timeout, never an unbounded hang (sprint 038).** Before this sprint, `StreamLoop.run` pumped the window and sampled `_latest` forever regardless of whether any frame had ever arrived — a wedged capture session (or any other reason a stream produces nothing) hung the whole window open-ended, with no error at all, which is exactly the symptom the source issue names as the costliest part of the wedge failure mode. `StreamLoop.run` now measures wall-clock time since it started against `first_frame_timeout_s` (a constructor parameter, defaulted for real use, overridden short in tests) and, if that bound elapses with `_latest` still `None`, closes the stream, joins the reader thread (the same bounded-join discipline already used for ordinary shutdown), and raises `FirstFrameTimeout(camera)` — a plain, local exception, deliberately **not** an `aprilcam.errors.AprilCamError` (mirrors `NoDisplayAvailable`'s existing "this is a client-local condition, not a member of the daemon-facing error taxonomy" pattern one paragraph below). Once any frame has been received, the timeout never fires again for the rest of that stream's life — this bounds only the *first* frame, not an ongoing "is the stream still healthy" check (no such check exists or is added this sprint).
 - **Raw means raw — a deliberate diagnostic property, not a
   limitation.** `run_raw_view`'s `_convert_raw_frame` never reads
   `frame.tags` at all, even though a raw frame may legitimately carry
@@ -206,6 +211,24 @@
 
 ## 4. Design
 
+**First-frame timeout, design rationale (sprint 038).** The bound is
+measured on the *caller's* thread (`run()`'s own pump loop), not inside
+the reader thread's stream iteration, because that is exactly where
+"has anything ever been shown yet" is already being tracked
+(`_latest`) — no new cross-thread signal is needed, only a
+`time.monotonic()` comparison alongside the existing sample-and-pump
+cadence. Considered and rejected: a socket-level timeout in
+`client/streams.py` (would not distinguish "no frame published yet"
+from the reader thread's own already-handled `EOFError`/`OSError`
+paths, and `client/` has no notion of "first" vs. "subsequent" frame to
+begin with — see `camera-DESIGN.md`'s Design Rationale for the fuller
+argument, recorded there since the alternative would have been a
+`client/` change and this sprint's affected-doc list needed to justify
+*not* touching that subsystem). `FirstFrameTimeout` is deliberately not
+threaded through `daemon/interfaces.py`'s `Daemon`/`ImageStream`
+contracts — it never crosses the daemon boundary at all; it is raised
+purely from client-local elapsed time.
+
 **Why streaming replaced polling.** The module docstring records the
 history explicitly: this module (ticket 027-007) supersedes a
 poll-driven design (ticket 022-002) that sprint 022's own docstring
@@ -256,13 +279,22 @@
 
 ### Exposes
 
-- **`run_raw_view(camera, daemon, *, window_factory=TkWindow)`** — the
+- **`run_raw_view(camera, daemon, *, window_factory=TkWindow, first_frame_timeout_s=...)`** — the
   entry point `cli/camera.py`'s `cmd_view` (`aprilcam camera view`)
   hands its connected `Daemon` to. Opens a window titled with the
   `[RAW]` marker, subscribes raw, displays every streamed frame's bytes
   unmodified. Unchanged by sprint 032 ticket 002: its `window_factory`
   call passes no `show_sidebar` kwarg (`TkWindow`'s default, `False`,
-  applies), and its converter still never reads `frame.tags`.
+  applies), and its converter still never reads `frame.tags`. **Sprint
+  038**: propagates `first_frame_timeout_s` straight through to its
+  `StreamLoop`; `FirstFrameTimeout` propagates out of this function
+  exactly like any other stream failure (window closed first, in the
+  same `finally`).
+- **`FirstFrameTimeout(camera: str)`** (**sprint 038**) — a plain,
+  local exception (not `AprilCamError`) raised by `StreamLoop.run` when
+  no frame arrives within the bound. Carries the camera name so a
+  caller's diagnostic can name it without re-threading the value
+  through separately.
 - **`run_playfield_view(camera, daemon, *, renderer_factory=PillowRenderer, window_factory=TkWindow, bare=False)`**
   — the entry point `cli/playfield.py`'s `cmd_view` (`aprilcam
   playfield view`) hands its connected `Daemon` and resolved camera to.
@@ -321,6 +353,13 @@
 
 ## 6. Open Questions / Known Limitations
 
+- **The first-frame timeout only bounds the *first* frame, not an
+  ongoing "stream still healthy" check (sprint 038).** If a stream
+  delivers one frame and then goes silent forever, `StreamLoop` has no
+  mechanism to detect that today, on either side of this sprint — the
+  window simply keeps showing the last frame. Naming this explicitly
+  since the new timeout could be mistaken for a general liveness check
+  when it is deliberately narrower.
 - **Specification.md §4.8 describes "collapsible panels toggle layers
   by name — built-in layers (tags, features) plus each named annotation
   layer," but the current implementation has no such UI.**
```
