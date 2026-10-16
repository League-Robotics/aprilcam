---
id: '001'
title: Viewer image scales with window resize
status: in-progress
use-cases:
- SUC-001
- SUC-002
depends-on: []
github-issue: ''
issue: camera-and-playfield-view-image-should-scale-with-the-window.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Viewer image scales with window resize

## Description

`TkWindow` (`src/aprilcam/view/viewer.py`) currently packs its image
`tk.Label` without `fill`/`expand` (`__init__`, ~line 565-597), and
`show()` (~line 612) only ever scales a decoded frame *down* when it
exceeds the fixed `_MAX_DISPLAY_WIDTH` (960px). The window's size is
therefore dictated by the last-shown frame, and resizing the window
just adds empty space or clips the image instead of rescaling it. Per
`clasi/issues/camera-and-playfield-view-image-should-scale-with-the-window.md`,
both `aprilcam camera view` and `aprilcam playfield view` should scale
their displayed image to the current window size — enlarging on a
bigger window, shrinking on a smaller one — for both view kinds, with
and without the tag sidebar.

Implement:

1. A pure, display-free fit-size helper, e.g.
   `_fit_size(src_w, src_h, area_w, area_h) -> (int, int)`, that
   computes the largest size preserving `src`'s aspect ratio that fits
   within `area_w x area_h`, scaling up or down as needed (letterbox,
   never stretch/crop). Handle degenerate inputs (zero width/height)
   without raising.
2. Repack the image label/frame with `fill=tk.BOTH, expand=True`
   inside its existing parent (`image_parent` — `self._root` in
   non-sidebar mode, `image_frame` in sidebar mode), while leaving the
   sidebar frame's own packing (`fill=tk.Y`, fixed-width `tk.Text`)
   untouched — the sidebar must not resize with the image area.
   Disable/avoid geometry propagation from the image widget back to
   its parent so the causal direction is strictly window → image,
   never image → window (no grow feedback loop).
3. Cache the last displayed frame's raw bytes (alongside the existing
   `self._photo` `PhotoImage` reference) and bind the window/image
   area's `<Configure>` event to re-render that cached frame through
   `_fit_size` immediately on resize, rather than waiting for the next
   streamed frame.
4. Update `show(data)` to read the image area's current size and pass
   it through `_fit_size` instead of the fixed `_MAX_DISPLAY_WIDTH`
   comparison, keeping the raw `data` bytes and everything sent to the
   daemon untouched (on-screen convenience only — preserve the
   existing "raw means raw" comment's guarantee).
5. Keep a sensible initial window size close to today's behavior
   (frame size capped at ~960px wide) so the window doesn't open huge
   or tiny before any resize.
6. Update `src/aprilcam/view/DESIGN.md` — its current "Frame data
   handed to `Window.show()`" constraint bullet and any other text
   describing the fixed `_MAX_DISPLAY_WIDTH`-only-shrink behavior — to
   describe the new resize-to-fit behavior, and record the design
   rationale for the `<Configure>`-triggered re-render and the
   grow-feedback guard (see sprint.md's Architecture / Design
   Rationale sections for the reasoning to carry over).

## Acceptance Criteria

- [ ] `_fit_size` (or equivalently named helper) is a pure function
      with no Tk/display dependency, covered by unit tests: landscape,
      portrait, and square sources; target areas both larger and
      smaller than the source (upscale and downscale); at least one
      degenerate-input case.
- [ ] Enlarging either view window (`camera view` or `playfield view`)
      enlarges the displayed image; shrinking it shrinks the image;
      aspect ratio is preserved in both directions (no stretch, no
      crop).
- [ ] With the tag sidebar present (`playfield view`, non-`bare`), the
      sidebar's width is unaffected by resizing the window — only the
      image area changes size.
- [ ] Resizing the window does not itself trigger further, unbounded
      window growth (no feedback loop).
- [ ] A resize immediately rescales the currently-displayed frame
      (does not wait for the next streamed frame to catch up).
- [ ] Initial window width for a typical camera frame stays close to
      the existing ~960px-capped behavior.
- [ ] The bytes passed to `show()` and anything sent to/received from
      the daemon are unchanged — no processing beyond on-screen
      display scaling.
- [ ] `src/aprilcam/view/DESIGN.md` is updated to describe the new
      resize behavior in place of the fixed-`_MAX_DISPLAY_WIDTH`
      description.

## Testing

- **Existing tests to run**: `uv run pytest tests/` scoped to the
  `view` module (e.g. `uv run pytest -k viewer` or the project's
  existing `view/` test file(s)), including any `@pytest.mark.camera`
  smoke tests for `TkWindow` if runnable in this environment.
- **New tests to write**:
  - Direct unit tests for the fit-size helper (no display needed) —
    table-driven over the cases listed in Acceptance Criteria.
  - A `@pytest.mark.camera` smoke test for `TkWindow` exercising
    resize → re-render and confirming the sidebar width is unaffected,
    mirroring this module's existing convention for real-window tests
    (e.g. the sidebar roster wiring tests).
- **Verification command**: `uv run pytest`
