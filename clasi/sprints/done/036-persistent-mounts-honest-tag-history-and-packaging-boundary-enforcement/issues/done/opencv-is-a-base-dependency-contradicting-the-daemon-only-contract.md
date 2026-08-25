---
status: done
sprint: '036'
tickets:
- '004'
- '005'
---

# `opencv-contrib-python` is a base dependency, contradicting the daemon-only contract

## Description

`CLAUDE.md`'s project overview and `docs/design/design.md` §9 both state the
contract plainly:

> OpenCV is installed only via the `daemon` optional extra
> (`pipx install 'aprilcam[daemon]'`); the base package requires Pillow instead.

`pyproject.toml` does not implement that. `opencv-contrib-python>=4.8` sits in
`[project.dependencies]`, not `[project.optional-dependencies.daemon]`.

**Measured 2026-08-23** (sprint 035 ticket 013): building the wheel and
installing it into a fresh venv with **no extras** pulled in
`opencv-contrib-python==5.0.0.93` unprompted, and `import cv2` succeeded in
that venv. The gap is real, not theoretical.

The whole point of the split (sprint 010, sprint 015) was that the MCP server,
web hub, and view client are thin clients needing only Pillow/NumPy. Today a
base install silently drags in all of OpenCV, so that separation is documented
but not enforced.

## Why it is not a one-line fix

Moving the dependency to the `daemon` extra breaks base-install code, because
`cv2` is imported at **module scope** in:

- `src/aprilcam/tags/generator.py` — this is `aprilcam taggen`, which the
  project overview lists as a **base** CLI capability
- `src/aprilcam/vision/detector.py`
- `src/aprilcam/vision/pipeline.py`
- `src/aprilcam/camera/backends/*`

`src/aprilcam/cli/DESIGN.md:113` already notes a CLI module doing a
module-scope `import cv2 as cv` for unrelated reasons.

Note the client import chain is already clean: `client.discovery` →
`client.daemon_client` / `daemon.interfaces` → `calibration.interfaces` /
`playfield.interfaces` → `config.loader` reaches no cv2. `client/renderer.py`
is the exception — it pulls cv2 transitively via `calibration/geometry.py`.

## Proposed fix

Move `opencv-contrib-python` to the `daemon` extra, then resolve `taggen`,
which is the only genuine conflict. Two options:

1. **Make `tags/generator.py`'s cv2 import lazy**, raising a clear
   "install `aprilcam[daemon]`" error at the point of use. Probably correct —
   the project overview lists tag generation as a base capability, so it should
   stay advertised in the base CLI even if it needs the extra to run.
2. **Move `taggen` behind the `daemon` extra** and update the CLI help and docs
   to match, accepting a narrower base package.

Option 1 preserves the documented capability set; option 2 is simpler but
changes what the base package promises. Decide deliberately rather than
letting the packaging decide.

## Verification

A test already exists and is waiting for this fix:
`tests/packaging/test_installed_package.py::test_import_cv2_fails_in_the_fresh_base_only_venv`
is marked `@pytest.mark.xfail(strict=True)`. Because it is **strict**, it will
turn into a hard failure the moment packaging is fixed — forcing whoever does
this work to remove the marker. That marker removal belongs in this issue's
acceptance criteria.

The rest of `tests/packaging/test_installed_package.py` (wheel build, fresh
venv, `import aprilcam.client`, console-script version match, camera-free
consumer round-trip against a private isolated daemon) already passes and will
keep guarding the result.

Also re-check `tests/test_no_cv2_in_client_modules.py`, the AST-scan gate for
direct imports in client modules — it should keep passing and may want widening
once the boundary is real.

## Related

- Found by sprint 035 ticket 013 (`5dedd70`)
- `docs/design/design.md` §9 items 1 and 6
- `src/aprilcam/cli/DESIGN.md:113`
- `docs/knowledge/daemon-restart-launches-pipx-build-no-cameras.md` — the
  earlier pipx incident in the same area
