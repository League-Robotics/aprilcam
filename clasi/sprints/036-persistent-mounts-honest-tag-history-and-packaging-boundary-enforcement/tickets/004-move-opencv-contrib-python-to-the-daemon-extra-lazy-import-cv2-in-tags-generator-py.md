---
id: '004'
title: Move opencv-contrib-python to the daemon extra; lazy-import cv2 in tags/generator.py
status: in-progress
use-cases:
- SUC-003
depends-on:
- '005'
github-issue: ''
issue: opencv-is-a-base-dependency-contradicting-the-daemon-only-contract.md
completes_issue: false
exception:
  thrown_by: programmer
  thrown_at: '2026-08-25T00:20:25.353280+00:00'
  attempted: 'Implemented all of ticket 004''s explicit acceptance criteria: moved
    opencv-contrib-python from [project.dependencies] to [project.optional-dependencies.daemon]
    in pyproject.toml; made src/aprilcam/tags/generator.py''s cv2 import lazy (new
    _import_cv()/_is_missing_cv2() helpers mirroring cli/mcp.py''s _import_build_server()/_is_missing_mcp_sdk()
    pattern), moved _DICTIONARY_BY_FAMILY''s construction inside that same lazy boundary
    via a new _dictionary_by_family(cv) helper so cv2 is touched only inside TagGenerator.valid_ids()/generate(),
    which now raise a typed errors.MissingOptionalDependency naming pip install ''aprilcam[daemon]''
    on failure instead of a bare ModuleNotFoundError; removed the xfail(strict=True)
    marker from tests/packaging/test_installed_package.py::test_import_cv2_fails_in_the_fresh_base_only_venv
    and updated its module/section docstrings; added unit tests to tests/aprilcam/tags/test_generator.py
    (an AST check that generator.py has no module-scope cv2 import, _is_missing_cv2
    classification tests, and end-to-end tests that _import_cv/valid_ids/generate
    raise MissingOptionalDependency when cv2 is blocked via a sys.meta_path finder
    mirroring test_mcp_cli.py''s _BlockMcpImports pattern); re-locked uv.lock (uv
    run''s implicit sync). Then ran the ticket''s own specified verification command,
    uv run pytest tests/packaging/test_installed_package.py tests/test_no_cv2_in_client_modules.py
    tests/aprilcam/tags/ -q, to confirm the green scoped run the ticket''s ''Scope
    boundary with ticket 005'' section promises. It is not green: 28 passed, 1 skipped,
    1 failed -- tests/packaging/test_installed_package.py::test_console_script_exists_and_reports_the_built_wheel_version,
    which runs the built console script''s `aprilcam -V` in the fresh base-only venv
    and gets ModuleNotFoundError: No module named ''cv2'' from calibration/geometry.py:66''s
    module-scope `import cv2 as cv`, reached via cli/__init__.py:39''s unconditional
    `from aprilcam.cli import camera, ...` -> cli/camera.py:75''s unconditional `from
    aprilcam.cli import locate as locate_cli` -> cli/locate.py''s import of calibration.geometry.
    Did not touch calibration/geometry.py, cli/locate.py, or cli/camera.py to fix
    this -- confirmed via ticket text that this is explicitly reserved for ticket
    005.'
  conflict: 'Ticket 004''s own ''Scope boundary with ticket 005'' section states:
    "this ticket does NOT need to touch calibration/geometry.py -- client/renderer.py/cli/locate.py
    stay transitively broken by this ticket alone until ticket 005 ... lands ... That
    is expected and safe: no existing test imports client.renderer/cli.locate in a
    fresh base-only venv today (confirmed: the packaging test''s consumer script deliberately
    avoids client.renderer), so this ticket''s own scoped test run stays green even
    though the fix is only half-done." This premise is false, confirmed by actually
    running the suite: merely importing aprilcam.cli (which every console-script invocation
    does via `from aprilcam.cli import main`, before argparse even parses any flag)
    unconditionally pulls in cli/camera.py -> cli/locate.py -> calibration/geometry.py
    -> cv2. So landing ticket 004''s mandatory AC1 (moving opencv-contrib-python out
    of [project.dependencies]) alone does not leave ''only taggen'' broken in a base-only
    install -- it makes the ENTIRE aprilcam CLI console script non-functional (crashes
    on `-V`, `--help`, every subcommand) until ticket 005 fixes calibration/geometry.py''s
    module-scope cv2 import. That fix is explicitly out of ticket 004''s stated scope
    (''this ticket does NOT need to touch calibration/geometry.py'') and belongs to
    ticket 005, which depends on ticket 004 landing first per the sprint''s ticket
    ordering -- so there is no ordering in which ticket 004 lands alone without this
    regression window. I cannot both honor ticket 004''s explicit scope boundary (leave
    calibration/geometry.py, cli/locate.py, cli/camera.py untouched) and satisfy the
    ticket''s own claim/expectation of a green scoped packaging-test run, without
    either overriding that boundary myself or leaving a known-failing test in the
    ticket''s own specified verification command with no ticket instruction on how
    to handle it. Recommendation for the team-lead: either resequence so ticket 005''s
    calibration/geometry.py split lands together with or before ticket 004, or explicitly
    accept and document the transitional regression (e.g. a temporary xfail on test_console_script_exists_and_reports_the_built_wheel_version
    naming ticket 005) so it''s a deliberate decision rather than a silently broken
    CLI between the two tickets landing.'
  surface: user-visible
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

