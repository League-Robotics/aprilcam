---
id: '036'
title: Persistent mounts, honest tag history, and packaging boundary enforcement
status: executing
branch: sprint/036-persistent-mounts-honest-tag-history-and-packaging-boundary-enforcement
use-cases: []
issues:
- mount-registrations-should-persist-across-daemon-restarts.md
- tag-history-gaps-and-jitter-speed-mislead-mcp-consumers.md
- opencv-is-a-base-dependency-contradicting-the-daemon-only-contract.md
- installed-package-test-does-not-exercise-aprilcam-mcp.md
- get-use-case-coverage-reports-zero-reads-the-wrong-file.md
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 036: Persistent mounts, honest tag history, and packaging boundary enforcement

## Goals

Clear the entire pending issue pool: five independent, small-to-medium
hardening/correctness items spanning daemon state persistence, MCP-facing
documentation honesty, a packaging boundary violation, a test gap that let
the packaging violation ship silently, and a broken traceability gate. None
of the five depends on any other; they are grouped into one sprint because
each is individually too small to justify its own sprint, not because they
share a subsystem.

## Problem

Five issues have accumulated in `clasi/issues/`, all `status: pending`,
each surfaced by recent sprint work (035) or live agent usage on
2026-08-23:

1. **Mount registrations don't survive a daemon restart.** `register_tag`
   pose data (mount_x/mount_y/mount_yaw_rad, mount_z for parallax) lives
   only in `DaemonService._mount_params`, an in-memory dict. A restart
   silently discards it, and queries afterward return the raw tag pose
   instead of the robot's pose — no error, no warning, just a
   plausible-looking wrong number.
2. **`get_tag_history`'s docstring promises a cadence the system doesn't
   guarantee, and stationary tags report phantom speed.** Demand-driven
   detection (sprint 021/027) means the 300-frame ring buffer can span
   hours, not "~10 seconds" as documented. Separately, `compute_motion`
   always reports `speed = displacement/dt` even below the noise floor,
   so tight-cadence queries turn ~0.25 cm of detector jitter into ~1 cm/s
   of confident-looking speed on a tag that hasn't moved.
3. **`opencv-contrib-python` sits in base dependencies**, contradicting
   the documented daemon-only contract (CLAUDE.md, design.md §9) and
   confirmed measured in sprint 035: a fresh no-extras venv pulls in all
   of OpenCV. A strict xfail test is already waiting for this fix.
4. **The installed-package test never starts `aprilcam mcp`.** A bad `mcp`
   SDK resolution (mcp 2.x moved `mcp.server.fastmcp`) shipped silently
   and broke every fresh install, because no test exercised the MCP entry
   point against a real dependency resolution.
5. **`get_use_case_coverage()` always reports zero.** It reads
   `clasi/usecases.md`, which this project never populated; real use
   cases live in `docs/design/use-cases/*.md` (29 use cases, 111 scenario
   IDs). A traceability gate that can't see the traceability data is
   worse than no gate — several sprint exit criteria treat this tool as
   an audit and would pass vacuously.

## Solution

Address each issue on its own terms, in one sprint, without inventing
shared abstractions across them just because they landed together:

1. Persist mount registrations to disk in the daemon's existing
   config/state directory (`src/aprilcam/config/directories.py`), load
   them at startup, preserve today's calibrated-but-not-located
   `mount_z` semantics exactly, and update AGENT_GUIDE.md §6. Mounts
   only — annotation persistence is explicitly out of scope (separate
   issue, not filed against this sprint).
2. Fix the `get_tag_history` docstring and add cadence/ring-buffer/motion
   guidance to AGENT_GUIDE.md (and ROBOT_API_GUIDE.md if it makes the
   same implicit promise). Separately, make a deliberate design call in
   Detail Mode on how `compute_motion` should stop reporting jitter as
   signal below `MIN_RELIABLE_DISPLACEMENT_CM` (the issue lists several
   options in increasing invasiveness) and implement it.
