---
id: '003'
title: 'GUI: live image per column with grid, labels, outline and tint'
status: open
use-cases: [SUC-001, SUC-002, SUC-003]
depends-on: ['001', '002']
github-issue: ''
issue: intrinsics-gui-shows-the-live-camera-image-with-a-labelled-coverage-grid.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# GUI: live image per column with grid, labels, outline and tint

## Description

Show each camera's live image in its column with the labelled grid, board
outline, reject note and cell tints, using Pillow only
(sprint.md Architecture What Changed item 7, Decisions 2-4).

## Implementation Plan

Approach:
- `IntrinsicsController.step()`: after the status poll, fetch
  `daemon.get_frame(cam)` per owned camera at most every ~0.22 s (rate limit
  by monotonic time, injectable clock for tests); on success store the JPEG
  bytes in the lock-protected slot and clear that camera's frame error; on
  failure keep the previous bytes and mark `frame_stale` for that camera. A
  frame failure must not mask or delay the status result, and must feed the
  existing error banner path. `Snapshot` gains `frames` and `frame_stale`.
- New pure function `compose_camera_image(jpeg_bytes, camera_model, board_outline,
  image_size, dest_size, *, dimmed)` (in `intrinsics_gui.py` or a small
  sibling `view/intrinsics_overlay.py` if the GUI module grows unwieldy)
  returning a PIL image: decode with Pillow, resize aspect-preserved with the
  model's `fit_rect`, draw translucent cell tints, grid lines, large NW..SE
  labels (font scaled from `font_sizes`), the outline scaled with
  `scale_polygons`, and dim toward the background when `dimmed`. A decode
  failure returns the last good image dimmed (never raises into Tk).
- `_TkShell._draw_camera`: allocate the image rectangle in the column, paint the
  composed `ImageTk.PhotoImage` (keep a reference to avoid GC), draw the reject
  note next to the outline area, and move the tilt tiles next to the image
  (below it in the column). Remove the 3x3 tile block only if the image is
  present; with no frame yet, keep the tiles as the fallback so the window is
  never empty.
- Unchanged: banner, "error now" / "could still change by", NEXT line, Enter/Esc,
  never auto-quit, `--no-gui` path, abort-on-exit.

Files: `src/aprilcam/view/intrinsics_gui.py` (and optional overlay module),
`tests/aprilcam/view/test_intrinsics_gui.py`.

## Acceptance Criteria

- [ ] Poller fetches frames at about 4-5 fps per camera without slowing status
      polling (rate limit tested with a fake clock).
- [ ] Composed image on a synthetic PIL image has: grid lines at the 40/20/40
      positions, tinted cells matching tile levels, outline pixels where
      expected (spot-check pixel colours), correct output size/aspect.
- [ ] Frame fetch failure keeps the last image and marks it dimmed; recovery
      undims; decode failure does not raise.
- [ ] Tilt tiles remain visible next to the image; existing GUI tests pass.
- [ ] No cv2 in `view/`; no-cv2 test passes; no display needed by any test.

## Testing

- **Existing tests to run**: `tests/aprilcam/view/`, `tests/aprilcam/cli/` lens tests, `tests/test_no_cv2_in_client_modules.py`
- **New tests to write**: controller frame polling/rate/failure with fake daemon returning synthetic JPEG bytes (made with Pillow); overlay composition pixel checks
- **Verification command**: `uv run pytest tests/aprilcam/view tests/test_no_cv2_in_client_modules.py`
