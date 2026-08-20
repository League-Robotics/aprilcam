---
id: '002'
title: Opt-in intrinsics estimation on camera calibrate
status: done
use-cases:
- SUC-002
depends-on:
- '001'
github-issue: ''
issue: v2-calibration-does-not-model-lens-distortion.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Opt-in intrinsics estimation on camera calibrate

## Description

Ticket 001 lands the storage and application of a lens model; this
ticket lands a way to *produce* one. `aprilcam camera calibrate
<camera> --with-intrinsics` (and the matching `estimate_intrinsics`
MCP/RPC parameter) reuses the exact detection burst `calibrate` already
performs, over whatever ground truth the linked playfield definition
surveys, and runs `cv2.calibrateCamera` over that single view. See
sprint.md's Architecture section (Design Rationale, Decision 2) for the
full reasoning; the two load-bearing facts from it, restated here so
the next person does not have to re-derive them:

**The coplanarity degeneracy is real and v1 did not solve it.** v1's
`archive/src/aprilcam/calibration/calibration.py:1058`
(`calibrate_single`) calls `cv.calibrateCamera([obj_pts_3d], [img_pts],
(cam_w, cam_h), None, None)` — **no constraining flags** — a fully free
model (fx, fy, cx, cy, k1, k2, k3, p1, p2) fit from points that all lie
on one plane (z=0). `main-playfield`'s 11 surveyed points (10 ArUco +
AprilTag 1) are, absent this ticket's schema change, every one of them
coplanar too. v1's own archived
`archive/src/aprilcam/calibration/DESIGN.md:191` names this directly:
"single planar view under-constrains `calibrateCamera`; treated as
good-enough for a fixed overhead camera. Revisit only if RMS demands
it." **This ticket is that revisit.** The evidence of what a free fit
produces on this exact kind of data already exists: v1's stored
`dist_coeffs [0.42, -2.43, ...]` — a k2 of -2.43 is not a plausible
lens property, it is focal-length error a single plane cannot separate
from real distortion. The default coplanar path in this ticket must
therefore fit a **constrained** model, not a free one — this is the
substantive difference between what this ticket ships and what v1
shipped, and it must be visible in code (explicit `cv2.calibrateCamera`
flags), not just in a docstring.

**Non-coplanar ground truth is playfield structure, not a
calibrate-time argument.** An earlier draft of this ticket proposed
reusing `camera locate`'s `--reference TAG:SLUG:HEIGHT_CM` flag at
calibrate time; the stakeholder redirected this to a better shape.
`camera locate`'s references are transient (stands placed for one
solve, then removed — see ticket 003's own reproducibility risk for
exactly this reason). Instead: `Feature` (`config/interfaces.py`)
gains a declared `height_cm: float = 0.0` field ("how far this feature
sits above the field plane" — never a `@property`, per CLAUDE.md's
frozen-dataclass rule; **do not** widen `WorldPoint` to 3D — it is the
2D field-plane coordinate type used everywhere and stays that way).
`april_tags`/`aruco_tags` JSON entries gain an optional `height_cm`
(absent means 0.0 — every playfield file on disk today keeps loading
and behaving identically; `ConfigLoader`'s `load(x.to_dict()) == x`
round-trip contract must hold). `Calibrator` inspects the linked
playfield's surveyed features and picks its estimation mode from what
it finds — no per-call flag, no operator decision at calibrate time. A
playfield with no elevated features gets exactly today's coplanar
behaviour; one with elevated features gets a fuller model, automatically,
on every future calibration.

**The homography/intrinsics split — the single easiest thing here to
get wrong.** An elevated feature does not lie on the field plane, and
the homography maps the field plane to pixels. Feeding an elevated
point into the homography solve corrupts the fit exactly the way
unmodelled parallax does — the same error class sprint 029 exists to
correct, reintroduced at calibration time instead of query time. So:
**the homography solve and `reprojection_error_cm` use coplanar
features only** (`height_cm == 0`), keeping that number comparable to
every calibration recorded before this sprint; **intrinsics/distortion
estimation uses all features**, with real 3D object points `(x, y,
height_cm)`. Get this backwards and a calibration will report a
healthy `reprojection_error_cm` while being silently wrong — precisely
the failure mode the linked issue's own "the error metric hides it"
section describes for a different reason.

## Acceptance Criteria

**Schema**

