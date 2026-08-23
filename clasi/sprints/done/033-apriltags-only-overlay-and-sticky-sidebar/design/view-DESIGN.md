# view/

**Owner:** Eric Busboom · **Last reviewed:** 2026-08-22 · **Status:** stable

---

## 1. Purpose

`view/` is the basic live-view GUI: a tkinter window that subscribes to
one of the daemon's image streams (raw or deskewed) and renders it.
Per specification.md §4.8 and the module's own docstring, it is
deliberately **basic viewing only** — a diagnostic tool for telling a
camera problem from a geometry problem apart, not a general-purpose
image-processing UI. Anything more sophisticated belongs in a separate
program built on the client library (`docs/design/adapters.md` §4.4),
not as a viewer feature. It imports no OpenCV — the daemon serves
already-encoded JPEG/PNG bytes, and this module decodes them with
Pillow only, for display (`docs/design/design.md` §2 principle 1's
vision boundary, extended to the one adapter that touches pixels at
all, to put them on screen rather than to process them).

## 2. Orientation

`view/viewer.py` (~780 lines) is the entire subsystem; `view/__init__.py`
is a docstring pointer to it. Three pieces:

- **`Window`** (base class) / **`TkWindow`** (real implementation) — a
  display surface: encoded bytes in, pixels on screen. `show(data)`
  decodes and displays; `set_status(text)` sets an advisory chrome
  line, never drawn onto the image itself; `pump()` processes one
  toolkit event-loop tick and reports whether the window is still
  open; `close()` is idempotent; `update_tags(tags)` (sprint 032
  ticket 002) refreshes an optional side panel — a **concrete no-op
  default** on `Window` itself (the one deliberate exception to this
  class's raise-`NotImplementedError` convention: a window with no
  panel has no wrong way to handle a tag update), overridden by
  `TkWindow` only when built with `show_sidebar=True`.
- **`StreamLoop`** — the loop plumbing shared by both view kinds. A
  background daemon-mode thread (`_read_stream`) iterates an
  `ImageStream`, converts each yielded `ImageFrame` via a caller-supplied
  `convert` callable into a `DisplayFrame`, and stores it in a
  lock-protected single attribute (`_latest`), replacing — never
  queuing — whatever was there before. The calling thread (Tk's own,
  via `run()`) samples that cell non-blockingly on `_PUMP_INTERVAL_S`'s
  cadence (0.05s), pumps the window, and calls `Window.update_tags(frame.tags)`
  unconditionally alongside `show`/`set_status` — safe for both view
  kinds because `Window.update_tags`'s no-op default absorbs the call
  for any window without a panel.
- **`run_raw_view`** / **`run_playfield_view`** — the two public entry
  points. Each opens a window, subscribes to `daemon.stream_images`
  with the matching `deskewed` flag, builds a view-specific `convert`
  callable (`_convert_raw_frame` / `_make_playfield_converter` /
  `_convert_bare_playfield_frame`), and drives a `StreamLoop`. This is
  the reuse seam: `Window` and `StreamLoop` know nothing about which
  view kind they serve; only the `deskewed` flag and the conversion
  callable differ — with one deliberately narrow exception, below.

`run_playfield_view` subscribes with `deskewed=True` **only** — no
second `get_tags`/`stream_tags` call — because a deskewed `ImageFrame`
already embeds its own `TagFrame` (`frame.tags`) and `AnnotationSet`
(`frame.annotations`); `_make_playfield_converter` hands each frame
straight to `client.renderer.Renderer.render()`, which draws tag
boundaries/hats and annotations from what the frame already carries.

