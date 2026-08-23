---
id: '002'
title: 'TkWindow tag sidebar and viewer wiring (bare mode, DisplayFrame.tags)'
status: open
use-cases:
- SUC-002
- SUC-003
depends-on:
- '001'
github-issue: ''
issue: playfield-view-should-draw-tag-ids-hats-and-a-tag-sidebar-like-v1.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# TkWindow tag sidebar and viewer wiring (bare mode, DisplayFrame.tags)

## Description

Add a tag sidebar to `TkWindow` (`view/viewer.py`) and wire
`run_playfield_view` to populate it and to support a new `bare` mode —
building on ticket 001's reworked overlay (the sidebar and the overlay
are drawn from the same `TagRecord`s, so this ticket assumes ticket
001's renderer is in place for the end-to-end geometry tests below).

**`DisplayFrame`** (`:194-201`) gains one new field:

```python
@dataclass(frozen=True, kw_only=True)
class DisplayFrame:
    data: bytes
    status: Optional[str] = None
    tags: Optional[Tuple[TagRecord, ...]] = None  # new
```

Additive and default-`None` — every existing construction site
(`_convert_raw_frame`) keeps compiling and behaving identically without
being touched.

**`Window`** (`:204-236`) gains one new method:

```python
def update_tags(self, tags: Optional[Tuple[TagRecord, ...]]) -> None:
    """Refresh any side panel with *tags* -- the current frame's full
    tag list, or None. Concrete no-op default (deliberate exception to
    this class's raise-NotImplementedError convention -- see sprint.md
    Design Rationale): a window with no side panel has no wrong way to
    handle a tag update."""
    return None
```

Document the exception explicitly in the docstring (word it as above or
equivalent) so it doesn't read as an accidental omission of
`NotImplementedError`.

**`TkWindow.__init__`** gains a keyword-only `show_sidebar: bool =
False`. When `True`, build a side panel next to the existing image
label, packed into the *same* `tk.Tk()` root (v1's `left_frame`/
`right_frame` shape, `view_cli.py:783-955` for reference — do not port
its widget code, just its "two frames side by side in one root" layout
idea): e.g. `image_frame = tk.Frame(root); image_frame.pack(side=tk.LEFT)`
holding the existing `_image_label`/status label, and
`sidebar_frame = tk.Frame(root); sidebar_frame.pack(side=tk.RIGHT,
fill=tk.Y)` holding the tag table. A `tk.Text` or a grid of `tk.Label`s
in a monospace font (e.g. `("Courier", 10)`) both work — a
`ttk.Treeview` is also reasonable if it renders headers cleanly; pick
whichever is simplest to keep in sync from `update_tags` and to test
headlessly is not required (Tk widget content can be asserted via
`.get()`/`cget` in a test harness that still needs a real, even if
off-screen, Tk root — match however this project's existing Tk tests,
if any, handle that; if none exist, keep the sidebar's row-formatting
logic in a small pure function so it can be unit-tested without
instantiating Tk at all, and cover the widget wiring with a thin
smoke-level check).

**Row content**, one row per tag in `tags`, ordered by
`tag.tag.number` (no mobile/stationary split — stakeholder-decided,
sprint.md Scope):

