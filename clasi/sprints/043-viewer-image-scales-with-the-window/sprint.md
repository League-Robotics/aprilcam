---
id: '043'
title: Viewer image scales with the window
status: ticketing
branch: sprint/043-viewer-image-scales-with-the-window
use-cases: []
issues:
- camera-and-playfield-view-image-should-scale-with-the-window.md
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

## Problem

Today `show()` (`viewer.py` around line 612) only ever scales a frame
*down* to `_MAX_DISPLAY_WIDTH`, and the image `tk.Label` is packed
without `fill`/`expand` (`__init__` around line 565-597), so the window
size is dictated by the frame and resizing the window just adds empty
space or clips the image rather than rescaling it.

Source issue:
`clasi/issues/camera-and-playfield-view-image-should-scale-with-the-window.md`.

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

## Success Criteria

- Resizing either view window rescales the displayed image to fill the
  image area, preserving aspect ratio, both up and down.
- The tag sidebar (when present) keeps a fixed width; only the image
  area resizes.
- Initial window size stays close to today's behavior (frame size
  capped at ~960px wide).
- No resize feedback loop (window doesn't grow on its own).
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

### Out of Scope

- Any change to what bytes are streamed, sent to the daemon, or
  processed as pixels (raw-means-raw is preserved).
- The collapsible per-layer panel UI noted as a spec/implementation gap
  in `DESIGN.md` — unrelated to this issue.
- Any non-Tk view client.

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
nothing streamed, subscribed, or sent to the daemon changes.

## Architecture

**Compact** — this sprint changes one module (`view/viewer.py`'s
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

### Migration Concerns

None — no data model, no persisted state, no daemon-facing contract
changes. Purely a client-local UI behavior change.

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

Tickets execute serially in the order listed.
