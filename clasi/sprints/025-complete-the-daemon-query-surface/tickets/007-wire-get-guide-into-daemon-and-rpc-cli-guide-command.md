---
id: '007'
title: Wire get_guide into daemon and RPC, CLI guide command
status: open
use-cases:
- SUC-003
depends-on:
- '006'
github-issue: ''
issue: complete-the-daemon-query-surface.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Wire get_guide into daemon and RPC, CLI guide command

## Description

Wire ticket 006's shared guide-content module into `Daemon.get_guide`
(`daemon/service.py`), add the `GetGuide` RPC, and add the top-level
CLI command `aprilcam guide [name]`. This is the last ticket in the
sprint's serial execution order — by the time it completes, all three
of the issue's Verification bullets have been satisfied (see the last
acceptance criterion below), which is why it carries
`completes_issue: true`.

**`DaemonService.get_guide(name=None)`**: reads from the same
`aprilcam/guides.py` module ticket 006's `client.guides.Guides`
reads — **not** a redundant reimplementation, so the two accessors are
byte-identical by construction, exactly as `sprint.md`'s Design
Rationale requires. Unknown name -> `errors.UnknownGuide`, same as the
client-side path.

**Proto**: `rpc GetGuide (GetGuideRequest) returns
(GetGuideResponse)`, `GetGuideRequest{optional string name = 1;}`,
`GetGuideResponse{string text = 1;}`.

**`client/daemon_client.py`**: one new `GrpcDaemon.get_guide` stub,
mirroring the existing pattern.

**CLI**: `aprilcam guide [name]` — **no `-d`/`--daemon` option, no
`Discovery` call** — reads locally via `client.guides.Guides()`
directly, matching UC-27's own resolved decision ("available
everywhere... a human reading the agent guide is exactly how the
conventions get reviewed") and the no-daemon-needed requirement. This
is a deliberate exception to every other CLI command in this codebase,
which all go through `Discovery`/`Daemon` — add a one-line comment in
the CLI module explaining why, so a future maintainer doesn't "fix" it
to match the others.

Test that proves the single-source-of-truth guarantee concretely:
fetch the guide via the CLI (no daemon), fetch it via a live daemon's
`GetGuide` RPC, and assert the two are byte-identical.

## Acceptance Criteria

- [ ] `Daemon.get_guide` implemented on `DaemonService`, reading from
      ticket 006's shared module.
- [ ] `GetGuide` RPC round-trips correctly through a real gRPC
      channel.
- [ ] `aprilcam guide` and `aprilcam guide robot` both work with **no
      daemon started anywhere** (CLI reads locally).
- [ ] `aprilcam guide nonexistent` reports the guides that do exist
      (`agent`, `robot`), matching UC-27.S-3.
- [ ] Test proves `Daemon.get_guide(name)` and
      `client.guides.Guides().get(name)` return byte-identical text
      for every valid name — not merely similar.
- [ ] This ticket's acceptance criteria, combined with tickets 002 and
      005, jointly satisfy the issue's full Verification section
      (`playfield where` returns all matches with no camera opened; a
      registered mount changes the reported position and unregistering
      reverts it; the guide is fetchable with no daemon running) — the
      point at which this sprint's issue is fully addressed.
- [ ] `uv run mypy` clean.
- [ ] Full suite green, headless and with hardware attached (baseline
      832 tests passing: 795 headless + 37 hardware, plus everything
      this sprint adds).

## Implementation Plan

**Approach**: proto + `DaemonService.get_guide` + RPC dispatch +
client stub first (mirrors every prior RPC-wiring ticket in this
sprint), CLI last since it deliberately does *not* go through the
client/RPC path at all for its primary no-daemon use — only the
byte-identical-text test exercises both paths together.

**Files**: `proto/aprilcam_v2.proto` (extend), regenerated stubs,
`src/aprilcam/daemon/service.py` (extend),
`src/aprilcam/daemon/rpc.py` (extend),
`src/aprilcam/client/daemon_client.py` (extend), a new top-level CLI
module or an addition to `cli/__init__.py` for the bare `aprilcam
guide` command.

**Testing plan**:
- **Existing tests to run**: `uv run pytest
  tests/aprilcam/daemon/test_service.py tests/aprilcam/cli/test_main.py
  -v`, plus the full headless suite.
- **New tests to write**: `DaemonService.get_guide` unit tests
  (default name, explicit name, unknown name); a gRPC round-trip test;
  a CLI test proving no `Discovery`/`Daemon` object is constructed for
  `aprilcam guide`; the byte-identical cross-path test described
  above.
- **Verification command**: `uv run pytest -m "not camera"` (CI) and
  full `uv run pytest` plus `uv run mypy` on this development machine
  before considering the sprint's issue closed.

**Documentation updates**: none beyond docstrings.
