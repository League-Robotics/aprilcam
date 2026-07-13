---
status: done
sprint: '002'
tickets:
- 002-010
---

# Agent-Drawn Paths on the AprilCam Live View

## Context

Agents using the AprilCam MCP server today can read the playfield (tag positions, history, image-processing operations) but cannot **draw** anything onto the live display. We want the agent to be able to plan a route in world coordinates and have the MCP server render that route — with labeled symbols and connecting lines — on the live deskewed playfield view in real time.

A path is a single object the agent submits as a whole; the server returns a `path_id`; the agent can delete it as a whole. No per-waypoint mutation, no half-paths.

## Design Decisions (from clarification)

- **Playfield required.** Paths attach to a `playfield_id`. The live view must be opened on a camera that has a calibrated playfield. Raw-camera paths are out of scope.
- **Per-waypoint size in cm.** Each waypoint specifies its own `size_cm`. Symbols scale with the playfield — a 4cm circle reads as 4cm wide regardless of camera angle.
- **Symbol set has 8 values** — filled vs. outlined are distinct symbols, not a flag: `square`, `filled_square`, `circle`, `filled_circle`, `triangle`, `filled_triangle`, `x`, `none`. (`x` is intrinsically lines; no filled variant. `none` draws no symbol at the waypoint — the waypoint is still a vertex for connecting lines, but invisible itself; `symbol_color` is unused.)
- Colors are sent as RGB triples; converted to OpenCV BGR at the drawing site only.

## Data Model

**New file:** [src/aprilcam/server/paths.py](src/aprilcam/server/paths.py)

```python
from dataclasses import dataclass
from typing import Literal, Tuple, List

Symbol = Literal[
    "square", "filled_square",
    "circle", "filled_circle",
    "triangle", "filled_triangle",
    "x",
    "none",      # waypoint is a vertex for lines only; no symbol drawn
]
RGB = Tuple[int, int, int]   # 0..255 per channel, agent-facing

@dataclass(frozen=True)
class Waypoint:
    x: float          # world cm, origin upper-left
    y: float          # world cm
    size_cm: float    # > 0
    symbol: Symbol
    symbol_color: RGB
    line_color: RGB   # color of the segment from this waypoint to the next; ignored on last

@dataclass
class Path:
    path_id: str            # "path_000", monotonic
    playfield_id: str
    waypoints: List[Waypoint]

    def to_dict(self) -> dict: ...
```

`PathRegistry` (also in `paths.py`): module-level dict `{path_id -> Path}`, monotonic `path_NNN` IDs (mirrors `FrameRegistry`), with `create`, `delete`, `get`, `list_for(playfield_id)`, `clear_for(playfield_id)`.

## MCP Tools (FastMCP, same shape as existing tools)

All return `list[TextContent]` with `json.dumps(result)`. Errors return `{"error": "<message>"}`.

| Tool | Inputs | Returns on success |
|---|---|---|
| `create_path` | `playfield_id: str`, `waypoints_json: str` | `{"path_id": "path_000"}` |
| `delete_path` | `path_id: str` | `{"deleted": true, "path_id": "..."}` |
| `list_paths` | `playfield_id: str` | `{"playfield_id": "...", "paths": [...]}` |
| `clear_paths` | `playfield_id: str` | `{"cleared": ["path_000", ...]}` |

**`create_path` validation** (in order; first failure returns):
1. `playfield_id` must exist in `playfield_registry`.
2. `json.loads(waypoints_json)` must succeed; result must be a non-empty list.
3. Each waypoint must have `x, y, size_cm, symbol, symbol_color, line_color`.
4. `x, y, size_cm` finite floats; `size_cm > 0`.
5. `symbol` in the 8-value set.
6. `symbol_color`, `line_color`: 3 ints each in `[0, 255]`.

Example input:
```json
{
  "playfield_id": "pf_cam_0",
  "waypoints_json": "[{\"x\":10,\"y\":10,\"size_cm\":3,\"symbol\":\"filled_square\",\"symbol_color\":[255,0,0],\"line_color\":[0,255,0]},{\"x\":50,\"y\":40,\"size_cm\":5,\"symbol\":\"x\",\"symbol_color\":[255,255,0],\"line_color\":[0,0,0]}]"
}
```

## IPC: Parent → Child Command Pipe

