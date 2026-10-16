---
id: '001'
title: 'Daemon and wire: board_outline on IntrinsicsStatus'
status: in-progress
use-cases:
- SUC-001
depends-on: []
github-issue: ''
issue: intrinsics-gui-shows-the-live-camera-image-with-a-labelled-coverage-grid.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Daemon and wire: board_outline on IntrinsicsStatus

## Description

Add an optional `board_outline` to `IntrinsicsStatus` and carry it end to end.
Keep the daemon change thin: the session publishes what the detector already
produced (sprint.md Architecture, What Changed items 1-3, Decision 1).

## Implementation Plan

Approach:
- `intrinsics/interfaces.py`: field `board_outline: Optional[tuple[tuple[tuple[float, float], ...], ...]] = None`
  (tuple of polygons, each 3+ native-pixel points). Document it in the dataclass.
- `intrinsics/session.py` `step()`: after detection, compute one convex hull per
  `BoardDetection` (small private helper using `cv2.convexHull`; session is
  daemon-side and already imports cv2) and pass `board_outline=` in the existing
  `_update(...)` call. Set it to `None` when no board is detected so it never
  goes stale. Hull of fewer than 3 distinct points is skipped.
- `proto/aprilcam_v2.proto`: `message IntrinsicsPolygon { repeated double xy = 1; }`
  and `repeated IntrinsicsPolygon board_outline = 23;` inside `IntrinsicsStatus`;
  regenerate `src/aprilcam/proto/*_pb2*.py` with `scripts/compile_proto.py`.
  Additive, no `PROTOCOL_VERSION` bump.
- `daemon/rpc.py` `_intrinsics_status_to_proto` and `client/models.py`
  `intrinsics_status_from_proto`: flatten/unflatten; empty repeated <-> `None`.
- `mcp/tools/intrinsics.py` `_status_to_dict`: add `board_outline` (nested lists).

Files to modify: the six above plus the generated pb2 files.

Testing plan: follow sprint 044 ticket 006 round-trip style; session tests drive
`step()` synchronously with a fake detector.

## Acceptance Criteria

- [ ] `IntrinsicsStatus.board_outline` exists, defaults to `None`.
- [ ] Session fills it with one hull per detected board each step and clears it
      when no board is visible.
- [ ] Python -> proto -> client round trip preserves polygons exactly, and
      preserves `None` (empty) as `None`.
- [ ] MCP status dict includes `board_outline`.
- [ ] Existing intrinsics proto/session/MCP tests still pass; no cv2 import
      added to `client/`, `view/`, or `mcp/`.

## Testing

- **Existing tests to run**: `tests/aprilcam/intrinsics/`, `tests/aprilcam/daemon/` intrinsics RPC tests, `tests/aprilcam/client/` models tests, `tests/aprilcam/mcp/` intrinsics tests, `tests/test_no_cv2_in_client_modules.py`
- **New tests to write**: session fills/clears outline (fake detector); proto round trip with 0, 1, 2 polygons; MCP dict contains outline
- **Verification command**: `uv run pytest tests/aprilcam/intrinsics tests/aprilcam/client tests/aprilcam/mcp tests/aprilcam/daemon tests/test_no_cv2_in_client_modules.py -k "intrinsics or no_cv2"`
