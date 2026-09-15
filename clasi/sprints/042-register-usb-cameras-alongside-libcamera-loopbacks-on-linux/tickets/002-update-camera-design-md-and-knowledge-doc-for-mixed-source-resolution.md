---
id: '002'
title: Update camera/DESIGN.md and knowledge doc for mixed-source resolution
status: open
use-cases:
- SUC-001
depends-on:
- '001'
github-issue: ''
issue: usb-cameras-ignored-on-a-linux-host-that-also-has-libcamera-loopbacks.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Update camera/DESIGN.md and knowledge doc for mixed-source resolution

## Description

Ticket 001 changed `camera/identity.py`'s enumeration-merge contract
(libcamera and cv2 sources merge on a shared `/dev/videoN` device-index
domain instead of one source winning outright). Document that contract
in the two places this project's process requires: the subsystem design
doc and the operational knowledge base.

1. **`src/aprilcam/camera/DESIGN.md`** — this file already documents
   `resolve_identity`/`resolve_all` around lines 56, 335, and 344 (see
   the current text: "`resolve_all()` gathers each enumeration source
   table once (avoiding a ... `resolve_identity(index, ...)` tries, in
   order: (1) a libcamera hit at ..."). Update these passages — and any
   other place in this file that describes `resolve_all` as choosing
   one source over the other — to describe the merge: both sources are
   gathered, keyed by the same physical device-index domain on Linux,
   and every currently-enumerated camera resolves exactly once,
   loopback identities taking priority at a shared key. Name the
   dedup rule explicitly (a loopback appearing in both sources
   resolves once, under its libcamera identity) since that is the
   behavior this sprint's issue was filed against.
2. **`docs/knowledge/raspberry-pi-camera-setup.md`** — add a short,
   focused entry (follow this file's existing per-topic style) noting
   that a Pi can have a USB/UVC camera and a CSI camera (bridged via
   `v4l2loopback`) attached simultaneously, and that both are now
   registered — this is the "going forward" expected setup per the
   stakeholder, not an edge case. Reference the sprint 042 issue/fix by
   name so a future reader knows why this note exists and where the
   fix landed.

This is a documentation-only ticket — no source code changes. Do not
touch `src/aprilcam/camera/identity.py` or any other production module;
if you find the docs need to say something ticket 001 didn't actually
implement, that's a signal to re-check ticket 001's diff, not to
document aspirational behavior.

## Acceptance Criteria

- [ ] `src/aprilcam/camera/DESIGN.md`'s description of
      `resolve_identity`/`resolve_all` accurately reflects the merged,
      device-index-keyed behavior shipped in ticket 001 (no remaining
      text describing the old either/or early-return shape).
- [ ] `docs/knowledge/raspberry-pi-camera-setup.md` has a new entry (or
      addition to an existing entry) stating that USB and CSI cameras
      coexist on the same host and are both registered.
- [ ] Both docs are internally consistent with each other and with the
      actual code in `camera/identity.py` as of ticket 001.

## Testing

Documentation ticket — no test suite changes expected. Confirm no
production or test files changed:

- **Existing tests to run**: none required (no source change); optionally
  `uv run pytest tests/aprilcam/camera/test_identity.py -m "not camera and not integration"`
  as a sanity check that nothing was accidentally touched.
- **New tests to write**: none.
- **Verification command**: `git diff --stat` should show only the two
  doc files (plus this ticket's own frontmatter/status update).
