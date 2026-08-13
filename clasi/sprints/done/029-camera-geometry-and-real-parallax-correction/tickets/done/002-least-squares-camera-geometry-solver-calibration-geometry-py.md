---
id: '002'
title: Least-squares camera-geometry solver (calibration/geometry.py)
status: done
use-cases:
- SUC-003
depends-on: []
github-issue: ''
issue: camera-geometry-for-real-parallax-correction.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Least-squares camera-geometry solver (calibration/geometry.py)

## Description

Add a pure function to `calibration/geometry.py`, alongside
`project_tag_to_field_plane` and friends, that solves a camera's ground
nadir and optical height from 2+ elevated reference observations at
known surveyed positions. No daemon state, no I/O, no cv2 — same shape
as every existing function in this module. This ticket is independent
of ticket 001 (it needs no `CameraConfig`/proto changes at all) and is
consumed by ticket 003 (`camera locate`), which supplies the
observations; this ticket does not itself talk to a camera or a
daemon.

**Why this is genuinely new math, not a port.** There is no isolated
camera-geometry solver in the v1 archive to lift — v1 asserted
`x_offset: 0, y_offset: 0` (assumed field-centred) and required a hand-
measured height; it never solved either. Derive fresh.

**The failure this ticket must not reproduce.** A first attempt at this
solve (by the stakeholder, before this sprint) intersected the two
displacement lines (surveyed -> observed, extended) and got a nadir
~20 cm off with a residual 150x worse than optimum. Cause: camera
nadir `C`, a reference's true footprint `T`, and its apparent
(uncorrected) observed position `A` are **collinear by construction** —
this is exactly what parallax *is*: a ray from the camera continuing
past the tag to the ground. Two references on opposite sides of `C`
produce two nearly-coincident lines; intersecting them (using only
*direction*, discarding *magnitude*) is wildly ill-conditioned. The fit
below uses the full 2D residual — magnitude and direction both — which
is what makes it well-conditioned.

**The math.** Forward relation (already proven correct and tested,
`project_tag_to_field_plane`'s own worked example — this ticket adds
no new physics, only inverts an already-verified relation):

```
apparent = C + k * (true - C),   k = H / (H - h)
```

where `C` is the camera's ground nadir, `H` its height, `true` a
reference's surveyed ground position, `h` its stand height, `apparent`
its observed (uncorrected) world position from `GetTag`. This is
bilinear in the unknowns `(C, H)` — direct least squares over `(Cx, Cy,
H)` is nonlinear. **Linearize by substitution**: let `A = C / H` (a
2-vector) and `b = 1 / H` (a scalar). Substituting and rearranging
(derivation — verify this algebra before implementing, it is exactly
the step that is easy to get subtly wrong):

```
true = apparent + (h/H) * (C - apparent)          [the already-tested reverse relation]
     = apparent + h*A - h*b*apparent
=> true - apparent = h*A - h*apparent*b
```

Per reference `i`, this gives **two linear equations** in the three
unknowns `(Ax, Ay, b)`:

```
true_x,i - apparent_x,i = h_i * Ax - h_i * apparent_x,i * b
true_y,i - apparent_y,i = h_i * Ay - h_i * apparent_y,i * b
```

