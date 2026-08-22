---
status: pending
---

# `playfield view` should draw tag IDs/hats/center-cross and show a tag-position sidebar, like v1's did

## Description

v2's `aprilcam playfield view` (`src/aprilcam/view/viewer.py` +
`src/aprilcam/client/renderer.py`) is a single Tk window: the deskewed
image with the tag boundary quad and a plain 2px direction line drawn
on it (`renderer.py:154-177`), plus a one-line status bar. That's it —
no tag ID, no world-position readout, no side panel.

v1's `aprilcam view` (`archive/src/aprilcam/cli/view_cli.py` +
`archive/src/aprilcam/ui/display.py`) drew a lot more, and it's what I
actually want back as the default `playfield view` experience:

**On the image itself** (`ui/display.py:162-278`, cv2-drawn):
- The tag ID number, centered on the tag (`display.py:252-257`,
  `cv2.FONT_HERSHEY_SIMPLEX`, red with a black outline).
- The "hat" is not a line — it's a **house pentagon**: three sides of
  the boundary quad drawn normally, and the front edge replaced by two
  lines up to a roof apex projected outward from the front-edge
  midpoint (`display.py:184-202`). That peaked-roof shape is what
  reads as "which way is this tag facing" at a glance; v2's single
  line is a much weaker version of the same idea.
- A separate **yellow crosshair** at the tag's *reprojected,
  height/parallax-corrected* world position (`display.py:258-278`,
  comment: "Yellow cross at the height-adjusted world position") --
  distinct from the raw boundary-quad center. This is what I mean by
  "cross-adjusted position of the projected position of the center of
  the tag."
- (v1 also drew a velocity arrow and a gripper-offset marker for
  registered robot tags -- `display.py:206-251` -- listed here for
  completeness, not requested above; see open questions.)

**Beside the image**, a genuine Tk side panel (`view_cli.py:783-955`,
`CollapsibleFrame` sections), not baked into the image: Camera Status,
Mobile Tags, Stationary Tags, Objects, and Paths, each a scrollable
monospace `tk.Text` updated ~30x/sec. The tag rows
(`_fmt_mobile_row`/`_fmt_stat_row`, `view_cli.py:306-337`):

```
ID  PxX  PxY   WldX   WldY  Ang  VelX  VelY   <- mobile
ID  PxX  PxY   WldX   WldY  Ang                <- stationary
```

pixel center, world X/Y (cm, A1-centred), orientation in degrees, and
for mobile tags, velocity. This sidebar is the main thing I want back
-- it's what makes the window useful for actually reading off tag
positions instead of just eyeballing a picture.

## The good news: no daemon changes needed, this is view-only

Everything the sidebar needs is already in v2's `TagRecord`
(`src/aprilcam/types.py:74-126`): `world`, `world_velocity`, `speed`,
`heading_rad`, `yaw_rad`, `tag.number`. A deskewed `ImageFrame` already
embeds the full `TagFrame` (`viewer.py`'s own docstring). This is
entirely a `client/renderer.py` + `view/viewer.py` (window layout)
change -- no daemon RPC, no new data on the wire.

## Constraint v1 didn't have: no cv2 on the client side

v1's overlay drawing (`ui/display.py`) is done with `cv2` calls baked
directly into the pixel buffer. v2's client-side code is deliberately
cv2-free (`tests/test_no_cv2_in_client_modules.py` sweeps
`client/`/`view/`; `renderer.py`'s own docstring states this
explicitly, per `docs/design/adapters.md`'s "adapters do no vision"
rule -- OpenCV is a daemon-only dependency, project-overview.md). So
this is **not a port** of `ui/display.py` -- the pentagon hat, ID
label, and crosshair all need re-implementing with Pillow's
`ImageDraw` in `renderer.py`, alongside the existing boundary-quad
code. The Tk sidebar itself has no such constraint (plain `tkinter`
widgets, same as v1); that part can follow v1's structure much more
directly.

## Proposed fix

1. `client/renderer.py`: extend `_draw_tag` (or add sibling draw
   calls) to also draw the tag ID number and replace the current
   straight hat-line with the house-pentagon shape, using Pillow.
2. Add a yellow crosshair at each tag's reprojected world-position
   pixel (distinct from the boundary-quad center) -- v2 doesn't
   currently do any "height-adjusted" reprojection step at all
   (unlike v1's parallax correction in `calibration/calibration.py`
   there), so confirm what "cross-adjusted position" should mean here:
   if v2 has no parallax/height correction yet, the cross may simply
   sit at the same center v1 used pre-correction, or this may motivate
   adding that correction -- needs a design decision, not an
   implementation guess.
3. `view/viewer.py`: give `TkWindow` a side panel (Tk `Text`
   widget(s), scrollable, monospace) listing tag ID / pixel pos /
   world pos / orientation, sourced from the same streamed
   `ImageFrame.tags` the renderer already draws from -- no new
   subscription needed.
4. Make this the **default** `playfield view` behavior (matches "I
   really need that view... that's what it should be by default").
   Add a flag (e.g. `--bare`) for the plain deskewed-image-only mode,
   for whoever just wants the picture.

## Open questions for design

- **Mobile/stationary split**: v1 classified this per-frame with a
  velocity-threshold heuristic (`_classify_tag_mobility`,
  `view_cli.py:262-302`, promote/timeout constants). v2 instead has an
  explicit mobile-tag *registry* (`register_mobile_tag`/
  `list_mobile_tags`/`clear_mobile_tags`, per the aprilcam MCP tool
  set) -- a different mechanism entirely. Should the sidebar split on
  registry membership instead of v1's heuristic, or drop the split and
  just show one list with a velocity column?
- Scope of the sidebar: v1's panel also had Objects and Paths sections
  (`view_cli.py:863-955`). v2 has daemon-side concepts for both
  already (`get_objects`, `list_paths`). Is this issue tags-only, or
  should the panel be built to hold those sections too from the start?
- Velocity arrow and gripper-offset marker (`display.py:206-251`) --
  in scope for this issue, or a separate follow-up? Not explicitly
  asked for above.
- Window layout: v1 packed `left_frame`/`right_frame` side by side in
  one `tk.Tk()` root (`view_cli.py:783-788`). Confirm `TkWindow`
  (currently a single `Label` + status line, `viewer.py:282-352`) grows
  to match that shape rather than a separate second window.
