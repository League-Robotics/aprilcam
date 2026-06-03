---
id: '001'
title: Restructure pyproject.toml extras
status: done
use-cases:
- SUC-001
- SUC-002
- SUC-004
depends-on: []
github-issue: ''
issue: plan-split-client-vs-daemon-dependencies.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Restructure pyproject.toml extras

## Description

Replace the monolithic `[project] dependencies` list in `pyproject.toml` with a
narrow base set and four optional extras. This is the foundational packaging change
that makes the other tickets possible.

The current flat list includes opencv-contrib-python, mss, mcp, websockets, fpdf2,
cv2-enumerate-cameras, msgpack, pillow, grpcio-reflection, zeroconf, ipykernel,
ipython, jupyter, and anthropic — all unconditional. After this ticket, only the
lightweight gRPC client stack is in base.

Also bump the package version to `0.20260603.1`.

## Acceptance Criteria

- [x] `[project] dependencies` in `pyproject.toml` contains exactly and only:
  `grpcio>=1.60`, `protobuf>=4.25`, `pydantic>=2.0`, `numpy>=1.23`,
  `python-dotenv>=1.0`, `rich>=13.0`.
- [x] `[project.optional-dependencies]` defines four extras:
  - `imaging = ["opencv-contrib-python>=4.8"]`
  - `daemon = ["aprilcam[imaging]", "mss>=9.0", "mcp>=1.0", "websockets>=12.0", "fpdf2>=2.7", "cv2-enumerate-cameras>=1.3", "msgpack>=1.0", "pillow>=10.0", "grpcio-reflection>=1.60", "zeroconf>=0.131"]`
  - `dev = ["ipykernel>=7.2.0", "ipython>=8.39.0", "jupyter>=1.1.1", "anthropic>=0.104.0", "grpcio-tools>=1.60"]`
  - `playfield = ["pygame>=2.5"]`
- [x] The `[dependency-groups] dev` section is retained as-is (uv local dev use).
- [x] `grpcio-tools` is removed from base `dependencies` (it moves to `dev` extra).
- [x] Package version is bumped to `0.20260603.1` in `[project] version`.
- [x] `pip install -e .` succeeds in a fresh venv and does not pull OpenCV, mss, mcp,
  jupyter, anthropic, fpdf2, pillow, grpcio-reflection, zeroconf, cv2-enumerate-cameras,
  or msgpack.
- [x] `pip install -e '.[daemon]'` in a fresh venv pulls opencv, mss, mcp, and the
  full server stack.

## Implementation Plan

### Approach

Edit `pyproject.toml` directly:
1. Replace the `dependencies = [...]` block with the six base packages.
2. Add `imaging`, `daemon`, `dev`, and updated `playfield` entries under
   `[project.optional-dependencies]`.
3. Update the `version` field.

Note: the `[dependency-groups]` section (uv) is separate from
`[project.optional-dependencies]` and is left unchanged.

### Files to Modify

- `pyproject.toml` — rewrite `dependencies` and `optional-dependencies` sections;
  bump `version`.

### Testing Plan

After editing:
1. Create a temporary fresh venv: `python -m venv /tmp/test-base && /tmp/test-base/bin/pip install -e /path/to/project`
2. Verify: `python -c "import grpc; import pydantic; import numpy; print('base ok')"`
3. Verify cv2 absent: `python -c "import cv2"` should fail with `ModuleNotFoundError`.
4. Verify pip list: `pip list | grep -E 'opencv|mss|mcp|jupyter|anthropic'` — should produce no output.
5. Separately test daemon extra: `pip install -e '.[daemon]'` → `python -c "import cv2; print('ok')"` succeeds.
6. Run `uv run pytest` from the project root to confirm existing tests still pass
   (will fail with cv2 errors until ticket 003 adds skip markers — that's expected).

### Documentation Updates

None in this ticket. README update is ticket 004.

### Commit Message

`feat(packaging): split client vs daemon dependencies into extras [010-001]`

Then: `chore: bump version` (per project rules).
