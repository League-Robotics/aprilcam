---
id: '043'
title: Viewer image scales with the window
status: executing
branch: sprint/043-viewer-image-scales-with-the-window
use-cases: []
issues:
- camera-and-playfield-view-image-should-scale-with-the-window.md
- tags-generate-should-produce-a-single-printable-pdf-like-v1.md
- stream-subscribe-errors-are-lost-so-playfield-view-reports-no-frame-instead-of-not-calibrated.md
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 043: Viewer image scales with the window

## Goals

Make the `aprilcam camera view` and `aprilcam playfield view` windows
(`TkWindow` in `src/aprilcam/view/viewer.py`) scale the displayed image
to the current window size instead of only ever shrinking a frame down
to a fixed `_MAX_DISPLAY_WIDTH` (960px). Dragging the window larger
should enlarge the displayed image; dragging it smaller should shrink
it — for both the raw camera view and the playfield view, with and
without the tag sidebar (the sidebar keeps its fixed width; the image
area takes the rest).

Two further issues were added to this sprint after it was first
planned, each independent of the viewer work:

- **Tag generation as a printable PDF.** Reinstate the v1 labelled
  multi-page PDF tag generator as `aprilcam tags generate` (stakeholder
  decision, 2026-10-16): one tag per 59 x 102 mm label page, a caption
  naming family and id under every tag, no size parameter.
- **Stream subscribe errors reach the client.** When the daemon's
  stream transport fails a subscribe (for example `NotCalibrated`),
  the client must see that typed error instead of a silent close, so
  `aprilcam playfield view` says "not calibrated" rather than "no frame
  received".

## Problem

Today `show()` (`viewer.py` around line 612) only ever scales a frame
*down* to `_MAX_DISPLAY_WIDTH`, and the image `tk.Label` is packed
without `fill`/`expand` (`__init__` around line 565-597), so the window
size is dictated by the frame and resizing the window just adds empty
space or clips the image rather than rescaling it.

Source issue:
`clasi/issues/camera-and-playfield-view-image-should-scale-with-the-window.md`.

**Tag generation regression.** Sprint 028's `aprilcam tags generate`
(UC-21, `cli/tags.py` -> `tags/generator.py`) writes one bare PNG per
id with no quiet zone, no caption, no DPI metadata and a *required*
`--size-cm`. The stakeholder's actual printing workflow (v1,
`archive/src/aprilcam/tags/taggen.py`) is one multi-page PDF of labelled
tags printed at 100% on a label printer, with size an output of printing
rather than an input to generation. Today the stakeholder runs the
archived script by hand. Source issue:
`clasi/issues/tags-generate-should-produce-a-single-printable-pdf-like-v1.md`.

**Lost stream subscribe errors.** `daemon/stream_transport.py`'s
`_Listener._sender_loop` catches an exception from the subscribe
callable, logs it, and closes the socket without sending anything. The
client (`client/streams.py`) sees a bare EOF, `view/viewer.py` times
out and raises `FirstFrameTimeout`, and `cli/playfield.py` prints the
"camera's capture session may be wedged" diagnostic; its
`NotCalibrated` handler (the UC-18.S-2 "calibrate first" pointer) is
never reached. Source issue:
`clasi/issues/stream-subscribe-errors-are-lost-so-playfield-view-reports-no-frame-instead-of-not-calibrated.md`.

## Solution

Give the image area a resizable layout (fill/expand within the window,
sidebar unaffected) and recompute the display size on each resize/each
frame from the current image-area dimensions, letterboxing to preserve
aspect ratio (no stretch/crop), including scaling *up* past the frame's
native size. The fit-size computation is a pure, display-free helper so
it is unit-testable without a Tk root. The raw bytes handed to `show()`
and anything the client sends to the daemon remain untouched — this is
purely an on-screen convenience, per the existing "raw means raw"
comment on `_MAX_DISPLAY_WIDTH`. Guard against a resize feedback loop:
the displayed image must never force the widget to grow the window.

