---
status: in-progress
sprint: '043'
tickets:
- 043-001
---

# Camera and playfield view image should scale with the window

## Description

2026-09-26 (stakeholder request): the `aprilcam camera view` and
`aprilcam playfield view` windows should scale the displayed image to
fit the window. Dragging the window larger should enlarge the camera
image; making it smaller should shrink it.

Today `TkWindow` (`src/aprilcam/view/viewer.py`) packs the image
`tk.Label` without `fill`/`expand`, and `show()` only ever scales a
frame *down* to the fixed `_MAX_DISPLAY_WIDTH = 960`. The window size
is therefore dictated by the frame, and resizing the window just adds
empty space (or clips) instead of rescaling the image.

### Needed

- The image area expands/contracts with the window (both raw camera
  view and playfield view, with and without the tag sidebar — the
  sidebar keeps its width, the image area takes the rest).
- Each displayed frame is resized to fit the current image area while
  preserving aspect ratio (letterboxed, no stretch/crop), including
  scaling *up* beyond the frame's native size.
- A sensible initial window size (roughly the current behaviour:
  frame size capped at ~960 px wide) so the window doesn't open huge.
- No feedback loop: a displayed image must not force the widget to
  grow, which would grow the window on every frame. The resize is a
  purely on-screen convenience — the bytes handed to `show()` and
  anything sent to the daemon are untouched (keep the raw-means-raw
  rule in the `_MAX_DISPLAY_WIDTH` comment).
- The fit-size computation should be a pure helper that is unit-
  testable without a display.
