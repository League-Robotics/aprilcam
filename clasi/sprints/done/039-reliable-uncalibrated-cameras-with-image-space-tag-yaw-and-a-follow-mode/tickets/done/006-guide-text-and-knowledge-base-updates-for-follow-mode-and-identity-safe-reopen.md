---
id: '006'
title: Guide text and knowledge-base updates for follow mode and identity-safe reopen
status: done
use-cases: []
depends-on:
- '003'
- '005'
github-issue: ''
issue: ''
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Guide text and knowledge-base updates for follow mode and identity-safe reopen

## Description

Documentation-only ticket, not tied to a single issue (it closes out
knowledge/guide gaps across the whole sprint). Depends on 003 and 005
so it documents the shipped behavior, not the planned behavior.

1. **`cli/guide.py`'s bundled guide text** (`client.guides.Guides`,
   read with no daemon running per UC-27.S-1): add a short section
   covering `camera tags --follow`/`--json` — when to use it (watching
   an uncalibrated camera's tag rotation over time), and a one-line
   mention that `image_yaw_deg` is a diagnostic, image-pixel-space
   field distinct from the world-frame `yaw_rad` an agent sees once a
   camera is calibrated.
2. **`docs/knowledge/`**: add one entry documenting the AVFoundation
   positional-index-shift incident this sprint fixes — what was
   observed (a camera leaving/rejoining an overloaded USB bus caused a
   reopen to silently serve a different physical camera's frames), the
   root cause (a cached index reused across reopens), and the fix
   (re-resolve + verify identity on every open). This is exactly the
   kind of "hard-won operational experience" CLAUDE.md's knowledge-base
   section asks to capture, and it will save whoever debugs a similar
   symptom on Pi/libcamera hardware later from rediscovering it.
3. **`docs/design/use-cases/cli.md`**: UC-20's "Open questions" note
   ("Is there a machine-readable output mode (JSON) as well as the
   human table?") is now partially answered — add a short note (not a
   full rewrite of UC-20) pointing at `camera tags --follow --json` as
   the first instance, and add scenario UC-20.S-4 or fold a
   sentence into UC-20.S-3 covering the continuous/follow case,
   whichever reads more naturally alongside the existing scenarios. Do
   not renumber any existing UC or scenario.

## Acceptance Criteria

- [x] `aprilcam guide` (or the relevant named sub-guide) mentions
      `camera tags --follow`/`--json` and `image_yaw_deg`, readable
      with no daemon running (verify by running the guide command with
      no daemon started).
- [x] A new `docs/knowledge/` entry documents the stale-index/identity-
      substitution incident, its root cause, and the fix, named for the
      topic per the existing knowledge-base convention (one topic per
      file).
- [x] `docs/design/use-cases/cli.md`'s UC-20 open-question note is
      updated to reflect the `--json` exception this sprint ships; no
      existing UC/scenario numbers are changed or reused.
- [x] No source code changes in this ticket — docs and guide text only.

## Testing

- **Existing tests to run**: none expected to be affected; run
  `uv run pytest tests/aprilcam/cli/test_guide.py -m "not camera and not integration"` if a guide-content test exists, to confirm nothing asserts exact guide text that this change would break.
- **New tests to write**: none required (documentation change) unless
  an existing test asserts guide text verbatim, in which case update
  that test's expected fixture rather than adding a new test.
- **Verification command**: `uv run pytest tests/aprilcam/cli -m "not camera and not integration"`