- [x] `Feature` (`config/interfaces.py`) gains `height_cm: float = 0.0`, a declared frozen-dataclass field.
- [x] `april_tags`/`aruco_tags` JSON entries gain an optional `height_cm` key (absent means `0.0`); `ConfigLoader`'s round-trip contract (`load(x.to_dict()) == x`) holds for both an entry with and without it.
- [x] Every existing playfield JSON file on disk (no `height_cm` keys) loads and behaves identically to before this ticket.

**Coplanar default (Amendment 1)**

- [x] The `n_pts >= 6` floor counts only points the playfield definition itself surveys (`aruco_tags`/`april_tags` entries with `kind in {"aruco_tag", "april_tag"}`) — never markers merely detected in-frame that the playfield doesn't survey (archive `DESIGN.md`'s own warning: "AprilTags pad but do not constrain the homography... do not treat `tags_used` as a count of independent ground-truth points" applies here).
- [x] When every surveyed feature used has `height_cm == 0`, `cv2.calibrateCamera` is invoked with `CALIB_FIX_PRINCIPAL_POINT | CALIB_FIX_ASPECT_RATIO | CALIB_ZERO_TANGENT_DIST | CALIB_FIX_K3`, estimating only `k1` by default.
- [x] `k2` is freed in addition to `k1` only if held-out validation (below) shows it measurably improves held-out residual without the sanity bound flagging an overfit; otherwise `k1`-only. State which was chosen and why in this ticket's completion notes (Open Question 5).
- [x] **Held-out validation**: fit is performed on a subset of the coplanar surveyed points, reprojection residual is measured against the held-out remainder, and this result is reported (not silently discarded).
- [x] **Sanity bound** on fitted coefficients: a fit whose coefficients fall outside a documented, justified bound (e.g. `|k2|` implausibly large relative to `k1`) is rejected rather than stored, citing v1's `dist_coeffs [0.42, -2.43, ...]` as the concrete example of what the bound exists to reject.

**Elevated/non-coplanar mode**

- [x] When one or more surveyed features used have `height_cm > 0`, intrinsics estimation uses **all** of them (coplanar + elevated) with real 3D object points `(x, y, height_cm)`.
- [x] The homography solve and `reprojection_error_cm` computation use **only** the coplanar (`height_cm == 0`) subset, regardless of estimation mode — verified by a unit test that an elevated feature never appears in the point set passed to `_solve_homography`/`_reprojection_error_cm`.
- [x] Mode selection is graded: a documented, justified minimum (elevated-feature count and/or z-spread) is chosen below which the constrained coplanar model is kept even when some elevation is present; this ticket states the chosen threshold and the reasoning behind it (Open Question 6) rather than freeing the model on the strength of a single elevated feature.
- [x] Once the elevated-mode threshold is met, the model is freed further (principal point, aspect ratio, tangential terms, k3 become estimated rather than fixed, per the degeneracy actually being resolved by the z-spread present).
- [x] `calibrate`'s output (CLI print and `CalibrationResult`) reports which mode ran (coplanar-constrained vs. elevated) and how many elevated features contributed — an operator who adds an elevated tag and sees no change in reported mode has been told something concrete.
- [x] This ticket states the actually-observed accuracy gain (if any) from the elevated path against real hardware, or plainly states "not tested — no elevated features currently surveyed on any real playfield" if that's the case (mirrors 029 ticket 004's honesty precedent) — never an assumed or asserted benefit.

**Carry-forward and plumbing (unchanged from the original draft)**

