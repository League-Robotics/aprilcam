---
status: done
sprint: '032'
tickets:
- 032-001
- 032-002
- 032-003
---

# `playfield view` should draw tag IDs/hats/center-cross and show a tag-position sidebar, like v1's did

## Description

v2's `aprilcam playfield view` (`src/aprilcam/view/viewer.py` +
`src/aprilcam/client/renderer.py`) is a single Tk window: the deskewed
image with the tag boundary quad and a plain 2px direction line drawn
on it (`renderer.py:154-177`), plus a one-line status bar. That's it —
no tag ID, no world-position readout, no side panel.

v1's `aprilcam view` (`archive/src/aprilcam/cli/view_cli.py` +
`archive/src/aprilcam/ui/display.py`; canonical current copy at
`/Volumes/Proj/proj/RobotProjects/AprilTags/src/aprilcamv1/`) drew a
lot more, and it's what the stakeholder wants back as the default
`playfield view` experience.

**Re-confirmed by the stakeholder 2026-08-22 (second session, direct
quote of intent):** tags on the Playfield view "ought to be exactly
like they are in the AprilTags directory view" — red three-sided box,
green hat on the top edge, yellow cross at the tag center, where that
center "is adjusted by the height and the configuration of the tag,
which is sent by clients." Plus the sidebar listing tag locations and
orientations, like the old view. And: "let's make something a little
prettier out of the one we got here" — v1 is the reference for
*content and semantics*, not a pixel-clone mandate; the presentation
should be cleaned up/modernized.

## On the image itself (v1 reference, mined 2026-08-22)

From `aprilcamv1/ui/display.py:162-278` (all thickness 2, antialiased;
colors given as RGB here — v1 code is BGR):

- **Red open "U"**: three sides of the boundary quad — `p1→p2`,
  `p2→p3`, `p3→p0` — in red `#FF0000`. The top/front edge `p0→p1` is
  **never drawn red**.
- **Green peaked-roof hat** closing the open edge: two green `#00FF00`
  lines from `p0` and `p1` to an apex at
  `mid(p0,p1) + top_dir × clamp(0.35·|p1−p0|, 6px, 80px)`, where
  `top_dir = normalize(mid(p0,p1) − centroid)`. (v2's corner contract
  `types.py:96-99` — `corners[0]→corners[1]` is the front edge — maps
  directly: v1 `p0,p1` = v2 `front_left, front_right`.)
- **Tag ID number** centered on the tag center: red text with a black
  outline (v1: `FONT_HERSHEY_SIMPLEX` scale 0.8, thickness 2, outline
  drawn first at thickness+2).
- **Yellow `#FFFF00` crosshair at the corrected world position** —
  distinct from the raw quad centroid. Arm length = the pixel length
  of 0.5 cm at that spot, min 4 px, 2 px lines.
- (v1 also drew a yellow velocity arrow and a blue gripper-offset dot
  for the registered robot tag, `display.py:206-251` — **not
  requested**; explicitly out of scope for this issue, possible
  follow-up.)

Current v2 scheme is backwards and flattened relative to this: full
4-sided lime box (`renderer.py:97,165`) + single red hat line
(`renderer.py:101,177`). Both shape and color assignment change.

## The corrected center — RESOLVED, no new correction logic

