---
id: '002'
title: 'Definition schema on the wire: proto, rpc and client models'
status: open
use-cases:
- SUC-002
depends-on: ['001']
github-issue: ''
issue: pose-based-playfield-calibration-with-marker-corners-and-measured-lens.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Definition schema on the wire: proto, rpc and client models

## Description

Carry the ticket-001 schema over the wire so a pushed definition keeps its
marker sizes, rotations and policies (the CLI loads a file client-side and
pushes it with `SetPlayfield`; the daemon rebuilds the definition in
`daemon/rpc.py`). Without this, a definition would silently lose `size_mm` in
transit and every field would fall back to centre-point.

Additive proto fields (follow sprint 044 tickets 006 and 010):
`PlayfieldFeatureEntry`: `optional double size_mm`, `optional double
rotation_deg`, `optional string policy`; `PlayfieldDefinition`: `optional
double default_marker_size_mm`. Absence encodes "unset" (use `optional` so
`HasField` distinguishes 0/unset where it matters).

## Acceptance Criteria

- [ ] `proto/aprilcam_v2.proto` updated with comments naming the mirrored
      Python fields; regenerated with
      `uv run --extra dev python scripts/compile_proto.py` (generated files
      committed).
- [ ] `daemon/rpc.py` `_playfield_*` conversions and
      `client/models.py` / `client/daemon_client.py` `set_playfield` /
      `get_playfield` round-trip all four fields.
- [ ] A definition with no new keys produces a message with none of the new
      fields set, and an old daemon/new client (or reverse) interoperates.
- [ ] `get_playfield` returns the stored sizes/policies (readable by
      `aprilcam playfield show`, if it prints definitions).

## Implementation Plan

**Approach.** Pure plumbing. Copy the `height_cm` conversion pattern.

**Files to modify:** `proto/aprilcam_v2.proto`, `src/aprilcam/proto/*` (generated),
`src/aprilcam/daemon/rpc.py`, `src/aprilcam/client/models.py`,
`src/aprilcam/client/daemon_client.py`, `src/aprilcam/mcp/tools/*` only if a
tool serialises definitions field-by-field.

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/daemon/test_rpc.py tests/aprilcam/client tests/test_no_cv2_in_client_modules.py`
- **New tests to write**: proto round-trip for populated and empty new fields;
  `set_playfield` -> `get_playfield` through the in-process gRPC fixture.
- **Verification command**: `uv run pytest tests/aprilcam/daemon tests/aprilcam/client`

## Documentation Updates

None beyond proto comments (ticket 012 covers prose docs).
