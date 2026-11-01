---
id: '004'
title: Test-card renderer
status: done
use-cases:
- SUC-004
depends-on: []
github-issue: ''
issue: camera-hotplug-rock-solid-drop-reopen-and-test-card.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Test-card renderer

## Description

New daemon-side leaf module rendering a "NO SIGNAL" test card with
OpenCV/numpy (daemon-only dependency): RCA Indian-head card geometry --
bullseye rings, resolution wedges, gray-scale step bars, crosshair --
with the camera number/name and "NO SIGNAL" in the centre. No caricature
head. Size chain: camera's configured frame size, else last-opened size,
else 1280x720. Cached per size (label drawn per request or cached per
size+label). For playfield/deskewed requests the card is returned at the
playfield output size with no warp. Behavioural base in interfaces.py
raises NotImplementedError.

## Plan

- Files: `src/aprilcam/daemon/testcard.py` (+ interface base),
  size-resolution helper.

## Acceptance Criteria

- [x] `render(width, height, number, name)` returns a BGR array of exactly that size.
- [x] Output contains rings, wedges, gray steps, crosshair and the label text (structure checks in tests).
- [x] Size fallback chain implemented and tested.
- [x] Per-size cache; repeated calls are cheap.

## Testing

- **Existing tests to run**: `uv run pytest tests/daemon -q`
- **New tests to write**: shape, determinism, step-bar monotonic gray values, fallback sizes, cache reuse.
- **Verification command**: `uv run pytest tests/daemon -q -k testcard`
