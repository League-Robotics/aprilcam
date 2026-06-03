---
id: '010'
title: Split Client vs. Daemon Dependencies
status: done
branch: sprint/010-split-client-vs-daemon-dependencies
use-cases:
- SUC-001
- SUC-002
- SUC-003
- SUC-004
- SUC-005
issues:
- plan-split-client-vs-daemon-dependencies.md
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 010: Split Client vs. Daemon Dependencies

## Goals

Make `pip install aprilcam` a lightweight gRPC client install. Move the heavy
daemon/server stack (OpenCV, mss, mcp, etc.) behind an `aprilcam[daemon]`
optional extra. Provide a guided error when frame-decode is attempted without
the `[imaging]` extra. Ensure tests pass cleanly in both configurations.

## Problem

`pip install aprilcam` currently pulls ~90 MB of C-extension wheels
(opencv-contrib-python, mss, fpdf2, pillow) plus jupyter, ipython, and
anthropic — even for a user who only wants the client stub to talk to an
already-running daemon over gRPC. This slows installs, breaks on machines
without C-compiler tooling, and imposes a heavy environment on robot
controllers and CI agents that need none of it.

## Solution

Restructure `pyproject.toml` with a narrow base (grpcio, protobuf, pydantic,
numpy, python-dotenv, rich) and four extras:

- `imaging` — adds OpenCV only.
- `daemon` — full server stack, self-referentially includes `[imaging]`.
- `dev` — development tools (jupyter, ipython, anthropic, grpcio-tools).
- `playfield` — pygame (unchanged).

The only two `import cv2` statements on the client path (`control.py` and
`stream.py`) are moved behind a `require_cv2()` lazy helper that raises a
clear, actionable `RuntimeError` if OpenCV is absent. No API surface changes.
Test markers are added so `pytest` is green on a base install.

## Success Criteria

- `pip install aprilcam` installs in seconds with no OpenCV, mss, mcp,
  jupyter, or anthropic.
- `from aprilcam.client import DaemonControl` succeeds on a base install.
- `DaemonControl.capture_frame(...)` raises `RuntimeError` (not `ImportError`)
  with an `[imaging]` install hint when OpenCV is absent.
- `pip install 'aprilcam[daemon]'` restores the full server environment;
  daemon and MCP server start; `capture_frame` round-trip succeeds.
- `pytest` exits 0 in both base-install and daemon-install configurations.

## Scope

### In Scope

- Rewrite `[project] dependencies` and `[project.optional-dependencies]` in
  `pyproject.toml`.
- Add `src/aprilcam/client/_imaging.py` with the `require_cv2()` helper.
- Patch `client/control.py` and `client/stream.py`: remove top-level `import cv2`;
  add lazy `require_cv2()` calls at the two `imdecode` sites.
- Add pytest skip markers (`needs_cv2`, `needs_daemon`) to tests that require
  the respective extras; update `conftest.py`.
- Update README install documentation.
- Version bump per project rules (`0.20260603.N`).

### Out of Scope

- Changes to daemon, server, or CLI source (no behavior changes).
- Changes to the gRPC proto or generated stubs.
- Adding new test coverage beyond what is needed for marker/skip logic.
- Streamable HTTP or any other transport change.

## Test Strategy

Four verification scenarios drive acceptance:

1. **Base venv**: `pip install -e .` → client import succeeds, cv2 import
   fails, light CLI works, `pip list` clean.
2. **Guided error**: call `capture_frame` or `ImageStreamConsumer.read` on
   base install → `RuntimeError` with `[imaging]` message.
3. **Daemon venv**: `pip install -e '.[daemon]'` → daemon starts, MCP starts,
   `capture_frame` round-trip succeeds.
4. **pytest clean**: `pytest` exits 0 in both venvs (daemon tests skip in
   base venv).

## Architecture Notes

The key structural insight: the CLI dispatcher is already fully lazy (no
heavy imports at `aprilcam.cli` module level). The only client-path `import cv2`
statements are two `imdecode` calls; `client/models.py` is pure pydantic;
`client/__init__.py` imports only control/stream/models/proto. This means the
source change footprint is minimal — two files patched, one file added.

See `architecture-update.md` for the full module diagram and design rationale.

## GitHub Issues

None linked externally. Internal issue: `plan-split-client-vs-daemon-dependencies.md`.

## Definition of Ready

Before tickets can be created, all of the following must be true:

- [x] Sprint planning documents are complete (sprint.md, use cases, architecture)
- [ ] Architecture review passed
- [ ] Stakeholder has approved the sprint plan

## Tickets

| # | Title | Depends On |
|---|-------|------------|
| 001 | Restructure pyproject.toml extras | — |
| 002 | Add lazy cv2 helper and patch client modules | 001 |
| 003 | Add pytest skip markers for optional deps | 002 |
| 004 | Update README install documentation | 001 |

Tickets execute serially in the order listed.
