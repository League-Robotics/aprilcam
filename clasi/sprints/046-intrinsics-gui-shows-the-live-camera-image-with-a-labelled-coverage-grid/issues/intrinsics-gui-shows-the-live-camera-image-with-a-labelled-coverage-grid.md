---
status: in-progress
sprint: '046'
tickets:
- 046-001
- 046-002
- 046-003
- 046-004
---

# Intrinsics GUI: show the live camera image with a labelled coverage grid and the detected board

## Description

Stakeholder request, 2026-10-16, after opening `aprilcam camera lens` for
the first time: the fullscreen window looks good and reads from across the
room, but the 3x3 coverage tiles are abstract. With several cameras it is
hard to tell which camera a column belongs to, and "northeast" versus
"southwest" is hard to map onto the real room when the camera is mounted
rotated. The window is missing an obvious opportunity: it should **show the
camera's live image** and draw the coverage grid **on the image**.

## What to build

- **Live image per camera column.** The GUI polls `get_frame` (raw JPEG,
  cv2-free, decoded with Pillow) at a modest rate (about 4 to 5 fps) in the
  same poller thread that already polls `get_intrinsics_status`, and draws
  the frame scaled to fit the column, aspect preserved. A dropped frame or
  connection shows the last image dimmed plus the existing error banner.
  The daemon's lens session already reads the camera at a capped rate; the
  GUI's frame polling must not disturb it (frames are the same `Camera.read()`
  path the session uses; the daemon arbitrates).
- **Grid on the image, labelled.** The 3x3 coverage grid is drawn over the
  image with the same outer-40 % band geometry the collector uses, each cell
  labelled by compass position (NW, N, NE, W, C, E, SW, S, SE) in large
  type; cell lines and labels readable from 10 feet.
- **Board outline.** The daemon's status gains the current frame's detected
  board polygon(s) in native pixels (additive, optional `board_outline`
  field on `IntrinsicsStatus`, from the detector's cluster corners), and the
  GUI draws it on the image so the operator sees where the board is being
  seen, and whether it is being seen at all. The last reject reason is
  shown next to it (moving / blurry / not novel).
- **Tint on success.** A cell tints green as soon as its coverage count
  reaches the collector's "covered" threshold (the same rule the tiles use
  today); partially covered cells use the intermediate colour the tiles
  already use; the tilt tiles stay as they are but move next to the image.
- **Existing behaviour unchanged:** banner, "error now" / "could still
  change by", NEXT line, Enter/Esc, never auto-quits, `--no-gui` text mode
  untouched, `view/` and `client/` stay cv2-free. The pure view-model in
  `view/intrinsics_model.py` grows the grid geometry and cell-colour
  decisions so they are tested without a display.

## Acceptance

- `aprilcam camera lens 2 4` shows two columns, each with the live image of
  its camera, a labelled 3x3 grid on the image, and the board outline
  following the board as it moves.
- Cells go green exactly when the corresponding tile would today.
- Frame polling at about 4 to 5 fps does not change the session's view
  acceptance rate noticeably (compare view counts per minute with and
  without the image, noted in the live check).
- No cv2 in `view/` or `client/`; the no-cv2 test passes.
- Hardware-free tests cover grid geometry, labels, cell colours, outline
  scaling into the column, and the status field round trip.