The 2026-08-22 session resolves the formerly-open parallax question:
the crosshair sits at the **height/config-adjusted center**, and v2
**already computes it** (the earlier note "v2 has no equivalent step
yet" was stale — sprint 029 added it):

- Clients register per-tag mount config each session via
  `register_tag` (`MountParameters`: `size_cm`, `mount_x/y/z`,
  `mount_yaw_rad` — `types.py:318-365`); the camera's nadir/height
  come from `camera locate` (`CameraConfig.camera_position`).
- The daemon applies parallax + lever-arm correction in
  `detection.py:918-937` via `correct_tag_pose` /
  `project_tag_to_field_plane` (`calibration/geometry.py:386-433,
  514-542`) **before** records go on the wire.
- So `TagRecord.world` IS the adjusted center. The renderer draws the
  yellow cross at `world` (converted via `world_to_deskewed_pixel`)
  — drawing only, zero new geometry. Note `world_corners` are
  deliberately NOT mount-corrected (`types.py:100-102`), so the red
  U + green hat outline the tag as seen while the cross shows the
  corrected position — that separation is the point of the cross.

## Beside the image: the sidebar

v1's Tk side panel (`view_cli.py:783-955`) had collapsible sections
(Camera Status, Mobile Tags, Stationary Tags, Objects, Paths) with
monospace rows:

```
ID  PxX  PxY   WldX   WldY  Ang  VelX  VelY   <- mobile
ID  PxX  PxY   WldX   WldY  Ang                <- stationary
```

**Decided by the stakeholder (2026-08-22), still standing: no
mobile/stationary split.** One single ordered list of every
currently-detected tag: ID, pixel pos, world pos (cm), orientation
(degrees), velocity when available (columns, not sections). The
sidebar is the main thing wanted back — reading off tag positions and
orientations instead of eyeballing the picture.

"Prettier" latitude for the planner/implementer: v1's web UI ghosted
tags that disappeared (kept the row at 40% opacity instead of dropping
it, `web_server.py:268-288`) — a nice touch worth considering; ditto a
compact camera-status header (FPS, tag count, calibrated). Keep it
tasteful and legible; monospace alignment for the numeric columns.

## The good news: no daemon changes needed, this is view-only

Everything needed is already in v2's `TagRecord`
(`src/aprilcam/types.py:112-126`): `world`, `world_velocity`, `speed`,
`heading_rad`, `yaw_rad`, `world_corners`, `pixel_corners`,
`tag.number`. A deskewed `ImageFrame` already embeds the full
`TagFrame` plus `px_per_cm`/`world_extent`. This is entirely a
`client/renderer.py` + `view/viewer.py` (window layout) change — no
daemon RPC, no new data on the wire.

## Constraint v1 didn't have: no cv2 on the client side

v1's overlay drawing is cv2-baked. v2's client side is deliberately
cv2-free (`tests/test_no_cv2_in_client_modules.py` sweeps `client/`,
`view/`, `mcp/`). So this is **not a port**: the U, hat, ID label, and
crosshair are re-implemented with Pillow `ImageDraw` in `renderer.py`.
The Tk sidebar has no such constraint (plain `tkinter`, like v1).
Mind the sign discipline: `world_to_deskewed_pixel` flips y exactly
once, inside the mapping — no second flip in view code.

## Proposed fix

1. `client/renderer.py`: replace `_draw_tag`'s shape and colors with
   v1's scheme — red open 3-sided U + green peaked-roof hat (apex
   geometry above) — and add the tag ID label (red, dark outline,
   centered), in Pillow.
2. Add the yellow crosshair at `TagRecord.world` converted to deskewed
   pixels (and at a raw-frame equivalent when un-deskewed), arm length
   ≈ 0.5 cm projected, min 4 px. Skip cleanly when `world` is None
   (uncalibrated).
3. `view/viewer.py`: give `TkWindow` a side panel listing all detected
   tags — single ordered list, columns ID / pixel / world / angle /
   velocity — updated from the same streamed `ImageFrame.tags` the
   renderer draws from; no new subscription (preserve the
   `test_run_playfield_view_never_calls_get_tags_only_stream_images`
   contract).
4. Make this the **default** `playfield view` behavior; add `--bare`
   for the plain image-only mode.
5. Update the pinned tests (`tests/aprilcam/client/test_renderer.py`
   geometry/color samples, `tests/aprilcam/view/test_viewer.py`
   `:718`/`:752` boundary-pixel checks) to the new scheme.

## Remaining open questions for design

- Sidebar scope: tags-only for this issue; lay the panel out so
  Objects/Paths sections could be added later without rework.
- Window layout: side panel packs into the same `tk.Tk()` root beside
  the image (v1's `left_frame`/`right_frame` shape), not a second
  window.
