---
id: '002'
title: Audit daemon extra for headless-Linux/aarch64 install safety
status: open
use-cases: []
depends-on: []
github-issue: ''
issue: plan-daemon-runs-on-a-remote-raspberry-pi-every-client-discovers-it-and-connects-over-tcp.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Audit daemon extra for headless-Linux/aarch64 install safety

## Description

The `plan-daemon-runs-on-a-remote-raspberry-pi-...md` issue names two
concrete deployment risks that must be checked *before* ticket 004 attempts
a live install, so a broken install is diagnosed here (cheaply, mostly
offline/on-Mac) rather than burning live-Pi time debugging it blind:

1. **`mss` headless import.** The `daemon` extra
   (`pyproject.toml`) lists `mss>=9.0`, a screen-capture library that needs
   X11 on Linux. A grep of `src/aprilcam/` for `import mss`/`from mss`
   currently finds **no hits** — `camera/backends/__init__.py`'s own
   module docstring confirms "Screen capture is not ported this ticket ...
   deferred, no stub file." That suggests the risk may already be moot in
   v2, but this ticket must *prove* it: import the full `aprilcam.daemon`
   module graph in a way that would surface a top-level `mss` import if one
   sneaks back in (a headless-import smoke test), not just re-grep by
   hand.
2. **`opencv-contrib-python` aarch64/cp312 wheel availability.** Confirm
   pip can resolve a working `opencv-contrib-python>=4.8` wheel for
   Ubuntu 24.04 aarch64 + Python 3.12.3 from PyPI. If not, document (in
   the deploy README, ticket 001's artifact) the piwheels fallback v1
   used: `--extra-index-url https://www.piwheels.org/simple`.

Also verify the actual wheel-build/install mechanics this sprint depends
on: `python -m build --wheel` (no `uv` on the Pi, per the sprint's stated
target-hardware constraints) produces a wheel pip can install via
`pipx install "aprilcam[daemon]==<version>" --pip-args "--find-links
<dir>"` — do a dry run on the Mac (build the wheel, inspect its contents)
even though the actual pipx install happens on vidar in ticket 004.

## Acceptance Criteria

- [ ] A documented finding (in this ticket file) for each of the two named
      risks: either "confirmed safe, here's how it was checked" or "found
      a problem, here's the fix."
- [ ] If `mss` (or any other daemon-extra-only package) turns out to have
      a top-level side-effecting import, it is made lazy (matching the
      precedent already set for `tags/generator.py`'s `cv2` import per
      `docs/design/design.md` §9 item 1) — a source fix, not just a note.
      If no problem is found, no source change is needed; say so plainly.
- [ ] `python -m build --wheel` succeeds on the Mac and produces a wheel
      under `dist/`; its `METADATA` correctly lists the `daemon` extra's
      dependencies.
- [ ] `deploy/README.md` (from ticket 001) gets a piwheels fallback
      instruction added if and only if this ticket's audit finds the
      aarch64/cp312 PyPI wheel is unavailable for any daemon-extra
      package — do not add unverified defensive instructions.
- [ ] `scripts/test-gate.sh` passes with no regressions.

## Implementation Plan

**Approach**: (1) Grep the full `src/aprilcam/` tree for every import of
`mss`, `cv2` (already daemon-only per design.md §9 item 1, but re-confirm
nothing regressed), and any other daemon-extra package, checking each hit
for whether it's reachable from `daemon/__main__.py`'s import graph at
module-load time vs. lazily. (2) Write or extend an existing "no daemon-
extra import from a headless entrypoint" test — following the pattern of
`tests/test_no_cv2_in_client_modules.py` if one exists — to mechanically
catch a future regression, not just this audit's snapshot. (3) Build the
wheel locally and inspect it. (4) Research (WebFetch/pip index queries, or
documented reasoning from opencv-contrib-python's published wheel tags) the
aarch64/cp312 wheel availability question; this can be done from the Mac
without SSH to vidar. If genuinely unresolvable without the real Pi, note
that plainly and defer final confirmation to ticket 004 rather than
guessing.

**Files to modify**: possibly `pyproject.toml` (only if a genuine ordering
issue is found — e.g. a package needs to move between `[project.
dependencies]` and `daemon` extra), possibly a `src/aprilcam/**` module
whose import is made lazy, and `deploy/README.md` (piwheels fallback, if
needed).

**Testing plan**: a new or extended offline test asserting the headless
import graph is clean; `uv run pytest -m "not camera and not integration"`
must still pass.

**Documentation updates**: `deploy/README.md` if a fallback is needed;
otherwise none — the "already safe" finding gets folded into ticket 005's
knowledge doc as one more thing verified, not a separate doc.