| Column | Source | When absent |
|---|---|---|
| ID | `record.tag.number` (and family, if both families are ever mixed on one playfield — check `record.tag.family` too; the mined v1 panel only showed AprilTag ids, but don't hide an ArUco id either) | always present |
| Pixel X/Y | `record.pixel.x`, `record.pixel.y` | `"--"` if `record.pixel is None` |
| World X/Y (cm) | `record.world.x`, `record.world.y` | `"--"` if `record.world is None` |
| Angle (deg) | `math.degrees(record.yaw_rad)` | `"--"` if `record.yaw_rad is None` |
| Velocity | `record.world_velocity` (x/y cm/s) or `record.speed` — pick one consistent presentation (e.g. `speed` as a single cm/s number, or both velocity components; either satisfies the issue, just be consistent and document the choice) | `"--"` if unavailable |

Numeric columns monospace-aligned (fixed-width formatting, e.g.
`f"{value:7.1f}"`, or a monospace font plus fixed-width string
formatting — either is fine as long as columns visually line up).
"Prettier" latitude (optional, not required for acceptance): a subtle
header row naming the columns; a compact status header above the table
(fps / tag count / calibrated — data already available via
`DisplayFrame.status`'s existing `calibration_stale`/`frame_dark`
signals plus a frame counter you may add); ghosting (dimming, not
removing) a tag's row for a short grace period after it stops appearing
in the stream, before dropping it. If you implement ghosting, keep its
state (last-seen frame/time per tag id) internal to `TkWindow` — no new
module, per sprint.md's Design Rationale.

**`StreamLoop.run()`** (`:438-462`): after `self._window.show(frame.data)`,
add `self._window.update_tags(frame.tags)` — unconditional, since the
base `Window.update_tags` no-ops for any window without a sidebar
(raw view's window included).

**`_make_playfield_converter`** (`:508-537`): when building the
non-bare `DisplayFrame`, additionally set
`tags=frame.tags.tags if frame.tags is not None else None`.

**New `_convert_bare_playfield_frame`** (parallel to
`_convert_raw_frame`, `:465-470`, but for the deskewed stream): same
`calibration_stale`/`frame_dark` status handling as
`_make_playfield_converter`'s `convert`, but `data=frame.data` unchanged
(no `Renderer` call) and `tags` left at its `None` default — mirrors
`_convert_raw_frame`'s "never reads `frame.tags`" zero-overhead shape.

**`run_playfield_view`** (`:540-577`) gains a keyword-only `bare: bool
= False`:

```python
def run_playfield_view(
    camera: str,
    daemon: Daemon,
    *,
    renderer_factory: Callable[[], RendererBase] = PillowRenderer,
    window_factory: Callable[..., Window] = TkWindow,
    bare: bool = False,
) -> None:
    window = window_factory(
        f"aprilcam playfield view -- {camera}", show_sidebar=not bare
    )
    try:
        stream = daemon.stream_images(camera, deskewed=True)
        if bare:
            convert = _convert_bare_playfield_frame
        else:
            convert = _make_playfield_converter(renderer_factory())
        StreamLoop(window, stream, convert).run()
    finally:
        window.close()
```

`window_factory`'s type annotation widens from `Callable[[str], Window]`
to `Callable[..., Window]` (or an explicit `Protocol`-free callable
shape consistent with this file's existing style) to accept the new
keyword — confirm `uv run mypy` is still clean with whatever shape you
pick.

**`run_raw_view`** — no functional change. Its `window_factory` call
(`window_factory(f"aprilcam camera view [RAW] -- {camera}")`) passes no
`show_sidebar` kwarg, so `TkWindow`'s default (`False`) applies; its
converter (`_convert_raw_frame`) still never reads `frame.tags`. Confirm
this explicitly with a regression test — this ticket must not change
`camera view`'s behavior at all.

## Acceptance Criteria

- [ ] `DisplayFrame` has a new `tags: Optional[Tuple[TagRecord, ...]] =
      None` field; every pre-existing construction site compiles and
      behaves identically without modification.
- [ ] `Window.update_tags` exists as a concrete no-op default; a test
      fake `Window` that does not override it does not raise when
      called.
- [ ] `TkWindow(title)` (no `show_sidebar` kwarg) behaves exactly as
      before this ticket — no sidebar, same image/status layout.
- [ ] `TkWindow(title, show_sidebar=True)` builds a side panel packed
      beside the image, in the same root.
- [ ] The sidebar lists every tag in the most recent `update_tags` call,
      ordered by tag number, with the five documented columns,
      `"--"` placeholders for absent fields.
- [ ] `run_playfield_view(..., bare=False)` (the default) populates
      `DisplayFrame.tags` from the streamed frame's `frame.tags.tags`
      and renders the overlay via `renderer_factory()` — end-to-end
      through `StreamLoop`.
- [ ] `run_playfield_view(..., bare=True)` shows `frame.data` unmodified
      (no `Renderer.render()` call — assert this directly, e.g. via a
      renderer factory that raises if called) and never populates
      `DisplayFrame.tags`; its window is built with `show_sidebar=False`.
- [ ] `run_raw_view` is unchanged: its window has no sidebar, its
      converter still never reads `frame.tags` (regression test).
- [ ] `test_run_playfield_view_never_calls_get_tags_only_stream_images`
      (`:533`) passes with zero changes to its own assertions — the
      sidebar adds no new daemon call.
- [ ] The end-to-end geometry tests (`:718`-area
      `test_run_playfield_view_draws_apriltag_1_at_the_image_centre_...`
      and the "moves right and up" test just after it) are updated:
      `_front_edge_midpoint` (`:664`) sampled a boundary-line pixel that,
      under ticket 001's new scheme, is the *front* edge — never drawn.
      Move the sample to a red U-side pixel or a green hat-line pixel;
      keep the tests' actual intent (prove the viewer's own wiring
      doesn't lose or shift the drawn position) unchanged.
- [ ] `uv run mypy` clean.

## Testing

- **Existing tests to run**: `uv run pytest
  tests/aprilcam/view/test_viewer.py -v` (full file — this ticket
  touches most of it) plus `tests/test_no_cv2_in_client_modules.py`.
- **New tests to write**: `Window.update_tags` default no-op;
  `TkWindow` sidebar row formatting (prefer a pure formatting function
  under unit test, independent of a real Tk root, per the Description's
  note); `DisplayFrame.tags` populated/not-populated in bare vs.
  non-bare mode; `run_playfield_view(bare=True)` never calls the
  injected renderer factory; `run_raw_view` regression (no sidebar,
  `frame.tags` never read).
- **Verification command**: `uv run pytest
  tests/aprilcam/view/test_viewer.py
  tests/test_no_cv2_in_client_modules.py -v && uv run mypy`

**Documentation updates**: `view/viewer.py`'s module docstring (its "The
reuse seam" section currently states `Window`/`StreamLoop` know nothing
about *what* is viewed except the `deskewed` flag and the convert
callable — note the new, deliberately narrow exception:
`show_sidebar`/`update_tags` are also view-kind-specific now) and
`src/aprilcam/view/DESIGN.md` (seeded into this sprint's
`design/view-DESIGN.md` overlay) — describe the sidebar and `bare` mode
in the "Orientation"/"Interfaces" sections.
