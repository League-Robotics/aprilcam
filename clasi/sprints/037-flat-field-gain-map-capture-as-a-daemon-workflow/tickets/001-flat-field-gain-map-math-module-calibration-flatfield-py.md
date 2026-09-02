---
id: "001"
title: "Flat-field gain-map math module (calibration/flatfield.py)"
status: open
use-cases: [SUC-001, SUC-002]
depends-on: []
github-issue: ""
issue: flat-field-gain-map-capture-as-a-daemon-workflow.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Flat-field gain-map math module (calibration/flatfield.py)

## Description

Add the pure, camera-agnostic gain-map-building engine the rest of this
sprint's daemon workflow depends on. Mirrors `calibration/calibration.py`'s
`Calibrator` shape exactly: a plain base class (`FlatFieldCapturer` in
`calibration/interfaces.py`, `NotImplementedError` bodies, never
`typing.Protocol`) plus a concrete `calibration/flatfield.py`
implementation that takes an already-captured frame burst and a
precomputed boolean field-interior mask — **never a camera, never a
filesystem** — and returns a `FlatFieldResult`.

See this sprint's `design/calibration-DESIGN.md` §2 ("Orientation" —
`flatfield.py` bullet), §4 ("The flat-field capture algorithm, end to
end" and the "Decision: home for flat-field capture math" rationale),
and §5 ("Interfaces — Exposes") for the full algorithm and interface
shapes this ticket implements. In short: normalise each frame to the
batch mean (corrects ~25% frame-to-frame flicker), average, median-blur
each channel (kills sheet wrinkles/specular highlights), fill outside
the mask with the mask's own interior mean, wide-Gaussian-blur to keep
only the low-frequency light field, compute `gain = target / L` per
channel, clip to a sane range, and evaluate a three-check quality gate
(in-field clipping, implausible >10x spread, frame-to-frame disagreement
beyond the flicker-normalisation tolerance) — a failing check refuses
(`succeeded=False`, spread still reported) unless `force=True`.

Also add `calibration/interfaces.py`'s `FlatFieldInfo` (the
`flatfield.json` metadata-sidecar shape a later ticket persists and
reads back) and `errors.py`'s `NoFieldMask` (raised by ticket 002 when
neither a calibration nor detectable ArUco corners exist to derive a
mask from — defined here alongside this ticket's other new value types,
following the existing typed-exception shape, e.g. `NoPlayfieldLinked`).

## Acceptance Criteria

- [ ] `calibration/interfaces.py` gains `FlatFieldCapturer` (plain base
      class, `NotImplementedError` bodies), `FlatFieldResult`, and
      `FlatFieldInfo` (both frozen `@dataclass(kw_only=True)`, declaring
      fields, never properties).
- [ ] `calibration/flatfield.py`'s concrete `FlatFieldCapturer.build(frames,
      mask, *, force=False) -> FlatFieldResult` implements the 8-step
      algorithm in `design/calibration-DESIGN.md` §4.
- [ ] A known synthetic gradient flattens to within a stated tolerance
      after correction.
- [ ] Synthetic wrinkle/speckle noise in the input does not leak into
      the built gain map (median blur suppresses it).
- [ ] A synthetic frame with in-field clipping is refused
      (`succeeded=False`) without `force=True`, and proceeds with
      `force=True`, with the returned result naming the overridden check.
- [ ] A synthetic frame set with >10x measured spread is refused the
      same way; a synthetic frame set whose frames disagree beyond the
      flicker-normalisation tolerance (a simulated "sheet moved" case)
      is refused the same way.
- [ ] `FlatFieldResult` always carries per-channel `spread_before`/
      `spread_after` (p98/p2), on both a passing and a refused gate.
- [ ] `FlatFieldCapturer.build` never touches a filesystem, a camera, or
      any daemon state — verified by a test asserting no such call is
      made (e.g. no `open`/`cv2.VideoCapture` symbols touched).
- [ ] `errors.NoFieldMask` added, following the existing typed-exception
      shape.

## Testing

- **Existing tests to run**: `tests/aprilcam/calibration/` (no
  regressions to `Calibrator`/`Deskewer`/`geometry`/`distortion` tests).
- **New tests to write**: `tests/aprilcam/calibration/test_flatfield.py`
  — synthetic-frame unit tests per the acceptance criteria above
  (gradient flattening, wrinkle suppression, clipping refusal, spread
  refusal, disagreement refusal, force override reporting, spread
  reporting on every path, no camera/filesystem access).
- **Verification command**: `uv run pytest tests/aprilcam/calibration/`
