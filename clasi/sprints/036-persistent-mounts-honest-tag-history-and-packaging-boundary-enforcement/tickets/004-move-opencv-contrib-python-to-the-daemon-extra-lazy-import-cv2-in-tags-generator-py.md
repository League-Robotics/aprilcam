---
id: '004'
title: Move opencv-contrib-python to the daemon extra; lazy-import cv2 in tags/generator.py
status: open
use-cases: [SUC-003]
depends-on: []
github-issue: ''
issue: opencv-is-a-base-dependency-contradicting-the-daemon-only-contract.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Move opencv-contrib-python to the daemon extra; lazy-import cv2 in tags/generator.py

## Description

`pyproject.toml` lists `opencv-contrib-python>=4.8` in
`[project.dependencies]` (base), not
`[project.optional-dependencies.daemon]`, contradicting CLAUDE.md's
project overview and `docs/design/design.md`'s daemon-only-OpenCV
framing. Confirmed measured in sprint 035 ticket 013: a fresh
no-extras venv install pulls in `opencv-contrib-python==5.0.0.93`
unprompted.

**This ticket reverses a prior, explicitly recorded sprint-028
architecture decision** (`tags/DESIGN.md` §1, as it read before this
sprint): OpenCV was deliberately kept a *permanent, unconditional* base
dependency specifically so `aprilcam taggen` works with zero setup on a
bare install. Sprint 036 revises that, because the higher-authority,
stakeholder-facing project overview names no `tags/` exception ("OpenCV
is installed only via the `daemon` extra; the base package requires
Pillow instead") and the packaging leak this created
(`client/renderer.py`/`cli/locate.py` transitively requiring `cv2` —
ticket 005) is a real, unintended violation of the client-side
cv2-free promise. This sprint's `design/` overlay
(`clasi/sprints/036-.../design/tags-DESIGN.diff.md` and
`design.diff.md`) records the reversal and its rationale in full —
read it before touching `tags/generator.py` so the "why" isn't
rediscovered from scratch.

Moving the dependency alone is not a one-line fix: `cv2` is imported at
**module scope** in `src/aprilcam/tags/generator.py`, which backs
`aprilcam taggen`, a capability the project overview lists as **base**.
The fix (issue's recommended option 1): keep `taggen` advertised in the
base CLI (importable, discoverable via `--help`), but make its `cv2`
import lazy — at the point of use — so it fails with a clear
"install `aprilcam[daemon]`" message rather than a bare
`ModuleNotFoundError` when the extra isn't installed.

A strict xfail test is already waiting for this fix and **must be
un-xfailed as part of this ticket**, not deferred:
`tests/packaging/test_installed_package.py::test_import_cv2_fails_in_the_fresh_base_only_venv`
is `xfail(strict=True)` — the moment `opencv-contrib-python` moves out
of base, that test starts **unexpectedly passing**, which `strict=True`
turns into a hard failure until the marker is removed. Do not leave
this marker in place after making the dependency change.

**Scope boundary with ticket 005**: this ticket does NOT need to touch
`calibration/geometry.py` — `client/renderer.py`/`cli/locate.py` stay
transitively broken by this ticket alone until ticket 005 (which
depends on this one) lands later in the same sprint. That is expected
and safe: no existing test imports `client.renderer`/`cli.locate` in a
fresh base-only venv today (confirmed: the packaging test's consumer
script deliberately avoids `client.renderer`), so this ticket's own
scoped test run stays green even though the fix is only half-done.
Ticket 005 completes the picture and adds the test that proves it.

## Acceptance Criteria

- [ ] `opencv-contrib-python` moves from `[project.dependencies]` to
      `[project.optional-dependencies.daemon]` in `pyproject.toml`.
- [ ] `src/aprilcam/tags/generator.py`'s `import cv2 as cv` at module
      scope is removed; `cv2` is imported inside the function(s) that
      need it (`generate()`/`valid_ids()`), with `ImportError` caught
      and re-raised as a clear message naming
      `pip install 'aprilcam[daemon]'`.
- [ ] `_DICTIONARY_BY_FAMILY` (currently a module-level dict built from
      `cv.aruco.DICT_APRILTAG_36h11`/`DICT_4X4_50` at import time)
      moves inside the same lazy-import boundary, so that simply
      `import aprilcam.tags.generator` — or constructing a
      `TagGenerator` — touches no `cv2` symbol at all.
- [ ] `aprilcam taggen`/`tags generate` still appears in `--help` and
      the CLI dispatch tree unchanged; running it without the
      `daemon` extra installed fails with the clear message above, not
      a raw traceback.
- [ ] `tests/packaging/test_installed_package.py::test_import_cv2_fails_in_the_fresh_base_only_venv`'s
      `xfail(strict=True)` marker is removed; the test passes for
      real.
- [ ] `tests/test_no_cv2_in_client_modules.py` still passes unchanged
      (it doesn't scan `tags/`, so this is a sanity check, not an
      expected code change).
- [ ] Existing `tags/` tests (detection of a missing/invalid id,
      output-directory-writability checks, etc.) still pass with `cv2`
      importable (i.e. run from this repo's own dev venv, which has
      the `daemon` extra) — this ticket does not change `generate()`'s
      behavior when `cv2` *is* available, only when it isn't.

## Testing

- **Existing tests to run**:
  `uv run pytest tests/packaging/test_installed_package.py tests/test_no_cv2_in_client_modules.py tests/aprilcam/tags/ -q`
  — note `test_installed_package.py` builds a real wheel and a fresh
  venv (`uv build`, `uv venv`, `uv pip install`); it is slow (roughly
  tens of seconds to a couple of minutes depending on cache warmth),
  not a quick unit-test run.
- **New tests to write**: a small unit test (or extend an existing
  `tests/aprilcam/tags/` test) asserting that calling `generate()`/
  `valid_ids()` with `cv2` unimportable (monkeypatch
  `sys.modules['cv2'] = None` or equivalent) raises a clear,
  `daemon`-extra-naming error rather than a bare `ImportError`.
- **Verification command**:
  `uv run pytest tests/packaging/test_installed_package.py tests/test_no_cv2_in_client_modules.py tests/aprilcam/tags/ -q`