**Scope boundary with ticket 005 (resequenced — see Revision note
below)**: ticket 005 now lands *before* this one and this ticket
**depends on it** (`depends-on: ["005"]`). This ticket does not need to
touch `calibration/geometry.py` at all — by the time this ticket
starts, ticket 005 has already split `undistort_point`/`distort_point`
out into `calibration/distortion.py` and `geometry.py` no longer
imports `cv2`. That means `client/renderer.py` and `cli/locate.py` (and
therefore the whole `aprilcam` console script, which imports
`cli/locate.py` transitively via `cli/__init__.py` → `cli/camera.py`)
are **already** `cv2`-free by construction before this ticket removes
`opencv-contrib-python` from `[project.dependencies]`. There is no
regression window: this ticket's own scoped verification command
(below), including
`test_console_script_exists_and_reports_the_built_wheel_version`, is
expected to be **fully green**, not partially green with a known
failure. If that test fails when you run it, do not treat it as
expected or out of scope the way the previous version of this ticket
did — stop and re-check that ticket 005 actually landed and that
`calibration/geometry.py` genuinely has no `cv2` import (`grep -n cv2
src/aprilcam/calibration/geometry.py`) before proceeding; a failure
here now means something regressed, not that a future ticket will fix
it.

**Stashed prior work exists.** A previous attempt at this ticket (the
one that threw the exception this Revision resolves) already
implemented the `pyproject.toml` move, `tags/generator.py`'s lazy
`_import_cv()`/`MissingOptionalDependency` pattern, the xfail removal,
new generator tests, and a re-locked `uv.lock` — that work was correct
on its own terms and was only blocked by the sequencing issue this
Revision fixes. The team-lead has parked it (git stash or equivalent —
see the dispatch message for exactly where) rather than discard it;
recover and reuse it rather than reimplementing from scratch, then
verify it still applies cleanly on top of ticket 005's changes (which
touch `calibration/`, not `tags/`, so a conflict is unlikely but worth
checking).

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
- [ ] **The dev/test environment keeps `cv2` available after this
      move.** Confirmed during sprint re-planning: neither
      `pyproject.toml`'s `[dependency-groups].dev` nor its
      `[project.optional-dependencies].dev` currently pulls in the
      `daemon` extra or any of its packages (`opencv-contrib-python`
      included) — `uv.lock`'s `[package.dev-dependencies].dev` confirms
      the same (no `opencv-contrib-python`/daemon-extra entries). Before
      this ticket's change, that didn't matter: `opencv-contrib-python`
      was a base dependency, so any `uv sync`/`uv run pytest` had `cv2`
      regardless of which group was active. After this ticket moves it
      to `daemon`, a bare `uv sync` / `uv run pytest` (no `--extra
      daemon` flag) would silently lose `cv2` and break every test
      anywhere in the suite that needs it (`calibration/`, `vision/`,
      `daemon/`, and more) — not just this ticket's own scope. `README.md`
      already documents `uv sync --group dev --extra daemon` as the
      setup command, but that relies on every invocation (contributor,
      CI, and `close_sprint`'s full-suite run) remembering to pass
      `--extra daemon` explicitly. Make the dev/test environment
      self-sufficient instead: extend `[dependency-groups].dev` (or add
      a `[tool.uv]` default-groups/extras setting, whichever the
      installed `uv` version supports cleanly) so that a bare `uv sync`
      /`uv run pytest` — with no extra flags — still resolves `cv2`
      alongside the rest of the daemon extra's packages. Re-lock
      `uv.lock` after the change and confirm with a fresh `uv sync`
      (no extra flags) followed by `uv run python -c "import cv2"`.

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
  — this now includes
  `test_console_script_exists_and_reports_the_built_wheel_version` and
  is expected to pass **fully green** with ticket 005 already landed
  (see Revision note below); a failure there is a real regression, not
  an accepted gap. Additionally run `uv sync` (no `--extra` flags) then
  `uv run python -c "import cv2"` to confirm the dev-environment AC
  above.

## Revision (2026-08-24)

This ticket originally had `depends-on: []` and assumed it would land
before ticket 005, with ticket 005's `calibration/geometry.py` split
completing the fix afterward. A programmer attempt found that premise
false: landing this ticket's AC1 (moving `opencv-contrib-python` out of
base) alone breaks the *entire* `aprilcam` console script in a
base-only install, not just `client/renderer.py`, because
`cli/__init__.py` unconditionally imports the chain that reaches
`calibration/geometry.py`'s module-scope `cv2` import — and threw an
exception rather than leave that regression window undocumented. The
sprint is resequenced in response: this ticket now depends on ticket
005 (`depends-on: ["005"]`), which lands first and removes the
transitive `cv2` dependency before this ticket's dependency-metadata
move can expose it. See ticket 005's own Revision note and sprint.md's
Architecture section for the full account. This ticket's scope and
acceptance criteria for `tags/generator.py` itself are unchanged by the
resequencing; only the "Scope boundary with ticket 005" section, the
expected test outcome, and the new dev-environment AC above are new.