Stack all references into a design matrix `M` (shape `2n x 3`) and
right-hand side `r` (shape `2n`), solve `[Ax, Ay, b] = lstsq(M, r)`
(`numpy.linalg.lstsq`, no iteration, no initial guess). Recover
`H = 1/b`, `C = A / b = A * H`. **Verify this reformulation
numerically against the fixture below before trusting it in code** —
plug the fixture's real numbers through both the forward relation and
the linearized system and confirm they agree to within the fixture's
own noise level; do not implement from the algebra alone without that
check, given how easy this exact class of derivation is to get subtly
wrong (the stakeholder's own line-intersection attempt is the proof).

**API** (exact names are this ticket's choice, but keep the shape):

```python
@dataclass(frozen=True, kw_only=True)
class ReferenceObservation:
    surveyed: WorldPoint    # true ground position (from the playfield definition)
    observed: WorldPoint    # apparent (uncorrected) world position, from GetTag
    height_cm: float        # physical stand height of this reference

@dataclass(frozen=True, kw_only=True)
class CameraGeometrySolution:
    nadir: WorldPoint
    height_cm: float
    rms_residual_cm: float
    condition_number: float   # of the design matrix M -- large means near-collinear

def solve_camera_geometry(
    references: Sequence[ReferenceObservation],
) -> CameraGeometrySolution:
    ...
```

`rms_residual_cm`: refit `apparent` from the solved `(C, H)` for every
reference via the forward relation, RMS the 2D error against the
actual `observed` values — this is what makes the fixture's `0.16 cm`
figure reproducible and testable.

`condition_number`: `numpy.linalg.cond(M)` (or the ratio of `M`'s
largest to smallest singular value via `numpy.linalg.svd`) — the
direct, principled measure of exactly the ill-conditioning the
stakeholder's failed attempt suffered from; do not substitute an
ad-hoc angle-spread heuristic, since the condition number is already
the rigorous version of "how collinear are these references relative
to the solved nadir."

Raises `ValueError` for fewer than 2 references, before any fitting is
attempted (mirrors this module's existing local-validation style,
e.g. `deskewed_output_size`'s positive-dimension check — a plain
`ValueError`, not an `AprilCamError` subclass, since this module has no
RPC boundary to cross).

## Acceptance Criteria

- [x] `ReferenceObservation`/`CameraGeometrySolution` are frozen
      `@dataclass(frozen=True, kw_only=True)` — no `typing.Protocol`.
- [x] `solve_camera_geometry` raises `ValueError` for 0 or 1
      references, before any linear algebra runs.
- [x] **The real fixture** (sprint.md's acceptance fixture, hand-copied
      from today's measurement, not re-derived): given tag 15
      (surveyed `(50, 30)`, height `17.2`, observed `(57.38, 34.90)`)
      and tag 14 (surveyed `(-50, -30)`, height `15.0`, observed
      `(-57.15, -33.40)`), `solve_camera_geometry` recovers nadir
      within a documented tolerance of `(3.05, -2.75)` cm, height
      within a documented tolerance of `128.25` cm, and
      `rms_residual_cm` within a documented tolerance of `0.16`.
- [x] **The ill-conditioned case**: a synthetic case with two
      references placed diametrically opposite a known nadir (chosen
      to reproduce the *shape* of the stakeholder's failed attempt, not
      necessarily its exact numbers) does not reproduce a ~20 cm-off
      answer with 150x-worse residual — either the solved answer is
      materially closer to ground truth than that failure, or
      `condition_number` is markedly elevated versus the well-
      conditioned fixture case (or both), proving the new solver
      degrades gracefully/visibly rather than silently.
- [x] A synthetic 3-reference case with angularly well-spread
      references (e.g. roughly 120 degrees apart around a known nadir)
      recovers the known ground truth to near-exact precision (no
      measurement noise in a synthetic case) and reports a low
      `condition_number`, establishing the "good" baseline the
      ill-conditioned case is compared against.
- [x] The forward-relation/linearization equivalence is verified
      numerically in a test (or as an inline sanity check the test
      suite exercises) — not merely asserted in a docstring.
- [x] `uv run mypy` clean; pure function(s), no daemon state, no I/O,
      no `cv2` import (module already has none — keep it that way).

## Completion Notes

Implemented `ReferenceObservation`, `CameraGeometrySolution`, and
`solve_camera_geometry` in `src/aprilcam/calibration/geometry.py`,
directly after `correct_tag_pose` — same pure-function shape as the
rest of the module, no new imports beyond `dataclasses.dataclass` and
`typing.Sequence`.

**Solver output on the real fixture** (tag 15 + tag 14, exactly as
specified in the ticket):

```
nadir              = (3.05701, -2.79870) cm
height_cm          = 128.22990
rms_residual_cm    = 0.15851
condition_number   = 67.44162
design-matrix rank = 3
singular values    = [1523.24260, 22.82192, 22.58609]
```

This matches the dispatch message's pre-verified numbers to 4-5
significant figures (nadir (3.06, -2.80), height 128.23, singular
values [1523.24, 22.82, 22.59], condition number 67.4) and, applying
the solved geometry via the already-tested `project_tag_to_field_plane`,
corrects the two fixture observations to (50.093, 29.843) and
(-50.107, -29.820) — errors of 0.182 cm and 0.209 cm, down from 8.86 cm
and 7.92 cm uncorrected, matching the ticket's documented ~0.18/0.21 cm
figures.

**Design decisions:**
- `rms_residual_cm` flattens both axes' scalar residuals across all
  references into one RMS (`sqrt(mean(dx_1^2, dy_1^2, dx_2^2, dy_2^2,
  ...)))` — this, not a per-point-then-averaged 2D-magnitude RMS, is
  the definition that reproduces the ticket's documented `0.16`
  figure (a per-point-magnitude RMS on this fixture gives ~0.224
  instead).
- `condition_number` is computed from the singular values `lstsq`
  already returns (`sv[0] / sv[-1]`), rather than a second
  `np.linalg.cond(M)` call — same number, no duplicate SVD.
- Verified the ticket's stated failure mode does *not* reproduce under
  this formulation: the real fixture's two references sit
  diametrically opposite the nadir (exactly the configuration that
  broke line-intersection) yet the design matrix here is well
  conditioned (67.4) — least squares over the full 2D residual
  (magnitude + direction) is fundamentally different conditioning from
  intersecting two directions-only lines. A genuinely degenerate
  synthetic case (two references on nearly the same ray from the
  nadir, 2% apart) does blow up the condition number (~7000, over 100x
  the well-conditioned baseline) — the loud-not-silent guard the
  ticket asks for.

**Tests added** to
`tests/aprilcam/calibration/test_geometry.py` (7 new tests, all
passing): the two `ValueError` cases; the numeric
forward-relation/linearization equivalence check; the real-fixture
regression (nadir/height/rms/condition_number); a fixture-correction
test composing the solve with `project_tag_to_field_plane` and pinning
the documented corrected positions/errors; the well-conditioned
120-degree-spread 3-reference baseline (near-exact recovery, low
condition number); the diametrically-opposite synthetic case (recovers
within 1 cm of ground truth, nowhere near the failed attempt's ~20 cm
error); and the near-collinear synthetic case asserting
`condition_number` is >20x the well-conditioned baseline and >1000
absolute.

**Verification**: `uv run mypy` — clean (78 source files, no issues).
`uv run pytest tests/aprilcam/calibration/test_geometry.py -q` — 38
passed (31 existing + 7 new). Full headless suite,
`uv run pytest -q -m "not camera and not integration"` — 1300 passed,
54 deselected (1293 + 7 new, no regressions). Did not run the
unfiltered `uv run pytest` per the caution about the live daemon
holding 3 cameras open for ticket 004.

No deviations from the ticket's specified API shape or math. No
scope creep — `cli/locate.py` (ticket 003) and the config/proto/daemon
plumbing (ticket 001) were untouched, and
`project_tag_to_field_plane`'s existing code was not modified.

## Testing

- **Existing tests to run**: `uv run pytest
  tests/aprilcam/calibration/test_geometry.py -v` (confirm no
  regression to the existing pure-geometry suite this ticket extends),
  plus the full headless suite.
- **New tests to write**: the real-fixture regression case, the
  ill-conditioned synthetic case, the well-conditioned synthetic
  baseline, the `ValueError` cases, added to
  `tests/aprilcam/calibration/test_geometry.py` alongside the existing
  suite — this module's tests never get relaxed
  (`docs/design/design.md` §6).
- **Verification command**: `uv run pytest -m "not camera"` and
  `uv run mypy`.

**Documentation updates**: docstrings only — the worked-example style
this module already uses throughout (e.g.
`project_tag_to_field_plane`'s own docstring) applies here too: show
the derivation and a concrete numeric check, not just the formula.
