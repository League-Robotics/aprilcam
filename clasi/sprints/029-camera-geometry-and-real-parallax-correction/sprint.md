---
id: 029
title: Camera geometry and real parallax correction
status: ticketing
branch: sprint/029-camera-geometry-and-real-parallax-correction
worktree: false
use-cases: []
issues:
- camera-geometry-for-real-parallax-correction.md
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 029: Camera geometry and real parallax correction

## Goals

Give the daemon a real answer to "where is this camera, and how high
is it above the field" — the one input `calibration.geometry.
project_tag_to_field_plane` has always needed and never had — so that a
registered `mount_z` actually corrects a real detected position instead
of silently no-op'ing. Deliver both halves the stakeholder specified:
(a) `camera_position` (nadir + optical height) as a first-class,
plumbed `CameraConfig` field, and (b) an `aprilcam camera locate`
command that *solves* nadir and height from elevated reference tags at
surveyed positions rather than asking anyone to tape-measure them.

## Problem

`DaemonService._camera_geometry_for` (`daemon/service.py` ~line 1273)
returns a hardcoded `(WorldPoint(0, 0), 0.0)` for every camera. Height
`0.0` is `project_tag_to_field_plane`'s own documented "nothing to
correct" sentinel, so parallax correction always no-ops. Sprint 025
wired a registered `mount_z` fully end-to-end and unit-tested it with
hand-picked geometry, but flagged this exact gap plainly in three
places (`calibration/geometry.py`'s module docstring, `daemon/
detection.py`'s module docstring, and `_camera_geometry_for`'s own
docstring): a registered `mount_z` has zero observable effect against
real hardware, while `mount_x`/`mount_y`/`mount_yaw_rad` (which need no
camera geometry) are verified working.

Real measurement taken today makes the size of the gap concrete. Camera
3 (`arducam-ov9782-usb-camera`), calibrated against `main-playfield`
(10/10 ArUco matched, reprojection error 0.168 cm), with no mounts
registered:

| tag | surveyed feature | true (x,y) cm | stand height | reported (x,y) cm | error |
|---|---|---|---|---|---|
| 15 | dot-northeast-orange | (50, 30) | 17.2 cm | (57.38, 34.90) | 8.86 cm |
| 14 | dot-southwest-orange | (-50, -30) | 15.0 cm | (-57.15, -33.40) | 7.92 cm |

An 8-9 cm error at a 15-17 cm mount height, on a well-calibrated camera,
is exactly the parallax shape `project_tag_to_field_plane` exists to
remove — and today nothing removes it.

Solving for the correction with a least-squares fit over both readings
(4 equations, 3 unknowns: nadir `Cx`, `Cy`, height `H`) gives camera
nadir `(3.05, -2.75)` cm, height `128.25` cm, RMS residual `0.16` cm —
applying it brings both tags to within ~0.2 cm of ground truth. Two
things this number proves, both binding on this sprint's design:

1. **The nadir is not the field centre.** V1's config asserted
   `x_offset: 0, y_offset: 0`; the real nadir is ~4 cm off-centre.
   Assuming centred leaves residual error — evidence *for* solving it,
   not tape-measuring it.
2. **The height cannot be tape-measured either.** The stakeholder's
   physical cross-check: sensor plane at 127 cm, lens end at 125 cm.
   The solved 128.25 cm is neither — it is the *effective optical
   centre* the pinhole model needs, about 1 cm above the physical
   sensor, most plausibly absorbing unmodelled lens distortion (v2
   calibration stores only the homography, no intrinsics). No ruler
   yields 128.25; only a solve does.

**A hazard already hit and worth naming up front**: solving nadir by
intersecting the two displacement lines (surveyed→reported, extended)
failed badly — nadir `(-16.1, -13.9)`, ~20 cm off, residual 150x worse
than the least-squares optimum. Cause: camera nadir, a tag's true
footprint, and its apparent (uncorrected) position are collinear by
construction (this is exactly what parallax *is* — a ray from the
camera continuing past the tag to the ground). Two references placed
on opposite sides of the camera produce two nearly-coincident lines;
intersecting them is wildly ill-conditioned. This sprint's solver must
use a genuine least-squares fit over the full 2D residual (direction
*and* magnitude), not a line intersection, and must say so to the
operator when the supplied references are too collinear to trust.

## Solution

**(a) `camera_position` on `CameraConfig`.** A new `CameraPosition`
value (nadir `WorldPoint` + `height_cm`), added to `config.interfaces.
CameraConfig` as `Optional[CameraPosition] = None`, with the same
proto + config-loader + CLI-push plumbing sprints 020-023 established
for every other camera setting (`docs/design/design.md` §7's storage
layout already names `camera_position` in its `config.json` comment —
this sprint is the first to actually implement it). `DaemonService.
_camera_geometry_for` reads it directly; absence means "not configured,"
which resolves to the exact same `(WorldPoint(0,0), 0.0)` stopgap
behaviour this sprint replaces — a pre-029 config file, and any camera
nobody has located yet, is unaffected.

**(b) `aprilcam camera locate`.** A new CLI command that takes 2+
references (AprilTag id, playfield feature slug, physical stand
height), drives detection through the daemon's existing `GetTag` RPC
(never opens a camera or touches a pixel itself — the daemon stays the
sole vision authority), solves nadir + height with a genuine
least-squares fit (a new pure function in `calibration/geometry.py`,
alongside `project_tag_to_field_plane` and friends), reports the RMS
residual and a near-collinearity warning, and — quality-gated — writes
the result into the camera's config, on disk and live.

Both halves are required; see Design Rationale for why config-fields-
alone and solver-alone were both rejected.

## Success Criteria

- [x] `DaemonService._camera_geometry_for` reads a real per-camera
  `camera_position` when one is configured, falling back to the exact
  pre-sprint stopgap when it is not. (Ticket 001; the fallback half is
  hardware-checked by ticket 004 below.)
- [x] `mount_x`/`mount_y`/`mount_yaw_rad` behaviour is unchanged — the
  existing, already-verified offset+yaw composition regresses in no
  way (the issue's own non-negotiable). **Hardware-verified (ticket
  004):** sprint 025's existing `tests/aprilcam/daemon/
  test_mobile_tags_hardware.py` re-run against camera 3, real
  hardware, now with `camera_position` configured — both tests passed
  unchanged.
- [x] A headless regression test built directly from today's measured
  fixture (tag 15 and tag 14 above) proves the new least-squares solver
  recovers nadir `(3.05, -2.75)` cm and height `128.25` cm within a
  documented tolerance, RMS residual within a documented tolerance of
  `0.16` cm — no camera needed to run it. (Ticket 002 —
  `test_solve_camera_geometry_real_fixture_regression`.)
- [x] A second headless test proves the solver does **not** reproduce the
  line-intersection failure (~20 cm off, 150x worse residual) for a
  near-collinear reference pair, and instead reports the ill-
  conditioning rather than returning a silently bad answer. (Ticket
  002 — the diametrically-opposite and near-collinear tests.)
- [x] `aprilcam camera locate <camera> --reference ... --reference ...`
  run against camera 3 / `main-playfield` with real hardware reproduces
  a nadir/height/RMS close to the documented fixture, and writes a
  `camera_position` the daemon picks up without a restart. **Real
  outcome (ticket 004, rescoped — see its completion notes for the
  full account):** this run happened for real, on camera 3 against
  `main-playfield`, before the stakeholder removed the elevated
  reference stands (tag 15 at `dot-northeast-orange`, tag 14 at
  `dot-southwest-orange`) and confirmed they are not needed again. It
  is the run that actually wrote the real
  `~/.config/aprilcam/cameras/arducam-ov9782-usb-camera/config.json`'s
  `camera_position` (`height_cm=128.22990496814816`,
  `nadir_x_cm=3.0570142285481654`, `nadir_y_cm=-2.798700632324644` —
  a near-exact match to the documented fixture), confirmed live,
  read-only, via `aprilcam camera config arducam-ov9782-usb-camera
  --dump` against the real running daemon as part of ticket 004. The
  "no restart" half is separately, freshly proven by a new hardware
  test (`tests/aprilcam/daemon/test_camera_geometry_hardware.py::
  test_mount_z_is_a_noop_until_camera_position_is_pushed_then_engages_with_no_restart`)
  that pushes `camera_position` mid-daemon-lifetime and shows the very
  next detection burst reflect it — genuinely passed once this session
  with concrete recorded numbers (see completion notes); a later
  re-run in the same session hit a live field-visibility condition
  (unrelated, concurrent activity on the same hardware — see
  completion notes) and could not complete end to end, honestly
  reported there rather than re-claimed as green. **A fresh re-run of `camera locate`
  itself, during ticket 004's own execution, was not possible and was
  not faked** — the stakeholder removed both reference stands
  beforehand; a new hardware test
  (`test_locate_reports_missing_references_or_reproduces_the_documented_fixture`)
  genuinely attempts it (`--dry-run`, read-only, against the real
  daemon) and skips cleanly on the real "not detected" response rather
  than faking a pass.
- [x] With `camera_position` configured, registering a real `mount_z` for a
  tag at a known height/offset now measurably improves the reported
  position toward hand-measured ground truth — the original issue's own
  Verification bullet, finally closeable. **Closed (ticket 004), with
  real, pre-removal hardware data:** tag 15 at `dot-northeast-orange`
  (surveyed (50, 30) cm, stand 17.2 cm) read raw at (57.38, 34.90) cm
  — 8.86 cm of real error; tag 14 at `dot-southwest-orange` (surveyed
  (-50, -30) cm, stand 15.0 cm) read raw at (-57.15, -33.40) cm — 7.92
  cm of error. Applying the solved geometry brings both to (50.093,
  29.843) cm and (-50.107, -29.820) cm — 0.182 cm and 0.209 cm
  residual, permanently pinned by
  `test_solve_camera_geometry_real_fixture_correction_matches_documented_errors`
  (headless — the fixture *is* this real hardware data). A fresh live
  re-observation of a genuinely elevated tag was not attempted this
  session, for the same reason as above; the new hardware test cited
  above demonstrates the same correction code path engaging live using
  a synthetic (not physically real) elevation on a flat ArUco corner
  marker — a mechanism check, not a re-derivation of ground truth. That
  test's own current pass/fail state at any given moment tracks live
  field conditions on hardware shared with concurrent, independent
  activity this session — see completion notes for the exact state
  observed and why it does not change this criterion's substance.
- [x] `uv run mypy` clean; full suite green headless (currently 1324
  passed, 4 skipped — this sprint only adds), green with hardware
  attached. **Actual (ticket 004):** `uv run mypy` clean, 79 source
  files. Headless: `uv run pytest -q -m "not camera and not
  integration"` -> 1335 passed. Hardware-attached, full unfiltered
  suite (`uv run pytest`): first continuous ~8.5-minute run -> 1380
  passed, 7 skipped cleanly, 4 failed — all 4 in
  `tests/aprilcam/daemon/test_detection_hardware.py` (ticket 024's own
  hardware suite; unrelated to this sprint — none of the 4 register a
  mount, so `_apply_mount_correction`'s new guard is never reached).
  Re-running that one file alone immediately after: all 12 (including
  the same 4) passed cleanly. Read as resource/timing contention from
  a long, continuous, hardware-exercising run — not a regression from
  this sprint — reported plainly per the brief's own instruction,
  rather than claimed as a false green or a false regression. See
  ticket 004's completion notes for the second full-suite run's result.

## Scope

### In Scope

- `config/interfaces.py` — new `CameraPosition` frozen dataclass;
  `CameraConfig.camera_position: Optional[CameraPosition]`.
- `config/loader.py` — parse/serialize `camera_position`, reject
  `height_cm <= 0` whole-document (mirrors the existing playfield
  `width_cm`/`height_cm` positivity check).
- `proto/aprilcam_v2.proto` — new `CameraPosition` message; `optional
  CameraPosition camera_position` field on `CameraConfig`.
- `daemon/rpc.py`, `client/models.py` — marshal the new field,
  `HasField`-guarded presence (mirrors `GetCalibrationResponse.
  calibration`'s existing discipline).
- `daemon/service.py` — `_camera_geometry_for` reads real config
  instead of the stopgap.
- `daemon/detection.py` — guard against an implausible registered
  `mount_z` (at or above a known camera height) producing a silently
  wrong extrapolated position; skip correction, don't extrapolate past
  the camera.
- `calibration/geometry.py` — new least-squares camera-geometry solver,
  pure function(s), no daemon state, no cv2 (same shape as every
  existing function in this module).
- New CLI command `aprilcam camera locate` (new `cli/locate.py`, wired
  under `cli/camera.py`'s subparser tree, mirroring `cli/calibrate.py`'s
  "own file, grouped under camera" shape).
- `docs/design/use-cases/daemon.md` — resolve UC-12's camera-geometry
  gap.
- `docs/design/use-cases/cli.md` — new UC-29 for `camera locate`.
- Real-hardware acceptance pass on camera 3 / `main-playfield`,
  reproducing today's measured fixture and closing the issue's
  Verification bullet.

### Out of Scope

- Full camera pose (tilt/orientation) — the existing deskew/homography
  model already assumes a purely overhead, undistorted view; this
  sprint's height/nadir model stays inside that assumption rather than
  reopening it (the issue's own scope note).
- Camera intrinsics / lens-distortion modelling — the ~1 cm gap between
  the solved effective height (128.25 cm) and the physical sensor
  plane (127 cm) is noted as most plausibly unmodelled distortion, not
  corrected. v2 calibration stores only the homography; this sprint
  does not add intrinsics.
- ArUco-family reference tags for `locate` — this sprint's references
  are AprilTag-family only, matching the real fixture; family
  generalization is a follow-up if ever needed, not precluded by the
  design.
- A dedicated `LocateCamera` RPC — the solve is pure algebra over
  already-vision-processed `WorldPoint`s (no cv2, no pixel access); see
  Design Rationale for why this stays a client-side pure function
  rather than new daemon/proto surface.
- Retroactively recomputing already-buffered tag history once
  `camera_position` is set — matches sprint 025's existing
  not-retroactive precedent for mount registrations; out of scope to
  revisit here.

## Test Strategy

Headless fixtures wherever a claim doesn't require hardware; hardware
verification (`@pytest.mark.camera`, skipping cleanly) only where a
claim is genuinely hardware-checkable — same split sprint 025 used.

- **Config model** — round-trip tests (`CameraConfig` with/without
  `camera_position`, `to_dict()`/`parse_camera_config` symmetry),
  `height_cm <= 0` rejected whole-document with `MalformedDocument`,
  `camera_position` absent parses as `None` (pre-029 files unaffected).
- **Proto round-trip** — `SetCameraConfig`/`GetCameraConfig` over a
  real gRPC channel carries `camera_position` correctly, and correctly
  omits it (`HasField` false) when unset.
- **`_camera_geometry_for`** — headless unit tests: configured camera
  returns its real nadir/height; unconfigured camera returns the exact
  pre-sprint stopgap `(WorldPoint(0,0), 0.0)`.
- **Mount correction regression** — `mount_x`/`mount_y`/`mount_yaw_rad`
  synthetic cases from sprint 025's existing suite pass unmodified;
  this is the issue's own "do not regress" bullet, made a literal test
  assertion, not just a review note.
- **Implausible `mount_z` guard** — synthetic: a registered `mount_z`
  at or above a configured `camera_height_cm` results in the raw
  (uncorrected) position being reported, never an extrapolated-past-
  the-camera value.
- **Least-squares solver — the real fixture** — hand-computed
  regression built directly from today's tag 15/tag 14 measurements:
  given the two (surveyed, observed, height) triples, the solver
  recovers nadir `(3.05, -2.75)` cm and height `128.25` cm within a
  documented tolerance, RMS residual near `0.16` cm. This is the
  sprint's acceptance fixture, headless, no camera needed.
- **Least-squares solver — the ill-conditioned case** — synthetic:
  two references placed diametrically opposite a known camera nadir
  (reproducing the stakeholder's own failed line-intersection
  scenario) must **not** reproduce that ~20 cm-off, 150x-worse-residual
  failure; the solver's least-squares fit recovers a materially closer
  answer and/or flags the near-collinearity.
- **Solver input validation** — fewer than 2 references raises
  `ValueError` before any fitting is attempted.
- **CLI `camera locate`** — argparse wiring tests (mocked `Discovery`/
  `Daemon`), mirroring `tests/aprilcam/cli/`'s existing pattern:
  reference parsing, feature-slug resolution against a fixture
  playfield, refusal when a reference tag is currently registered as a
  mobile tag with nonzero `mount_z` (contamination guard), refusal to
  auto-apply a poorly-conditioned/high-residual solve without
  `--force`, `--dry-run` prints without writing.
- **Hardware verification** (`@pytest.mark.camera`) — live `camera
  locate` run against camera 3 / `main-playfield` reproduces a
  nadir/height/RMS close to the documented fixture; registering a real
  `mount_z` afterward measurably improves a live elevated tag's
  reported position toward hand-measured ground truth (closing the
  issue's own Verification bullet).
- **Typing** — `uv run mypy` clean across every new/changed module.

## Architecture

**Substantial** — touches 3+ existing modules (`config/interfaces.py`,
`config/loader.py`, `proto/aprilcam_v2.proto`, `daemon/rpc.py`,
`client/models.py`, `daemon/service.py`, `daemon/detection.py`,
`calibration/geometry.py`) and introduces a genuinely new
cross-module dependency (`cli/` importing `calibration/geometry.py`
directly for the first time — the CLI has never depended on a
`calibration` module before, since calibration itself has always been
vision-in-the-daemon-only) plus a data-model change (`CameraConfig`
gains a new optional field). Full 7-step methodology below, with a
component diagram — the same "several independently-composed pieces
land in one place" case sprint 025's diagram earned its keep for.

### Architecture Overview

**Step 1 — the problem.** `project_tag_to_field_plane` has always
needed a camera's ground position and height; nothing in the codebase
has ever supplied it. This sprint closes that gap on both ends: an
operator-set config value the daemon reads, and a tool that solves it
correctly rather than asking for a tape measure.

**Step 2 — responsibilities.**
- *Model and carry a camera's physical position as configuration* —
  changes only when the config schema/wire shape changes; independent
  of how the value gets solved.
- *Solve nadir + height from surveyed references* — pure geometry,
  changes only when the math or its validation changes; independent of
  how the result is stored.
- *Drive detection for those references and orchestrate the solve+write*
  — the CLI-level workflow that ties the two together; changes when the
  operator-facing contract (reference format, apply/dry-run/force
  semantics) changes.
- *Guard against implausible corrections at detection time* — a small,
  separable responsibility (a tag at/above a known camera height),
  changes independently of the other three.

**Step 3 — modules.**

| Module | Purpose (one sentence) | Boundary | Serves |
|---|---|---|---|
| `config/interfaces.py` (extended) | Declare a camera's physical position as a value type. | New `CameraPosition` dataclass + `CameraConfig.camera_position` field; no behaviour. | SUC-002 |
| `config/loader.py` (extended) | Parse/serialize `camera_position`, reject implausible height whole-document. | Touches only the JSON<->dataclass boundary; no RPC, no daemon state. | SUC-002 |
| `proto/aprilcam_v2.proto`, `daemon/rpc.py`, `client/models.py` (extended) | Carry `camera_position` over the wire. | Wire shapes and marshalling only; no business logic (matches every prior extension of this file). | SUC-002 |
| `daemon/service.py` (extended) | Answer "where is this camera, how high" from real config instead of a stopgap. | `_camera_geometry_for` reads `self._camera_configs`; no new state. | SUC-001 |
| `daemon/detection.py` (extended) | Refuse to extrapolate a mount correction past a known camera's own height. | One guard inside the existing `_apply_mount_correction` path; no new injectable. | SUC-001 |
| `calibration/geometry.py` (extended) | Solve a camera's nadir and height from surveyed reference observations. | Pure function(s), no daemon state, no cv2 — same shape as `project_tag_to_field_plane`/`pixel_to_world`. | SUC-003 |
| `cli/locate.py` (new) | Orchestrate reference resolution, detection (via daemon RPC), the solve, and the config write. | No vision of its own; calls the daemon for detection and `calibration/geometry.py` for the solve — the one CLI module with a `calibration` dependency. | SUC-003 |

**Step 4 — diagram.** Required: this sprint composes a new config
field, new wire plumbing, a new pure-math solver, and a new CLI
workflow that is the first CLI module ever to depend on `calibration/`
directly — exactly the shape a reader benefits from seeing, not
inferring.

```mermaid
graph TD
    CLI["cli/locate.py (new)<br/>camera locate"]
    Client["Client library<br/>client/models.py, discovery.py"]
    Wire["Wire protocol<br/>aprilcam_v2.proto, daemon/rpc.py"]
    Config["Config model<br/>config/interfaces.py, config/loader.py"]
    DaemonSvc["Daemon service<br/>service.py"]
    Detection["Detection engine<br/>detection.py"]
    Geometry["Geometry math<br/>calibration/geometry.py"]

    CLI -->|GetPlayfield, GetTag,<br/>ListTagParameters, SetCameraConfig| Client
    CLI -->|solve_camera_geometry (new, pure)| Geometry
    CLI -->|writes config.json directly| Config
    Client -->|gRPC| Wire
    Wire -->|dispatch| DaemonSvc
    DaemonSvc -->|reads CameraConfig.camera_position| Config
    DaemonSvc -->|_camera_geometry_for feeds| Detection
    Detection -->|project_tag_to_field_plane| Geometry
```

No cycles: `Geometry` and `Config` are leaves with no outgoing edges.
The two independent paths into `Geometry` (from `CLI` directly, and
from `DaemonSvc` via `Detection`) are the diagram's point — the solver
and the corrector are the same pure module, never duplicated, exactly
as `pixel_to_world`/`world_to_pixel` already share one implementation
across every caller.

**Step 5 — What Changed / Why / Impact / Migration Concerns.**

*What Changed.*
- `config/interfaces.py`: new `CameraPosition` (`nadir: WorldPoint`,
  `height_cm: float`), frozen `@dataclass(kw_only=True)`. `CameraConfig`
  gains `camera_position: Optional[CameraPosition] = None`. Lives as a
  dedicated field, not inside `settings: dict[str, Any]` — see Design
  Rationale for why.
- `config/loader.py`: `parse_camera_config`/`to_dict` gain a
  `camera_position` block — on-disk shape `{"nadir_x_cm": ..., "nadir_y_cm":
  ..., "height_cm": ...}` (a fresh v2 shape, diverging from v1's
  `{height, x_offset, y_offset}` — see Design Rationale). Absent key ->
  `None` (pre-029 files parse unchanged). `height_cm <= 0` -> whole-document
  `MalformedDocument`, mirroring `_require_geometry`'s existing
  positivity check for playfield dimensions.
- `proto/aprilcam_v2.proto`: new `message CameraPosition { WorldPoint
  nadir = 1; double height_cm = 2; }`; `CameraConfig` gains `optional
  CameraPosition camera_position = 4;` (field 4 — `name`/`playfield`/
  `settings_json` already occupy 1-3). `HasField("camera_position") ==
  false` means "not configured," mirroring `GetCalibrationResponse.
  calibration`'s existing presence discipline.
- `daemon/service.py`: `_camera_geometry_for` reads
  `self._camera_configs.get(slug)`; when present and
  `camera_position is not None`, returns its `(nadir, height_cm)`;
  otherwise returns the exact pre-sprint stopgap `(WorldPoint(0,0),
  0.0)` — an unconfigured camera behaves identically to every camera
  before this sprint, by construction.
- `daemon/detection.py`: `_apply_mount_correction` gains one guard —
  when the looked-up `camera_height_cm > 0` (i.e., a real value is
  configured) and `mount.mount_z >= camera_height_cm`, skip the
  correction (report the raw, uncorrected position) rather than
  calling `project_tag_to_field_plane` with a ratio >= 1, which would
  extrapolate *past* the camera and produce a plausible-looking but
  physically nonsensical position — exactly the "silently wrong"
  failure mode this codebase treats as the worst outcome. See Design
  Rationale for why this check lives here (per-burst, per-observing-
  camera) and not at `register_tag` time.
- `calibration/geometry.py`: a new pure least-squares solver —
  `ReferenceObservation` (surveyed `WorldPoint`, observed `WorldPoint`,
  `height_cm`) and `solve_camera_geometry(references) ->
  CameraGeometrySolution` (`nadir: WorldPoint`, `height_cm: float`,
  `rms_residual_cm: float`, plus a conditioning signal — see ticket 002
  for the exact linear reformulation, chosen deliberately over the
  line-intersection approach that failed in practice). `ValueError` for
  fewer than 2 references. No cv2, no daemon state, no I/O — same shape
  as every existing function in this module.
- `cli/locate.py` (new): `aprilcam camera locate <camera> --reference
  TAG:SLUG:HEIGHT_CM [--reference ...] [-d HOST] [--dry-run] [--force]`.
  Resolves each `SLUG` against the camera's linked playfield (`GetPlayfield`,
  exact-slug lookup into `features()` — not the fuzzy `FindFeature`
  matcher, since an operator's exact-slug typo should be a hard error,
  not a near-miss); refuses if any reference tag is currently a
  registered mobile tag with nonzero `mount_z` (`ListTagParameters`) —
  that registration's own (possibly stale) correction would contaminate
  the raw reading the solver needs; drives detection per reference via
  `GetTag`; calls `solve_camera_geometry`; prints the result, RMS
  residual, and a near-collinearity warning when the fit is
  ill-conditioned; writes the resulting `camera_position` into the
  on-disk `config_dir/cameras/<slug>/config.json` and pushes it live
  via `SetCameraConfig` — unless the fit is flagged ill-conditioned or
  high-residual, in which case it refuses to write without `--force`
  (quality gate; see Design Rationale). `--dry-run` reports without
  writing regardless of quality.
- `docs/design/use-cases/daemon.md`: UC-12's camera-geometry gap
  (previously an explicit, named "Open" caveat) is resolved — camera
  geometry now sourced from `CameraConfig.camera_position`; the
  mount-at/above-camera-height failure mode is now a real, tested
  guard rather than an aspirational note.
- `docs/design/use-cases/cli.md`: new UC-29 — "Determine a camera's
  physical position and height" (`camera locate`), following UC-15's
  existing shape for a CLI-level use case built on a daemon RPC
  surface.

*Why.* `project_tag_to_field_plane`'s parallax half has been fully
wired and unit-tested since sprint 025 with nothing real to feed it.
The measured 8-9 cm real-hardware error this sprint's Problem section
documents is the concrete cost of that gap; both the config plumbing
and the solver are needed to close it (see Design Rationale for why
neither alone suffices).

*Impact on Existing Components.* Additive throughout. Every existing
`CameraConfig`/proto/config-file consumer that never sets
`camera_position` sees identical behaviour to before this sprint — the
`None`/absent/`(0,0),0.0` chain is unbroken at every layer. `mount_x`/
`mount_y`/`mount_yaw_rad` composition (`apply_mount_offset`) is
untouched; only `project_tag_to_field_plane`'s two inputs go from
always-stopgap to sometimes-real. `daemon/interfaces.py`'s `Daemon`
base class needs no new abstract methods — `locate` is built entirely
from RPCs the daemon already exposes (`GetPlayfield`, `GetTag`,
`ListTagParameters`, `GetCameraConfig`, `SetCameraConfig`).

*Migration Concerns.* None for existing data: a pre-029 `config.json`
with no `camera_position` key parses exactly as before (the field is
`Optional`, defaulting to `None`). No data migration, no schema
version bump. Operators who want real parallax correction must
explicitly run `camera locate` (or hand-author the new JSON block) —
this sprint changes no camera's behaviour until an operator opts in.

### Design Rationale

**Decision: build both the `camera_position` config fields and the
`locate` solver command — not one alone.**
- *Context*: the stakeholder considered and rejected two smaller
  alternatives.
- *Alternatives considered*: (a) config-fields-only — an operator
  hand-authors `camera_position` in JSON; (b) solver-only, writing
  directly into a per-calibration `calibration.json` rather than the
  config document.
- *Why this choice*: (a) alone means re-deriving nadir/height after
  every camera bump becomes manual, error-prone work, and — this
  sprint's own measured evidence — the useful height (128.25 cm) is not
  tape-measurable at all; nothing to hand-author it *from* exists
  without a solve. (b) alone would force reference tags to be
  physically staged on the field at every recalibration, coupling two
  operations (homography solving, which is frequent and routine, and
  camera-geometry solving, which is rare — only needed after a physical
  camera move) that have no reason to be coupled.
- *Consequences*: two pieces of work, sequenced so the config plumbing
  (ticket 001) exists before `locate` (ticket 003) needs somewhere to
  write its answer.

**Decision: `camera_position` is a dedicated `CameraConfig` field, not
a key inside `settings: dict[str, Any]`.**
- *Context*: `settings` already carries every other camera setting
  (exposure, gain, resolution, UVC controls) sprints 020-023 built
  plumbing for.
- *Alternatives considered*: add `camera_position` as three more keys
  inside `settings`, reusing the existing dict-shaped plumbing
  directly.
- *Why this choice*: `settings`' contract is specifically "hardware
  controls, pushed to `Camera.apply_config` and validated against the
  device" (`config/interfaces.py`'s own docstring: "a fetched config
  reports every settable key, blank when unset"). `camera_position` is
  pure metadata an operator asserts — it is never pushed to hardware,
  never validated against a device's control surface, and folding it
  into `settings` would mean either `apply_config` silently ignoring an
  unrecognized-but-present key (undermining `UnsupportedSetting`'s
  whole-push-fails contract) or special-casing it out of the loop
  hardware settings already share. A dedicated field keeps both
  contracts honest. This also matches `docs/design/design.md` §7's
  storage-layout comment, which already lists `camera_position`
  separately from "gains/shutter/brightness, UVC controls, resolution."
- *Consequences*: `Camera.apply_config` needs no change at all — it
  iterates `config.settings` only, and `camera_position` is a sibling
  field it never sees.

**Decision: the on-disk `camera_position` shape diverges from v1's
`{height, x_offset, y_offset}`.**
- *Context*: v1 stored `camera_position: {height, x_offset, y_offset}`
  under an assumed-field-centred convention (`x_offset: 0, y_offset: 0`
  in its own shipped config).
- *Alternatives considered*: carry v1's field names forward unchanged
  for continuity.
- *Why this choice*: (1) "offset" implies offset-from-something, and
  v1's implicit answer (field centre) is exactly the assumption this
  sprint's own measurement disproves (the real nadir is ~4 cm
  off-centre) — keeping the name would keep smuggling in the wrong
  mental model. "nadir" names the actual geometric point (directly
  below the optical centre) with no implied reference. (2) v2's config
  fields consistently carry explicit unit suffixes (`width_cm`,
  `height_cm` on playfields); `x_offset`/`y_offset`/`height` do not.
  `nadir_x_cm`/`nadir_y_cm`/`height_cm` matches the established v2
  convention exactly.
- *Consequences*: no automatic migration from a v1-shaped file — none
  is needed, since v1 config files are not consumed by v2's loader at
  all (different repo, different schema already, per the archive's own
  "mine for ideas, don't copy" framing).

**Decision: the least-squares solve is a client-side pure function, not
a new `LocateCamera` daemon RPC.**
- *Context*: `docs/design/design.md`'s Architectural Principle 1
  restricts OpenCV/vision work — capture, detection, tracking,
  homography, deskew, calibration — to the daemon; `calibration.
  calibrate`'s existing shape (an RPC that returns a `CalibrationResult`)
  is the closest precedent for "the daemon solves geometry."
- *Alternatives considered*: (a) a new `LocateCamera` RPC that takes
  the reference list and runs the entire solve server-side, mirroring
  `Calibrate`; (b) a client-side pure function that consumes RPC
  results (`GetTag`) but computes locally.
- *Why this choice*: the least-squares fit operates entirely on
  already-vision-processed `WorldPoint`s returned by `GetTag` — it
  touches no pixel, no cv2, no camera, and needs no daemon state.
  Principle 1's "daemon is the sole vision authority" is about vision
  (pixels in, geometry out); this solve's inputs are already geometry.
  `calibration/geometry.py` already establishes the precedent that pure
  coordinate math (`pixel_to_world`, `project_tag_to_field_plane`) is a
  module, not an RPC, importable wherever it is needed. (a) would add
  proto/RPC surface for computation that gains nothing from running
  inside the daemon process. The literal constraint this sprint must
  honour — "the CLI must not do pixel work" — is satisfied because
  *detection* (the only pixel-touching step) still crosses into the
  daemon via `GetTag`; only the arithmetic on the results it returns
  runs client-side.
- *Consequences*: `cli/` gains its first-ever dependency on
  `calibration/` — a new cross-module edge, called out explicitly in
  Step 4's diagram rather than left implicit. `calibration/geometry.py`
  has no `cv2` import today and this sprint keeps it that way, so this
  new dependency does not put `cli/` at risk of importing OpenCV
  transitively (verified: the module's only imports are `math`,
  `numpy`, and `aprilcam.types`).

**Decision: the solver uses a closed-form linear least-squares fit, not
iterative nonlinear optimization.**
- *Context*: the natural residual equation (`apparent = nadir +
  (height/(height - stand_height)) * (surveyed - nadir)`) is bilinear
  in the unknowns `(nadir, height)`, which is why the stakeholder's own
  first attempt (intersecting displacement lines — a special case that
  discards the equations' magnitude information, using only direction)
  failed so badly.
- *Alternatives considered*: (a) iterative nonlinear least squares
  (e.g., Gauss-Newton) directly on the bilinear residual; (b) a
  substitution that linearizes the problem exactly.
- *Why this choice*: substituting `A = nadir / height` (a 2-vector) and
  `b = 1 / height` (a scalar) turns each reference's two equations
  (x and y) into genuinely **linear** equations in the three unknowns
  `(Ax, Ay, b)` — solvable in one `numpy.linalg.lstsq` call, no
  iteration, no initial guess, no convergence risk. `nadir = A / b`,
  `height = 1 / b` recovers the physical answer. This matches
  `calibration/geometry.py`'s established "pure NumPy linear algebra,
  no iterative solving anywhere in this module" style (its own module
  docstring), and avoids adding a new dependency (`scipy`) for a
  three-unknown fit that does not need one.
- *Consequences*: ticket 002 must derive and document this substitution
  explicitly (mirroring how `apply_mount_offset`'s docstring proves its
  own sign convention with a worked example) — it is exactly the kind
  of non-obvious, easy-to-get-wrong step the stakeholder already lost
  time to once.

**Decision: `locate` defaults to applying its result, but refuses
without `--force` when the fit is flagged as poor.**
- *Context*: the stakeholder's own plain framing ("solves ... then
  writes the result into the camera's config") implies apply-by-default,
  matching `calibrate`'s existing "store on success" precedent. But
  `locate`'s quality signal (RMS residual, conditioning) is advisory,
  not the crisp all-or-nothing gate `Calibrate`'s `missing_ids` gives —
  and the stakeholder already hit a case (~20 cm off, undetected by eye
  until compared against ground truth) where a bad solve looked
  plausible enough to almost trust.
- *Alternatives considered*: (a) always apply, no gate; (b) never apply
  automatically, require a separate confirm step every time; (c)
  apply by default, but refuse above a documented residual/conditioning
  threshold unless `--force` is passed.
- *Why this choice*: (c) matches the literal ask for the common case
  (good references, good fit, one command) while making the one
  demonstrated failure mode loud instead of silent — consistent with
  this codebase's general refusal to report a "plausible but wrong"
  answer as though it were trustworthy (the same instinct behind
  `NotCalibrated`, `UnsupportedSetting`'s whole-push-fails rule, and
  UC-04's all-or-nothing calibration acceptance).
- *Consequences*: ticket 003 must pick and document explicit default
  thresholds (RMS residual, a conditioning signal from the solver),
  clearly marked provisional/tunable — the same "evidence-based,
  revisit if real data disagrees" posture this codebase already applies
  to `CONFIRMATION_STREAK` (`daemon/detection.py`). Ticket 004's
  real-hardware pass is the first real data point to validate them
  against.

**Decision: the `mount_z >= camera_height_cm` implausibility guard
lives in `_apply_mount_correction` (per burst, per observing camera),
not in `register_tag` (registration time).**
- *Context*: `project_tag_to_field_plane`'s own docstring explicitly
  defers this check to "a registration-time / service-layer concern,"
  without saying which service-layer moment.
- *Alternatives considered*: (a) reject at `register_tag` time if
  `mount_z` exceeds any known camera's height; (b) check per-burst,
  per-camera, at correction time.
- *Why this choice*: a registered tag is not tied to one camera — it
  may be seen by whichever camera happens to be watching (this is
  already true of the existing `register_tag`/`mount_z`-without-any-
  calibration check). At registration time there is no single "the
  camera" to validate `mount_z` against; the question "is this height
  plausible" only has a concrete camera to compare against once a
  specific burst's `_camera_geometry_for(slug)` result is in hand — the
  same moment `correct_tag_pose` is already called. Checking there
  reuses data already fetched, adds no new lookup, and correctly
  varies per camera if a tag is seen by more than one.
- *Consequences*: an implausible `mount_z` is never rejected at
  `register_tag` time (registration still always succeeds, matching
  UC-12's existing "accepted even for a tag never yet seen" rule) — it
  is silently downgraded to "uncorrected" per burst, per camera, exactly
  like the existing "no registration" no-op path. This is a deliberate
  choice to never crash or reject mid-detection-burst.

### Migration Concerns

None. See Step 5 above — every change is additive, `Optional`-typed, or
`None`-defaulted; no on-disk schema version bump; no existing config
file's parsed meaning changes. Operators opt in per camera by running
`camera locate` (or hand-authoring the new JSON block).

### Open Questions / Follow-ups

- Exact default RMS-residual and conditioning thresholds for `locate`'s
  apply-vs-refuse gate are provisional at ticket-writing time — ticket
  003 picks explicit values with a documented rationale; ticket 004's
  real-hardware pass is the first evidence point to sanity-check them
  against, in the same spirit `CONFIRMATION_STREAK` was tuned from real
  detection runs.
- ArUco-family reference tags for `locate` (this sprint supports
  AprilTag family only, matching the real fixture) — a follow-up if a
  future stakeholder need arises, not a gap in the current design.
- Camera intrinsics / lens-distortion modelling, which the ~1 cm
  effective-height-vs-sensor-plane gap hints at — explicitly out of
  scope; recorded as a possible future issue, not a defect of this
  sprint's solve.

## Use Cases

### SUC-001: Parallax correction actually corrects, on real hardware
Parent: UC-12

- **Actor**: A-01 (robot program), also served by A-03
- **Preconditions**: Camera calibrated; `camera_position` configured
  for it (via SUC-003's `locate` command or a hand-authored config).
- **Main Flow**:
  1. A tag is mounted at a known height above its robot, with a
     registered nonzero `mount_z`.
  2. The daemon's `_camera_geometry_for` now returns the camera's real
     nadir and height instead of the stopgap.
  3. `project_tag_to_field_plane` performs a real parallax projection
     using those values, before `apply_mount_offset`'s existing
     X/Y/yaw composition runs.
  4. The reported position is measurably closer to hand-measured
     ground truth than the uncorrected (or stopgap-corrected) position.
- **Postconditions**: None beyond the corrected reading — no new
  stored state.
- **Acceptance Criteria**:
  - [x] With `camera_position` configured, a registered `mount_z` for a
        tag at a known height/offset produces a corrected world
        position matching hand-measured ground truth, closing the
        original issue's Verification bullet (hardware-verified).
        **Real, pre-removal hardware data (ticket 004): 8.86 cm / 7.92
        cm raw error corrected to 0.182 cm / 0.209 cm — see Success
        Criteria above and ticket 004's completion notes for the full
        account and why a fresh live re-observation was not attempted
        this session.**
  - [x] `mount_x`/`mount_y`/`mount_yaw_rad` behaviour is bit-for-bit
        unchanged versus sprint 025's existing verified behaviour
        (regression, not just "still passes some test"). **Hardware-
        verified (ticket 004): sprint 025's own hardware test re-run
        against camera 3 with `camera_position` now configured — both
        tests passed unchanged.**
  - [x] A registered `mount_z` at or above a configured camera's
        height produces the raw (uncorrected) position, never an
        extrapolated-past-the-camera value (synthetic). (Ticket 001,
        headless — the guard in `_apply_mount_correction`.)
  - [x] An unconfigured camera (`camera_position` absent) behaves
        identically to every pre-029 camera — the stopgap path,
        unchanged. **Hardware-verified (ticket 004), once, genuinely:**
        a fresh hardware test registers a synthetic `mount_z` against an
        isolated, calibrated-but-unconfigured instance of camera 3 and
        confirms the reported position is unchanged. See ticket 004's
        completion notes for that test's later re-run, which hit a live
        field-visibility condition unrelated to this criterion.

### SUC-002: A camera's physical position round-trips like every other setting
Parent: UC-03

- **Actor**: A-04 (operator), also served by A-01
- **Preconditions**: A camera config document exists or is being
  authored.
- **Main Flow**:
  1. Operator (or `locate`, SUC-003) sets `camera_position` (nadir +
     height) in a camera config document, on disk or via a live push.
  2. The daemon accepts and stores it whole, exactly like `playfield`/
     `settings` today (`SetCameraConfig`).
  3. `GetCameraConfig`/`camera config --dump` report it back verbatim.
  4. A config document with `height_cm <= 0` is rejected whole,
     client-side, before any RPC (mirrors UC-23.S-4's existing
     self-naming/malformed-document rejection).
- **Postconditions**: `camera_position` held by the daemon, applied on
  every subsequent detection burst for that camera; persisted on disk
  if pushed from a file, exactly like every other config field.
- **Acceptance Criteria**:
  - [x] `CameraConfig.camera_position` round-trips through
        `to_dict()`/`parse_camera_config` (`load(x.to_dict()) == x`).
        (Ticket 001, headless.)
  - [x] `camera_position` round-trips through `SetCameraConfig`/
        `GetCameraConfig` over a real gRPC channel; absent on the wire
        (`HasField` false) when unset. (Ticket 001, headless; also
        hardware-confirmed live by ticket 004 — `camera config
        arducam-ov9782-usb-camera --dump` against the real daemon
        reports the real, pushed `camera_position` verbatim.)
  - [x] A document with `height_cm <= 0` is rejected with
        `MalformedDocument`, naming the problem, before any RPC.
        (Ticket 001, headless.)
  - [x] A pre-029 config file with no `camera_position` key parses
        with `camera_position is None` — no behaviour change. (Ticket
        001, headless.)

### SUC-003: Solve, don't tape-measure, a camera's position
Parent: UC-29

- **Actor**: A-04 (operator)
- **Preconditions**: Camera calibrated and linked to a playfield; at
  least 2 reference tags physically placed at known playfield feature
  positions, at known (measured) stand heights; none of the reference
  tags currently registered as a mobile tag with nonzero `mount_z`.
- **Main Flow**:
  1. Operator runs `aprilcam camera locate <camera> --reference
     TAG:SLUG:HEIGHT_CM [--reference ...]`.
  2. The command resolves each `SLUG` to a surveyed world position via
     the camera's linked playfield (`GetPlayfield`), and drives one
     `GetTag` per reference through the daemon — the only pixel-
     touching step, and it happens entirely inside the daemon.
  3. The collected (surveyed, observed, height) triples are fit with a
     least-squares solve (`calibration.geometry.solve_camera_geometry`)
     for nadir and height.
  4. The command reports the solved nadir, height, and RMS residual,
     and warns if the supplied references are too near-collinear with
     the estimated nadir to trust.
  5. If the fit is good (or `--force` is passed), the result is written
     into the camera's config, on disk and pushed live via
     `SetCameraConfig` — the daemon picks it up with no restart.
     `--dry-run` reports without writing, regardless of fit quality.
- **Postconditions**: `camera_position` set for the camera (unless
  `--dry-run`), immediately in effect for subsequent detection bursts.
- **Acceptance Criteria**:
  - [x] UC-29.S-1 (the real fixture): given today's tag 15/tag 14
        measurements, `solve_camera_geometry` recovers nadir
        `(3.05, -2.75)` cm and height `128.25` cm within a documented
        tolerance, RMS residual near `0.16` cm (headless). (Ticket
        002/003.)
  - [x] UC-29.S-2 (ill-conditioned references): two references placed
        diametrically opposite a known nadir do **not** reproduce the
        ~20 cm-off line-intersection failure; the solver either
        recovers a materially closer answer, flags the near-
        collinearity, or both (headless, synthetic). (Ticket 002.)
  - [x] UC-29.S-3 (fewer than 2 references): refused with a clear
        error before any RPC is made. (Ticket 003, headless.)
  - [x] UC-29.S-4 (contaminated reference): a reference tag currently
        registered as a mobile tag with nonzero `mount_z` is refused
        with an explicit error, before any detection is attempted.
        (Ticket 003, headless.)
  - [x] UC-29.S-5 (poor fit, no `--force`): a high-residual or
        near-collinear solve is reported but not written; re-running
        with `--force` writes it; `--dry-run` never writes regardless.
        (Ticket 003, headless.)
  - [x] Hardware-verified: a live run against camera 3 / `main-playfield`
        reproduces a nadir/height/RMS close to the documented fixture,
        and the daemon observably uses the written `camera_position` on
        its very next detection burst (no restart). **Real outcome
        (ticket 004):** the reproduction and the config write are real
        — that live run happened before the reference stands were
        removed, and its result is what camera 3's real config carries
        today (confirmed live, read-only, via `--dump`). The "no
        restart" half is additionally proven fresh by a new isolated
        hardware test. A *fresh re-run* of `camera locate` during
        ticket 004's own execution was not possible (stands removed)
        and was not faked — a new hardware test genuinely attempts it
        and skips cleanly on the real "not detected" response. See
        Success Criteria above and ticket 004's completion notes for
        the full account.

## GitHub Issues

(None — this sprint's work is tracked via `clasi/issues/`, not GitHub
issues.)

## Definition of Ready

Before tickets can be created, all of the following must be true:

- [x] Sprint planning document is complete (sprint.md, including its
      Architecture and Use Cases sections)
- [x] Architecture review passed (or skipped, for changes with no
      architectural impact)
- [x] Stakeholder has approved the sprint plan

## Tickets

| # | Title | Depends On |
|---|-------|------------|
| 001 | `camera_position` config model, wiring, and mount-height guard | - |
| 002 | Least-squares camera-geometry solver (`calibration/geometry.py`) | - |
| 003 | `aprilcam camera locate` command | 001, 002 |
| 004 | Real-hardware acceptance pass and documentation | 003 |

Tickets execute serially in the order listed (`worktree: false`).
Tickets 001 and 002 are independent of each other (config plumbing vs.
pure math) but both must land before 003, which needs a place to write
its answer (001) and something to compute it with (002). Ticket 004
closes the loop against real hardware and the original issue's own
Verification bullet, and updates the use-case documents. The single
linked issue (`camera-geometry-for-real-parallax-correction.md`) is
archived only once all four reach `done`.