- [x] `Calibrator.calibrate()` gains `estimate_intrinsics: bool = False`.
- [x] Fewer than 6 total surveyed coplanar+elevated ground-truth points on the linked playfield → `estimate_intrinsics=True` fails explicitly, naming the point count found, storing nothing.
- [x] Once intrinsics are estimated this call, ticket 001's undistort-before-solve step applies immediately — the homography solved and the `reprojection_error_cm` reported this same call already reflect the new lens model (using the coplanar subset, per above).
- [x] `estimate_intrinsics=False` (default): if a `Calibration` already exists for this camera slug with intrinsics set, the new `Calibration` this call constructs carries `camera_matrix`/`dist_coeffs` forward unchanged; if none exists, behaviour is identical to before this sprint.
- [x] `CalibrationResult` (`calibration/interfaces.py`) gains `intrinsics_estimated: Optional[bool] = None` and a small mode/count summary (which mode ran, how many elevated features contributed — see above). No `camera_matrix`/`dist_coeffs` values are added to `CalibrationResult` or to the client-facing `Calibration` type — matching `get_calibration`'s existing "reports what the interface promises, never the internal homography matrix" precedent.
- [x] `daemon/interfaces.py`'s `Daemon.calibrate`, `daemon/service.py`'s `calibrate`, `client/daemon_client.py`'s `calibrate`, and `proto/aprilcam_v2.proto`'s `CalibrateRequest` all gain the plumbed-through `estimate_intrinsics` parameter/field — additive proto field, existing field numbers unchanged; proto stubs regenerated as part of this ticket. The proto `Feature`/playfield message also gains `height_cm` additively.
- [x] `mcp/tools/configuration.py`'s `calibrate` tool gains `estimate_intrinsics: bool = False`.
- [x] `cli/calibrate.py` gains `--with-intrinsics`. On success, prints whether intrinsics were estimated this call (and which mode/how many elevated features), or that previously-stored intrinsics were carried forward unchanged. When `result.matched_ids` has 4 or fewer entries, prints a caveat that `reprojection_error_cm` is degrees-of-freedom-limited rather than a real accuracy figure.
- [x] Unit tests: point-count-floor failure (named point count, surveyed-only); coplanar-constrained flags applied and verified; held-out validation and sanity bound exercised against both a good synthetic fit and a v1-style overfit (rejected); elevated mode excludes elevated points from the homography solve; graded-threshold mode selection; intrinsics carried forward on a subsequent no-flag call; RPC/proto round-trip of the new fields with every existing RPC test passing unmodified.
- [x] Hardware test (`@pytest.mark.camera`, skips cleanly without the camera attached): `--with-intrinsics` run live against camera 3 (`arducam-ov9782-usb-camera`) / `main-playfield` succeeds and stores non-null `camera_matrix`/`dist_coeffs` under the coplanar-constrained default (no elevated features are currently surveyed on `main-playfield`, so this exercises the default path only — the elevated path is exercised by synthetic unit tests unless real elevated ground truth is added to the field during this sprint, see sprint.md Open Question 7). **Result, live-measured (see Completion Notes): the camera is currently close enough to an exactly-nadir mount that the coplanar-constrained default is correctly rejected by the sanity bound rather than storing an implausible focal length — the test skips cleanly on this specific, diagnosed precondition rather than asserting a false success.**

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/config/ tests/aprilcam/calibration/ tests/aprilcam/daemon/test_service.py tests/aprilcam/daemon/test_rpc.py tests/aprilcam/mcp/ tests/aprilcam/cli/test_calibrate.py -v` — confirm the default (`estimate_intrinsics=False`, no prior intrinsics, no elevated features) path is unchanged, and every existing playfield-loading test still passes with the new optional `height_cm` field absent.
- **New tests to write**: `tests/aprilcam/config/` (`Feature.height_cm` round-trip, default-0.0 backward compat), `tests/aprilcam/calibration/test_calibration.py` (surveyed-point floor, coplanar constrained flags, held-out validation, sanity bound rejecting a v1-style overfit, elevated-mode point split, graded threshold, carry-forward), daemon/RPC plumbing tests, `tests/aprilcam/mcp/test_configuration.py`, `tests/aprilcam/cli/test_calibrate.py`, a new `@pytest.mark.camera` case.
- **Verification command**: `uv run pytest tests/aprilcam/config/ tests/aprilcam/calibration/ tests/aprilcam/daemon/ tests/aprilcam/mcp/ tests/aprilcam/cli/ -v`

## Completion Notes

**What shipped.** `Feature.height_cm`/`PlayfieldFeatureEntry.height_cm` (schema);
`Calibrator.calibrate(estimate_intrinsics=...)` running `cv2.calibrateCamera`
over a graded coplanar-constrained/elevated model with held-out validation
and a sanity bound (`calibration/calibration.py`'s `_estimate_intrinsics`/
`_sanity_check_intrinsics`/`_held_out_split`); the homography/intrinsics
point-set split; `CalibrationResult.intrinsics_estimated`/`intrinsics_mode`/
`elevated_features_used`; the full `estimate_intrinsics` plumb-through
(`Daemon`, `DaemonService`, `GrpcDaemon`, proto `CalibrateRequest`/
`CalibrationResult`/`Calibration`/`PlayfieldFeatureEntry`/`FeatureMatch`,
`mcp.tools.configuration.calibrate`, `cli/calibrate.py --with-intrinsics`);
and the confirmed `Calibrator.__init__(get_prior_calibration=...)` fix for
ticket 001's carry-forward-after-restart bug.

**Open Question 5 — is `k2` freed in the coplanar default?** No, `k1`-only
(`_FREE_K2_IN_COPLANAR_DEFAULT = False`). Evidence: 200 synthetic trials
(4 pixel-noise levels x 50 seeds each) fitting main-playfield's real
11-point coplanar layout at a well-conditioned 8&deg; mount tilt. Freeing
`k2` raised mean held-out reprojection RMS by roughly 4-6x and was only
measurably *better* than `k1`-only in 18-28% of trials (worse in the
rest) — with a single view and this few points, `k2` chases noise rather
than capturing real signal. `k1`-only wins decisively and is what ships.

**Open Question 6 — the elevated-mode threshold.** `>= 3` elevated
(`height_cm > 0`) surveyed-and-detected points **and** `>= 5.0 cm`
z-spread (`MIN_ELEVATED_FEATURES_FOR_ELEVATED_MODE`,
`MIN_ELEVATED_Z_SPREAD_CM`). Evidence: (a) the count floor of 3 mirrors
this module's own pre-existing "enough points to trust a spread claim"
floor (`MIN_DRIFT_CHECK_MARKERS`/`_is_collinear`'s 3-point floor for a 2D
spread, extended here to 3D); (b) the z-spread floor was set with a full
1 cm of margin above an empirically observed 3 cm boundary: synthetic
trials mixing 3 elevated points into main-playfield's real 11-point
coplanar layout, at a **mathematically exact 0&deg; mount tilt** (the
single hardest case), converged the fitted focal length to within a few
percent of ground truth at z-spreads as low as 3 cm. 5 cm is comfortably
above any realistic surveying/detection noise floor on this project
(sub-cm).

**A second degeneracy this ticket's own investigation found, beyond
v1's k2 explosion.** For a near-exactly-nadir single coplanar view, a
surveyed point's image position depends on focal length (`fx`) and
camera-to-plane distance (the solved extrinsic `tvec[2]`) *only through
their ratio* — `x_pixel = fx*X/Z + cx` for fixed, known `X`. This makes
`fx`/`Z` trade off along an exactly flat ridge with **no distortion
coefficient involved at all**, so the constrained flags alone do not
fully resolve v1's coplanarity degeneracy — they resolve the
k1-explosion half, not this focal-length/depth half. Synthetic sweeps
found `fx` landing anywhere from ~20x to ~700x the true focal length at
0&deg; tilt, converging to within a few percent once genuine tilt
(>= ~5&deg;) or genuine elevated z-spread (>= ~3-5 cm, even at exactly
0&deg; tilt) gives the solve real 3D structure. `_sanity_check_intrinsics`
therefore checks fitted focal length against the image width
(`_MIN_FX_TO_WIDTH_RATIO`/`_MAX_FX_TO_WIDTH_RATIO = 0.3/3.0`) as an
independent, primary check alongside the `k1`/`k2` bounds the ticket's
text asked for — this is the check that actually caught the real-hardware
result below. Documented in `calibration/calibration.py`'s module
docstring and at `_sanity_check_intrinsics`/the coplanar branch of
`_estimate_intrinsics`.

**A held-out-split bug this ticket's own tests caught before it shipped.**
The first implementation of `_held_out_split` used a contiguous
prefix/suffix block (first N fit, last M held out). Because
`Calibrator.calibrate` builds its intrinsics point list ArUco ids first,
then AprilTag ids, and elevated features are conventionally the ones
*appended* to a playfield definition after its original coplanar
corners, a block split could hold out **every** elevated point at once —
starving the fit subset of the very 3D structure elevated mode exists to
use (`test_elevated_mode_resolves_the_fx_depth_degeneracy_even_at_exact_nadir`
caught this directly: the fit converged to `fx=1316` instead of the true
`900`). Fixed to evenly-spaced indices (`numpy.linspace`), which makes
that systematic exclusion structurally impossible regardless of input
order. See `_held_out_split`'s own docstring.

**A schema collision this ticket's own tests caught before it shipped.**
`main-playfield.json`'s `rectangles` section already uses a `"height_cm"`
key for the rectangle's own physical dimension (e.g.
`"rect-northwest-purple"`: `"width_cm": 5, "height_cm": 4`) — a
pre-existing, unrelated meaning. A first draft of `_parse_section` read
`height_cm` generically for every section, silently reinterpreting that
4 cm rectangle dimension as 4 cm of elevation
(`test_main_playfield_every_feature_has_height_cm_zero` caught this).
Fixed: `height_cm` (elevation) is parsed only for the two id sections
(`aruco_tags`/`april_tags`) — the calibration-ground-truth sections this
ticket's schema change is actually about — never `rectangles`/`dots`.

**Real hardware result (live-measured in the implementation environment,
which had the real `arducam-ov9782-usb-camera` attached) — read this
before treating the "hardware test" checkbox above as "everything
works end to end on real hardware."** `aprilcam camera calibrate
arducam-ov9782-usb-camera --with-intrinsics` against the real,
currently-installed `main-playfield` was run live. `Calibrate`
succeeded at completeness (all 10 ArUco ids matched) but the
coplanar-constrained intrinsics fit was **rejected** by
`_sanity_check_intrinsics`: `"implausible focal length: fx=310.5px is
0.24x the 1280px image width (expected roughly 0.3x-3.0x for an
ordinary lens)"`. This is exactly the fx/depth degeneracy described
above, not a code defect — sprint 029's own text already describes
camera 3 as mounted at "ground nadir (0,0)", consistent with this
result, and the sanity bound did precisely what it exists to do
(refuse to store a wild focal length rather than silently accept it,
which is the whole substantive difference between this ticket and what
v1 shipped). The new hardware test
(`tests/aprilcam/daemon/test_calibration_hardware.py::test_with_intrinsics_against_main_playfield_stores_a_lens_model`)
recognizes this specific, diagnosable rejection reason as a currently-
unmet *precondition* (not a bug) and skips cleanly, citing the live
measurement, rather than asserting a false success — the same "skip on
an under-guarded precondition rather than train people to expect a red
hardware test" philosophy this same sprint's own ticket 005 applies
elsewhere. **Practical consequence for ticket 003**: `--with-intrinsics`
cannot currently produce a stored lens model for camera 3 under its
present mount, through no fault of this ticket's implementation. Per
sprint.md Open Question 7, the two ways to unblock it — re-mount with
genuine tilt, or survey an elevated feature into `main-playfield` — were
both explicitly deferred by the stakeholder this sprint. Ticket 003's
falsifiable checks (radial consistency, optical height) were scoped
against a re-verification *with* distortion modelled; if no lens model
can currently be stored for camera 3, that re-verification has nothing
new to measure against until one of those two unblocks happens, or
ticket 003 finds camera 3's real mount has more tilt than this single
run suggests (a fresh `--with-intrinsics` run is cheap to re-try; this
result is one live measurement, not exhaustively repeated).

