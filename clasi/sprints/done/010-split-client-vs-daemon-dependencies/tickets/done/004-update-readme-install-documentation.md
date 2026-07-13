---
id: '004'
title: Update README install documentation
status: done
use-cases:
- SUC-001
- SUC-002
- SUC-003
- SUC-004
depends-on:
- '001'
github-issue: ''
issue: plan-split-client-vs-daemon-dependencies.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Update README install documentation

## Description

With the extras now defined, the README install section must be updated to describe
the three install tiers. Users landing on the project page will otherwise not know
they need `[daemon]` to run the server, and developers will not know about `[imaging]`
or `[dev]`. This ticket closes the sprint by updating the user-facing documentation.

This ticket also closes the linked issue `plan-split-client-vs-daemon-dependencies.md`
(all work complete).

## Acceptance Criteria

- [x] `README.md` contains an "Installation" or "Getting Started" section that
  documents at minimum these four install commands with a brief description of each:
  - `pip install aprilcam` — lightweight gRPC client; connect to a running daemon.
  - `pip install 'aprilcam[imaging]'` — client with frame-decode support (adds OpenCV).
  - `pip install 'aprilcam[daemon]'` — full server stack; run the daemon and MCP server.
  - `pip install 'aprilcam[dev]'` — development tools (jupyter, ipython, anthropic).
- [x] Any existing README content that implies a single monolithic install
  (`opencv-contrib-python` in base deps, etc.) is updated or removed.
- [x] The README note about `pipx install aprilcam` (if present) mentions that
  `pipx install 'aprilcam[daemon]'` is needed for the full server.
- [x] `uv run pytest` still exits 0 after these changes (documentation-only, no
  test regressions expected).

## Implementation Plan

### Approach

Read the current `README.md` to locate the existing installation section. Update or
add an "Installation" section covering the four install tiers above. Keep changes
minimal — only the installation section needs to change. Do not restructure or
reformat other sections.

### Files to Modify

- `README.md` — installation section update.

### Testing Plan

No automated tests for README content. Manual verification:
- Read the updated section to confirm all four install commands are present.
- Run `uv run pytest` to confirm no regressions.

### Documentation Updates

This ticket IS the documentation update. No other files need changing.

### Commit Message

`docs: update README with extras-based install tiers [010-004]`
