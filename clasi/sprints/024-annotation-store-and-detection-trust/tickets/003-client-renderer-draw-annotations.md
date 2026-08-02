---
id: '003'
title: "Client renderer \u2014 draw annotations"
status: done
use-cases:
- SUC-001
depends-on: []
github-issue: ''
issue: annotation-store-for-shared-spatial-state.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Client renderer — draw annotations

## Description

Fill in `client/renderer.py`'s already-stubbed annotation half.
`Renderer.render`'s tag-drawing half (sprint 022) is complete and
untouched; the annotation half is currently inert — the module
docstring says so explicitly ("frame.annotations is always None this
sprint... the annotation-drawing step below is simply never reached").
This ticket makes it real.

**No code dependency on tickets 001/002.** `Annotation`/`AnnotationSet`/
the shape union/`Style` all already exist in `types.py`, shipped in a
prior architecture pass — this ticket can be written and fully unit
tested (constructing `ImageFrame`/`AnnotationSet` objects directly, the
same way `tests/aprilcam/client/test_renderer.py` already tests the
tag-drawing half) without a running daemon or any of tickets 001/002's
wire code. It is listed in the sprint after 001/002 for narrative
build-order (draw what the wire will deliver), not because it needs
their code to exist.

Builds:

- **`client/renderer.py`** — one draw routine per shape:
  - `Marker` — a small filled/outlined point marker at `at`, plus
    `label` text if present (Pillow `ImageDraw.text`).
  - `PathShape` — an open polyline through `points` (`ImageDraw.line`
    with the full point list).
  - `Polygon` — a closed region (`ImageDraw.polygon`).
  - `Circle` — `center` + `radius_cm`, converted to pixel radius via
    the frame's own `px_per_cm` (world-cm radius × `px_per_cm` —
    uniform scale by construction, per `ImageFrame`'s own docstring,
    so this one multiplication is exact, no distortion to correct
    for).
  - `Text` — a free-standing label at `at`.
  All world→pixel conversion goes through
  `calibration.geometry.world_to_deskewed_pixel` — the same function
  the tag-drawing half already imports and uses, so both halves agree
  on the exact same anchor/scale math (no second, independently-signed
  conversion to accidentally disagree with the first).
  - **`Style` handling**: honor `color`/`line_width` when present;
    choose a sensible, documented default when either is absent (style
    is presentation — `types.Style`'s own docstring says a renderer
    may ignore any hint, and choosing a default for an absent one is
    squarely a renderer's job, not a daemon or wire concern).
  - **`layers` filtering**: `render(frame, layers=(...))` draws only
    annotations whose `.layer` is in the given tuple; `layers=None`
    (the default) draws every layer — already the right default for
    `view/viewer.py`'s "deliberately basic" viewer, which calls
    `render(frame)` with no `layers=` argument and needs **no code
    change** to start showing annotations once this ticket lands.
  - **`extra` annotations**: draw `extra` (purely local, never sent to
    the daemon) exactly like embedded ones — same draw routines, same
    `Style` handling, no second vocabulary.

## Acceptance Criteria

- [x] Each of the five shapes renders correctly against a synthetic
      `ImageFrame`/`AnnotationSet` fixture — assert drawn pixel colors
      at expected coordinates, mirroring
      `test_renderer.py`'s existing boundary/hat assertions for tags.
  - [x] A `Circle`'s rendered pixel radius matches `radius_cm *
      frame.px_per_cm` within a small pixel tolerance.
- [x] `layers=None` draws every annotation in `frame.annotations`;
      `layers=("planner",)` draws only that layer's annotations and
      none of another layer's, on the same frame.
- [x] `extra` annotations render identically to embedded ones of the
      same shape (same draw routine exercised both ways) and are drawn
      even when `frame.annotations` is `None` or empty.
- [x] A `Style` with `color`/`line_width` unset renders with a
      documented default, not a crash and not silently-blank geometry.
- [x] Rendering a frame with `frame.annotations is None` (a raw frame,
      or a pre-annotation-store deskewed frame) behaves exactly as it
      does today — no exception, nothing drawn for annotations, tags
      still draw normally. This is the regression guard for the
      existing tag-drawing half.
- [x] `tests/test_no_cv2_in_client_modules.py` still passes — no `cv2`
      import anywhere in this ticket's diff (Pillow only, matching the
      module's existing discipline).
- [x] `uv run mypy` clean; zero `typing.Protocol`.
- [x] Full suite green: `uv run pytest -m "not camera"`.

## Implementation Plan

**Approach**: extend `Renderer.render`'s existing method body — the
tag-drawing loop stays exactly as-is; add a parallel loop over
`frame.annotations.annotations` (filtered by `layers`) plus `extra`,
dispatching on `isinstance(shape, ...)` per the five shape types (a
`Union`, so `isinstance` dispatch is the correct, type-safe pattern —
no kind-enum to switch on, matching `AnnotationShape`'s own "valid by
construction" design intent).

**Files**:
- `src/aprilcam/client/renderer.py` (extend `Renderer.render` and
  helpers; update module docstring — the "annotations are inert"
  paragraph becomes inaccurate and must be rewritten, not left stale)

**Testing plan**:
- **Existing tests to run**: `uv run pytest tests/aprilcam/client/
  test_renderer.py tests/test_no_cv2_in_client_modules.py -v`, plus
  full headless suite.
- **New tests to write**: one test per shape (marker/path/polygon/
  circle/text), a `layers` filtering test, an `extra` test, a
  `Style`-defaults test, and a regression test that a `None`
  `frame.annotations` draws nothing extra and does not error.
- **Verification command**: `uv run pytest -m "not camera"` and
  `uv run mypy`.

**Documentation updates**: `client/renderer.py`'s module docstring —
rewrite the paragraph describing annotations as inert; it is
inaccurate the moment this ticket lands.