3. Move `opencv-contrib-python` to the `daemon` extra; make
   `tags/generator.py`'s `cv2` import lazy with a clear
   "install aprilcam[daemon]" error (issue's recommended option 1 —
   keeps `taggen` advertised as a base capability). Remove the strict
   xfail marker on `test_import_cv2_fails_in_the_fresh_base_only_venv`
   as part of the fix, and re-check `tests/test_no_cv2_in_client_modules.py`.
4. Extend `tests/packaging/test_installed_package.py` to start
   `aprilcam mcp` in the fresh venv, complete a real stdio `initialize`
   handshake (reusing `tests/aprilcam/mcp/test_stdio_offline.py`
   machinery), assert the pinned tool inventory, and assert the resolved
   `mcp` version satisfies the declared constraint. Migrating to mcp 2.x
   is explicitly out of scope.
5. Populate `clasi/usecases.md` as an index the CLASI tool can read,
   sourced from `docs/design/use-cases/*.md`, and add a regression test
   asserting the generated index stays consistent with the source
   directory (so it can't silently rot) — see Revision note below on why
   this no longer names `get_use_case_coverage()`'s own return value.
   The `get_use_case_coverage()` tool's own source lives in a different
   repository and is out of scope for this project's sprint — pointing
   the tool at `docs/design/use-cases/` directly (the issue's option 1)
   is recorded as a recommended follow-up issue, not a ticket here.

## Success Criteria

- Mount registrations survive a daemon restart with existing
  precondition behavior (mount_z inert until camera is located)
  unchanged; AGENT_GUIDE.md §6 no longer tells consumers to re-register
  every session.
- `get_tag_history`'s docstring and AGENT_GUIDE.md accurately describe
  cadence and history semantics; a stationary tag no longer reports
  confident-looking non-zero speed at tight query cadence.
- `import cv2` fails in a fresh base-only install; the strict xfail is
  gone (converted to a real pass); `aprilcam taggen` still exists as a
  base CLI capability with a clear error if `[daemon]` isn't installed.
- `tests/packaging/test_installed_package.py` starts `aprilcam mcp` in
  the fresh venv, completes a real handshake, and would have caught the
  mcp 2.x incident.
- `clasi/usecases.md` is a generated index consistent with
  `docs/design/use-cases/`, guarded by a regression test (see Revision
  note below: `get_use_case_coverage()` itself cannot be made to report
  this in-repo — that requires a CLASI-package fix tracked as a
  follow-up issue).

## Scope

### In Scope

All five pending issues in `clasi/issues/`, each sized independently
(sizing is a Detail Mode call, not a Roadmap Mode commitment — these are
first-pass estimates):

1. `mount-registrations-should-persist-across-daemon-restarts.md` —
   **medium-large**, the biggest item in the sprint: new persistence
   read/write path in `config/directories.py`, startup load, precondition
   preservation, AGENT_GUIDE.md update. Likely 2-3 tickets.
2. `tag-history-gaps-and-jitter-speed-mislead-mcp-consumers.md` —
   **small-medium**: a low-risk docs-only fix (tool docstring +
   AGENT_GUIDE.md) plus one small but deliberate design decision and
   implementation change in `vision/motion.py`. Likely 2 tickets
   (doc fix; motion.py decision + implementation).
3. `opencv-is-a-base-dependency-contradicting-the-daemon-only-contract.md`
   — **medium**: one dependency-metadata move plus one lazy-import
   change with a handful of call sites to verify. Likely 1-2 tickets.
4. `installed-package-test-does-not-exercise-aprilcam-mcp.md` —
   **small-medium**: additive test-only change reusing existing stdio
   handshake machinery. Likely 1 ticket.
5. `get-use-case-coverage-reports-zero-reads-the-wrong-file.md` —
   **small**: generate an index file plus one regression test. Likely 1
   ticket.

Rough total estimate: **7-9 tickets**, refined during Detail Mode
ticketing.

### Out of Scope

- Annotation persistence (the other per-session state called out in
  AGENT_GUIDE.md §6) — explicitly deferred to a separate issue by issue
  1's own text.
- An explicit `--session-only` ephemeral mount-registration option —
  open question in issue 1, decide in Detail Mode whether it's worth
  adding or whether `unregister_tag` is sufficient.
- Migrating the `mcp` Python SDK to 2.x (`mcp.server.mcpserver`) — issue
  4 explicitly scopes this out; the `<2` pin stays as the stopgap.
- Changing `get_use_case_coverage()`'s own implementation — it lives in
  a different repository (the CLASI package itself), out of scope for
  this project. Recorded as a recommended follow-up issue for that
  repository, not addressed here.

## Test Strategy

Each issue already names or implies its own test surface; Detail Mode
will size these precisely, but roughly:

- Mount persistence: unit/integration coverage for write-through on
  register/unregister, load-at-startup, and the mount_z
  calibrated-but-not-located precondition surviving a restart.
- Tag history/motion: a docs-only change needs no new tests beyond
  existing suite passing; the motion.py speed-suppression change needs
  unit tests around `MIN_RELIABLE_DISPLACEMENT_CM` boundary behavior at
  small and large dt.
- Packaging boundary: the existing strict xfail
  (`test_import_cv2_fails_in_the_fresh_base_only_venv`) flips to a real
  pass; `tests/test_no_cv2_in_client_modules.py` re-verified, possibly
  widened.
- Installed-package MCP handshake: new fresh-venv test starting
  `aprilcam mcp`, completing a stdio `initialize`, asserting the pinned
  tool inventory and the resolved `mcp` version.
- Use-case coverage index: a regression test asserting
  `get_use_case_coverage()`'s total is non-zero and matches
  `docs/design/use-cases/`'s actual count, so the index can't silently
  rot back to zero.

## Architecture

**Substantial** — this sprint touches 5+ modules with no shared reason to
change together (`config`/`daemon` for mount persistence, `vision` for
motion semantics, `calibration` for a module split, `tags` for a lazy
import, `mcp`/`tests/packaging` for a test gap, `clasi/` for a
traceability index) and one of the five issues (mount persistence) adds a
genuinely new on-disk data store. Signals meet the substantial bar by
module count alone; the mount-persistence and cv2-boundary pieces also
each carry a real design decision (storage location/format; a module
split resolving an accidental transitive dependency) that the compact
tier's "one module, no diagrams" treatment would under-serve. Per
`architecture-authoring` Mode 2a: this project has
`design_docs_opt_in: true` (`.clasi/config.yaml`), so the full
architecture write-up for this sprint lives in this sprint's `design/`
overlay (`clasi/sprints/036-.../design/*.diff.md`), not in this section —
see that directory for What Changed / Why / Impact / Migration Concerns
per affected canonical doc, plus Design Rationale for the two
non-obvious decisions (mount persistence storage shape; the
`calibration/geometry.py` → `calibration/distortion.py` split) and one
recorded reversal of a prior sprint-028 decision (OpenCV as a permanent
`tags/` base dependency). Affected canonical docs: `docs/design/design.md`,
`config/DESIGN.md`, `daemon/DESIGN.md`, `vision/DESIGN.md`,
`calibration/DESIGN.md`, `tags/DESIGN.md`, `cli/DESIGN.md`. A
component diagram is omitted with a stated reason in the overlay
(sprint-020 precedent): the five issues are independent hardening items
touching many existing modules with no new cross-module composition
between them — the one genuinely new thing this sprint composes (the
mount-persistence read/write path) is small enough that the module list
and one paragraph of prose say everything a 5-node diagram would.

### Revision (2026-08-24): ticket 004/005 resequencing

Ticket 004's programmer threw an internal (structural) exception after
implementation and its own specified verification command showed the
plan's premise was false. Ticket 004's original "Scope boundary with
ticket 005" text asserted that moving `opencv-contrib-python` out of
`[project.dependencies]` alone would leave only `tags/generator.py`
(and, transitively, `client/renderer.py`/`cli/locate.py`) broken in a
base-only install until ticket 005 landed — and that no existing test
would notice in the meantime. Actually running
`tests/packaging/test_installed_package.py` after implementing 004
showed otherwise: `import aprilcam.cli` — which every invocation of the
`aprilcam` console script performs, before argparse parses a single
flag — unconditionally imports `cli/camera.py`, which unconditionally
imports `cli/locate.py`, which imports `calibration/geometry.py`,
whose module-scope `import cv2 as cv` then fails. So landing ticket
004's dependency move alone doesn't narrowly break `tags/` or the
client-rendering path — it makes the **entire console script**
non-functional (`-V`, `--help`, every subcommand) until ticket 005's
`geometry.py` split lands. There is no ordering under the original
`004 → 005` sequencing in which ticket 004 lands alone without this
regression window, since 005 depended on 004.

Resolution: the two tickets are resequenced. **Ticket 005 (the
`calibration/geometry.py` → `calibration/distortion.py` split) now
lands first**, standalone (`depends-on: []`) — it is a pure module
split with no dependency-metadata change and no behavior change for
any caller that still has `cv2` available, so it is safe regardless of
what `pyproject.toml` currently declares as a base dependency. **Ticket
004 now depends on ticket 005** (`depends-on: ["005"]`): by the time
004 moves `opencv-contrib-python` to the `daemon` extra, the transitive
`cv2` dependency through `cli/locate.py`/`client/renderer.py` has
already been removed, so the move is safe by construction and 004's
own scoped packaging-test run (including
`test_console_script_exists_and_reports_the_built_wheel_version`) is
expected to be fully green rather than partially green with an
accepted known failure. **Ticket 006 now depends on ticket 004**
(previously depended on 005): 005, 004, and 006 all edit
`tests/packaging/test_installed_package.py` in sequence, and since 004
is now the last of the pair to touch that file, 006 must build on 004's
version of it, not 005's.

Because ticket 005 now lands while `opencv-contrib-python` is still a
base dependency, its own new packaging-test assertion
(`import aprilcam.client.renderer` succeeds in a fresh base-only venv)
would pass trivially at that point regardless of whether the split
actually happened — the venv still has `cv2` from the base dependency
either way. Ticket 005's real proof is therefore an import-blocker test
(`sys.meta_path`-based, mirroring `test_mcp_cli.py`'s
`_BlockMcpImports`) covering the whole console-script import graph
(`aprilcam.cli` and everything it pulls transitively) plus
`aprilcam.client.renderer`, independent of what `pyproject.toml`
happens to declare at the moment it runs. See ticket 005's own Revision
note for the full detail.

This is a pure reordering of already-planned work, not a change to any
architectural decision: the module split itself (Design Rationale in
`design/calibration-DESIGN.diff.md`) is unaffected, no new module or
dependency is introduced, and `design/calibration-DESIGN.md` and
`design/cli-DESIGN.md` were checked and found to make no ordering claim
that resequencing would falsify (both describe only the *end state*
after both tickets land, e.g. "as of sprint 036, `geometry.py` is
`cv2`-free" — true regardless of which of 004/005 landed first — so
neither needed a correction). Ticket numbering is unchanged (tickets
are not renumbered when reordered); execution order is 001, 002, 003,
005, 004, 006, 007, 008, per the updated `## Tickets` table below.

Separately, this exception surfaced a related gap this sprint's tickets
did not originally cover: `[dependency-groups].dev` in `pyproject.toml`
(and `uv.lock`'s corresponding `[package.dev-dependencies].dev`) does
not pull in the `daemon` extra or any of its packages. Before ticket
004, this didn't matter — `opencv-contrib-python` was a base
dependency, so any `uv sync`/`uv run pytest` had `cv2` regardless of
group. After ticket 004, a bare `uv sync`/`uv run pytest` with no
`--extra daemon` flag would silently lose `cv2` for the dev/test
environment, breaking every test elsewhere in the suite that needs it
— a real risk to `close_sprint`'s own full-suite run and to
contributors who don't remember `README.md`'s documented `--extra
daemon` flag. Ticket 004 gained a new acceptance criterion to make the
dev/test environment self-sufficient (extend `[dependency-groups].dev`
or an equivalent `[tool.uv]` setting so a bare `uv sync` keeps `cv2`
available) rather than relying on every invocation remembering an
explicit flag.

## Use Cases

### SUC-001: Mount registrations survive a daemon restart
Parent: UC-12

- **Actor**: Robot control program / MCP agent registering a mobile tag's
  mount parameters.
- **Preconditions**: A daemon is running; a tag mount has been registered
  via `register_tag` at some point.
- **Main Flow**:
  1. Client calls `register_tag` with `mount_x`/`mount_y`/`mount_yaw_rad`/
     `mount_z`.
  2. The daemon accepts the registration (existing precondition checks on
     `mount_z` unchanged) and writes it through to disk.
  3. The daemon restarts (crash, upgrade, manual restart).
  4. On startup, the daemon reloads every persisted mount registration
     before serving any RPC.
  5. A subsequent `get_tag`/`get_tags` call for that tag reports the
     *robot's* corrected pose, exactly as it would have before the
     restart — no client action required.
- **Postconditions**: The registration is durable across restarts;
  `mount_z`'s calibrated-but-not-located precondition behavior (registration
  stands, correction inert, `mount_z_applied` reports the truth) is
  unchanged whether the registration was just made or reloaded from disk.
- **Acceptance Criteria**:
  - [ ] A mount registered, then a daemon restart, then a query — reports
        the corrected pose, not the raw tag pose.
  - [ ] `unregister_tag` removes the persisted entry, not just the
        in-memory one.
  - [ ] A persisted nonzero-`mount_z` registration reloaded into a daemon
        with no calibration at all does not crash startup and does not
        silently vanish (mirrors `_load_calibrations`'s tolerant-load
        stance, not `register_tag`'s raising guard).
  - [ ] `AGENT_GUIDE.md` §6 and `ROBOT_API_GUIDE.md`'s "does not survive a
        daemon restart" section no longer instruct re-registering every
        session for mounts.

### SUC-002: Tag history and motion fields are honestly documented and computed
Parent: UC-26

- **Actor**: MCP agent or robot program reading `get_tag_history` and
  interpreting `TagRecord.speed`/`heading_rad`/`world_velocity`.
- **Preconditions**: A camera is calibrated and has some detection
  history.
- **Main Flow**:
  1. Client calls `get_tag_history` with no active tag-stream subscriber
     running.
  2. The returned frames may span minutes or hours between consecutive
     entries — the tool's docstring and `AGENT_GUIDE.md` say so plainly,
     pointing at each frame's own `timestamp` as authority.
  3. Client reads two close-together frames for a stationary tag.
  4. `speed` is `None` (not a small confident-looking non-zero number)
     because the two-sample displacement is below
     `MIN_RELIABLE_DISPLACEMENT_CM`, exactly like `heading_rad`/
     `world_velocity` already were.
- **Postconditions**: No documentation promises a cadence the system
  does not guarantee; no motion field reports jitter as signal.
- **Acceptance Criteria**:
  - [ ] `get_tag_history`'s docstring no longer claims "roughly the last
        10 seconds" unconditionally.
  - [ ] `AGENT_GUIDE.md` gains a cadence/history/motion-interpretation
        section; `ROBOT_API_GUIDE.md` is checked and updated only where
        it is not already accurate (its polling-vs-streaming section
        already documents the cadence gap correctly).
  - [ ] `compute_motion` returns `speed=None` (alongside
        `heading_rad=None`, `world_velocity=None`) when two-sample
        displacement is below `MIN_RELIABLE_DISPLACEMENT_CM`; unit tests
        cover the boundary at both small and large `dt`.
  - [ ] `vision/motion.py`'s module docstring no longer claims speed is
        "bounded near zero for jitter" as a safe reading.

### SUC-003: Base install stays cv2-free except for `tags/`, at point of use
Parent: UC-21

- **Actor**: Anyone installing `aprilcam` with no extras (MCP agent
  deployer, base-install CI check).
- **Preconditions**: A fresh venv, `pip install aprilcam` (no `[daemon]`).
- **Main Flow**:
  1. Installer imports `aprilcam.client`, `aprilcam.client.renderer`, and
     `aprilcam.cli.locate` — all succeed with no `cv2` anywhere in the
     process.
  2. Installer runs `aprilcam taggen ...` — the command exists and is
     discoverable via `--help`, but fails with a clear
     "install aprilcam[daemon]" message rather than a bare
     `ModuleNotFoundError`.
  3. Installer runs `import cv2` directly — fails, proving `cv2` is
     genuinely absent from the base install closure.
- **Postconditions**: `opencv-contrib-python` lives only in the `daemon`
  extra; `client/renderer.py` and `cli/locate.py` no longer transitively
  require it (via a `calibration/geometry.py` module split); `tags/`
  generation is still advertised as a base CLI capability, honestly
  gated at the point it actually needs pixels.
- **Acceptance Criteria**:
  - [ ] `test_import_cv2_fails_in_the_fresh_base_only_venv` passes for
        real; its `xfail(strict=True)` marker is removed.
  - [ ] `import aprilcam.client.renderer` succeeds in a fresh base-only
        venv (new packaging-test coverage).
  - [ ] `aprilcam taggen` (or `tags generate`) fails with a message
        naming `aprilcam[daemon]`, not a raw import traceback, when run
        without the extra.
  - [ ] `tests/test_no_cv2_in_client_modules.py` still passes unchanged.

### SUC-004: Installed-package test proves the real MCP entry point works
Parent: UC-22

- **Actor**: CI / a developer verifying a release before it ships.
- **Preconditions**: A built wheel, a fresh venv with the `daemon` extra
  (or whatever extra `aprilcam mcp` genuinely needs) installed.
- **Main Flow**:
  1. Test starts `aprilcam mcp` as a real subprocess in the fresh venv.
  2. Test completes a real `initialize` handshake over stdio
     (`mcp.client.stdio`/`ClientSession`, reusing
     `tests/aprilcam/mcp/test_stdio_offline.py`'s machinery).
  3. Test asserts the pinned 27-tool inventory and that the resolved
     `mcp` package version satisfies the declared `>=1.0,<2` constraint.
  4. Test shuts the subprocess down cleanly.
- **Postconditions**: A future dependency-resolution regression (like the
  mcp 2.x incident) fails this test instead of shipping silently.
- **Acceptance Criteria**:
  - [ ] `tests/packaging/test_installed_package.py` starts `aprilcam mcp`
        against the fresh venv and completes a handshake.
  - [ ] The test asserts the pinned tool inventory.
  - [ ] The test asserts the resolved `mcp` version satisfies
        `>=1.0,<2`.

### SUC-005: Use-case coverage index reflects real traceability data
Parent: N/A — process/tooling fix (CLASI's own `get_use_case_coverage()`
gate, not an AprilCam product use case)

- **Actor**: Sprint-planner / team-lead running the traceability gate.
- **Preconditions**: `docs/design/use-cases/*.md` holds the real,
  maintained use cases (29 UCs, 111 scenario IDs at time of writing).
- **Main Flow**:
  1. A generator (script or test-side fixture) reads every
     `### UC-NNN — Title` heading from `docs/design/use-cases/*.md`.
  2. It writes `clasi/usecases.md` with one `## UC-NNN: Title` line per
     use case — the exact format `get_use_case_coverage()`'s regex
     (`##\s+(UC-\d+):\s*(.+)`) parses.
  3. A regression test re-derives the same list and asserts it matches
     the committed `clasi/usecases.md` byte-for-byte (or asserts the
     count/id-set matches), failing if the index drifts from the source.
- **Postconditions**: `get_use_case_coverage()` reports `total_use_cases`
  matching the real count instead of 0.
- **Acceptance Criteria**:
  - [ ] `clasi/usecases.md` exists with 29 `## UC-NNN: Title` entries.
  - [ ] A regression test fails if `docs/design/use-cases/` gains or
        loses a UC without `clasi/usecases.md` being regenerated.
  - [ ] `get_use_case_coverage()`, called by hand, reports
        `total_use_cases: 29` (or the then-current count).

## GitHub Issues

(GitHub issues linked to this sprint's tickets. Format: `owner/repo#N`.)

## Definition of Ready

Before tickets can be created, all of the following must be true:

- [ ] Sprint planning document is complete (sprint.md, including its
      Architecture and Use Cases sections)
- [ ] Architecture review passed (or skipped, for changes with no
      architectural impact)
- [ ] Stakeholder has approved the sprint plan

## Tickets

| # | Title | Depends On | Issue |
|---|-------|------------|-------|
| 001 | Generate clasi/usecases.md index from docs/design/use-cases/ | — | get-use-case-coverage-reports-zero-reads-the-wrong-file.md |
| 002 | Fix get_tag_history docstring and document detection cadence in AGENT_GUIDE.md | — | tag-history-gaps-and-jitter-speed-mislead-mcp-consumers.md |
| 003 | Suppress compute_motion speed below the noise floor | 002 | tag-history-gaps-and-jitter-speed-mislead-mcp-consumers.md |
| 005 | Split calibration/geometry.py's lens-distortion functions into calibration/distortion.py | — | opencv-is-a-base-dependency-contradicting-the-daemon-only-contract.md |
| 004 | Move opencv-contrib-python to the daemon extra; lazy-import cv2 in tags/generator.py | 005 | opencv-is-a-base-dependency-contradicting-the-daemon-only-contract.md |
| 006 | Exercise the real aprilcam mcp entry point in the installed-package test | 004 | installed-package-test-does-not-exercise-aprilcam-mcp.md |
| 007 | Persist mobile-tag mount registrations across daemon restarts | — | mount-registrations-should-persist-across-daemon-restarts.md |
| 008 | Update AGENT_GUIDE.md and ROBOT_API_GUIDE.md for persistent mount registrations | 007 | mount-registrations-should-persist-across-daemon-restarts.md |

Tickets execute serially in the order listed (**note**: ticket numbers
are not renumbered when reordered — see Revision note below; ticket 005
now executes before ticket 004 despite the numbering). Ordering
rationale: the cheap, independent, high-signal tickets (001, 002) run
first; 003 follows 002 since it appends to the AGENT_GUIDE.md section
002 creates. 005→004→006 are sequenced because 005's
`calibration/geometry.py` split must land *before* 004 moves
`opencv-contrib-python` out of base dependencies — landing 004 first
would leave the entire `aprilcam` console script transitively
`cv2`-dependent-but-broken in a base-only install for as long as 005
hadn't yet landed (see Revision note below). 006 depends on 004 because
005, 004, and 006 all edit `tests/packaging/test_installed_package.py`
in sequence, and 006 must build on the file state 004 (the last of the
three) leaves behind. Mount persistence (007, the sprint's largest
item) and its doc follow-up (008) run last so they don't block the
smaller, independent tickets ahead of them.

## Revision (2026-08-24)

Solution item 5 and the fifth Success Criteria bullet originally
promised `get_use_case_coverage()` itself would report a non-zero
total. Ticket 001's implementation and verification found this
unsatisfiable from this repository: the installed CLASI tool resolves
its index path from a fixed `.clasi` anchor that ignores this
project's `paths:` config, and separately scans for per-sprint
`usecases.md` files, a layout the single-doc sprint model retired.
Both are CLASI-package defects, out of scope per this sprint's own
Solution item 5 and ticket 001's AC5. Both passages above were amended
to describe what was actually delivered — a correct, tested,
`clasi/usecases.md` index — rather than the tool's own return value,
which is tracked as a CLASI-package follow-up issue instead.
