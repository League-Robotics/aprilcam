---
id: '001'
title: 'Definition schema: marker size, rotation and required/helpful policy'
status: open
use-cases:
- SUC-002
depends-on: []
github-issue: ''
issue: pose-based-playfield-calibration-with-marker-corners-and-measured-lens.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Definition schema: marker size, rotation and required/helpful policy

## Description

Teach the playfield definition the data the pose solve needs, without
changing behaviour for any existing definition. On each `aruco_tags` entry:
`size_mm` (outer side of the black square, optional), `rotation_deg`
(marker orientation on the field; 0 = upright, top edge toward north; default
0), `policy` (`required` | `helpful`, optional). On the definition:
`default_marker_size_mm` (optional; an entry's own `size_mm` overrides it).

Resolution rules live on the concrete `PlayfieldDefinition` as additive
methods (same pattern as `aruco_position`): `marker_size_mm(id) -> Optional[float]`,
`marker_rotation_deg(id)`, `required_marker_ids()` and `helpful_marker_ids()`.
`required_aruco_ids()` keeps its current meaning (every listed id) so existing
callers and the centre-point path are untouched; the new `required_marker_ids()`
returns the ids whose effective policy is `required` (unspecified = required,
Open Question 5). Elevated entries (`height_cm > 0`) are excluded from pose
use (documented, enforced in ticket 006).

Validation (whole-document `MalformedDocument`, naming the entry): `size_mm`
and `default_marker_size_mm` must be positive numbers (reject bool, NaN, inf,
absurd > 1000 mm); `policy` must be one of the two literals; `rotation_deg`
must be finite. `to_dict` emits the new keys only when set, so
`load(x.to_dict()) == x` and old files are byte-identical on dump.

## Acceptance Criteria

- [ ] `PlayfieldFeatureEntry` gains `size_mm`, `rotation_deg`, `policy`;
      `PlayfieldDefinition` gains `default_marker_size_mm`; all default to
      "absent" and equal today's values.
- [ ] Resolution methods implemented and unit-tested (entry overrides default;
      no size anywhere -> `None`).
- [ ] Invalid values rejected with messages naming the entry and key.
- [ ] `config/aprilcam/playfields/main-playfield.json` and
      `secondary-playfield.json` (and every playfield fixture under tests/)
      load and round-trip unchanged.
- [ ] `rectangles`/`dots` entries do not gain these keys (their `height_cm` /
      `width_cm` meaning is untouched).
- [ ] `config/interfaces.py` base class docstring updated: the
      "all-or-nothing" wording on `required_aruco_ids` now points at the
      per-field policy.

## Implementation Plan

**Approach.** Extend `_parse_section` for the two id sections only (mirror
`_parse_feature_height_cm`), add `_parse_default_marker_size`, extend `to_dict`.
Keep `config` cv2-free.

**Files to modify:** `src/aprilcam/config/loader.py`,
`src/aprilcam/config/interfaces.py` (docstrings, no new Protocol),
`src/aprilcam/config/DESIGN.md` (schema section; full doc pass is ticket 012,
but record the schema here so it is not lost).

**Files to create:** tests in `tests/aprilcam/config/test_loader.py` (extend).

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/config tests/aprilcam/calibration/test_calibration.py -x`
- **New tests to write**: parse/round-trip with and without each key; default
  vs entry size; invalid size/policy/rotation; shipping files unchanged
  (golden `to_dict` equality against pre-change output).
- **Verification command**: `uv run pytest tests/aprilcam/config`

## Documentation Updates

`config/DESIGN.md` schema paragraph; full pass in ticket 012.
