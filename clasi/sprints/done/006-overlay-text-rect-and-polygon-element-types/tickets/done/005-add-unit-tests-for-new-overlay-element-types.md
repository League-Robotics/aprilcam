---
id: '005'
title: Add unit tests for new overlay element types
status: done
use-cases:
- SUC-001
- SUC-002
- SUC-003
depends-on:
- '002'
- '003'
github-issue: ''
issue: plan-add-text-rect-and-polygon-overlay-element-types.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Add unit tests for new overlay element types

## Description

Add four new tests to `tests/test_display_overlay.py` covering the three new
element types. Look at the existing test file for the test fixture pattern
(frame setup, overlay frame construction, homography).

Depends on tickets 002 (draw branches) and 003 (text field passthrough).

## Acceptance Criteria

- [x] `test_text_draws` — `type="text"`, `text="hello"`, verifies frame is non-zero after draw
- [x] `test_text_empty_string` — `type="text"`, `text=""`, verifies no exception is raised
- [x] `test_rect_draws` — `type="rect"` with `thickness=-1`, verifies frame is non-zero
- [x] `test_polygon_draws` — `type="polygon"` with `thickness=-1` (filled triangle), verifies frame is non-zero
- [x] All four tests pass: `uv run pytest tests/test_display_overlay.py -v`
- [x] Full suite passes: `uv run pytest tests/ --ignore=tests/system -q`

## Implementation Plan

### Approach

Read `tests/test_display_overlay.py` to understand the existing fixture (how
`PlayfieldDisplay`, a synthetic homography, and a test `OverlayFrame` are set up).
Add four tests following the same pattern.

### Files to Modify

| File | Change |
|------|--------|
| `tests/test_display_overlay.py` | Add four new test functions |

### Test Patterns

Each test should:
1. Create a black frame (e.g., `np.zeros((480, 640, 3), dtype=np.uint8)`).
2. Build an `OverlayFrame` proto with a single element of the type under test.
3. Call `display.draw_live_overlay(frame, overlay_frame, homography)`.
4. Assert the expected outcome (non-zero pixels, or no exception for empty string).

Key details per test:
- `test_text_draws`: element `type="text"`, `params=[50, 50]`, `text="hello"`. Assert `frame.any()`.
- `test_text_empty_string`: element `type="text"`, `params=[50, 50]`, `text=""`. Just call draw — assert no exception raised.
- `test_rect_draws`: element `type="rect"`, `params=[20, 20, 80, 80]`, `thickness=-1`. Assert `frame.any()`.
- `test_polygon_draws`: element `type="polygon"`, `params=[50,10, 90,90, 10,90]`, `thickness=-1`. Assert `frame.any()`.

### Testing Plan

- `uv run pytest tests/test_display_overlay.py -v`
- `uv run pytest tests/ --ignore=tests/system -q`

### Documentation Updates

None required for this ticket.
