---
id: '006'
title: Packaged guide content and no-daemon client accessor
status: done
use-cases:
- SUC-003
depends-on: []
github-issue: ''
issue: complete-the-daemon-query-surface.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Packaged guide content and no-daemon client accessor

## Description

Author the packaged guide text and the shared leaf module both
accessors read: a new `aprilcam/guides.py` (a `_GUIDE_DIR`-relative
pair of markdown files, mirroring v1's `AGENT_GUIDE.md`/
`ROBOT_API_GUIDE.md` exactly, read by a `read_guide(name)` function
structurally identical to v1's 21-line
`archive/src/aprilcam/guides.py`).

Two guides, matching v1's split: `"agent"` (primary, MCP-agent-facing)
and `"robot"` (Python/CLI robot-programmer-facing); an omitted name
defaults to `"agent"` (the primary actor for UC-27, A-02).

**Content is rewritten for v2's conventions, not ported.** v1's guide
describes mechanisms v2 no longer has (`open_camera`/`create_playfield`
MCP tools, mobile-tag MCP tool names, homography JSON files, `age`-
based tag staleness) — none of that applies here. Cross-check every
claim against `types.py`, `daemon/interfaces.py`, and the UC-01...UC-28
scenarios rather than trusting v1's text as ground truth:

- World frame: A1-centred, +x east, +y north, cm; heading/yaw 0 =
  east, CCW positive (`types.py`'s own module docstring, word for
  word).
- Tag addressing: family + number, never a bare id
  (`TagFamily.APRILTAG`/`.ARUCO`, UC-09).
- Bring-up order: UC-01 (start daemon) -> UC-02 (push/load playfield)
  -> UC-03 (configure camera) -> UC-04 (calibrate) -> UC-05 (confirm
  ready) -> UC-06 onward (query).
- What "uncalibrated" means: pixel-only results, explicitly flagged,
  never presented as world coordinates (UC-09.S-5, UC-10.S-3).
- Surveyed-definition vs. vision: `find_feature` answers from the
  definition, not observation — a feature can be reported "there" even
  if physically removed (UC-08).
- Per-session re-registration: mount parameters (UC-12) and
  annotations (UC-28) are runtime-only and vanish on daemon restart —
  a client re-registers/re-draws at the start of every session, not
  just once ever.
- Examples use placeholder names (`arducam-ov9782-usb-camera`,
  `main-playfield`) presented explicitly as illustrative, not
  introspected — consistent with this sprint's static-content decision
  (`sprint.md`'s Design Rationale).

**`client/guides.py`** (new): concrete `Guides`
(`client.interfaces.Guides`), reading only from `aprilcam/guides.py` —
no daemon, no `Discovery`, no network. `get(name=None)` defaults to
`"agent"`; an unknown name raises `errors.UnknownGuide` naming the
guides that exist (`("agent", "robot")`) (UC-27.S-3). `available()`
returns `("agent", "robot")`.

**The critical test for this ticket**: construct `client.guides.
Guides()` and call `.get()`/`.available()` **with no `Daemon`, no
`Discovery`, and no daemon process running anywhere** — this is the
literal, mechanical proof of UC-27.S-1's "must work with no daemon
running" property, not merely "doesn't happen to fail today."

## Acceptance Criteria

- [x] `aprilcam/guides.py` holds packaged text for `"agent"` and
      `"robot"` guides, rewritten for v2 conventions (not a copy of
      v1's text).
- [x] `client/guides.py`'s `Guides` concrete implementation derives
      from `client.interfaces.Guides`.
- [x] UC-27.S-1: `Guides().get()` (no name — defaults to `"agent"`)
      succeeds with zero daemon/network objects constructed anywhere
      in the call path.
- [x] UC-27.S-2: the returned "agent" guide text contains the
      world-frame statement (+x east, +y north, A1-centred, heading 0
      = east CCW positive) verbatim or equivalently.
- [x] UC-27.S-3: `Guides().get("nonexistent")` raises
      `errors.UnknownGuide` naming `"agent"` and `"robot"` as the
      guides that exist.
- [x] `available()` returns exactly `("agent", "robot")`.
- [x] Guide text explicitly states the per-session re-registration
      rule for mounts (UC-12) and annotations (UC-28).
- [x] `uv run mypy` clean; `Guides` derives from the existing plain
      base class, no `typing.Protocol`.

## Implementation Plan

**Approach**: write the content first (informed by v1's guides as a
starting structure, rewritten fact-for-fact against v2's actual
conventions), then the thin `read_guide`/`Guides` accessor code, which
is almost entirely a port of v1's already-proven 21-line shape.

**Files**: `src/aprilcam/guides.py` (new, packaged guide text
module/files), `src/aprilcam/client/guides.py` (new).

**Testing plan**:
- **Existing tests to run**: `uv run pytest tests/aprilcam/client/
  -v`, plus the full headless suite.
- **New tests to write**: `tests/aprilcam/client/test_guides.py` —
  the no-daemon-constructed test (the key UC-27.S-1 proof), an
  unknown-name test, a content-contains-world-frame-statement test,
  and a default-name test.
- **Verification command**: `uv run pytest -m "not camera"` and
  `uv run mypy`.

**Documentation updates**: the guide text itself *is* the
documentation deliverable here — no separate `docs/` update needed
beyond what `sprint.md`/UC-27 already record.
