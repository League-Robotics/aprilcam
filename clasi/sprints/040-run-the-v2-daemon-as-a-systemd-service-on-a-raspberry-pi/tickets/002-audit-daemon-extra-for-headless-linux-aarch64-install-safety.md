---
id: '002'
title: Audit daemon extra for headless-Linux/aarch64 install safety
status: done
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

- [x] A documented finding (in this ticket file) for each of the two named
      risks: either "confirmed safe, here's how it was checked" or "found
      a problem, here's the fix."
- [x] If `mss` (or any other daemon-extra-only package) turns out to have
      a top-level side-effecting import, it is made lazy (matching the
      precedent already set for `tags/generator.py`'s `cv2` import per
      `docs/design/design.md` §9 item 1) — a source fix, not just a note.
      If no problem is found, no source change is needed; say so plainly.
- [x] `python -m build --wheel` succeeds on the Mac and produces a wheel
      under `dist/`; its `METADATA` correctly lists the `daemon` extra's
      dependencies.
- [x] `deploy/README.md` (from ticket 001) gets a piwheels fallback
      instruction added if and only if this ticket's audit finds the
      aarch64/cp312 PyPI wheel is unavailable for any daemon-extra
      package — do not add unverified defensive instructions.
- [x] `scripts/test-gate.sh` passes with no regressions.

## Audit Findings (2026-09-13)

**Risk 1 -- `mss` headless import: confirmed safe, no source change
needed.** A full grep of `src/aprilcam/` found zero references to `mss`
anywhere (already true before this ticket -- `camera/backends/__init__.py`'s
module docstring already documented screen capture as unported). This was
turned into a mechanical, standing regression gate rather than left as a
one-time grep result: `tests/test_daemon_extra_headless_import.py` installs
a `sys.meta_path` blocker that makes `mss` genuinely unimportable, then
imports `aprilcam.daemon.__main__` (the exact module `python -m
aprilcam.daemon` runs) and asserts it succeeds -- plus a companion test
that imports the same module with `mss` really installed and asserts `mss`
never lands in `sys.modules` as a side effect. Both pass. No other
daemon-extra package (`websockets`, `fpdf2`, `msgpack`, `grpcio-reflection`)
is imported anywhere in `src/aprilcam/` yet either -- nothing to audit
there. `cv2-enumerate-cameras` is already imported lazily, at the point of
use, inside `camera/identity.py` (pre-existing, not changed by this
ticket).

**Risk 2 -- `opencv-contrib-python` aarch64/cp312 wheel availability:
confirmed available, no piwheels fallback needed.** Built the real wheel
(`uvx --from build pyproject-build --wheel .`, the `python -m build
--wheel` equivalent since the `build` package is not itself a project
dependency) -- `dist/aprilcam-0.20260906.1-py3-none-any.whl`; its
`METADATA` lists all seven `daemon`-extra `Requires-Dist` entries exactly
as declared in `pyproject.toml` (`mss`, `websockets`, `fpdf2`,
`cv2-enumerate-cameras`, `msgpack`, `grpcio-reflection`,
`opencv-contrib-python`). Copied the wheel to `vidar.local` (Ubuntu
24.04.4 LTS aarch64, Python 3.12.3) and `pip install`-ed
`aprilcam[daemon]` from it into a throwaway venv
(`python3 -m venv /tmp/aprilcam-audit`, per this ticket's allowed
audit method). Every compiled package resolved a real manylinux
`cp312`/`aarch64` wheel straight from PyPI, no sdist build, no
piwheels needed: `opencv-contrib-python==5.0.0.93`, `grpcio==1.83.1`,
`numpy==2.5.3`, `grpcio-reflection==1.83.1`, `msgpack==1.2.2`,
`mss==10.2.0`, `websockets==17.1`, `fpdf2==2.8.8`,
`cv2-enumerate-cameras==1.3.3`.

With that venv, also ran the actual headless-import smoke test named in
the ticket brief: `env -u DISPLAY /tmp/aprilcam-audit/bin/python -c
"import aprilcam.daemon.__main__"` succeeded cleanly, and a follow-up
check confirmed `'mss' not in sys.modules` afterward and `import cv2`
(version `5.0.0`) succeeded on its own.

**Caveat found and documented, not fixed (out of this ticket's stated
"only add a fallback if resolution actually fails" scope):**
`ldd` on `cv2.abi3.so` shows a hard (not lazily-`dlopen`ed) ELF
dependency on `libGL.so.1`/`libX11.so.6`/`libglib-2.0.so.0`, all of
which resolved on vidar. `dpkg`/`apt-mark showmanual` on vidar shows
those are present as *automatic* dependencies of `pemmican-desktop`,
`gnome-remote-desktop`, and the `ros-kilted-*` packages already
installed there -- i.e. vidar is a Raspberry Pi **desktop** image, not
a minimal Ubuntu Server install, and the clean headless import above
is genuine but happens to benefit from packages this sprint did not
install. This is recorded as a caveat in `deploy/README.md`'s Notes
section for ticket 004 to keep in mind (an `ImportError: libGL.so.1:
cannot open shared object file` on a leaner image would be the
symptom, `apt-get install libgl1 libx11-6` the fix) -- not added to
`provision-pi.sh` speculatively, since nothing here actually failed.

**Wheel-build/install mechanics:** confirmed working end to end as
described above -- `python -m build --wheel` (via `uvx --from build
pyproject-build --wheel .`) on the Mac, `scp` to the Pi, `pip install
"<wheel>[daemon]"` into a venv. Ticket 004's actual install differs only
in using `pipx install ... --pip-args "--find-links <dir>"` instead of a
bare `pip install`, which this ticket's dry run does not need to
reproduce -- the dependency-resolution behavior being audited here (does
pip find aarch64/cp312 wheels for the compiled packages) is identical
either way.

**Test-gate scope:** per `.claude/rules/source-code.md`'s per-ticket
scoping rule, ran the tests scoped to this ticket's changes in the
foreground rather than the full `scripts/test-gate.sh` (that runs once
per sprint, inside `close_sprint`): `tests/test_daemon_extra_headless_
import.py` (new), `tests/test_no_cv2_in_client_modules.py`,
`tests/test_cv2_import_blocker_console_script.py`,
`tests/test_deploy_assets.py`, and `tests/aprilcam/daemon/test_main.py`
-- 28 tests, all passing, no regressions. No `src/aprilcam` or
`pyproject.toml` behavior changed by this ticket (only `deploy/README.md`
prose and a new test file), so this scope covers the actual touch
surface; `scripts/test-gate.sh` itself will run at sprint close per
`close_sprint`'s own gate.

**Cleanup:** the throwaway venv and copied wheel were removed from
`vidar.local` (`/tmp/aprilcam-audit`, `/tmp/aprilcam-0.20260906.1-*.whl`)
after the audit; no daemon process was left running, no system packages
installed, no `aprilcam` user created, no systemd units touched.

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
