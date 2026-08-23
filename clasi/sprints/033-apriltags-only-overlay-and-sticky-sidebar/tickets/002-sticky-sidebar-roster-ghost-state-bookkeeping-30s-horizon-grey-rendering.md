---
id: '002'
title: 'Sticky sidebar roster: ghost-state bookkeeping, 30s horizon, grey rendering'
status: open
use-cases: ['SUC-002']
depends-on: ['001']
github-issue: ''
issue: sidebar-tag-list-sticky-30s-grey-ghosting.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sticky sidebar roster: ghost-state bookkeeping, 30s horizon, grey rendering

## Description

The sprint-032 sidebar rebuilds its row set from each individual
frame's tags, so a tag that misses even one frame (occlusion, ordinary
detection flicker) vanishes and reappears — the list churns instead of
reading like a stable roster
(`clasi/issues/sidebar-tag-list-sticky-30s-grey-ghosting.md`).

Add per-tag-id sticky bookkeeping inside `view/viewer.py`, per sprint
033's architecture (Design Rationale, "the sticky roster's state lives
inside `view/viewer.py` as one new pure class, not a new module"):

- A new, pure, Tk-free structure keyed by `TagId` (family+number),
  holding each tracked tag's latest `TagRecord` plus a monotonic
  last-seen timestamp. The clock is injectable (`time.monotonic` by
  default) so tests control elapsed time without sleeping.
- Given one frame's already AprilTag-filtered live tags (from ticket
  001 — this ticket must not re-filter by family; it trusts ticket
  001's filter and only ever receives AprilTag-family tags on the real
  path), the structure:
  - Refreshes any entry present in the current frame (live, current
    values).
  - Marks any previously-live entry newly absent this frame as
    ghosted, **immediately** (no multi-frame grace period) — its
    last-seen values are retained, not blanked.
  - Drops any entry whose last-seen timestamp is ≥ a new named
    constant `_GHOST_HORIZON_S = 30.0` (documented rationale: the
    stakeholder-specified 30-second retention window,
    `clasi/issues/sidebar-tag-list-sticky-30s-grey-ghosting.md`).
  - Restores a reappearing entry to live with fully refreshed values.
- `TkWindow.update_tags` feeds this frame's tags into the roster and
  renders its current ordered snapshot (live and ghosted rows
  interleaved by tag number — one list, no separate "stale" section)
  into the `tk.Text` sidebar, applying a Tk text tag
  (`tag_add`/`tag_config`) to grey out ghosted rows' lines.
  `_format_tag_row` stays unchanged and pure, reused for both live and
  ghosted rows — do not fold the color decision into it (sprint.md's
  third Design Rationale entry explains why: color is a Tk-rendering
  concern, decided one layer up from the pure row formatter).
- Each per-frame update must touch only currently-tracked/currently-live
  entries — cost proportional to the number of tracked ids, not to
  elapsed wall time — since `update_tags` runs on every streamed frame
  at `_PUMP_INTERVAL_S`'s ~20Hz cadence.

## Acceptance Criteria

- [ ] A tag's row greys out on the very first frame it is absent from
      — no multi-frame grace period before greying.
- [ ] A greyed row keeps its last-seen values (not blanked, not
      zeroed) until dropped.
- [ ] A row is dropped only once at least 30 seconds
      (`_GHOST_HORIZON_S`) have elapsed since its last live sighting.
- [ ] A tag that reappears before the 30-second horizon is fully
      restored to live color and current values, not left grey.
- [ ] Live and ghosted rows share one ordered list by tag number — no
      separate "stale" section.
- [ ] The roster's live/ghost/drop bookkeeping is unit-testable with an
      injected fake clock, with no real Tk window required.
- [ ] An ArUco record is never admitted to the roster (defense in
      depth on top of ticket 001's upstream filter).
- [ ] `test_tkwindow_with_sidebar_builds_a_side_panel_and_updates_it_from_tags`
      is extended to confirm a ghosted row actually renders with the
      grey Tk text tag applied (real widget behavior).
- [ ] `test_run_playfield_view_never_calls_get_tags_only_stream_images`
      continues to pass unmodified.

## Implementation Plan

**Approach**: One new pure roster class/structure inside
`view/viewer.py`, owned and consumed only by `TkWindow` — no new
module (sprint.md's Design Rationale: one consumer, no reason to
split). `_format_tag_row` is reused unchanged; `TkWindow.update_tags`
is reworked to consult the roster and apply per-row Tk styling.

**Files to modify**:
- `src/aprilcam/view/viewer.py`:
  - Add the new roster structure (id-keyed by `TagId`, entries holding
    `TagRecord` + last-seen monotonic timestamp), with an
    `_GHOST_HORIZON_S = 30.0` named constant (documented rationale
    comment, matching this module's existing constant style, e.g.
    `_PUMP_INTERVAL_S`).
  - Wire a roster instance into `TkWindow.__init__` (only meaningful
    when `show_sidebar=True`, but harmless to construct unconditionally
    if simpler).
  - Rework `TkWindow.update_tags` to feed the roster and render its
    ordered live+ghosted snapshot into `self._sidebar_text`, applying
    `tag_add`/`tag_config` per ghosted line for the grey foreground.
- `src/aprilcam/view/DESIGN.md` — describe the sticky roster and its
  30s horizon (via this sprint's seeded `design/` overlay).

**Testing plan**:
- New pure unit tests (no Tk root) against the roster structure
  directly, with an injected fake/controllable clock: tag present →
  live; tag absent one call → immediately ghosted, values retained;
  tag still absent once the fake clock crosses 30s → dropped; tag
  reappearing before the horizon → restored to live with refreshed
  values; ordering by tag number holds across live+ghosted rows in one
  list; an ArUco record, if ever handed to the roster, is not admitted
  (belt-and-suspenders given ticket 001's upstream filter).
- Extend `test_tkwindow_with_sidebar_builds_a_side_panel_and_updates_it_from_tags`
  (`@pytest.mark.camera`) to confirm a ghosted row's line actually
  carries the grey Tk text tag in the real widget.
- Confirm `test_run_playfield_view_never_calls_get_tags_only_stream_images`
  and the rest of `tests/aprilcam/view/test_viewer.py` still pass
  unmodified in substance.
- Run scoped: `uv run pytest tests/aprilcam/view/test_viewer.py`.

**Documentation updates**: `src/aprilcam/view/DESIGN.md` (via the
seeded overlay) — describe the sticky roster, its state machine
(live/ghost/dropped), and the 30s horizon constant.

## Testing

- **Existing tests to run**: `tests/aprilcam/view/test_viewer.py`
  (full file, including the `@pytest.mark.camera` sidebar test).
- **New tests to write**: see Implementation Plan's Testing plan above
  — pure roster state-machine tests with an injected clock, plus the
  extended camera-marked grey-rendering smoke test.
- **Verification command**: `uv run pytest tests/aprilcam/view/test_viewer.py`
