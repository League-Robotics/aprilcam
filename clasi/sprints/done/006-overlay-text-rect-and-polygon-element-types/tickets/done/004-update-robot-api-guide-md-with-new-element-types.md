---
id: '004'
title: Update ROBOT_API_GUIDE.md with new element types
status: done
use-cases:
- SUC-001
- SUC-002
- SUC-003
depends-on:
- '003'
github-issue: ''
issue: plan-add-text-rect-and-polygon-overlay-element-types.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Update ROBOT_API_GUIDE.md with new element types

## Description

`src/aprilcam/ROBOT_API_GUIDE.md` contains a table of overlay element types.
Add rows for `"text"`, `"rect"`, and `"polygon"`, and update the example snippet
to include a `"text"` element.

Depends on ticket 003 (mcp_server docstring already updated; guide update is consistent).

## Acceptance Criteria

- [x] The element types table in `ROBOT_API_GUIDE.md` has rows for `"text"`, `"rect"`, and `"polygon"`
- [x] Each new row documents the `params` encoding and any special keys (`text` for `"text"`)
- [x] `thickness=-1` fill behavior is documented for `"rect"` and `"polygon"`
- [x] The example dict in the guide includes at least one `"text"` element
- [x] No other content in the guide is changed

## Implementation Plan

### Approach

Open `src/aprilcam/ROBOT_API_GUIDE.md`, locate the element types table, and append
three rows. Locate the example dict and add a `"text"` element entry.

### Files to Modify

| File | Change |
|------|--------|
| `src/aprilcam/ROBOT_API_GUIDE.md` | Three new table rows + updated example |

### Table Rows to Add

Append after the existing `polyline` row:

```markdown
| `"text"` | `[x, y]` or `[x, y, font_scale]` | Text label at world-cm anchor. Also set `"text"` key in the element dict. `font_scale` defaults to 0.6. |
| `"rect"` | `[x1, y1, x2, y2]` | Axis-aligned rectangle. `thickness=-1` fills. |
| `"polygon"` | `[x0, y0, x1, y1, …]` | Closed polygon (at least 3 vertices). `thickness=-1` fills. |
```

### Example Update

In the example element list, add a `text` element alongside the existing examples:

```python
{"type": "text", "params": [60, 45], "text": "Tag 3", "color": [255, 220, 0]},
```

### Testing Plan

Documentation only — no automated tests. Verify visually that the table renders
correctly in a Markdown viewer.

### Documentation Updates

This ticket IS the documentation update.
