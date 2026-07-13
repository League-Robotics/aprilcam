---
sprint: "009"
status: final
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Use Cases — Sprint 009: Viewer collapsible panels and paths sidebar

## SUC-001: User collapses and expands right-panel sections

**Actor**: User watching the `aprilcam view` window.

**Preconditions**: `aprilcam view <camera>` is running; right panel visible.

**Main Flow**:
1. User sees all sections expanded (▼ triangle in each header).
2. User clicks a section header toggle.
3. Section content hides; header shows ▶.
4. User clicks the toggle again.
5. Section content reappears; header shows ▼.

**Postconditions**: Section collapse state matches the last toggle action.

**Acceptance Criteria**:
- [ ] Camera Status, Mobile Tags, Stationary Tags, and Objects sections each have a
      clickable triangle toggle in their header row.
- [ ] All sections start expanded (▼) on launch.
- [ ] Each section can be independently collapsed and expanded.
- [ ] Collapsing hides the content frame; the header row remains visible.

---

## SUC-002: Collapsing the Objects section stops detection; expanding resumes it

**Actor**: User who wants to reduce CPU load when object detection is not needed.

**Preconditions**: `aprilcam view` is running.

**Main Flow**:
1. User collapses the Objects section; `_detect_objects.clear()` is called.
2. Object boxes disappear from the video display.
3. User expands the Objects section; `_detect_objects.set()` is called (lazily
   initializing `ColorClassifier` if not yet created).
4. Object detection resumes; boxes reappear when objects are present.

**Postconditions**: `_detect_objects` event state matches the Objects section state.

**Acceptance Criteria**:
- [ ] No separate "Objects: On/Off" toggle button exists.
- [ ] Collapsing Objects stops detection immediately.
- [ ] Expanding Objects starts detection; `ColorClassifier` is lazily initialized.
- [ ] Objects list clears from the panel when collapsed.

---

## SUC-003: Agent creates a named path visible in the Paths panel

**Actor**: AI agent calling the MCP `create_path` tool.

**Preconditions**: A playfield is open; `aprilcam view` is running.

**Main Flow**:
1. Agent calls `create_path(playfield_id=..., waypoints_json=..., name="Robot path")`.
2. MCP tool stores the path with `name="Robot path"` in `paths.json`.
3. Viewer polls `paths.json`; Paths section shows the new row with symbol preview,
   line color swatch, label "Robot path", and a delete button.

**Postconditions**: Path row appears in the Paths panel with the given name.

**Acceptance Criteria**:
- [ ] `create_path` accepts an optional `name` parameter (default `""`).
- [ ] `paths.json` stores the `name` field.
- [ ] Panel row shows `path.name` when set; falls back to `path.path_id` when blank.
- [ ] Symbol preview is a 16×16 canvas drawing the first waypoint's symbol.
- [ ] Line swatch is a 20×4 px filled rectangle in the first waypoint's `line_color`.

---

## SUC-004: User hides all paths from the video display

**Actor**: User who wants a clean video view without path overlays.

**Preconditions**: At least one path exists in `paths.json`.

**Main Flow**:
1. User collapses the Paths section; `_show_paths` is set to `False`.
2. Render loop skips `draw_paths()`; all path overlays disappear from video.
3. User expands the Paths section; `_show_paths` becomes `True`; paths reappear.

**Postconditions**: `_show_paths` state matches the Paths section expand/collapse state.

**Acceptance Criteria**:
- [ ] Collapsing the Paths section removes path overlays from the video display.
- [ ] Expanding the Paths section restores path overlays.
- [ ] Paths remain in `paths.json`; they are only hidden in the display.

---

## SUC-005: User deletes a path from the Paths panel

**Actor**: User managing paths visible in the viewer.

**Preconditions**: Paths panel is expanded; at least one path row is visible.

**Main Flow**:
1. User clicks the 🗑 button on a path row.
2. Viewer calls the `delete_path` MCP tool for that `path_id`.
3. `paths.json` is re-read; the row disappears from the Paths panel on the next poll.

**Postconditions**: Path is removed from `paths.json`; row no longer shown.

**Acceptance Criteria**:
- [ ] Each path row has a delete button (🗑 or ✕ fallback).
- [ ] Clicking delete removes the path from `paths.json`.
- [ ] The deleted row disappears from the panel without restarting the viewer.
- [ ] Other path rows are unaffected.