**Tag generation.** `aprilcam tags generate` writes a v1-style PDF by
default: ids spec (`1-8`, `0-3,7`), `-A/--aruco` to choose ArUco 4x4
over AprilTag 36h11, `-o` output path, `--png` as the opt-in
per-id-image alternative. Each page is 59 x 102 mm with the tag centred
inside a small quiet zone and a caption `ArUco 4x4 ID: 3` /
`AprilTag 36h11 ID: 7` beneath it. The v2 implementation is written
fresh against `tags/interfaces.py` (a PDF-sheet method added to
`TagGenerator`), using the page geometry and quiet-zone ratio v1 learned;
v1's hard-coded `ARUCO_POSITIONS` captions are not carried over (they
are stale against `main-playfield.json`). `fpdf2` moves into the base
dependencies. UC-21 and `tags/DESIGN.md` are updated: the deliverable
is a printable sheet and the "PDF output" open question is closed.

**Stream errors.** On subscribe failure the stream transport sends one
typed error frame before closing; `SocketImageStream`/`SocketTagStream`
decode it and raise the matching `AprilCamError` subclass
(`NotCalibrated`, `UnknownCamera`, `CameraNotPresent`) from iteration,
so the existing `cli/playfield.py`/`cli/camera.py` handlers fire.

## Success Criteria

- Resizing either view window rescales the displayed image to fill the
  image area, preserving aspect ratio, both up and down.
- The tag sidebar (when present) keeps a fixed width; only the image
  area resizes.
- Initial window size stays close to today's behavior (frame size
  capped at ~960px wide).
