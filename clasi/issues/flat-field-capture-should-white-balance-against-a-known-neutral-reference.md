---
status: pending
---

# Flat-field capture should set white balance from a known-neutral reference, not assume the sheet is neutral

## Description

Sprint 037's `aprilcam camera flatfield` builds the per-channel gain map
by normalising every channel of the white-sheet capture to the same grey.
That silently assumes the sheet is neutral **as this camera sees it at
the capture exposure**. It is not: on camera 3 (`arducam-ov9782-usb-camera`,
2026-09-02) the tarp read B152 G110 R152 while ordinary white printer
paper (the ArUco/AprilTag borders) at the working exposure read roughly
B220 G200 R225. Forcing the tarp to grey therefore over-boosted green by
about 20%, and the corrected frames swapped the original magenta cast for
a green one (stakeholder-visible; two white references disagreeing by
this much means a single global colour scalar is missing, not that the
spatial map is wrong).

Verified fix, done by hand the same day: sample the white paper of every
detected tag in a raw frame at the **working** exposure, push those
pixels through the map, take the median mapped white, and scale the map's
channels so that white is neutral (scalars B 1.106, G 0.870, R 1.055 for
camera 3). After that, unclipped whites across the field agree to within
±4% in G/R, wood reads a natural brown, and the map's gain range became
0.79-2.37. The rebalanced map is what is installed; the tarp-neutral one
is kept beside it as `flatfield.npy.tarp-neutral.bak`.

### Needed

1. **A white-balance step inside `capture_flat_field`**, after the
   spatial map is built and the working config is restored: capture one
   frame at working exposure, locate the linked playfield's tags (they
   are always in view and their borders are known white), sample
   unclipped paper pixels through the new map, and fold the per-channel
   balance scalars into the persisted map. Record the scalars and the
   reference used in `flatfield.json`. Refuse (report, keep the unbalanced
   map, warn) if too few unclipped white pixels are found -- e.g. every
   tag border clips at the working exposure.
2. **An explicit alternative reference**: `--neutral-reference sheet`
   (today's behaviour) vs `tags` (default), so a genuinely neutral grey
   card can still be used when the field has no tags.
3. **Report the balance** in the CLI/MCP result (the three scalars and the
   post-balance white spread), and show `balanced: yes/no` in
   `get_flatfield`.
4. **Clipping guidance in the report**: white paper already clips at
   camera 3's working exposure (exposure 6) before any map is applied,
   and the map's >1 gains push more of the field into saturation. The
   capture report should say what fraction of the tag paper clips at the
   working exposure so an operator doing colour work knows to drop the
   exposure.
5. Tests: synthetic frames with a tinted sheet and neutral tags must come
   out neutral; a fully-clipped tag set must produce the warning path.

### Out of scope

Per-pixel colour non-uniformity beyond what the sheet captures (faint
residual blotches remain on camera 3's mat after balancing); replacing
the camera's hardware white-balance setting.
