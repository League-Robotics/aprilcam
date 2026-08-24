---
id: '013'
title: Installed-package cv2-free base-install test
status: done
use-cases:
- UC-10
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

- [x] `uv build` produces a wheel; a fresh venv is created and
      `pip install`s that wheel with the BASE extra only (no `[daemon]`).
- [x] `import aprilcam.client` succeeds in that fresh venv.
- [x] `import cv2` FAILS in that fresh venv (the actual proof of the
      cv2-free base install — not inferred from `pyproject.toml`).
      **Written honestly, and currently `xfail(strict=True)`** — see
      Implementation Notes below; the assertion itself is unweakened
      and will flip to a hard failure (forcing the marker's removal)
      the day the packaging gap closes.
- [x] The console script (`aprilcam`) exists in the fresh venv's `bin/`
      and `aprilcam -V` reports a version matching the built wheel.
- [x] A consumer script, run from the fresh venv's Python, connects to a
      private isolated daemon (started from the dev venv, which does
      have the `[daemon]` extra) and round-trips camera-free data:
      `set_playfield`/`get_playfield`, `draw_marker`/`list_annotations`/
      `clear_layer`, `get_guide`. (Client-side method names used:
      `set_playfield`/`get_playfield`, `put_annotation`/
      `get_annotations`/`clear_annotations`, `get_guide` — the
      `draw_marker`/`list_annotations`/`clear_layer` names in this
      criterion are the MCP tool layer's names for the same operations;
      this test drives `aprilcam.client` directly, per the ticket's own
      "Prior findings" note.)
- [x] The build artifact and fresh venv are cleaned up after the test
      (temp directories, not left in the repo working tree).
- [x] Marked `@pytest.mark.integration` (daemon-dependent, no camera
      needed — not `@pytest.mark.camera`).

## Implementation Notes

Ran `uv run pytest tests/packaging/test_installed_package.py -m
integration -v`: **4 passed, 1 xfailed** in ~27s.

- `test_build_and_base_only_install_produce_a_working_venv` — PASSED.
- `test_import_aprilcam_client_succeeds_in_the_fresh_base_only_venv` —
  PASSED.
- `test_import_cv2_fails_in_the_fresh_base_only_venv` — **XFAIL**, as
  expected per the ticket's "Acknowledged tension" section. Confirmed
  by hand while writing this test: `uv pip install` of the freshly
  built wheel into a bare venv, **no extras**, pulls in
  `opencv-contrib-python==5.0.0.93` unprompted (`+
  opencv-contrib-python==5.0.0.93` in the install log) because it is
  listed under `[project.dependencies]` in `pyproject.toml`, not
  `[project.optional-dependencies.daemon]`. `import cv2` then succeeds
  in that "base-only" venv. Marked `xfail(strict=True)` with a reason
  citing design.md §9 item 1, so this stays visible in every sprint
  gate without blocking it, and converts to a hard failure (XPASS) the
  day someone moves the dependency — forcing the marker's removal
  rather than letting the fix go unnoticed.
- `test_console_script_exists_and_reports_the_built_wheel_version` —
  PASSED (`aprilcam -V` reported `aprilcam 0.20260823.5`, matching the
  built wheel's filename version).
- `test_consumer_script_in_fresh_venv_round_trips_camera_free_data_against_isolated_daemon`
  — PASSED. Confirms the import chain `client.discovery` ->
  `client.daemon_client` / `daemon.interfaces` ->
  `calibration.interfaces` / `playfield.interfaces` -> `config.loader`
  is genuinely cv2-free at the source level (independent of the base
  dependency gap above, which makes cv2 *importable* but not
  *imported* by this chain).

No leftover temp directories after the run (`ls -d /tmp/ac-013-*` found
nothing); real `~/.config/aprilcam`/`~/.local/share/aprilcam` untouched
throughout (no fixture here constructs a bare `Directories()`, and no
`camera_position` is ever written).

**Recommendation for the follow-up packaging ticket** (not done here,
per the ticket's explicit scope boundary): move
`opencv-contrib-python>=4.8` from `[project.dependencies]` to
`[project.optional-dependencies.daemon]` in `pyproject.toml`. This
alone will make `aprilcam taggen` (base-install CLI, `tags/generator.py`
imports `cv2` at module scope) and any other base-install code path
that reaches `vision/detector.py`, `vision/pipeline.py`, or
`camera/backends/*` break immediately for a base-only install — so the
follow-up ticket needs to either (a) make `tags/generator.py`'s `cv2`
import lazy/optional with a clear "install `aprilcam[daemon]`" error
for `taggen` specifically (project overview lists `tags/` as a base
capability, so this is the more likely correct shape), or (b) move
`taggen` itself behind the `daemon` extra and update the project
overview/CLI help accordingly. Either way, once that lands, this
ticket's `test_import_cv2_fails_in_the_fresh_base_only_venv` will XPASS
and must have its `xfail` marker removed as part of that same ticket's
acceptance criteria.

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
