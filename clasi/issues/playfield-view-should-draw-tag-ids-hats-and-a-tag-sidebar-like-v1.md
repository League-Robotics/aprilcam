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
  the boundary quad drawn in **red** (a "U" shape, open on the front
  edge) -- `p1-p2`, `p2-p3`, `p3-p0` -- and the open front edge closed
  by two **green** lines up to a roof apex projected outward from the
  front-edge midpoint (`display.py:184-202`). Confirmed directly by
  the stakeholder (2026-08-22): red U + green hat, matching v1 exactly
  -- **not** v2's current scheme, which is backwards and flattened:
  the full 4-sided boundary drawn in green/lime
  (`renderer.py:97,165` -- `_BOUNDARY_COLOR = "lime"`,
  `draw.polygon(...)`) plus a single 2px **red** line standing in for
  the hat (`renderer.py:101,177` -- `_HAT_COLOR = "red"`). Both the
  shape (full box vs. open U + peaked roof) and the color assignment
  need to change, not just the shape.
- A separate **yellow crosshair** at the tag's *corrected* world
  position -- distinct from the raw boundary-quad center. Confirmed by
  the stakeholder that "corrected" here means two things, both real
  and already-relevant to v2, not hypothetical: (a) **mount/lever-arm
  correction** -- when the tag is mounted on a robot, the tag's own
  pixel center is not where the robot actually is, and v2 *already*
  applies this correction to `TagRecord.world` for a registered mobile
  tag (`register_mobile_tag`, `types.py`'s "Mount corrections... are
  already applied" docstring) -- the crosshair should show this
  already-corrected `world` position, not the raw tag center, and this
  part needs no new correction logic, only new drawing. (b) **parallax
  /height correction** -- v1 also reprojected for a tag's height above
  the field plane (`display.py:258-278`, "Yellow cross at the
  height-adjusted world position"); v2 has no equivalent step yet (see
  the open question below, now sharpened: (a) is a confirmed
  requirement, (b) is still an open design question, not both equally
  open as originally written).
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

**Decided by the stakeholder (2026-08-22), resolving the mobile/
stationary open question below**: do **not** split the list into
separate mobile/stationary sections the way v1 did. Show **one single
ordered list of every currently-detected tag** with its position --
same information (ID, position, etc.), just not partitioned. This
replaces v1's `_fmt_mobile_row`/`_fmt_stat_row` two-table layout with
one table/list covering all tags.

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

1. `client/renderer.py`: extend `_draw_tag` to also draw the tag ID
   number, and replace the current shape *and* colors with v1's
   scheme: a red, open, 3-sided "U" boundary (not the current full
   green/lime 4-sided box) closed by a green peaked-roof hat (not the
   current single red line) -- using Pillow, per the module's existing
   cv2-free constraint.
2. Add a yellow crosshair at each tag's *corrected* world-position
   pixel (distinct from the boundary-quad center), using the already-
   mount-corrected `TagRecord.world` -- confirmed, this needs no new
   correction logic for the lever-arm/mount-offset case, only drawing.
   Height/parallax correction (v1's `calibration/calibration.py`
   equivalent) remains a separate, still-open design question: v2 has
   no such reprojection step today, so decide whether this issue adds
   it or the crosshair ships without it for now, sitting at the
   already-corrected (mount-only) position.
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

- **Mobile/stationary split -- RESOLVED, no split** (see above): show
  one ordered list of all tags. v1's registry-vs-heuristic distinction
  (`_classify_tag_mobility`, `view_cli.py:262-302`) is now moot for
  the sidebar's own layout, though `world_velocity`/`speed` should
  still be shown per-tag when available (a column, not a section).
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