**The tag sidebar and `bare` mode (sprint 032 ticket 002).**
`run_playfield_view` gained a keyword-only `bare: bool = False`. In the
default, annotated mode, the window is built with `show_sidebar=True`
and `DisplayFrame.tags` is populated from the same `frame.tags` the
renderer already reads — no second subscription — feeding a `TkWindow`
side panel (a `tk.Text` in a monospace font, packed into a second
`tk.Frame` beside the image, both inside the one `tk.Tk()` root) that
lists every currently-detected tag, one row per tag, ordered by tag
number: ID (family-abbreviated, e.g. `AT7`/`AR3`), pixel x/y, world x/y
(cm), orientation (degrees, from `yaw_rad`), and speed (cm/s) —
`"--"` for any field a record does not carry. Row formatting
(`_format_tag_row`/`_format_sidebar_text`) is a pure function of a
`TagRecord` tuple, independent of any real Tk widget, so it is
unit-tested without a display; `TkWindow`'s own widget wiring gets a
thin `@pytest.mark.camera` smoke test instead (this project's existing
convention for anything needing a real window).

**AprilTag-family only (sprint 033 ticket 001).** `_make_playfield_converter`'s
`convert()` filters `frame.tags.tags` to `TagFamily.APRILTAG` records
before assigning `DisplayFrame.tags`, so the field's ArUco calibration
corner markers never reach the sidebar — `_format_tag_row`'s own
`AR3`-style abbreviation stays reachable as a pure function (still
exercised directly by its own unit test) but is never invoked from the
real, post-filter `convert()` path any more. This filter is independent
of the equivalent guard in `client/renderer.py`'s `render()` — two
inline checks at the two points that each read a frame's tag list, not
a shared exported predicate (sprint 033 sprint.md Design Rationale).
`_convert_raw_frame`/`_convert_bare_playfield_frame` need no equivalent
filter — neither ever populates `.tags` at all.

`bare=True` is the fully undecorated deskewed stream: the window is
built with `show_sidebar=False`, frames are shown via
`_convert_bare_playfield_frame` (`data=frame.data` unmodified, no
`Renderer.render()` call at all — mirroring `_convert_raw_frame`'s
zero-overhead shape), and `DisplayFrame.tags` stays at its `None`
default. It is a new capability, not a preserved old one: the
lime-box/single-hat overlay `bare` replaces was retired
project-wide, not made optional (sprint.md Design Rationale).

**The sticky sidebar roster (sprint 033 ticket 002).** The sprint-032
sidebar rebuilt its row set from each individual frame's tags, so a tag
missing even one frame (occlusion, ordinary detection flicker) vanished
and reappeared, reading as churn rather than a stable roster
(`clasi/issues/sidebar-tag-list-sticky-30s-grey-ghosting.md`).
`TkWindow` now owns a private `_TagRoster` — a pure, Tk-free structure
keyed by `TagId` (family+number), holding each tracked tag's latest
`TagRecord` plus the monotonic timestamp of its last *live* sighting.
`update_tags` feeds this frame's (already AprilTag-filtered)
`tags` into the roster's `update()` before rendering, and renders the
roster's `snapshot()` — not `tags` directly — so:

- A tag present this frame is (re)admitted live, values fully
  refreshed.
- A previously-tracked tag absent this frame is ghosted *immediately*
  (no multi-frame grace period), its last-seen `TagRecord` retained
  unchanged.
- A tag unseen for `_GHOST_HORIZON_S` (30.0s, a named constant with a
  documented rationale comment — the stakeholder-specified retention
  window) or more, measured from its last live sighting, is dropped.
- A reappearing tag is restored to live with fully refreshed values.

The roster's clock is injectable (`time.monotonic` by default), so its
live/ghost/drop transitions are unit-tested directly with a fake clock
and no Tk root at all. Each `update()` call touches only the current
frame's tags plus the currently-tracked ids — cost proportional to the
number of tracked ids, not to elapsed wall time — since `update_tags`
runs on every streamed frame at `_PUMP_INTERVAL_S`'s ~20Hz cadence.
`_format_tag_row`/`_format_sidebar_text` stay pure and unchanged,
reused for both live and ghosted rows; `TkWindow.update_tags` is the
only place that decides color, applying a Tk text tag (`_GHOST_TK_TAG`,
grey foreground `_GHOST_ROW_COLOR`) via `tag_add`/`tag_remove` to a
ghosted row's line range one layer above the pure formatters (sprint.md
Design Rationale: color is a Tk-rendering concern, never folded into
the row formatter itself). Live and ghosted rows share one list ordered
by tag number — no separate "stale" section. `_TagRoster` never admits
a non-AprilTag record (defense in depth on top of ticket 001's own
upstream filter, which is what the roster otherwise trusts completely
rather than re-filtering).