- No resize feedback loop (window doesn't grow on its own).
- `aprilcam tags generate 1-8 -A -o out.pdf` writes one 59 x 102 mm page
  per id with a family-and-id caption under each tag and no `--size-cm`;
  `--png` still produces per-id images; an out-of-range id still errors
  naming the valid range and writes nothing.
- No stale position captions; `fpdf2` is a declared base dependency;
  UC-21 and `tags/DESIGN.md` describe the sheet deliverable.
- `aprilcam playfield view` against an uncalibrated camera prints the
  "not calibrated" message, not "no frame received"; likewise unknown
  and not-present cameras surface their own errors.
- The fit-size computation is covered by unit tests with no display
  required.

## Scope

### In Scope

- `TkWindow.__init__`/`show()` layout and resize handling in
  `src/aprilcam/view/viewer.py`.
- A pure, unit-testable fit-size helper (aspect-preserving, scale up or
  down, given a source size and a target area).
- Updating `src/aprilcam/view/DESIGN.md` to reflect the new resize
  behavior, superseding the current fixed-`_MAX_DISPLAY_WIDTH`
  description.
- Tag generation PDF: `tags/generator.py`, `tags/interfaces.py`,
  `cli/tags.py`, `pyproject.toml` (`fpdf2` to base dependencies),
  UC-21 in `docs/design/use-cases/cli.md`, `src/aprilcam/tags/DESIGN.md`.
- Stream error propagation: `daemon/stream_transport.py` (send a typed
  error frame on subscribe failure), the wire message in
  `proto/aprilcam_v2.proto` plus regenerated `proto/aprilcam_v2_pb2*`,
  `client/streams.py` (decode and raise), and the `stream_transport`/
  `streams` design notes.

### Out of Scope

- Any change to what bytes are streamed, sent to the daemon, or
  processed as pixels (raw-means-raw is preserved).
- The collapsible per-layer panel UI noted as a spec/implementation gap
  in `DESIGN.md` — unrelated to this issue.
- Any non-Tk view client.
- Tag generation: captioning ids with playfield positions (omitted
  entirely), a size parameter on the PDF, multi-up/sheet layouts other
  than one tag per label page, other label sizes.
- Stream errors: mid-stream (post-subscribe) failures, reconnect logic,
  and changing the FirstFrameTimeout diagnostic text itself.

## Test Strategy

Split by testability, per this project's existing convention for
`view/`: the fit-size computation (aspect-preserving scale, given a
source size and a target area, including the up-scale case) is a pure
function of plain numbers, independent of any real Tk widget, so it
gets direct unit tests with no display required — table-driven over
landscape/portrait/square sources, target areas larger and smaller than
the source, and degenerate (zero-height/width) inputs. `TkWindow`'s own
widget wiring (layout, `<Configure>` handling, re-render on resize, no
grow-feedback) gets a `@pytest.mark.camera` smoke test, mirroring how
this module already tests other real-window behavior (e.g. the sidebar
roster wiring). No integration or daemon-facing tests are needed —
nothing streamed, subscribed, or sent to the daemon changes *for the
viewer ticket*.

The tag-generation ticket is tested without hardware or a daemon:
`tests/aprilcam/tags/` covers the PDF (page count and size equal ids and
59 x 102 mm, captions present in the extracted text, no position text,
range validation writes nothing), and
`tests/aprilcam/cli/test_tags_cli.py` covers the CLI shape with the
existing fake-generator pattern. The stream-error ticket uses a loopback
socket pair / fake daemon: `tests/aprilcam/client/test_streams.py` for
the error frame to typed exception mapping, the daemon-side transport
test for the frame being sent, and
`tests/aprilcam/cli/test_playfield_cli.py` for a fake daemon whose
`stream_images` raises `NotCalibrated` surfacing as "not calibrated" in
CLI output (not the FirstFrameTimeout text).

## Architecture

**Sizing (revised when issues 2 and 3 were added): Substantial, no
diagram.** Three independent changes: the viewer (one module,
`view/viewer.py`), tag generation (`tags/` and `cli/tags.py`), and
stream error propagation (`daemon/stream_transport.py`,
`client/streams.py`, plus the wire definition they share). The stream
fix touches the daemon/client wire contract (a new message on an
existing channel), which by the sizing rules pushes the sprint past
compact. No diagram is included: no new dependency edge or composition
exists between the three changes, and the daemon-to-client socket edge
already exists, so a diagram would restate existing structure.

The original viewer analysis follows unchanged: this part changes one module (`view/viewer.py`'s
`TkWindow`) with no new cross-module dependency, no dependency-direction
change, and no data-model change; it revises how one class lays out and
resizes its own widgets. No diagrams are included per the compact
variant of the architecture methodology — there is nothing composed
across modules for a diagram to clarify.

### Architecture Overview

**What changed.** `TkWindow` currently packs `self._image_label` with
no `fill`/`expand` (`__init__`, `viewer.py` ~565-597), and `show()`
(~612) scales a decoded frame down only when it exceeds
`_MAX_DISPLAY_WIDTH` (960px) — the label, and therefore the window,
sizes itself to whatever image was last shown rather than the other
way around. This sprint inverts that: the image area (the whole window
in `bare`/no-sidebar mode, or the `image_frame` beside the sidebar)
becomes the thing that owns the window's usable space, and `show()`
computes the display size *from* that area's current dimensions each
time it renders a frame, rather than from a fixed constant. A new pure
helper — call it `_fit_size(src_w, src_h, area_w, area_h) -> (w, h)` —
does the aspect-preserving fit-to-area arithmetic (letterbox, scale up
or down) so it can be unit-tested without a Tk root; `show()` becomes a
thin caller that reads the current image-area size from the widget,
calls the helper, resizes with Pillow, and keeps its existing
`ImageTk.PhotoImage` reference-retention behavior unchanged.

The label is repacked with `fill=tk.BOTH, expand=True` inside its
parent (`image_parent`, unchanged variable — either `self._root` or the
existing `image_frame` in sidebar mode), and the window is bound to
`<Configure>` so a resize re-renders the *last-shown* frame at the new
size immediately, rather than waiting for the next streamed frame —
`TkWindow` already caches nothing frame-shaped today beyond
`self._photo`, so this sprint adds a small "last raw bytes" cache
alongside it for that purpose. The sidebar frame's own width is left
exactly as configured today (`width=48`-character `tk.Text`, `pack`ed
with `fill=tk.Y` only) — it does not participate in the new
fill/expand behavior, satisfying "sidebar keeps its width."

**Why.** The issue's need is a straightforward layout inversion: today
the image dictates window size; the fix makes the window (image area)
dictate image size, computed by a pure function so the arithmetic is
directly testable. No new module, data structure, or cross-module
contract is needed — `Window`/`StreamLoop`'s existing seam (per
`DESIGN.md` §4, "the reuse seam") is untouched; both view kinds get the
new behavior for free because it lives entirely in `TkWindow`.

