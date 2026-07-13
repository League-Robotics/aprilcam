---
id: 009
title: Write unit tests for daemon components (protocol, backpressure, spawn-race,
  config)
status: done
use-cases:
- SUC-001
- SUC-002
- SUC-003
- SUC-004
- SUC-007
depends-on:
- 008
github-issue: ''
issue: aprilcam-daemon-architecture.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Write unit tests for daemon components (protocol, backpressure, spawn-race, config)

## Description

Write the new unit test files covering the daemon components introduced in
T001-T008. All tests must run without camera hardware (mock where necessary)
and must pass with `uv run pytest`. Also confirm the existing sprint-001
tests still pass after all the refactoring.

## Acceptance Criteria

- [x] `tests/test_config_loader.py` — covers `Config.load()`:
  - [x] Default values when no config sources exist.
  - [x] Env var overrides file value.
  - [x] Project-local `.aprilcam` overrides `~/.aprilcam`.
  - [x] `socket_dir` created when it does not exist.
  - [x] `AppConfig.load()` still works (regression test).
- [x] `tests/test_daemon_protocol.py` — covers `encode_frame` / `decode_frame`
  / `read_frame`:
  - [x] Round-trip with all fields populated.
  - [x] Round-trip with `homography=None`.
  - [x] `read_frame` handles partial recv (two-chunk send).
  - [x] `read_frame` raises `ConnectionError` on EOF.
- [x] `tests/test_daemon_backpressure.py` (or added to an existing file) —
  covers `CameraPipeline` subscriber drop:
  - [x] With a subscriber queue at `maxsize=2` and the capture loop producing
    3 frames, the subscriber receives at most 2 frames (drop-on-full verified).
  - [x] The capture loop is not blocked by the full queue.
- [x] `tests/test_daemon_spawn_race.py` — covers `ensure_running` spawn guard:
  - [x] Two threads call `ensure_running()` concurrently with no daemon running
    (mocked `subprocess.Popen`); assert `Popen` was called exactly once.
  - [x] `ensure_running()` raises `RuntimeError` if the socket does not appear
    within the timeout (mock the socket poll).
- [x] `tests/test_mcp_path_tools.py` extended with:
  - [x] `test_path_tools_write_paths_json` — after calling the `create_path`
    handler with mocked `ensure_running` and `info.json`, asserts that
    `paths.json` exists and is valid JSON matching registry state.
- [x] All existing sprint-001 tests pass: `test_paths.py`,
  `test_mcp_path_tools.py` (existing cases), `test_draw_paths.py`.
- [x] `uv run pytest` exits 0 with no failures.

## Implementation Plan

### Approach

Use `pytest`, `tmp_path` fixture for filesystem tests, `socket.socketpair()`
for protocol tests, `threading.Thread` for spawn-race tests, and
`unittest.mock.patch` for mocking `subprocess.Popen`.

### Files to Create

- `tests/test_config_loader.py`
- `tests/test_daemon_protocol.py`
- `tests/test_daemon_backpressure.py`
- `tests/test_daemon_spawn_race.py`

### Files to Modify

- `tests/test_mcp_path_tools.py` — add `test_path_tools_write_paths_json`.

### Notes

- Backpressure test: instantiate `CameraPipeline` with a mock `VideoCapture`
  that returns synthetic frames. Or test the queue drop logic directly without
  starting the camera thread.
- Spawn-race test: `ensure_running` will try to connect to the daemon's
  control socket — mock the socket connection attempt to fail initially, then
  succeed after a simulated spawn delay.
- Config tests: use `tmp_path` to write temp dotfiles; monkeypatch
  `Path.home()` to a temp directory.

### Documentation Updates

No external documentation. Test file docstrings describe what each test suite
covers.
