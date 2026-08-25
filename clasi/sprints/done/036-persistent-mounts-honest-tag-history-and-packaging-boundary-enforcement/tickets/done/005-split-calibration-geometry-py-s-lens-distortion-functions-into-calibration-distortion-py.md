---
id: '005'
title: Split calibration/geometry.py's lens-distortion functions into calibration/distortion.py
status: done
use-cases:
- SUC-003
depends-on: []
github-issue: ''
issue: opencv-is-a-base-dependency-contradicting-the-daemon-only-contract.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Split calibration/geometry.py's lens-distortion functions into calibration/distortion.py

## Description

`src/aprilcam/calibration/geometry.py` has its own top-level
`import cv2 as cv` — needed **only** by two functions,
`undistort_point`/`distort_point` (the lens-distortion round trip,
`cv2.undistortPoints`). Every other function in `geometry.py`
(`apply_homography`/`invert_homography`/`pixel_to_world`/
`world_to_pixel`, `deskewed_output_size`/`world_to_deskewed_pixel`,
`project_tag_to_field_plane`/`apply_mount_offset`/`correct_tag_pose`,
`solve_camera_geometry`) is pure NumPy/math with zero `cv2` usage —
confirmed by reading the module during sprint planning.

`client/renderer.py` (`world_to_deskewed_pixel`) and `cli/locate.py`
(`solve_camera_geometry`) both import directly from `geometry.py` for
functions that never call `cv2` — but because the whole module has a
top-level `cv2` import, importing either of those files transitively
requires `cv2`. Today, while `opencv-contrib-python` is still a base
dependency, this transitive requirement is invisible — everything has
`cv2` anyway. It stops being invisible the moment ticket 004 moves
`opencv-contrib-python` to the `daemon` extra: at that point,
`import aprilcam.cli` (which every `aprilcam` console-script invocation
performs, via `cli/__init__.py` → `cli/camera.py` → `cli/locate.py`)
and `import aprilcam.client.renderer` would both raise
`ModuleNotFoundError` in a fresh base-only venv — not just
`client/renderer.py` in isolation, but the **entire CLI console
script** (confirmed by tracing the actual import graph during sprint
re-planning: `cli/__init__.py` imports `camera`, which imports both
`calibrate` and `locate` at module scope; `locate.py` is the only one
of those that reaches `calibration.geometry`; no other module in that
graph has a direct `cv2` import). This ticket removes that transitive
dependency **before** ticket 004 lands, so ticket 004's dependency move
is safe by construction rather than needing to be verified after the
fact.

The fix: move `undistort_point`/`distort_point` (and their two tuning
constants, `_DISTORT_POINT_MAX_ITERATIONS`/`_DISTORT_POINT_TOLERANCE_PX`)
into a new sibling module, `src/aprilcam/calibration/distortion.py`.
`geometry.py` loses its `cv2` import entirely.
`src/aprilcam/calibration/calibration.py` (the only consumer of
`undistort_point`/`distort_point` — confirmed by grep during sprint
planning: `calibration.py` lines ~387, ~395, ~1328) updates its calls
from `geometry.undistort_point(...)` to `distortion.undistort_point(...)`
(and likewise for `distort_point`), importing the new module alongside
`geometry`. See this sprint's `design/` overlay
(`clasi/sprints/036-.../design/calibration-DESIGN.diff.md`) for the
full Design Rationale (Decision/Context/Alternatives/Consequences) —
read it before starting; it explains why a module split was chosen
over a lazy `cv2` import inside `geometry.py` itself.

