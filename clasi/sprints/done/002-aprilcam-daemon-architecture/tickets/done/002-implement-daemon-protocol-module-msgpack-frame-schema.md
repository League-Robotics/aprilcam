---
id: '002'
title: Implement daemon protocol module (msgpack frame schema)
status: done
use-cases:
- SUC-002
- SUC-003
- SUC-005
- SUC-006
depends-on:
- '001'
github-issue: ''
issue: aprilcam-daemon-architecture.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Implement daemon protocol module (msgpack frame schema)

## Description

Create the new module `src/aprilcam/daemon/protocol.py` that defines the
per-frame message schema and provides encode/decode functions. This is the
shared contract between the daemon (which encodes) and all subscribers (which
decode). It must be importable with no runtime dependencies other than
`msgpack` and stdlib.

Also create the package marker `src/aprilcam/daemon/__init__.py` (empty) and
add `msgpack` to `pyproject.toml` dependencies.

## Acceptance Criteria

- [x] `src/aprilcam/daemon/__init__.py` created (empty package marker).
- [x] `src/aprilcam/daemon/protocol.py` created with:
  - `SCHEMA_VERSION: int = 1` constant.
  - `FrameMessage` dataclass with fields: `schema: int`, `frame_id: int`,
    `ts_mono_ns: int`, `ts_wall_ms: int`, `frame_jpeg: bytes`, `frame_w: int`,
    `frame_h: int`, `tags: list[dict]`, `homography: list[list[float]] | None`,
    `playfield_corners: list[list[float]]`, `paths_file: str`, `fps: float`.
  - `encode_frame(msg: FrameMessage) -> bytes` — msgpack-packs the dataclass
    as a dict, prepends a 4-byte big-endian uint32 length prefix.
  - `decode_frame(data: bytes) -> FrameMessage` — strips the length prefix,
    msgpack-unpacks, constructs and returns a `FrameMessage`.
  - `read_frame(sock: socket.socket) -> FrameMessage` — reads exactly 4 bytes
    for the length, then exactly N bytes for the payload; calls `decode_frame`.
    Raises `ConnectionError` on EOF.
- [x] `pyproject.toml` has `msgpack` added to `[project.dependencies]`.
- [x] Round-trip: `decode_frame(encode_frame(msg)) == msg` for a fully populated
  message.
- [x] `read_frame` handles data arriving in two recv chunks (partial read).
- [x] `homography=None` round-trips correctly (msgpack nil).

## Implementation Plan

### Approach

Pure Python + msgpack. Use `dataclasses.asdict()` for encoding. Keep the
module free of OpenCV, numpy, and AprilCam domain imports so it can be unit-
tested in isolation. Use `raw=False` in msgpack pack/unpack so string keys
decode as `str`.

### Files to Create

- `src/aprilcam/daemon/__init__.py` (empty)
- `src/aprilcam/daemon/protocol.py`

### Files to Modify

- `pyproject.toml` — add `"msgpack>=1.0"` to `[project.dependencies]`.

### Notes

- `tags` field: each tag record is a plain dict. The daemon populates this
  from its `TagRecord` objects via `to_dict()`. Protocol does not import
  `TagRecord`.
- `homography` is `None` when camera is uncalibrated; encoder writes nil,
  decoder returns `None`.

### Testing Plan

New file `tests/test_daemon_protocol.py`:
- Round-trip with all fields populated.
- Round-trip with `homography=None` and empty `tags`.
- `read_frame` using `socket.socketpair()` — data sent in one `send`.
- `read_frame` — data split across two sends (partial read test).
- `read_frame` raises `ConnectionError` on immediate socket close.

### Documentation Updates

Module-level docstring in `protocol.py` describes the schema version and
framing format. No external docs needed.
