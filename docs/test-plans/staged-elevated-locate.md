# Test Plan: Staged Elevated Locate — raised-block dry-run coverage

**Status:** IMPLEMENTED — test module written and verified skipping cleanly
(sprint 035 ticket 016, UC-29, SUC-003 step 4). **Not yet exercised for
real** — no raised blocks are currently on the field (stakeholder-confirmed,
2026-08-23). This document is written for whoever stages the blocks later.
**Test module:** `tests/acceptance/test_elevated_locate.py`.
**Date:** 2026-08-23.

## 1. What this is, in one paragraph

`aprilcam camera locate <camera> --reference TAG:SLUG:HEIGHT_CM ...` solves
a camera's ground nadir (x, y) and optical height from two or more AprilTags
mounted *above* the field plane — on a raised block of known height — at
surveyed playfield positions (`src/aprilcam/cli/locate.py`). Run with
`--dry-run`, it detects the references, runs the solve, and prints the full
report (nadir, height, RMS residual, condition number) **without ever
writing anything** — `cmd_locate`'s dry-run branch returns before the
quality-gated write logic even runs, regardless of whether the fit is good
or poor. This test plan's one live test
(`test_elevated_dry_run_locate_reports_full_solve_within_quality_gates`)
runs exactly that command against real, physically staged blocks and checks
the reported RMS and condition number against the same thresholds
`cmd_locate` itself uses to gate a real write.

**This coverage is opt-in.** It is gated entirely behind
`APRILCAM_TEST_ELEVATED=1` (unset by default) and does not run in a normal
acceptance pass — see §7.

## 2. Current field state — no blocks staged

As of 2026-08-23, no raised blocks are on the field. This is the normal,
expected state: the stakeholder confirmed the elevated stands used for
sprint 029's original measurement (§3 below) were removed and are not
needed again for day-to-day operation. **Anyone running this suite today
will see the live test skip** with a message naming exactly which reference
tag was not detected — that is success for the default state, not a
failure. See §7 for what a normal run actually shows.

## 3. Blocks, tags, heights — the exact staging recipe

Two references, reusing the exact pair sprint 029 measured live on this
same camera/playfield and that
`tests/aprilcam/calibration/test_geometry.py::
test_solve_camera_geometry_real_fixture_regression` pins as a permanent
headless regression (§8) — not invented for this ticket:

| Block | AprilTag (36h11) | Placed at (surveyed feature) | Surveyed field position | Stand height |
|---|---|---|---|---|
| Block A | id **15** | `dot-northeast-orange` | (50, 30) cm | **17.2 cm** |
| Block B | id **14** | `dot-southwest-orange` | (−50, −30) cm | **15.0 cm** |

Both slugs are defined in `config/aprilcam/playfields/main-playfield.json`
(the field's real, checked-in definition — do not hand-edit; if the
playfield changes, re-derive the surveyed positions from that file, not
from this table).

**Physical staging:**

1. Print AprilTag 36h11 ids 14 and 15 at a size you can read reliably from
   camera 3's normal working distance (10 cm is a safe default; this rig
   already uses that size elsewhere):
   ```
   aprilcam tags generate --family apriltag --ids 14,15 --size-cm 10 --out /tmp/elevated-blocks
   ```
2. Mount each printed tag flat, face-up, on top of a rigid block/stand of
   the exact height listed above (measured from the field surface to the
   tag's *printed face*, not to the top of the block material if there is
   any additional mounting thickness — that thickness is part of the
   height you must measure and use).
3. Center each block directly over its surveyed dot position:
   - Block A (tag 15) at `dot-northeast-orange` — 50 cm east, 30 cm north
     of the field origin (AprilTag 1, field center).
   - Block B (tag 14) at `dot-southwest-orange` — 50 cm west, 30 cm south
     of the field origin.
   The field's dot markers (`config/aprilcam/playfields/
   main-playfield.json`'s `dots` array) are printed on the field surface
   specifically to make this placement unambiguous — center the block over
   the printed dot.
4. Confirm camera 3 (`arducam-ov9782-usb-camera`) can actually see both
   tags before running the test — `aprilcam camera tags
   arducam-ov9782-usb-camera` should list both `apriltag 14` and
   `apriltag 15` after a few polls (detection needs priming; see
   `docs/knowledge/daemon-camera-grant-and-detection-priming.md`).

A third reference is not required — the two above already clear both
quality gates by a wide margin in the pinned regression (condition_number
~67.4 against a 447.2 threshold; §5) — but the CLI itself prints a
non-blocking advisory recommending 3+ references for redundancy if you
want tighter conditioning. Adding a third staged block only requires
updating both this table and `tests/acceptance/test_elevated_locate.py`'s
`_REFERENCE_1`/`_REFERENCE_2` (add a `_REFERENCE_3`) together, deliberately
in sync — do not let the doc and the test drift apart.

## 4. Running it

```
APRILCAM_TEST_ELEVATED=1 uv run pytest tests/acceptance/test_elevated_locate.py -v
```

This requires: blocks staged per §3, the real local daemon already running
(never started by the test — see `CLAUDE.md`'s hard rule), and camera 3
attached and usable. It is a deliberate, human-run command — it is not part
of any per-ticket or per-sprint automated loop (an unset
`APRILCAM_TEST_ELEVATED` is what every automated pass sees, including
`close_sprint`'s full-suite gate).

The command the test itself runs, for reference (exactly reproducible by
hand):

```
aprilcam camera locate arducam-ov9782-usb-camera \
  --reference 15:dot-northeast-orange:17.2 \
  --reference 14:dot-southwest-orange:15.0 \
  --dry-run