The live view renders in a **child process** ([src/aprilcam/ui/liveview.py:30-225](src/aprilcam/ui/liveview.py#L30-L225)). MCP tools run in the parent. The existing IPC is two OS pipes (data: child→parent; stop: parent→child). We add a third pipe (commands: parent→child).

**Protocol** — line-delimited JSON written by parent, polled by child via `select.select` alongside the existing stop pipe:
```
{"op": "add",    "path": { ... Path.to_dict() ... }}
{"op": "remove", "path_id": "path_001"}
{"op": "clear"}
```

The child keeps a local `dict[str, dict]` indexed by `path_id`. Add/remove/clear messages update it; each frame, `display.draw_paths(...)` iterates that dict. The child never imports the `Path` dataclass — it consumes plain dicts as they cross the pipe.

**Initial-state push.** Paths created before the live view starts must appear on the first frame. In `_handle_start_live_view`, snapshot `path_registry.list_for(pf_id)` and pass the JSON via a new `LiveViewProcess.set_initial_paths(...)` setter before `proc.start(...)`. The child's seeded dict picks them up before the first `imshow`.

**Surviving live-view restart.** `path_registry` lives in the parent process and outlives any live view. Restart reseeds from the registry.

## Drawing

**New method** `PlayfieldDisplay.draw_paths(frame, paths, playfield, homography)` in [src/aprilcam/ui/display.py](src/aprilcam/ui/display.py), called from `_child_main` right after the existing `display.draw_overlays(...)` call near [liveview.py:167](src/aprilcam/ui/liveview.py#L167).

Per path, per waypoint:
1. **World → source pixel** by inlining the inverse homography (the child loads `homography` at [liveview.py:65-74](src/aprilcam/ui/liveview.py#L65-L74)). Reuse the math from [src/aprilcam/core/playfield.py:451-457](src/aprilcam/core/playfield.py#L451-L457) — do not call back through the dataclass.
2. **Source pixel → display pixel** via the existing `self._map_points_to_display(...)` at [display.py:109-128](src/aprilcam/ui/display.py#L109-L128). This already handles deskew + crop.
3. **Compute pixel radius for `size_cm`**: map the waypoint center AND a second world point at `(x + size_cm/2, y)` through the same pipeline; the Euclidean distance between the two display points is the symbol half-extent. This makes a 5cm circle stay 5cm on the playfield regardless of perspective.

Draw order: lines first (so symbols cover the line ends), then symbols.

Symbol rendering (`r = pixel half-extent`, `color = (b, g, r)` after RGB→BGR flip, `LINE_AA` always):
| Symbol | Call |
|---|---|
| `circle` | `cv.circle(..., r, color, thickness=2)` |
| `filled_circle` | `cv.circle(..., r, color, thickness=cv.FILLED)` |
| `square` | `cv.rectangle(..., (cx±r, cy±r), color, thickness=2)` |
| `filled_square` | `cv.rectangle(..., thickness=cv.FILLED)` |
| `triangle` | `cv.polylines([apex, lower-left, lower-right], True, color, 2)` |
| `filled_triangle` | `cv.fillPoly([...], color)` |
| `x` | two `cv.line` calls between opposite corners, thickness 2 |
| `none` | no symbol drawn at this waypoint (lines still attach to it) |

Lines between waypoints: `cv.line(..., from_waypoint.line_color, thickness=2, cv.LINE_AA)`. Last waypoint's `line_color` unused.

## Files to Modify

**New:** [src/aprilcam/server/paths.py](src/aprilcam/server/paths.py) — `Symbol`, `RGB`, `Waypoint`, `Path`, `PathRegistry`.

[src/aprilcam/server/mcp_server.py](src/aprilcam/server/mcp_server.py):
- import `paths` module (~line 34)
- instantiate `path_registry = PathRegistry()` next to `playfield_registry` (~line 154)
- inside `_handle_start_live_view`, before `proc.start(...)` (~line 1242): `proc.set_initial_paths([p.to_dict() for p in path_registry.list_for(pf_id)])`
- after `stop_live_view` (~line 2647): add `_live_view_for_playfield(playfield_id)` helper, four `_handle_*` functions, and the four `@server.tool()` wrappers

[src/aprilcam/ui/liveview.py](src/aprilcam/ui/liveview.py):
- `_child_main` signature (line 30): add `cmd_fd: int` and `initial_paths_json: str = "[]"`
- after the existing `stop_in = os.fdopen(...)` (~line 55): open `cmd_in`, seed `paths: dict[str, dict]` from `initial_paths_json`
- replace `_should_stop` (~lines 101-105) with `_drain_commands()` that `select`s on both pipes; mutates `paths` on `add`/`remove`/`clear`; returns True only on stop
- update the in-loop call (~line 121) to use `_drain_commands()`
- after `display.draw_overlays(...)` (~line 167): `display.draw_paths(disp, paths, cam.playfield, homography)`
- in the `finally` (after line 222): close `cmd_in`
- `LiveViewProcess.__init__` (~lines 257-262): add `_cmd_write_fd = None`, `_initial_paths: list[dict] = []`
- new `LiveViewProcess.set_initial_paths(paths)` and `send_command(msg)` methods
- in `start()` (~line 283): create `cmd_r, cmd_w = os.pipe()`; add `cmd_r` and `json.dumps(self._initial_paths)` to the `Process(args=...)` tuple; close `cmd_r` in the parent after `proc.start()`
- in `stop()` (~line 332): close `_cmd_write_fd`

[src/aprilcam/ui/display.py](src/aprilcam/ui/display.py):
- new method `draw_paths(frame, paths, playfield, homography)` after `draw_overlays` (~line 262). No existing code is changed.

## Reuse / Don't Reinvent

- Inverse-homography math: pattern at [src/aprilcam/core/playfield.py:451-457](src/aprilcam/core/playfield.py#L451-L457).
- Display-space mapping: reuse `PlayfieldDisplay._map_points_to_display` ([display.py:109-128](src/aprilcam/ui/display.py#L109-L128)).
- Registry id generation: mirror `FrameRegistry` ([src/aprilcam/server/frame.py:54-201](src/aprilcam/server/frame.py#L54-L201)).
- View-id lookup: existing `view_id = f"live_{camera_id}"` convention; `PlayfieldEntry.camera_id` ([src/aprilcam/server/mcp_server.py:117](src/aprilcam/server/mcp_server.py#L117)).
- BGR color tuples and the outline/fill primitives are all the same primitives `draw_overlays` already uses ([display.py:131-262](src/aprilcam/ui/display.py#L131-L262)).

## Edge Cases

- **Playfield not yet calibrated** when path is created → `create_path` succeeds; `draw_paths` is a no-op until calibration. Note: the child snapshots `homography` at startup ([liveview.py:65-74](src/aprilcam/ui/liveview.py#L65-L74)), so a calibration that happens *after* the live view starts will not take effect without restarting the view. This is a pre-existing limitation, not introduced here — leave as-is, document it.
- **Non-finite or non-positive `size_cm`** → reject in `create_path` validation.
- **`delete_path` unknown id** → `{"error": "Unknown path_id '<id>'"}`.
- **`clear_paths`** → registry clears for that playfield; child receives a single `{"op": "clear"}` (each live-view child is bound to exactly one camera, so a full clear is correct).
- **Path created before live view starts** → handled by initial-state push.
- **Live view restarted** → registry survives; initial-state push reseeds.
- **Same `path_id` added twice on the child** → dict key collision overwrites, safe.
- **Waypoints off-screen** → OpenCV clips, no special handling.

## Version Bump

After implementation, bump `version` in [pyproject.toml](pyproject.toml) following the `0.YYYYMMDD.N` scheme (today = 2026-05-14).

## Verification

Manual MCP test sequence (requires camera with 4 ArUco corner tags visible):

1. `open_camera(0)` → `cam_0`; `create_playfield("cam_0")` → `pf_cam_0`; `calibrate_playfield("pf_cam_0")`.
2. `create_path(pf_cam_0, [...filled_square at (10,10), 3cm, red, line green → circle at (50,50), 4cm, blue...])` → `path_000`.
3. `start_live_view(camera_id="cam_0")`. Confirm: red filled square + blue circle outline visible, connected by a green line. **Colors must look red/blue, not blue/red** (RGB→BGR boundary check).
4. While live view runs: `create_path(... yellow x and filled_triangle ...)`. Confirm new symbols appear within ~1 frame.
5. `delete_path("path_000")` → only the second path remains on screen.
6. `stop_live_view`, `list_paths(pf_cam_0)` → still returns path_001. `start_live_view` again → path_001 reappears immediately (proves initial-state push works).
7. `clear_paths(pf_cam_0)` → all symbols vanish; `list_paths` returns `[]`.
8. Size sanity: a 10cm `filled_circle` should look roughly twice as wide as a 5cm one on the same screen.
9. Error cases:
   - `delete_path("path_999")` → `{"error": "Unknown path_id 'path_999'"}`
   - `create_path("pf_nope", "[]")` → `{"error": "Unknown playfield_id 'pf_nope'"}`
   - Symbol = `"hexagon"` → `{"error": "Invalid symbol 'hexagon'"}`
   - `waypoints_json="not json"` → `{"error": "Invalid waypoints JSON: ..."}`
   - `size_cm = -1` → `{"error": "size_cm must be positive"}`
10. No-calibration case: create a playfield, skip `calibrate_playfield`, `create_path` should succeed, `start_live_view` should not crash, `draw_paths` no-ops cleanly.
