---
id: '003'
title: aprilcam camera locate command
status: done
use-cases:
- SUC-003
depends-on:
- '001'
- '002'
github-issue: ''
issue: camera-geometry-for-real-parallax-correction.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# aprilcam camera locate command

## Description

New CLI command `aprilcam camera locate` that resolves reference
positions from the camera's linked playfield, drives detection through
the daemon (never opening a camera or touching a pixel itself), calls
ticket 002's solver, reports the result, and — quality-gated — writes
the solved `camera_position` (ticket 001) into the camera's config, on
disk and live.

**New file** `cli/locate.py`, wired under `cli/camera.py`'s `camera`
subparser tree, mirroring `cli/calibrate.py`'s exact shape: its own
module, grouped under `camera`, called from `cli/camera.py`'s
`add_subparser` alongside the existing `calibrate_cli.add_subparser(sub)`
call. Like `cli/calibrate.py`, this module is a **pure RPC/orchestration
shell around vision the daemon does** — the one exception, spelled out
explicitly, is that the least-squares *solve itself* (ticket 002) runs
here, client-side, because it is pure algebra over already-vision-
processed `WorldPoint`s (no cv2, no pixel access) — see sprint.md's
Design Rationale ("client-side pure function, not a new
`LocateCamera` RPC") for the full reasoning. Detection — the only
pixel-touching step — still crosses into the daemon via `GetTag`.

**CLI surface**:
```
aprilcam camera locate <camera> --reference TAG:SLUG:HEIGHT_CM
    [--reference TAG:SLUG:HEIGHT_CM ...] [-d HOST]
    [--dry-run] [--force]
```
`TAG` is an AprilTag id (int; this sprint supports AprilTag-family
references only — matches the real fixture, see sprint.md Scope). `SLUG`
is a playfield feature slug (e.g. `dot-northeast-orange`). `HEIGHT_CM`
is the reference's physical stand height, a positive float. At least 2
`--reference` args are required; fewer is a CLI-level argument error
(`EXIT_ERROR`, printed before any connection is attempted — mirrors
`cmd_image`'s "error before capture" discipline). Exactly 2 references
prints an advisory (not blocking) recommending 3+ for redundancy —
sprint.md's own resolved guidance ("ask for references spread in
angle... consider requiring/recommending 3").

**Main flow**:
1. Connect (`discovery_factory().connect(args.daemon)`), refuse `--all`
   up front exactly like `cmd_calibrate`/`cmd_view` (single-target;
   there is nothing sensible about locating N cameras across N daemons
   in one command).
2. Fetch the camera's config (`GetCameraConfig`) to find its linked
   playfield; no link -> raise the same `NoPlayfieldLinked`-shaped
   failure `Calibrate` already uses for this precondition (reuse the
   existing error type, do not invent a new one).
3. Fetch the playfield (`GetPlayfield`) and resolve each reference's
   `SLUG` via an **exact** lookup into `definition.features()` — not
   the fuzzy `FindFeature`/`FeatureQuery` matcher (`playfield/query.py`):
   an operator's exact-slug typo must be a hard, specific error naming
   the slug that did not resolve, never a near-miss fuzzy match or an
   ambiguous multi-result. An unresolved slug is a CLI-level error
   before any detection is attempted.
4. **Contamination guard**: `ListTagParameters` and refuse (before any
   detection) if any reference `TAG` is currently registered with a
   nonzero `mount_z` — that registration's own (possibly stale)
   parallax correction would corrupt the raw reading this solve needs.
   A reference tag registered with an all-zero mount (or unregistered
   entirely) is fine — it has no effect on `GetTag`'s reported world
   position (confirmed: `_apply_mount_correction`'s no-op path covers
   both "no registration" and "mount_z <= 0" identically).
5. For each reference, call `GetTag(camera, TagId(TagFamily.APRILTAG,
   TAG))`. Not detected (`GetTagResponse.record` absent) -> a specific
   error naming which reference tag was not seen, before attempting the
   rest (fail fast, not a partial solve over whatever *was* seen).
6. Build `ReferenceObservation`s (`surveyed` = the resolved feature
   position, `observed` = the returned record's `world`, `height_cm` =
   the operator-given stand height) and call `solve_camera_geometry`
   (ticket 002).
7. Print the solved nadir, height, RMS residual, and condition number.
   Flag a warning when the fit is poor (see thresholds below).
8. **Apply, quality-gated**: if `--dry-run`, stop here — never write,
   regardless of fit quality. Otherwise, if the fit is good (both
   thresholds pass) or `--force` was given, write the result:
   - Load the camera's on-disk config file
     (`config_dir/cameras/<slug>/config.json`) via the existing
     `ConfigLoader`, replace/add its `camera_position` block (preserve
     every other field untouched — `playfield`, `settings`), and write
     it back (atomic write — tmp + fsync + rename, matching this
     project's existing convention for every shared JSON file,
     `docs/design/design.md` §6). This is the CLI, run by the human
     operator, writing to `config_dir` — consistent with that
     directory being human-owned; the *daemon* still never writes it
     (design.md §2's "config/state split" is about the daemon, not
     about a human-driven CLI tool acting on the operator's explicit
     command).
   - Push it live via `SetCameraConfig` so the daemon picks it up
     without a restart (mirrors `cmd_config`'s existing push path).
   - If the fit is poor and `--force` was not given, refuse to write,
     print the RMS/condition-number numbers that triggered the refusal,
     and say exactly what `--force` or `--dry-run` do next — never a
     silent partial effect (config file written but daemon not pushed,
     or vice versa).

**Provisional quality-gate thresholds** (document as such — tunable,
same "evidence-based, revisit if real data disagrees" posture this
codebase already applies to `CONFIRMATION_STREAK`; ticket 004's
real-hardware pass is the first data point to sanity-check them
against): `rms_residual_cm > 1.0` **or** `condition_number` above a
documented cutoff (pick a concrete default, e.g. by comparing the
ticket 002 well-conditioned-baseline test's condition number against
its ill-conditioned-case test's — set the cutoff meaningfully between
the two, not an arbitrary round number) triggers the "poor fit, refuse
without `--force`" path. State the chosen numbers plainly in the CLI's
help text and in a code comment naming this ticket, not just buried in
a docstring.

## Acceptance Criteria

- [x] UC-29.S-1 (the real fixture, end-to-end through the CLI layer):
      given the same tag 15/tag 14 observations as ticket 002's
      fixture (fed through a fixture `Daemon`/`Discovery` double, not a
      real camera), `camera locate` reports nadir/height/RMS matching
      ticket 002's solver output and, without `--force` needed (a good
      fit), writes `camera_position` both to the config file and via
      `SetCameraConfig`.
- [x] UC-29.S-2 (ill-conditioned references): reported, with a visible
      warning; not written without `--force`.
- [x] UC-29.S-3 (fewer than 2 `--reference` args): refused with a clear
      error, before any connection is attempted.
- [x] UC-29.S-4 (contaminated reference): a reference tag currently
      registered with nonzero `mount_z` is refused with an explicit
      error naming the tag, before any `GetTag` call.
- [x] UC-29.S-5 (poor fit, no `--force`): reported but not written;
      `--force` writes it; `--dry-run` never writes regardless of fit
      quality (tested independently of the quality gate).
- [x] An unresolved feature slug is refused with a specific error
      naming the slug, before any detection.
- [x] A reference tag not detected is refused with a specific error
      naming the tag, before the remaining references are even
      attempted (fail-fast, not a partial solve).
- [x] No playfield linked on the target camera -> the same
      `NoPlayfieldLinked`-shaped failure `camera calibrate` already
      produces for this precondition (reused, not reinvented).
- [x] `--all` is refused up front (single-target), matching
      `cmd_calibrate`/`cmd_view`'s existing precedent.
- [x] The config-file write is atomic (tmp + fsync + rename) and
      preserves every field `camera_position` did not touch
      (`playfield`, `settings`) — regression test round-tripping a
      config with pre-existing `settings` through a `locate --apply`-
      shaped write.
- [x] `uv run mypy` clean; `cli/locate.py` imports `calibration.geometry`
      directly and nothing from `cv2` (verify — `cli/` is not in
      `tests/test_no_cv2_in_client_modules.py`'s enforced set, but this
      module still must not import `cv2` in practice, matching every
      other CLI module's existing discipline).

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/cli/ -v`
  (confirm the existing argparse-wiring pattern this ticket's tests
  follow is unbroken), plus the full headless suite.
- **New tests to write**: argparse wiring tests for `camera locate`
  (mocked `Discovery`/`Daemon`, mirroring `tests/aprilcam/cli/`'s
  existing pattern exactly) covering every scenario in Acceptance
  Criteria above: the real-fixture happy path, ill-conditioned
  warning-without-write, insufficient-references refusal,
  contaminated-reference refusal, unresolved-slug refusal,
  tag-not-detected refusal, no-playfield-link refusal, `--all`
  refusal, `--dry-run` never writing, `--force` overriding a poor-fit
  refusal, and the config-file-preserves-other-fields round trip.
- **Verification command**: `uv run pytest -m "not camera"` and
  `uv run mypy`.

**Documentation updates**: none beyond docstrings and the CLI's own
`--help` text (thresholds, reference format) — the use-case document
(`docs/design/use-cases/cli.md` UC-29) is ticket 004's, written once
the command's real behavior (including its real-hardware quirks, if
any) is confirmed.

## Completion Notes

**Implemented**: `src/aprilcam/cli/locate.py` (new), wired into
`src/aprilcam/cli/camera.py`'s `add_subparser` right after
`calibrate_cli.add_subparser(sub)`. `cmd_locate` follows the ticket's
Main Flow exactly: `--all` refusal -> `--reference` parsing/count check
(both before any connection) -> `GetCameraConfig` ->
`NoPlayfieldLinked` if unset -> `GetPlayfield` -> exact-slug resolution
against `definition.features()` -> `ListTagParameters` contamination
guard (`mount_z > 0.0` refused, matching
`project_tag_to_field_plane`'s own `<= 0` no-op sentinel exactly) ->
per-reference `GetTag` (fail-fast on the first miss) ->
`solve_camera_geometry` -> report nadir/height/RMS/condition_number ->
quality gate -> atomic on-disk write (`tmp` + `fsync` + `os.replace`,
mirroring `camera/registry.py::_save`) + `SetCameraConfig` push.

**Thresholds chosen** (both stated in `--force`'s help text and in code
comments naming this ticket, `src/aprilcam/cli/locate.py`):
`RMS_RESIDUAL_THRESHOLD_CM = 1.0` (sprint.md's own stated value).
`CONDITION_NUMBER_THRESHOLD = 447.2` — the geometric mean of ticket
002's own two conditioning-test bounds
(`test_solve_camera_geometry_well_conditioned_three_reference_baseline`'s
`< 200.0` and
`test_solve_camera_geometry_near_collinear_references_condition_number_blows_up`'s
`> 1000.0`); geometric rather than arithmetic mean because condition
numbers vary multiplicatively. Both are documented as provisional in
the module, matching the `CONFIRMATION_STREAK` precedent; ticket 004's
real-hardware pass is the intended first sanity check against them.

**Testing**: `tests/aprilcam/cli/test_locate_cli.py` (new, 35 tests) —
headless, fake `Discovery`/`Daemon`/`ConfigLoader`/`Directories`
throughout, mirroring `test_calibrate_cli.py`/`test_camera_cli.py`'s
established style. Covers every Acceptance Criterion above, plus
`--reference` format-validation edge cases and a "disk write succeeded
but the live push failed" case (not explicitly required, but a
reasonable extension of the "never a silent partial effect" rule).
`tests/aprilcam/cli/ -v` (298 passed) and `uv run pytest -q -m "not
camera and not integration"` (1335 passed = 1300 baseline + 35 new)
both green; `uv run mypy` clean (79 source files). `cli/locate.py`
imports only `math`-free, `cv2`-free modules — no `cv2` import,
confirmed by inspection (the ticket's own acceptance criterion, since
`cli/` is not in `test_no_cv2_in_client_modules.py`'s enforced set).

**Live smoke test — skipped, deliberately.** The ticket permitted one
read-only, non-writing smoke test against the live daemon
(`--dry-run`). Before attempting it, `aprilcam daemon status` and
`aprilcam camera list` were run (both read-only) to locate camera 3.
`camera list` returned `Method not found!` and `daemon status` reported
`cameras: 0`, `config_dir: /Users/eric/.config/aprilcam` — inconsistent
with the staged setup described (camera 3 calibrated, 3 cameras open,
tags 14/15 staged). `ps` showed **two** live `python3 -m
aprilcam.daemon` processes on this machine, from two different
checkouts (`AprilTags/.venv` and `aprilcam/.venv`); the local unix
socket this checkout's CLI resolves to is apparently not the one
holding the staged hardware state. Given the explicit instruction not
to restart or otherwise disturb the live daemon, and given this
ambiguity, further probing was judged unsafe and skipped rather than
risk interfering with ticket 004's staged acceptance setup. Both
read-only calls made are informational RPCs with no state mutation;
nothing was written or changed. Ticket 004 does the live acceptance
pass and should resolve which daemon/socket the operator's shell
actually targets before running `camera locate` for real.
