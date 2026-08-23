---
status: done
sprint: '033'
tickets:
- 033-002
---

# Sidebar tag list should be sticky: grey out a disappeared tag immediately, drop it only after 30 s unseen

## Description

Stakeholder request (2026-08-22 session, verbatim intent): "The list
of tags should be very slow to change... if a tag drops out of a
frame, you don't remove it from the list. You just change its color to
be grey... the list should retain a tag in there for 30 seconds at
least before it drops out. If the tag disappears, you make it grey,
and then 30 seconds later you can take it if you haven't seen it
again."

The sprint-032 sidebar rebuilds the list from each frame's tags, so a
tag that misses a frame (occlusion, detection flicker) vanishes and
reappears — the list churns. It should read like a roster, not a
per-frame dump.

## Behavior spec

Per tag id, keyed on the sidebar's own state (inside `TkWindow`, per
sprint 032's design decision — no new module):

1. Tag present in the current frame's records → row shown in the
   normal foreground color, row data refreshed from the live record.
2. Tag absent from the current frame but seen within the last 30
   seconds → row stays in the list, rendered **grey**, showing its
   last-seen values. Grey-out is immediate on the first absent frame.
3. Tag unseen for ≥ 30 seconds → row removed.
4. Tag reappears at any point before removal → row returns to normal
   color with live values (state fully refreshed).

Ordering stays by tag number, live and ghosted rows interleaved in the
same single list (no separate "stale" section). The 30-second horizon
is a named constant with a documented rationale, in the spirit of the
module's existing presentation constants.

Implementation notes: the sidebar is a `tk.Text`; per-row color needs
Tk text tags (`tag_add`/`tag_config`) or an equivalent — keep the pure
row-formatting helpers from sprint 032 testable without a Tk root, and
keep the last-seen bookkeeping (id → record + monotonic timestamp) in
a pure, unit-testable structure too, with the Tk widget as a thin
consumer. Use a monotonic clock, injectable for tests.

## Constraints

- Interacts with the AprilTags-only filter issue (same sprint is
  fine): ghost state tracks only tags the sidebar displays.
- View-only; cv2-free; single-subscription contract preserved.
