---
id: '001'
title: Add text field to proto and regenerate bindings
status: done
use-cases:
  - SUC-001
depends-on: []
github-issue: ''
issue: plan-add-text-rect-and-polygon-overlay-element-types.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Add text field to proto and regenerate bindings

## Description

The `OverlayElement` protobuf message needs a `string text = 5` field to carry
the string content for `"text"` type overlay elements. After adding the field,
Python bindings must be regenerated and the grpc file's import line fixed.

This ticket is a hard prerequisite for all other tickets in the sprint — no other
file can import `aprilcam_pb2` safely until the regeneration is complete.

## Acceptance Criteria

- [ ] `proto/aprilcam.proto` contains `string text = 5;` inside `OverlayElement`
- [ ] `src/aprilcam/proto/aprilcam_pb2.py` reflects the new field (regenerated)
- [ ] `src/aprilcam/proto/aprilcam_pb2_grpc.py` uses `from aprilcam.proto import aprilcam_pb2 as aprilcam__pb2`
- [ ] Smoke check passes: `uv run python -c "from aprilcam.proto import aprilcam_pb2; print(aprilcam_pb2.OverlayElement(text='hi'))"`
- [ ] `uv run pytest tests/ --ignore=tests/system -q` passes with no regressions

## Implementation Plan

### Approach

Edit the proto, regenerate, fix import.

### Files to Modify

| File | Change |
|------|--------|
| `proto/aprilcam.proto` | Add `string text = 5;` to `OverlayElement` |
| `src/aprilcam/proto/aprilcam_pb2.py` | Regenerated (do not edit by hand) |
| `src/aprilcam/proto/aprilcam_pb2_grpc.py` | Regenerated, then fix one import line |

### Proto Change

In `proto/aprilcam.proto`, update `OverlayElement` to:

```protobuf
message OverlayElement {
  string         type      = 1;
  repeated float params    = 2;
  repeated int32 color     = 3;
  int32          thickness = 4;
  string         text      = 5;  // content for "text" type elements
}
```

### Regeneration Command

```bash
uv run python -m grpc_tools.protoc \
  -I proto \
  --python_out=src/aprilcam/proto \
  --grpc_python_out=src/aprilcam/proto \
  proto/aprilcam.proto
```

### Import Fix

After regeneration, in `src/aprilcam/proto/aprilcam_pb2_grpc.py` change:

```python
import aprilcam_pb2 as aprilcam__pb2
```

to:

```python
from aprilcam.proto import aprilcam_pb2 as aprilcam__pb2
```

### Testing Plan

- Smoke import: `uv run python -c "from aprilcam.proto import aprilcam_pb2; print(aprilcam_pb2.OverlayElement(text='hi'))"`
- Full suite: `uv run pytest tests/ --ignore=tests/system -q`

### Documentation Updates

None required for this ticket.
