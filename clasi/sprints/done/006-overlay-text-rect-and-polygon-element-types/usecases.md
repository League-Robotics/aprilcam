---
status: final
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 006 Use Cases

## SUC-001: Label a Tag or Position with Text

- **Actor**: Robot program (via `DaemonControl.publish_overlay` or MCP `set_live_overlay`)
- **Preconditions**: A playfield with a valid homography is open. The live view is running.
- **Main Flow**:
  1. Robot program calls `publish_overlay` with one or more elements of `type="text"`,
     providing `params=[x, y]` (world cm anchor) and a `text` string.
  2. The overlay is published via gRPC to the daemon.
  3. `view_cli` receives the `OverlayFrame` and passes it to `draw_live_overlay()`.
  4. `draw_live_overlay()` maps the world-cm anchor to display pixels and renders the
     text with an outline using `_draw_text_with_outline()`.
  5. The label appears on the live view at the correct playfield position.
- **Postconditions**: The text label is visible for the overlay TTL duration.
- **Acceptance Criteria**:
  - [ ] `type="text"` element with a non-empty string renders text at the correct position
  - [ ] An optional `params[2]` value overrides the default `font_scale` of 0.6
  - [ ] An empty `text` string does not raise an exception
  - [ ] Text disappears when the TTL expires (inherited from existing overlay behavior)

---

## SUC-002: Draw a Bounding Rectangle

- **Actor**: Robot program
- **Preconditions**: A playfield with a valid homography is open. The live view is running.
- **Main Flow**:
  1. Robot program calls `publish_overlay` with an element of `type="rect"`,
     providing `params=[x1, y1, x2, y2]` (world cm corners).
  2. The overlay is rendered as a rectangle in display coordinates.
  3. When `thickness=-1` the rectangle is filled; otherwise the border is drawn.
- **Postconditions**: The rectangle is visible on the live view.
- **Acceptance Criteria**:
  - [ ] `type="rect"` with valid corner params renders a rectangle
  - [ ] `thickness=-1` produces a filled rectangle
  - [ ] Positive `thickness` produces an outlined rectangle

---

## SUC-003: Draw a Filled or Outlined Polygon Zone

- **Actor**: Robot program
- **Preconditions**: A playfield with a valid homography is open. The live view is running.
- **Main Flow**:
  1. Robot program calls `publish_overlay` with an element of `type="polygon"`,
     providing `params=[x0, y0, x1, y1, ...]` (world cm vertices, at least 3 points).
  2. The overlay is rendered as a closed polygon in display coordinates.
  3. When `thickness=-1` the polygon is filled; otherwise the closed outline is drawn.
- **Postconditions**: The polygon zone is visible on the live view.
- **Acceptance Criteria**:
  - [ ] `type="polygon"` with three or more vertex pairs renders a closed shape
  - [ ] `thickness=-1` produces a filled polygon
  - [ ] Positive `thickness` produces an outlined closed polyline