## 3. Constraints and Invariants

- **Raw means raw — a deliberate diagnostic property, not a
  limitation.** `run_raw_view`'s `_convert_raw_frame` never reads
  `frame.tags` at all, even though a raw frame may legitimately carry
  one (an active ring buffer from an unrelated recent `camera tags`/
  `camera calibrate` call, or detection simply running for another
  reason). No deskew, no homography, and no drawn overlay is ever
  applied on this path — enforced by omission (the function structurally
  cannot draw what it never reads), not by a runtime switch. This
  mirrors `run_playfield_view`'s "never fall back": there is no
  `except` around its `stream_images(deskewed=True)` call, so
  `NotCalibrated` propagates untouched rather than silently
  substituting a raw frame. Together these are UC-16/UC-18's "raw means
  raw, deskewed means deskewed" contract enforced in both directions,
  structurally rather than by discipline.
- **Sprint 031 ticket 004: the raw window title now carries an
  explicit `[RAW]` marker** — `run_raw_view` opens the window as
  `f"aprilcam camera view [RAW] -- {camera}"` (previously just
  `f"aprilcam camera view -- {camera}"`). Confirmed directly in the
  current code and its docstring: this is signposting only — "the
  stream subscription and every byte handed to the window are
  unchanged from before this ticket." It exists because an operator
  hit exactly this confusion in practice, expecting deskew from `camera
  view`; `cli/camera.py`'s `cmd_view` was given a matching printed
  startup line pointing at `playfield view` as the deskewed
  alternative, emitted before any daemon connection is attempted.
- **Older undisplayed frames are dropped, not queued** — `StreamLoop`'s
  single-slot `_latest` cell is a replace, never an append. This
  carries the streaming subsystem's own backpressure model
  (`docs/design/design.md` §5: bounded per-subscriber queues, silent
  drop — a slow consumer loses frames, never stalls the pipeline) one
  layer further, to the one display slot a live-view window has room
  for anyway.
- **No OpenCV, ever** — mechanically enforced by
  `tests/test_no_cv2_in_client_modules.py`.
- **Headless detection is layered, not single-shot.** `TkWindow.__init__`
  first runs a cheap, toolkit-free `_headless_reason()` check (no
  `$DISPLAY`/`$WAYLAND_DISPLAY` on Linux) before tkinter is even asked
  to try, then falls back to catching `tkinter.TclError` from `tk.Tk()`
  itself as the second line of defense for every other headless shape
  (including headless macOS, which uses neither env var). Both raise
  `NoDisplayAvailable` — deliberately **not** an `aprilcam.errors.AprilCamError`,
  since this condition is local to the client process's own
  environment, not part of the daemon-facing error taxonomy.
  `cli/camera.py`/`cli/playfield.py` are the boundaries that catch it
  and print the UC-16.S-3/UC-18-worded message.
- **Frame data handed to `Window.show()` is never mutated for anything
  but on-screen convenience.** `TkWindow.show` scales an image down to
  `_MAX_DISPLAY_WIDTH` (960px) purely as screen real estate — never
  sent back to the daemon, never applied to the underlying bytes, and
  not "processing" in the raw-means-raw sense (no crop, no color
  change, no overlay).
