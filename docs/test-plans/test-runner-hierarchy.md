# Test Plan: The Three-Script Runner Hierarchy

**Status:** IMPLEMENTED (sprint 035 ticket 017, UC-29). Scripts:
`scripts/test-gate.sh` (ticket 008), `scripts/test-hardware.sh`,
`scripts/test-acceptance.sh` (this ticket).
**Date:** 2026-08-23.

## 1. Why three scripts, not one

This project's test suite is layered by how much real, physical
context a test needs (sprint 035's own framing):

- **L0** -- offline, no camera, no daemon. Everything not marked
  `camera` or `integration` (`tests/image_matrix/` plus the
  pre-existing unit/system tree).
- **L1** -- live-hardware system tests. Marked `camera` and/or
  `integration`, most spinning up their own short-lived, isolated
  daemon per test against whatever camera hardware is actually
  attached.
- **L2** -- acceptance. Marked `acceptance` (always layered on top of
  `camera`/`integration`, never replacing them -- sprint.md
  Decision 4), running read-only/reversible checks against the REAL,
  already-running daemon and the real, physical field, guarded by a
  sha256 no-write sweep over `config_dir`/`state_dir`
  (`tests/acceptance/conftest.py`).

One script per layer, each a thin wrapper with no test logic of its
own -- the same `-m` marker expression a human would type by hand, so
there is never drift between what CI/`close_sprint` runs and what an
operator runs manually.

| Script | Layer | Needs a rig? | Mutates real state? | Runs from |
|---|---|---|---|---|
| `scripts/test-gate.sh` | L0 | No | No | Anywhere, including CI and an agent shell |
| `scripts/test-hardware.sh` | L1 | Yes | No (isolated roots) | **Operator's Terminal only** |
| `scripts/test-acceptance.sh` | L2 | Yes | No (no-write-guarded) | **Operator's Terminal only, real daemon already up** |

## 2. `scripts/test-gate.sh` -- always, no hardware

```
bash scripts/test-gate.sh
```

Runs `uv run pytest -m "not camera and not integration"` -- 1668 of
this repo's 1772 collected tests as of this ticket. This is the
canonical close-sprint gate: run it from CI, from `close_sprint`, and
by hand, with zero special setup. It needs no camera, no daemon, and
runs fine from an agent shell. See `scripts/test-gate.sh`'s own header
comment and `docs/test-plans/camera-image-matrix.md` for what it
covers.

## 3. `scripts/test-hardware.sh` -- rig required, no acceptance risk

```
bash scripts/test-hardware.sh
```

Runs `uv run pytest -m "(camera or integration) and not acceptance"`
-- 85 tests as of this ticket (sprint 035 ticket-group 3: tickets 009,
011, 012, 013). `and not acceptance` is what keeps this suite disjoint
from L2 even though every acceptance test also carries `camera`/
`integration` -- verified empirically, not assumed: 85 hardware node
ids and 19 acceptance node ids were both collected and diffed directly
against each other at ticket-017 time, with zero overlap.

**This can only truly execute from the operator's Terminal.** A daemon
started from an agent process (including the private, per-test daemons
most of this suite spins up itself) inherits the launching shell's
macOS TCC camera grant -- Claude Code's helper bundle has none.
Enumeration still succeeds either way, so this does **not** show up as
a clean wall of skips; it shows up as real test **failures** citing
`RuntimeError: failed to open camera at index N` / OpenCV's `not
authorized to capture video`. Confirmed directly while building this
ticket: an agent-shell run of this exact script produced 18 failed /
24 passed / 42 skipped / 1 xfailed, while nothing about the code or
the rig was actually broken. Full mechanism:
`docs/knowledge/daemon-camera-grant-and-detection-priming.md`.

The script itself prints a full preflight checklist before running --
Terminal-only, daemon status, cameras free, ambient-light caveat, and
what healthy vs. wrong-shell output looks like. Read it every time; it
is not a one-time reminder.

**Never starts, stops, or restarts a daemon.** If a daemon this suite
needs is unreachable, the script does not try to fix that -- stop and
ask the operator to start it from Terminal (see the knowledge-base doc
above for why an agent-initiated restart is actively harmful, not just
unhelpful).

## 4. `scripts/test-acceptance.sh` -- rig + real daemon, no-write-guarded

```
bash scripts/test-acceptance.sh
```

Runs `uv run pytest -m acceptance` -- 19 tests as of this ticket
(sprint 035 ticket-group 4, UC-29), tee-ing full output to a
timestamped log file under `.clasi/log/` (e.g.
`.clasi/log/acceptance-20260823-182239.log`; that directory is already
gitignored end-to-end -- see `.clasi/log/.gitignore` -- so these logs
are never accidentally committed). The tee runs under `pipefail`
(`set -euo pipefail` in the script), so a pytest failure still fails
the script -- tee-ing output never hides a real exit code. Verified
directly: a synthetic `false | tee /dev/null` under the same shell
options propagates exit code 1, and the script's own real run (§5)
produced the log file above with matching content.

Unlike `test-hardware.sh`'s suite, every test here talks to the
**real, already-running** daemon on its normal socket -- it never
spins up an isolated one of its own, and
`tests/acceptance/conftest.py`'s `pytest_configure` hard-refuses to
run at all (a `pytest.UsageError`, pytest exit code 4) if
`APRILCAM_CONFIG_DIR`/`APRILCAM_STATE_DIR` isolation overrides are set
-- this layer exists specifically to exercise the operator's real
environment, so `test-acceptance.sh` never sets, exports, or unsets
either variable itself.

**This layer is no-write-guarded, not merely read-only by convention.**
Every real file under the real `config_dir`/`state_dir` is
sha256-hashed once at session start and re-hashed after every test; a
mismatch fails loudly naming the exact file. `camera_position` for
`arducam-ov9782-usb-camera` (CLAUDE.md: sacred) is proven
byte-identical before and after every run, not assumed. A
collection-time AST guard additionally refuses to even *collect* any
test in this directory that calls `.calibrate(...)` or a non-dry
`locate` -- see `tests/acceptance/conftest.py`'s module docstring for
both mechanisms in full.

**Elevated ("raised block") coverage is separate and opt-in.** Gated
behind `APRILCAM_TEST_ELEVATED=1` and requiring physically staged
blocks at surveyed field positions; `test-acceptance.sh` does not set
that variable, so it skips cleanly by default. See
`docs/test-plans/staged-elevated-locate.md` before ever setting it by
hand -- it documents the exact staging recipe, quality gates, and why
a real (non-dry) `locate` is never something any test in this
repository runs on its own.

**Ambient light affects this layer too.** Run live against the real
field on 2026-08-23 at dusk: `test_get_tags_superset_camera_3`,
`test_get_tags_superset_camera_5`, and
`test_mount_z_register_moves_per_formula_then_unregister_restores` all
skipped cleanly (not failed) with a diagnostic naming exactly which
markers were not detected across a generous poll budget -- markers
that are solid in daylight, per `docs/knowledge/
dark-camera-image-uvc-drift-reset.md`'s "Camera 5's exposure is
ambient-dependent" section. That specific dusk run: 14 passed, 5
skipped, 0 failed. Re-run in daylight for a harder read of recall.

## 5. Real runs observed while building this ticket (2026-08-23)

Both scripts were run for real against this dev rig while implementing
this ticket, from the same (agent) shell that wrote the code -- which
is itself a live demonstration of §3's warning:

- `bash scripts/test-hardware.sh`: exit 1. 18 failed, 24 passed, 42
  skipped, 1 xfailed, 268.82s -- the expected wrong-shell signature
  (camera opens failing, not a code regression); see §3.
- `bash scripts/test-acceptance.sh`: exit 0. 14 passed, 5 skipped
  (elevated coverage opt-out + dusk-degraded recall, see §4), 34.24s.
  Log written to `.clasi/log/acceptance-20260823-182239.log`.

Neither run is a substitute for a real Terminal-launched run with
daylight and an idle rig -- they are recorded here as evidence the
scripts themselves behave exactly as designed under both the "wrong
shell" and "real daemon reachable" conditions, not as a certification
that the hardware suite is currently green.
