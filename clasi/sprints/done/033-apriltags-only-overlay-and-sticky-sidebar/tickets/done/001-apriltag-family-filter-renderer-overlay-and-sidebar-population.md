---
id: '001'
title: 'AprilTag-family filter: renderer overlay and sidebar population'
status: done
use-cases:
- SUC-001
depends-on: []
github-issue: ''
issue: playfield-overlay-and-sidebar-should-show-apriltags-only.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# AprilTag-family filter: renderer overlay and sidebar population

## Description

`playfield view`'s overlay (`client/renderer.py`) and tag sidebar
(`view/viewer.py`) currently draw/list every `TagRecord` a deskewed
frame carries, with no regard for fiducial family. This buries the
field's ten ArUco 4x4 calibration corner markers under the full
red-U/green-hat/ID/crosshair overlay and lists them in the sidebar,
which the stakeholder explicitly does not want
(`clasi/issues/playfield-overlay-and-sidebar-should-show-apriltags-only.md`).

Add an AprilTag-family filter at the two independent points that read
a frame's tag list, per sprint 033's architecture (Design Rationale,
"the family filter is two independent inline checks... not a single
shared exported predicate"):

1. `client/renderer.py`'s `Renderer.render()`: in the per-record loop,
   skip any record whose `record.tag.family != TagFamily.APRILTAG`
   before `_tag_points`/any geometry is computed for it. Add
   `TagFamily` to the module's existing `from aprilcam.types import
   (...)` list.
2. `view/viewer.py`'s `_make_playfield_converter`'s `convert()`: filter
   `frame.tags.tags` to AprilTag-family records before assigning
   `DisplayFrame.tags`. Add `TagFamily` to the module's `from
   aprilcam.types import ...` line.

`_convert_raw_frame` and `_convert_bare_playfield_frame` need no
change — neither ever populates `.tags`.

This ticket does **not** touch detection (`daemon/detection.py`,
`vision/detector.py`) — ArUco detection must keep running unchanged,
since calibration depends on it. This is presentation-side filtering
only, on data already on the wire.

Note for ticket 002: this ticket's filtering is what makes it correct
for ticket 002's sticky roster to assume it is only ever handed
AprilTag-family tags — do not build ticket 002 in a way that depends
on ArUco records being filtered a second time downstream.

## Acceptance Criteria

- [x] No ArUco (`TagFamily.ARUCO`) record is boxed, hatted,
      ID-labeled, or crosshaired by `Renderer.render()` — a mixed-family
      frame draws the AprilTag record(s) exactly as sprint 032 left
      them and draws nothing at all for the ArUco record(s).
- [x] A frame whose `tags.tags` is entirely ArUco draws nothing (no
      exception, no stray pixels).
- [x] `_make_playfield_converter`'s `convert()` populates
      `DisplayFrame.tags` with AprilTag-family records only, from a
      mixed-family input frame.
- [x] `_convert_raw_frame`/`_convert_bare_playfield_frame` behavior is
      unchanged (neither ever populates `.tags`).
- [x] ArUco detection is untouched — no changes to `daemon/detection.py`
      or `vision/detector.py`.
- [x] `test_run_playfield_view_never_calls_get_tags_only_stream_images`
      continues to pass unmodified.
- [x] `tests/test_no_cv2_in_client_modules.py` continues to pass (no
      new `cv2` import introduced).

## Implementation Plan

**Approach**: Two small, independent inline guards — no new module, no
new exported predicate (sprint.md's Design Rationale explains why a
shared predicate was rejected as speculative generality for a one-line
check with exactly two call sites).

**Files to modify**:
- `src/aprilcam/client/renderer.py` — add `TagFamily` to the type
  import; add the family guard as the first check inside `render()`'s
  `for record in frame.tags.tags:` loop, before `_tag_points` is
  called.
- `src/aprilcam/view/viewer.py` — add `TagFamily` to the type import;
  filter `frame.tags.tags` to AprilTag-family before the
  `tags = frame.tags.tags if frame.tags is not None else None` line in
  `_make_playfield_converter`'s `convert()`.
- `src/aprilcam/client/DESIGN.md`, `src/aprilcam/view/DESIGN.md` — add
  a short note describing the AprilTag-only filter (this sprint's
  seeded `design/` overlay is where the working copy lives during the
  sprint).

**Testing plan**:
- `tests/aprilcam/client/test_renderer.py`: new fixture/test cases —
  (a) a mixed-family frame (one AprilTag record, one ArUco record)
  draws the AprilTag boundary/hat/ID/crosshair and draws nothing for
  the ArUco record (assert no stray pixels near the ArUco record's own
  corners); (b) an all-ArUco frame draws nothing and raises nothing.
  Existing AprilTag-only fixtures/tests are unaffected (they were
  already `TagFamily.APRILTAG`, per verification during planning).
- `tests/aprilcam/view/test_viewer.py`: new test asserting
  `_make_playfield_converter`'s `convert()` returns a `DisplayFrame`
  whose `.tags` excludes an ArUco record present in the input
  `ImageFrame.tags.tags`. Leave the existing
  `test_format_tag_row_formats_present_numeric_fields_and_abbreviates_aruco`
  test as-is — it exercises `_format_tag_row` directly as a pure,
  family-agnostic function, independent of this ticket's filtering.
- Run scoped: `uv run pytest tests/aprilcam/client/test_renderer.py
  tests/aprilcam/view/test_viewer.py tests/test_no_cv2_in_client_modules.py`.

**Documentation updates**: `src/aprilcam/client/DESIGN.md` and
`src/aprilcam/view/DESIGN.md` (via this sprint's seeded overlay) —
describe the family filter and its rationale (mirroring the level of
detail those docs already give the sprint-032 overlay/sidebar work).

## Testing

- **Existing tests to run**: `tests/aprilcam/client/test_renderer.py`,
  `tests/aprilcam/view/test_viewer.py`,
  `tests/test_no_cv2_in_client_modules.py`.
- **New tests to write**: see Implementation Plan's Testing plan above
  — mixed-family and all-ArUco renderer cases; converter-level
  AprilTag-only `DisplayFrame.tags` case.
- **Verification command**: `uv run pytest tests/aprilcam/client/test_renderer.py tests/aprilcam/view/test_viewer.py tests/test_no_cv2_in_client_modules.py`