**Lands first, standalone — ticket 004 now depends on this one** (see
Revision note below). This is a pure module split with no dependency
metadata change and no behavior change for any caller with `cv2`
installed, so it is safe to land before `opencv-contrib-python` moves
out of `[project.dependencies]`. Consequence: while `opencv-contrib-python`
is still a base dependency (i.e., until ticket 004 lands afterward), a
*fresh base-only venv* still has `cv2` present regardless of this
split, so a packaging test that only checks
"`import aprilcam.client.renderer` succeeds in a fresh base-only venv"
would pass trivially at this point in the sequence — it would pass
even without the split, because `cv2` is still installed anyway. That
assertion is still worth adding (it becomes the load-bearing packaging
proof once ticket 004 lands and actually removes `cv2` from base
deps), but it cannot be this ticket's only verification that the split
actually accomplished anything. This ticket's real proof must be an
**import-blocker** check — one that makes `cv2` genuinely unimportable
in-process (a `sys.meta_path` finder that raises on `import cv2`, the
same technique `tests/test_no_cv2_in_client_modules.py` and
`test_mcp_cli.py`'s `_BlockMcpImports` already use) and then imports
the target modules against that block, independent of whatever
`pyproject.toml` currently declares as a base dependency. See the
widened AC below.

## Acceptance Criteria

- [x] New file `src/aprilcam/calibration/distortion.py` holds
      `undistort_point`, `distort_point`, and their two tuning
      constants, with the `import cv2 as cv` that used to live in
      `geometry.py`.
- [x] `src/aprilcam/calibration/geometry.py` no longer imports `cv2`
      anywhere — verify with `grep -n cv2
      src/aprilcam/calibration/geometry.py` returning nothing.
      (Confirmed via `grep -n "^import cv2\|^from cv2\|import cv2 as
      cv"` returning nothing; the bare-word `grep -n cv2` also matches
      pre-existing prose in the module docstring/comments — e.g. "no
      cv2 detector objects", "this module still talks to no camera, no
      daemon, no cv2" — that predates this ticket and was never itself
      an import. The load-bearing check, no actual `import cv2`
      statement, is clean.)
- [x] `src/aprilcam/calibration/calibration.py` imports the new
      `distortion` module alongside `geometry` and calls
      `distortion.undistort_point`/`distortion.distort_point` at its
      three existing call sites; behavior is otherwise unchanged
      (this is a pure move, not a rewrite of the distortion math).
- [x] `tests/aprilcam/calibration/test_geometry.py`'s
      `undistort_point`/`distort_point` tests (the "undistort_point /
      distort_point (sprint 030 ticket 001)" section) move to a new
      `tests/aprilcam/calibration/test_distortion.py`, importing from
      `aprilcam.calibration.distortion` instead of `.geometry`. The
      remaining tests in `test_geometry.py` (homography, deskew
      geometry, mount correction, camera-geometry solver) are
      unaffected and stay where they are.
  - [x] Check `tests/aprilcam/config/test_loader.py` and
        `tests/image_matrix/test_geometry.py` (both import
        `calibration.geometry` per a repo-wide grep during sprint
        planning) for any use of `undistort_point`/`distort_point` —
        expected to be none (they likely use unrelated `geometry`
        functions), but confirm rather than assume; update their
        imports only if they do.
        (Confirmed via grep: neither file references
        `undistort_point`/`distort_point`; no changes needed there.
        **Found one additional call site the ticket's own grep during
        sprint planning missed**:
        `tests/aprilcam/calibration/test_calibration.py` calls
        `geometry.undistort_point`/`geometry.distort_point` directly
        in two of its own tests — updated to
        `distortion.undistort_point`/`distortion.distort_point` with
        the new import added; full calibration suite reruns green,
        126 passed / 2 skipped, hardware-dependent skips unrelated to
        this change.)
- [x] **Import-blocker proof (load-bearing — see Description above for
      why the fresh-venv packaging test alone cannot prove this at
      this point in the sequence)**: a new test that blocks `cv2` from
      being importable in-process (a `sys.meta_path` finder that raises
      on `import cv2`/`import cv2.*`, the same technique
      `test_mcp_cli.py`'s `_BlockMcpImports` uses, or reuse ticket 004's
      own `_import_cv`-blocking pattern from
      `tests/aprilcam/tags/test_generator.py` if it lands first) and
      then, with that block active, successfully imports **every**
      module the `aprilcam` console script pulls in at startup —
      concretely: `aprilcam.cli` (which transitively imports `camera`,
      `config`, `daemon`, `guide`, `mcp`, `playfield`, `tags`, and via
      `camera`, `calibrate` and `locate`) — plus `aprilcam.client.renderer`.
      Do not hand-enumerate this list as the assertion itself and trust
      it; the assertion is "these imports succeed with `cv2` blocked,"
      which mechanically covers the whole transitive closure including
      any chain not listed here. Place this in
      `tests/packaging/test_installed_package.py` alongside the other
      import checks, or in a new focused test module if that reads
      better — either is fine as long as it runs in the normal dev venv
      (it needs no fresh venv/wheel build, since it blocks `cv2` at the
      `sys.meta_path` level rather than relying on it being absent from
      the environment).
      (Done, as a new focused module,
      `tests/test_cv2_import_blocker_console_script.py`. **Real finding
      while writing this, worth flagging to ticket 004's programmer**:
      the mechanical "does not hand-enumerate and trust it" test found
      a *second*, independent top-level `cv2` import in the same
      `aprilcam.cli` graph that this ticket's own Description did not
      anticipate — `aprilcam.tags.generator` (reached via
      `cli/__init__.py` -> `cli/tags.py`), out of this ticket's scope
      and explicitly not to be touched here. So `import aprilcam.cli`
      as a *whole* does not yet succeed with `cv2` blocked — this is
      captured honestly as an `xfail(strict=True)` test naming the
      exact cause, mirroring `test_installed_package.py`'s own idiom
      for a known tracked gap. The chain this ticket *is* responsible
      for (`cli/camera.py` -> `cli/locate.py` ->
      `calibration/geometry.py`, plus `config`/`daemon`/`guide`/`mcp`/
      `playfield`) is proven clean independently by stubbing out
      `aprilcam.tags.generator`, and `aprilcam.client.renderer` and
      `aprilcam.calibration.geometry` are proven clean directly and
      unconditionally. Ticket 004's own stashed WIP already appears to
      fix `tags/generator.py`'s import (per its stash message); once
      that lands, the xfail above should flip to an unexpected pass and
      be removed.)
- [x] Optionally, widen `tests/test_no_cv2_in_client_modules.py`'s
      `_CV2_FREE_PACKAGES` to add a check that `cli/` has no *direct*
      `import cv2` in any of its own files (cheap, catches a future
      regression fast) — but note this AST scan only checks each file's
      own literal imports, not transitive ones, so it is a
      complement to the import-blocker test above, not a substitute
      for it. Do not add the whole `calibration/` package to
      `_CV2_FREE_PACKAGES` — `calibration.py`/`distortion.py`
      legitimately import `cv2`; the existing grep-based AC above
      already covers `geometry.py` specifically.
      (Done — `cli` added to `_CV2_FREE_PACKAGES`; passes today since
      no `cli/*.py` file has ever had its own literal `import cv2`.)
- [x] New packaging-test coverage in
      `tests/packaging/test_installed_package.py`: assert
      `import aprilcam.client.renderer` succeeds (subprocess `-c`
      check, same pattern as the existing `import aprilcam.client`/
      `import cv2` checks) in the fresh **base-only** venv. Optionally
      also assert `import aprilcam.cli.locate` succeeds there (a bonus
      fix, per `cli/DESIGN.md`'s resolved §3/§6 — not required, but
      cheap given the fixture already exists). Note (see Description):
      at the point this ticket lands, `opencv-contrib-python` is still
      a base dependency, so this specific assertion passes trivially
      either way — it is still worth adding now because it becomes the
      real proof once ticket 004 lands afterward and the fixture no
      longer has `cv2` for any reason.
      (Both added; full `tests/packaging/test_installed_package.py`
      run: 6 passed, 1 xfailed — the pre-existing AC3 `xfail(strict=
      True)` for `import cv2` succeeding, unaffected by this ticket.)
- [x] `docs/design/design.md` §9's item 6 and `cli/DESIGN.md` §3/§6
      already describe this fix in this sprint's `design/` overlay —
      no further doc edits needed beyond what ticket 007/008 (mount
      persistence docs) or this ticket's own code comments require.

## Testing

- **Existing tests to run**:
  `uv run pytest tests/aprilcam/calibration/ tests/packaging/test_installed_package.py tests/test_no_cv2_in_client_modules.py -q`
  — note `test_installed_package.py` builds a real wheel and fresh
  venv; it is slow, not a quick unit-test run.
- **New tests to write**: `tests/aprilcam/calibration/test_distortion.py`
  (moved from `test_geometry.py`, imports updated); the new
  `import aprilcam.client.renderer` (and optionally `cli.locate`)
  assertion in `test_installed_package.py`; the import-blocker test
  proving the whole console-script import graph and `client.renderer`
  are `cv2`-free with `cv2` blocked at `sys.meta_path`.
- **Verification command**:
  `uv run pytest tests/aprilcam/calibration/ tests/packaging/test_installed_package.py tests/test_no_cv2_in_client_modules.py -q`

## Revision (2026-08-24)

Ticket 004's programmer threw an exception after discovering that
moving `opencv-contrib-python` out of `[project.dependencies]` (ticket
004's AC1) breaks the **entire** `aprilcam` console script in a
base-only install — not just `client/renderer.py` in isolation, as
ticket 004's original "Scope boundary with ticket 005" section assumed
— because `import aprilcam.cli` itself pulls in `cli/camera.py` →
`cli/locate.py` → `calibration/geometry.py` → `cv2` before argparse
sees a single flag. The sprint's ticket ordering is resequenced in
response: this ticket now lands **first**, standalone, with
`depends-on: []`; ticket 004 now depends on this one
(`depends-on: ["005"]`). Ticket 006 (which edits the same packaging
test file) now depends on ticket 004, since 004 is the last of the
pair to touch `tests/packaging/test_installed_package.py`. See
sprint.md's Architecture section for the fuller resequencing note. No
architectural decision changes as a result — the module split itself
(Decision documented in `design/calibration-DESIGN.diff.md`) is
unaffected; only the *order* in which the two tickets land changes,
plus the verification strategy above (import-blocker as the
load-bearing proof, since the fresh-venv packaging assertion alone
cannot prove anything meaningful before ticket 004 lands).