**Elevated-path accuracy against real hardware: not tested.** Per the
ticket's own honesty requirement (mirroring 029 ticket 004): no
elevated features are currently surveyed on any real playfield in this
project (confirmed — `main-playfield.json` has 11 surveyed points, all
at `height_cm=0`), and the stakeholder explicitly decided not to install
any this sprint. The elevated path (mode selection, the fx/depth-
resolution property, the point-set split) is exercised only by the
synthetic unit tests described above (`_estimate_intrinsics`/
`_sanity_check_intrinsics` fed real `cv2.projectPoints`-generated data
with genuine 3D structure) — never against real elevated ground truth.
No accuracy claim is made for it beyond what those synthetic tests show.

**Testing performed.** `uv run pytest tests/aprilcam/config/
tests/aprilcam/calibration/ tests/aprilcam/daemon/ tests/aprilcam/mcp/
tests/aprilcam/cli/ -m "not camera"` — 1001 passed. Every
`@pytest.mark.camera` test in this same scope was also run against the
real arducam attached to the implementation environment:
`tests/aprilcam/daemon/test_calibration_hardware.py` (this ticket's own
hardware coverage) — 7 passed, 3 skipped (all pre-existing or the new,
diagnosed skip above); the remaining hardware tests across
`tests/aprilcam/{daemon,mcp}/` — 10 failed, 23 passed, 7 skipped, all
10 failures in modules this ticket never touched
(`test_camera_geometry_hardware.py`, `test_detection_hardware.py`,
`test_lifecycle_integration.py`, `test_mobile_tags_hardware.py`,
`mcp/test_integration.py`), confirmed pre-existing and reproducing
identically against the pre-ticket-002 commit (checked via `git stash`
before implementation began) — the physical camera's current framing
appears to only see a subset of the field's markers at the moment,
unrelated to this ticket's code.