```

## 5. Quality gates

Both gates come directly from `src/aprilcam/cli/locate.py` (imported into
the test, never re-declared, so the two cannot drift apart):

- **RMS residual ≤ `RMS_RESIDUAL_THRESHOLD_CM` = 1.0 cm**
- **Condition number ≤ `CONDITION_NUMBER_THRESHOLD` = 447.2** (the
  ticket's own prose rounds this to "≤ 447"; the code's actual threshold —
  the geometric mean of the two conditioning tests in
  `test_geometry.py`, 200 and 1000 — is 447.2, and that is the exact value
  the test asserts against)

These are the same thresholds `cmd_locate` uses to decide whether a real
(non-dry) write needs `--force`. A dry run never fails on a poor fit by
itself — it always reports and exits 0 — so this test's own assertions are
what turn "poor fit" into a real, visible failure for this suite.

## 6. Reading a pass

A clean pass looks like this (numbers will vary slightly run to run —
compare against §3's pinned regression as a sanity check, not an exact
match):

```
solved: nadir=(3.057, -2.799) cm  height=128.230 cm  rms_residual_cm=0.1585  condition_number=67.44
dry-run: reporting only -- nothing written
```

`pytest -v` reports the test itself as `PASSED`; the acceptance layer's
terminal summary (`conftest.py`'s `pytest_terminal_summary`) additionally
prints a `passed:`/`failed:`/`skipped:` roll-up for the whole
`tests/acceptance/` run.

## 7. What a normal (default) run shows — verified, not assumed

With `APRILCAM_TEST_ELEVATED` unset (the default, and what today's field
state produces even if set — see §2), both tests in this module are
*collected* and then *skip* cleanly:

```
$ uv run pytest tests/acceptance/test_elevated_locate.py -v
tests/acceptance/test_elevated_locate.py::test_elevated_dry_run_locate_reports_full_solve_within_quality_gates SKIPPED
tests/acceptance/test_elevated_locate.py::test_this_modules_dry_run_locate_call_passes_the_node_id_allowlist SKIPPED
  SKIP ... -- Skipped: APRILCAM_TEST_ELEVATED=1 not set -- elevated ('with raised blocks') coverage is opt-in only ...
