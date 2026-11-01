---
id: '005'
title: Proto placeholder flag; serve test card on frames and streams
status: open
use-cases:
- SUC-004
depends-on:
- '002'
- '004'
github-issue: ''
issue: camera-hotplug-rock-solid-drop-reopen-and-test-card.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Proto placeholder flag; serve test card on frames and streams

## Description

Add an additive `placeholder` bool to frame responses and image stream
messages, and a camera-absent status to tag-stream heartbeats (regenerate
stubs). When a camera is ABSENT or reopening, `get_frame`, playfield
image and image streams return the test card (flag true) instead of the
INTERNAL "no frame available" error (`service.py:943-944, 1018-1019`) or
silence (`streaming.py:476-498, 856-858`); tag/detection streams keep
sending heartbeats with an absent status. Real frames carry flag false
and resume automatically on return.

## Plan

- Files: proto + generated stubs, `daemon/service.py`,
  `daemon/streaming.py`. Keep service additions thin; renderer from 004.

## Acceptance Criteria

- [ ] Unplugged camera: get_frame returns the flagged card at configured size, not an error.
- [ ] Playfield image and image streams return the flagged card at playfield/camera size.
- [ ] Tag stream heartbeats carry an explicit absent status instead of going silent.
- [ ] Real frames have flag false; old clients unaffected (additive fields).

## Testing

- **Existing tests to run**: `uv run pytest tests/daemon tests/proto -q`
- **New tests to write**: service/streaming tests with an absent fake camera, flag on/off, heartbeat status, resume.
- **Verification command**: `uv run pytest tests/daemon -q`