**Impact on Existing Components.** None outside `TkWindow` itself.
`StreamLoop`, `run_raw_view`, `run_playfield_view`, the sidebar
roster/formatting functions, and every daemon-facing contract are
unchanged — `show(data)`'s signature and the raw-means-raw guarantee
("bytes handed to `show()` ... are untouched") are preserved exactly;
only how those bytes are *displayed* changes.

### Design Rationale

**Decision: resize triggers a re-render of the cached last frame, not
just a passive relayout.** Context: without an explicit re-render on
`<Configure>`, the image would only rescale on the *next* streamed
frame, which could be up to `_PUMP_INTERVAL_S` (0.05s) away in the
common case but arbitrarily longer if the stream has gone idle —
visibly laggy for a live diagnostic tool. Alternatives considered:
(a) do nothing and accept the lag, rejected because it fails the
issue's explicit "dragging the window ... should enlarge the ...
image" expectation on the very drag that triggered it; (b) force a
synthetic frame fetch on resize, rejected because it would reach back
into the daemon/stream layer for a purely cosmetic concern, breaking
the "no pixel processing changes" scope boundary. Chosen: cache the
last displayed frame's raw bytes (already decoded once for `show()`)
purely for the resize-driven re-render path, and reuse the same
`_fit_size`-based path `show()` uses. Consequence: `TkWindow` gains one
small piece of state (last raw bytes) it did not have before, scoped
entirely to this sprint's ticket.

