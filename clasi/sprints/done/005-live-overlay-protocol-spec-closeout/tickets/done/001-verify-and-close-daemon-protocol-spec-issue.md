---
id: '001'
title: Verify and close daemon protocol spec issue
status: done
use-cases:
  - SUC-001
depends-on: []
github-issue: ''
issue: aprilcam-daemon-protocol-specification.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Verify and close daemon protocol spec issue

## Description

The `aprilcam-daemon-protocol-specification.md` issue describes the intended gRPC
architecture for the AprilCam daemon. Sprint 004 implemented this architecture, but
the issue was never formally verified and closed. This ticket checks that every
major claim in the spec is satisfied by the actual implementation, then marks the
issue done.

This is a read-and-verify task — no code changes are expected. If a significant gap
is found, note it and open a new issue rather than expanding this ticket's scope.

## Acceptance Criteria

- [x] `proto/aprilcam.proto` exists and defines the `AprilCam` gRPC service with
      the RPCs described in the spec (ListCameras, OpenCamera, CloseCamera, GetTags,
      GetImageStream, GetTagStream, PublishOverlay as of Sprint 005, etc.).
- [x] `src/aprilcam/daemon/grpc_server.py` exists and implements `AprilCamServicer`.
- [x] `src/aprilcam/daemon/stream.py` exists with `ImageStreamProducer` and
      `TagStreamProducer`.
- [x] `src/aprilcam/client/control.py` exists with `DaemonControl`, including
      `connect_default`, `get_image_stream`, `get_tag_stream`.
- [x] `src/aprilcam/client/stream.py` exists with `ImageStreamConsumer` and
      `TagStreamConsumer`.
- [x] `src/aprilcam/client/models.py` exists with `TagRecord`, `TagFrame`,
      `CameraInfo`, `PathRecord` Pydantic models.
- [x] `src/aprilcam/daemon/mdns.py` exists and provides mDNS advertisement.
- [x] Any minor gaps between spec and implementation are noted in a comment; no
      significant unimplemented areas remain.
- [x] Issue `aprilcam-daemon-protocol-specification.md` is marked done via
      `move_issue_to_done` MCP tool.

<!-- Verification note: All files confirmed present and importable. 242 tests pass,
     8 skipped. One pre-existing failure in tests/system/test_video_pipeline.py
     due to missing test asset bright-gsc.mov — unrelated to daemon protocol spec. -->

## Implementation Plan

### Approach

1. Read `aprilcam-daemon-protocol-specification.md` (Part 2 — Proposed Architecture).
2. For each section (2.1 endpoints, 2.2 gRPC service, 2.3–2.4 stream protocols,
   2.5 producers, 2.6 consumers, 2.7 control, 2.8 models, 2.9 file list, 2.10
   discovery), check that the corresponding file and class/method exists.
3. Confirm pyproject.toml includes grpcio, grpcio-tools, grpcio-reflection,
   protobuf, zeroconf, pydantic.
4. Note any discrepancies — acceptable if functionally equivalent.
5. Call `move_issue_to_done` for `aprilcam-daemon-protocol-specification.md`.

### Files to Read

- `proto/aprilcam.proto`
- `src/aprilcam/daemon/grpc_server.py`
- `src/aprilcam/daemon/stream.py`
- `src/aprilcam/client/control.py`
- `src/aprilcam/client/stream.py`
- `src/aprilcam/client/models.py`
- `src/aprilcam/daemon/mdns.py`
- `pyproject.toml`

### Testing Plan

- `uv run python -c "from aprilcam.daemon import grpc_server; print('ok')"` — import check
- `uv run python -c "from aprilcam.client.control import DaemonControl; print('ok')"` — import check
- `uv run pytest tests/` — no regressions

### Documentation Updates

None — this is a verification and closeout task, not a code change.