- **Shutdown is bounded.** Closing the window or the reader thread
  ending with an exception both stop `StreamLoop.run` promptly: the
  stream is closed (`stream.close()` — a daemon-visible unsubscribe
  event per `daemon/interfaces.py`'s `ImageStream` docstring) and the
  reader thread is joined with a `_READER_JOIN_TIMEOUT_S` (2.0s) bound,
  mirroring `daemon/stream_transport.py`'s own bounded-join discipline
  — a wedged reader thread blocked in a socket read can never block
  window shutdown forever; it is left to exit as a daemon thread
  instead.

## 4. Design

**Why streaming replaced polling.** The module docstring records the
history explicitly: this module (ticket 027-007) supersedes a
poll-driven design (ticket 022-002) that sprint 022's own docstring
deferred replacing until "the streaming subsystem exists and the two
can be compared" (Decision 3). Once the streaming subsystem existed
(tickets 027-001 through 027-006 — `daemon/broadcast.py`'s fan-out,
`daemon/streaming.py`'s producers, `daemon/stream_transport.py`'s
socket transport, `client/streams.py`'s `SocketImageStream`), this
module is the result of that comparison: both view commands now
subscribe once to `daemon.stream_images(camera, deskewed=...)` instead
of calling `get_frame`/`get_tags` on a fixed-rate timer. For the
playfield view specifically, this is a real simplification over the
old two-call `_fetch_playfield` pattern (`get_tags` then `get_frame`)
— one subscription is everything the renderer needs, on every frame it
is handed, because a deskewed frame already embeds its own tags and
annotations.

**The single timing-constant simplification.** The old `PollLoop`
needed two numbers: a poll interval governing fetch rate, and a
sub-interval purely to notice a mid-poll window close promptly.
`StreamLoop` needs only `_PUMP_INTERVAL_S`, because it now does double
duty as both the window-close responsiveness cadence and the display
refresh cadence — the reader thread already runs at whatever rate the
daemon's stream publishes, independent of the pump. This was ticket
027-007's own explicit instruction: reuse the existing pump cadence
rather than inventing a second one.

**Closing/teardown sequencing.** `StreamLoop.run`'s `finally` block
always closes the stream and joins the reader thread, in that order,
regardless of whether the loop ended by window close, by a re-raised
stream error, or by an exception escaping the loop body — so no reader
thread survives past a `run_raw_view`/`run_playfield_view` call
returning, in ordinary operation. A stream exception is captured on the
reader thread (never allowed to just print a traceback and vanish
silently, which is what a bare thread target would otherwise do) and
re-raised on the caller's own thread only after the loop has fully
stopped.

**The reuse seam.** `Window` and `StreamLoop` are entirely agnostic to
*what* is being viewed; `run_raw_view`/`run_playfield_view` and their
private `_convert_raw_frame`/`_make_playfield_converter` are the only
view-kind-specific code. This is the same seam ticket 022-006
established for the old `PollLoop`, carried forward unchanged onto
`StreamLoop` — only the subscription's `deskewed` flag and the
conversion callable differ between the two views.

## 5. Interfaces

### Exposes

- **`run_raw_view(camera, daemon, *, window_factory=TkWindow)`** — the
  entry point `cli/camera.py`'s `cmd_view` (`aprilcam camera view`)
  hands its connected `Daemon` to. Opens a window titled with the
  `[RAW]` marker, subscribes raw, displays every streamed frame's bytes
  unmodified. Unchanged by sprint 032 ticket 002: its `window_factory`
  call passes no `show_sidebar` kwarg (`TkWindow`'s default, `False`,
  applies), and its converter still never reads `frame.tags`.
- **`run_playfield_view(camera, daemon, *, renderer_factory=PillowRenderer, window_factory=TkWindow, bare=False)`**
  — the entry point `cli/playfield.py`'s `cmd_view` (`aprilcam
  playfield view`) hands its connected `Daemon` and resolved camera to.
  Opens a window subscribed deskewed. When `bare` is `False` (the
  default), the window is built with `show_sidebar=True`, frames are
  rendered through `client/renderer.py`, and `DisplayFrame.tags` feeds
  the sidebar every frame. When `bare` is `True`, the window has no
  sidebar and frames are shown unmodified — no `Renderer` call, no
  `DisplayFrame.tags`. As a side effect (either mode — `bare` only
  changes presentation, not the subscription), opening this view is
  what makes the target camera's ring buffer start filling for as long
  as the window stays open and at least one subscriber remains — the
  streaming subsystem's idle-gating, superseding sprint 022's per-poll
  `get_tags` side effect.
- **`Window.update_tags(tags)`** — refreshes a side panel from the
  current frame's full tag list, or `None`. A concrete no-op on
  `Window` itself (deliberate exception to this module's
  raise-`NotImplementedError` convention); `TkWindow` overrides it to
  feed `tags` into its private `_TagRoster` and render the roster's
  current live+ghosted snapshot (sprint 033 ticket 002 — see this doc's
  "sticky sidebar roster" note above) into its `tk.Text` sidebar, only
  when built with `show_sidebar=True`; otherwise inherits the no-op
  unchanged.
- Both raise `NoDisplayAvailable` before any daemon call is made if no
  display can be opened, and propagate any `aprilcam.errors.AprilCamError`
  raised mid-view (e.g. a mid-session disconnect, or `NotCalibrated` for
  the playfield view) after closing the window — the CLI call sites are
  the boundary that turns either into a use-case-worded message.
- `Window`, `TkWindow`, `StreamLoop`, `DisplayFrame`, `NoDisplayAvailable`,
  `DEFAULT_POLL_HZ` (kept only as the pre-streaming comparison baseline,
  no longer read to drive any timer) are also exported, primarily for
  tests and for a future consumer wanting the same display primitives.

### Consumes

- `aprilcam.daemon.interfaces.Daemon.stream_images(camera, deskewed=)`
  — returns an `aprilcam.daemon.interfaces.ImageStream`; the sole RPC
  this module calls. Frame geometry is stable for the life of the
  stream; backpressure is silent drop from a bounded queue
  (`design.md` §5).
- `aprilcam.client.streams.SocketImageStream` (the concrete `ImageStream`
  a real `Daemon.stream_images` call returns) — a raw, length-prefixed
  socket stream this module iterates but never constructs directly; it
  only ever holds the `ImageStream` interface.
- `aprilcam.client.renderer.Renderer` (concrete) /
  `aprilcam.client.interfaces.Renderer` (interface) — the playfield
  view's rasterizer, injected via `renderer_factory`.
- `aprilcam.types.ImageFrame` — the streamed payload (`data`, `tags`,
  `annotations`, `frame_dark`, `calibration_stale`).
- `aprilcam.errors` — not imported directly, but `NotCalibrated` and
  other `AprilCamError` subclasses are allowed to propagate uncaught
  from the daemon call.
- Call sites: `cli/camera.py`'s `cmd_view` and `cli/playfield.py`'s
  `cmd_view`, both of which connect to a daemon first and pass the
  live `Daemon` in — this module never resolves its own connection.

## 6. Open Questions / Known Limitations

- **Specification.md §4.8 describes "collapsible panels toggle layers
  by name — built-in layers (tags, features) plus each named annotation
  layer," but the current implementation has no such UI.**
  `client/renderer.py`'s `Renderer.render()` does accept a `layers`
  filter parameter (`None` = every layer), but
  `_make_playfield_converter`'s `convert` calls `renderer.render(frame)`
  with no `layers` argument — every layer is always rendered, and
  `view/viewer.py` exposes no widget or state to change that. This is a
  real gap between the spec's aspiration and what the code does today,
  not something this design doc should paper over.
- **`cli/camera.py`'s and `cli/playfield.py`'s own module docstrings
  still describe their view commands as opening "a poll-driven window"**
  ("`cmd_view` ... hands the connected `Daemon` to `view/viewer.py`'s
  `run_raw_view`, which owns the poll loop") — stale wording left over
  from before ticket 027-007's streaming migration; `view/viewer.py`
  itself is unambiguous that it streams and does not poll. Worth a
  documentation pass in `cli/` independent of this doc.
- **No automatic reconnect on stream failure.** If the daemon restarts
  mid-view, the reader thread's exception ends `StreamLoop.run` and
  propagates out of `run_raw_view`/`run_playfield_view`, closing the
  window — there is no in-place resubscribe. Not called out as a defect
  anywhere in the code read for this doc, but worth naming as a
  limitation for anyone assuming the window survives a daemon bounce.
