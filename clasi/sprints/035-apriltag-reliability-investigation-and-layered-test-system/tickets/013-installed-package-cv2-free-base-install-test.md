---
id: '013'
title: Installed-package cv2-free base-install test
status: open
use-cases: [UC-10]
depends-on: []
github-issue: ''
issue: aprilcam-v2-layered-test-system.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Installed-package cv2-free base-install test

## Description

Turns the 2026-08-23 "pipx daemon silently sees zero cameras" incident
(`docs/knowledge/daemon-restart-launches-pipx-build-no-cameras.md`) into
a permanent regression test, and proves the base-install/cv2-free
packaging promise for real rather than by convention. This test is
camera-free by design — it round-trips camera-free data (playfield push,
annotation draw/list/clear, guide) against a private isolated daemon run
from the dev venv, from a consumer script running inside a genuinely
separate, freshly built, base-only-install venv.

## Acceptance Criteria

- [ ] `uv build` produces a wheel; a fresh venv is created and
      `pip install`s that wheel with the BASE extra only (no `[daemon]`).
- [ ] `import aprilcam.client` succeeds in that fresh venv.
- [ ] `import cv2` FAILS in that fresh venv (the actual proof of the
      cv2-free base install — not inferred from `pyproject.toml`).
- [ ] The console script (`aprilcam`) exists in the fresh venv's `bin/`
      and `aprilcam -V` reports a version matching the built wheel.
- [ ] A consumer script, run from the fresh venv's Python, connects to a
      private isolated daemon (started from the dev venv, which does
      have the `[daemon]` extra) and round-trips camera-free data:
      `set_playfield`/`get_playfield`, `draw_marker`/`list_annotations`/
      `clear_layer`, `get_guide`.
- [ ] The build artifact and fresh venv are cleaned up after the test
      (temp directories, not left in the repo working tree).
- [ ] Marked `@pytest.mark.integration` (daemon-dependent, no camera
      needed — not `@pytest.mark.camera`).

## Acknowledged tension with design.md §9 item 1

`docs/design/design.md` §9 records that `opencv-contrib-python` is
*currently* a base dependency in `pyproject.toml`, not gated behind the
`daemon` extra — a known, documented gap, not something this ticket is
scoped to close. **If this test is written and fails because `cv2` is
still importable in a genuinely base-only install**, that is expected
given the current known gap, not a bug in this test. In that case:
record the failure and its cause plainly (do not silently skip or
weaken the assertion to make it pass), and surface this as an explicit
open item for the team-lead rather than quietly declaring victory — this
test's job is to make the gap starkly visible until a future sprint
closes it, per design.md §9's own framing of that gap as real and
current, not to hide it.

## Implementation Plan

**Approach**: Use `uv build`/a `tempfile`-created venv/`subprocess` pip
install, matching how `pyproject.toml`'s own build backend is already
invoked elsewhere in this project's tooling. The consumer script is a
short, throwaway `.py` file written to the temp venv's own directory,
invoked via that venv's own `python` binary — never imported into the
test process itself (which already has cv2 installed via the dev venv,
so an in-process import check would prove nothing).

**Files to create**:
- `tests/packaging/__init__.py` (new package)
- `tests/packaging/test_installed_package.py`

**Testing plan**:
- No camera hardware needed; a real daemon subprocess and a real build
  are needed.
- **Verification command**: `uv run pytest tests/packaging/test_installed_package.py -m integration -v`

**Documentation updates**:
- If the cv2-free assertion fails per the "Acknowledged tension" section
  above, note the observed failure in this ticket's implementation notes
  for the team-lead's sprint-close report — no `docs/` edit required
  beyond that, since design.md §9 already documents the gap.