2 skipped
```

With `APRILCAM_TEST_ELEVATED=1` set but no blocks staged (today's actual
opt-in outcome), the live test still skips, but for a different, more
specific reason — the CLI genuinely tried and genuinely could not detect
the reference tag:

```
SKIP ... -- Skipped: reference AprilTags 14/15 are not physically staged/elevated
on the field right now ... camera locate's own response: error: reference tag 15
(AprilTag) was not detected by 'arducam-ov9782-usb-camera' -- check it is staged,
in view, and the camera is calibrated, then retry
```

Both outcomes were run and observed live against the real rig while
writing this document (2026-08-23) — collection succeeds either way (this
matters: it is what lets the node-id allowlist's collection-time AST scan,
§8, inspect this module's source on every acceptance run, gated or not),
and the real production daemon's `~/.config/aprilcam` /
`~/.local/share/aprilcam` state was confirmed byte-identical before and
after by the same sha256 no-write guard every other acceptance test relies
on (`tests/acceptance/conftest.py`).

## 8. When the numbers miss the gates

If a genuine staged run reports RMS or condition number over the
thresholds in §5:

1. **Re-check placement first, not the code.** The single most likely
   cause is a block not actually centered over its surveyed dot, or a
   height measured to the wrong reference plane (block top vs. tag face).
   A few cm of placement error is enough to blow the RMS gate; a block
   placed close to diametrically opposite the camera's nadir from another
   reference is the specific failure mode that produced sprint 029's own
   ~20 cm real error (`src/aprilcam/cli/locate.py`'s own warning text) and
   inflates the condition number.
2. **Add a third, well-separated reference** (§3) rather than trusting a
   marginal two-reference fit — conditioning improves with angular spread
   between references as seen from the camera's estimated nadir, not just
   with more references anywhere.
3. **Re-run `--dry-run` again** before concluding anything — a single bad
   detection burst (partial occlusion, a momentary exposure glitch) can
   produce one bad sample; the CLI does a fresh detection each invocation.
4. **If it still fails after (1)–(3), this is a real finding, not a test
   bug.** Record it — do not loosen `RMS_RESIDUAL_THRESHOLD_CM` or
   `CONDITION_NUMBER_THRESHOLD` in `src/aprilcam/cli/locate.py` to make the
   acceptance test pass. Those constants gate a real, non-dry write
   elsewhere in the same code; weakening them here weakens the actual
   safety property they exist for, for the sake of a green test.
5. **Never** react to a failing or skipped elevated test by running a
   real, non-dry `camera locate` "just to see" — see §9.

## 9. A real, non-dry `locate` is a deliberate, separate stakeholder action

**No test in this suite — and no test in `tests/acceptance/` generally —
ever runs a non-dry `camera locate`.** This is structural, not a
convention anyone has to remember:

- Every command this module's live test runs includes `--dry-run`
  literally in the argument list.
- `tests/acceptance/conftest.py`'s collection-time node-id allowlist
  (`find_disallowed_calls`, sprint 035 ticket 015) parses every test
  module's source under this directory and refuses to even *collect* a
  test whose call contains the string `"locate"` without `"--dry-run"`
  among the same call's arguments, or any `.calibrate(...)` call. A
  synthetic proof of this — this exact module's dry-run call shape passing
  the guard, and the same shape with `--dry-run` removed being flagged —
  lives in
  `test_this_modules_dry_run_locate_call_passes_the_node_id_allowlist`
  (§7 shows it collected and, once `APRILCAM_TEST_ELEVATED=1` is set,
  passing).
- `conftest.py`'s autouse sha256 no-write guard independently re-hashes
  every real file under `config_dir`/`state_dir` after every test in this
  directory, this module included, and fails loudly naming the exact file
  if anything changed — a second, independent check that would catch a
  write even if the allowlist somehow had a gap.

Running `camera locate arducam-ov9782-usb-camera --reference ... --force`
(or without `--dry-run`) for real is a **deliberate, rare, human action** —
it solves and writes `camera_position`, the one hand-solved value CLAUDE.md
calls out as sacred
(`height_cm=128.22990496814816, nadir_x_cm=3.0570142285481654,
nadir_y_cm=-2.798700632324644` for `arducam-ov9782-usb-camera`, currently
live and correct). If a real re-locate is ever genuinely needed (the
camera physically moved, for example), a human runs that command directly,
reads the printed solve report, and decides — it is never something any
automated test in this repository performs on its own, and it must never
become one.

## 10. Coverage that exists whether or not blocks are staged

Without raised blocks on the field (§2 — today's actual state), coverage
for the solve math this module exercises live continues to rest on the
existing unit/real-fixture regressions in
`tests/aprilcam/calibration/test_geometry.py:485-887`. In particular:

- `test_solve_camera_geometry_real_fixture_regression` (line 852) pins the
  *exact same two references* this document's §3/§4 use — nadir (3.05,
  −2.75) cm, height 128.25 cm, `rms_residual_cm` ≈ 0.16, `condition_number`
  ≈ 67.4 — as a headless, always-running regression. It needs no camera,
  no daemon, and no staged blocks, and it runs in every normal test pass.
- `test_solve_camera_geometry_well_conditioned_three_reference_baseline`
  and
  `test_solve_camera_geometry_near_collinear_references_condition_number_blows_up`
  (lines 941 and 1019) independently pin the two conditioning-number
  reference points (`< 200.0` and `> 1000.0`) that
  `CONDITION_NUMBER_THRESHOLD` in §5 was derived from.
- `test_project_tag_to_field_plane_hand_computed_worked_example` and its
  neighbors (lines 485–559) cover the parallax-correction math a raised
  reference's apparent position depends on.

This ticket does not weaken, replace, or duplicate that coverage — it adds
one additional, opt-in, live-hardware corroboration on top of it, for the
day blocks are staged again.