**Decision: guard against a grow feedback loop by never asking the
image area to size itself off the image.** Context: a label that
requests its own size from its current image content, combined with a
window that then requests its size from its content's requested size,
is exactly the loop the issue calls out ("a displayed image must not
force the widget to grow"). Alternative considered: constrain the
label's size only, leaving the window free to auto-fit — rejected
because Tk's default geometry propagation would still let the window
follow the label. Chosen: keep geometry propagation off for the image
label/frame (or explicitly disable `pack_propagate`/rely on `fill`/
`expand` sizing from the parent rather than the reverse) so the causal
direction is always window → image, never image → window.

### Architecture: Tag generation PDF (`tags/`, `cli/tags.py`)

**What changed.** `tags.interfaces.TagGenerator` gains a PDF-sheet
method (working name `generate_sheet(family, ids, *, out_path) -> Path`)
alongside the existing `generate(...)`, which remains as the `--png`
path. The concrete `tags/generator.py` renders each tag with OpenCV as
today (lazy `_import_cv`, so the base CLI stays importable without the
daemon extra), then lays it out on a 59 x 102 mm page with `fpdf2`:
tag centred, small quiet zone (v1 used a 0.04 ratio so the tag fills the
label), caption centred beneath (`ArUco 4x4 ID: n` / `AprilTag 36h11 ID:
n`). Page geometry, quiet-zone ratio and caption position are module
constants taken from v1's evidence, not v1's code. `cmd_generate`
changes shape: ids spec, `-A/--aruco`, `-o`, `--png`; `--size-cm` is
removed from the PDF path (retained only with `--png`, ticket 002 pins
the default). Id validation (`InvalidTagId`) and writability checks
(`UnwritableOutputPath`) run before anything is written, as today.

**Why.** The printing workflow needs one labelled file; size is measured
after printing. **Impact.** `TagGenerator` consumers: only `cli/tags.py`
(verify `mcp/tools/mobile_tags.py` is unaffected; it uses mount
parameters, not generation). `fpdf2` moves from the `daemon` extra to
base dependencies (pure Python). **Rationale.** Decision: add a method
rather than change `generate`'s signature, so the PNG path and its
tests are untouched; alternative (one method with a format switch)
rejected as it would blur two different return shapes. Captions omit
position: v1's table is stale against `main-playfield.json`, and
deriving from a playfield definition would make an offline tool depend
on config.

### Architecture: Stream subscribe error propagation (`daemon/stream_transport.py`, `client/streams.py`, wire definition)

**What changed.** In `_Listener._sender_loop`, the subscribe `except`
now, before `_drop(conn)`, best-effort sends one length-prefixed error
frame (error kind among `NotCalibrated`, `UnknownCamera`,
`CameraNotPresent`, otherwise a generic daemon error, plus the message
text) and still logs the exception. The frame must be distinguishable
from the `GetFrameResponse`/tag payloads sharing the channel; the exact
encoding (a new `StreamError` message in `proto/aprilcam_v2.proto` with
regenerated modules, plus how it is told apart from data frames) is
ticket 003's decision, constrained to leave successful streams
byte-identical. `SocketImageStream.__iter__` and the tag stream's
equivalent decode the error frame and raise the matching `AprilCamError`
subclass from `aprilcam.errors`, so `viewer.StreamLoop` relays it
(it already relays reader-thread exceptions to `run()`), and the existing
`except NotCalibrated` in `cli/playfield.py` fires.

**Why / Impact.** Without a payload the client cannot tell a failed
subscribe from a wedged camera. Existing consumers of successful
streams are unaffected. A client reading an old daemon sees no error
frame and behaves as today. **Design check.** `streams.py` stays
cv2-free (standing gate) and imports only `aprilcam.errors` additionally,
an already-allowed dependency. Dependency direction is unchanged
(client and daemon both depend on the shared wire/errors, neither on
the other).

### Architecture Self-Review (additions)

Consistency: the three parts are independent and each section matches
its ticket. Codebase alignment: `_sender_loop` and `cmd_generate`
behave as described above. Design quality: `TagGenerator` stays
cohesive (render printable fiducials); the transport gains one
responsibility (report subscribe failure) in the module that owns the
failure. Anti-patterns: none; the one watch item is that the error frame
encoding must not leak into data-frame parsing (tested). Risks: wire
change (mitigated: additive, successful path identical), regenerated pb2
diffs (mechanical). Verdict: APPROVE.

### Migration Concerns

None for the viewer — no data model, no persisted state. Tag generation:
`--size-cm` leaves the default path, so anyone scripting
`tags generate --size-cm N` must add `--png` (UC-21 and DESIGN.md note
this). Stream errors: additive wire message; daemon and client ship
together in this repo, and an old client reading a new daemon's error
frame sees a malformed data frame only in the already-failing
subscribe case.

### Open Questions

- Error-frame discrimination on the shared stream channel (ticket 003
  to settle and record in the transport's docstring).
- Default `--size-cm` for `--png` (ticket 002 to settle).

## Use Cases

Both sprint use cases are refinements of the existing view-command use
cases named in `DESIGN.md` (UC-16 for `camera view`, UC-18 for
`playfield view`) — no new top-level use case, since this is a
presentation refinement of an existing capability, not a new one; kept
brief per the compact tier.

### SUC-001: Resize the camera/playfield view window
Parent: UC-16 / UC-18

- **Actor**: Operator running `aprilcam camera view` or
  `aprilcam playfield view`.
- **Preconditions**: A view window is open and showing a frame (with or
  without the tag sidebar).
- **Main Flow**:
  1. Operator drags a window edge/corner to resize the window.
  2. The image area (window minus any sidebar) is recomputed; the
     currently-displayed frame is rescaled to fit it, preserving aspect
     ratio, letterboxed rather than stretched or cropped.
  3. Any sidebar present keeps its existing fixed width unaffected by
     the resize.
- **Postconditions**: The displayed image visually fills the resized
  image area (up to letterboxing for aspect mismatch); the window does
  not grow again on its own as a result.
- **Acceptance Criteria**:
  - [ ] Enlarging the window enlarges the displayed image (upscale
        beyond the frame's native size works).
  - [ ] Shrinking the window shrinks the displayed image.
  - [ ] Aspect ratio is preserved (no stretch, no crop) in both
        directions.
  - [ ] With the sidebar present (`playfield view`, non-`bare`), the
        sidebar's width is unaffected by the resize.
  - [ ] No feedback loop: resizing the window does not itself trigger
        further, unbounded window growth.

### SUC-002: Open a view window at a sensible initial size
Parent: UC-16 / UC-18

- **Actor**: Operator running `aprilcam camera view` or
  `aprilcam playfield view`.
- **Preconditions**: None (window not yet open).
- **Main Flow**:
  1. Operator runs the view command.
  2. The window opens sized close to today's behavior — the frame's
     size capped at roughly 960px wide — rather than opening
     unexpectedly large or tiny.
- **Postconditions**: The window is immediately usable at a familiar
  size; the operator can then resize per SUC-001.
- **Acceptance Criteria**:
  - [ ] Initial window width is close to the existing ~960px-capped
        behavior for a typical camera frame size.

### SUC-003: Print a labelled tag sheet
Parent: UC-21

- **Actor**: Developer building or repairing a physical playfield.
- **Preconditions**: None; offline (cv2 available, as UC-21 already requires).
- **Main Flow**: Developer runs `aprilcam tags generate 1-8 -A -o tags.pdf`;
  the command writes one PDF, one tag per 59 x 102 mm page with a
  family-and-id caption beneath; developer prints at 100%.
- **Postconditions**: One PDF exists; nothing else changes.
- **Acceptance Criteria**:
  - [ ] Page count equals the number of ids; page size is 59 x 102 mm.
  - [ ] Every page's caption names family and id, with no position text.
  - [ ] An out-of-range id errors naming the valid range and writes nothing.
  - [ ] `--png` still writes per-id images.

### SUC-004: See why a stream view failed
Parent: UC-18 / UC-16

- **Actor**: Operator running `aprilcam playfield view` (or `camera view`).
- **Preconditions**: The named camera is not calibrated (or unknown, or not present).
- **Main Flow**: Operator runs the view command; the daemon rejects the
  subscribe and reports the typed reason; the CLI prints the
  corresponding message ("not calibrated -- calibrate first", unknown
  camera, camera not present) and exits non-zero.
- **Postconditions**: No misleading "capture session may be wedged" text.
- **Acceptance Criteria**:
  - [ ] A subscribe failure arrives at the client as the matching typed error.
  - [ ] `playfield view` on an uncalibrated camera prints "not calibrated".
  - [ ] Successful streams are unchanged.

## GitHub Issues

(GitHub issues linked to this sprint's tickets. Format: `owner/repo#N`.)

## Definition of Ready

Before tickets can be created, all of the following must be true:

- [ ] Sprint planning document is complete (sprint.md, including its
      Architecture and Use Cases sections)
- [ ] Architecture review passed (or skipped, for changes with no
      architectural impact)
- [ ] Stakeholder has approved the sprint plan

## Tickets

| # | Title | Depends On |
|---|-------|------------|
| 001 | Viewer image scales with window resize | (none) |
| 002 | Tag generation as a labelled printable PDF | (none) |
| 003 | Propagate stream subscribe errors to the client | (none) |

Tickets execute serially in the order listed.
