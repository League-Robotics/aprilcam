"""Tests for ``calibration/gates.py`` (sprint 045 ticket 004): every ladder branch and edge."""

from __future__ import annotations

import numpy as np
import pytest

from aprilcam.calibration import gates
from aprilcam.calibration.gates import GateMetrics, classify

GOOD = GateMetrics(rms_px=0.4, rms_cm=0.1, scale_ratio=1.01, angular_spread_deg=45.0,
                   ambiguity_ratio=5.0, layout_extent_ratio=0.5, loo_worst=(3, 0.4))


def test_angular_spread():
    assert gates.angular_spread_deg(np.array([[0, 0, 1.0]])) == 0.0
    rays = np.array([[0, 0, 1.0], [1, 0, 1.0], [0, 1, 1.0], [-1, 0, 1.0]])
    assert gates.angular_spread_deg(rays) == pytest.approx(90.0)
    assert gates.angular_spread_deg(np.array([[0, 0, 5.0], [0, 0, 1.0]])) == pytest.approx(0.0, abs=1e-6)


def test_scale_agreement():
    assert gates.scale_agreement([100.0], [100.0]) is None
    assert gates.scale_agreement([101, 99, 130], [100, 100, 100]) == pytest.approx(1.01)
    with pytest.raises(ValueError):
        gates.scale_agreement([1, 2], [1])
    assert gates.scale_ok(None) and gates.scale_ok(1.05) and gates.scale_ok(0.95)
    assert not gates.scale_ok(1.0501) and not gates.scale_ok(0.9499)


def test_ambiguity_edges():
    assert gates.ambiguity_ok(None)
    assert gates.ambiguity_ok(1.3) and not gates.ambiguity_ok(1.2999)


def test_layout_extent_ratio():
    assert gates.layout_extent_ratio(np.array([[0.0, 0.0]])) is None
    assert gates.layout_extent_ratio(np.array([[0, 0], [10, 10]])) == pytest.approx(0.0, abs=1e-9)
    assert gates.layout_extent_ratio(np.array([[0, 0], [10, 0], [0, 10], [10, 10]])) == pytest.approx(1.0)
    assert gates.layout_extent_ratio(np.array([[0.0, 0.0], [0.0, 0.0]])) == 0.0


def test_leave_one_out_selection():
    assert gates.per_marker_leave_one_out({1: 0.1, 2: 0.2}) is None
    assert gates.per_marker_leave_one_out({}) is None
    assert gates.per_marker_leave_one_out({1: 0.1, 2: 3.0, 3: 0.2}) == (2, 3.0)


def test_full():
    label, notes, fail = classify([1, 2, 3], [1, 2], [3], GOOD)
    assert (label, notes, fail) == ("full", (), None)


def test_reduced_when_helpful_missing():
    label, notes, fail = classify([1, 2], [1, 2], [3, 4], GOOD)
    assert label == "reduced" and fail is None and "[3, 4]" in notes[0]


def test_reduced_with_all_required_only_policy():
    # legacy definition: everything required, nothing helpful -> a missing one fails
    label, _, fail = classify([1, 2], [1, 2, 3], [], GOOD)
    assert label == "fail" and "[3]" in fail


def test_single_marker_is_low_and_stored():
    label, notes, fail = classify([2], [], [1, 2], GOOD)
    assert label == "low" and fail is None and "one marker" in notes[0]


def test_zero_matched_fails():
    label, _, fail = classify([], [], [1], GOOD)
    assert label == "fail" and "no calibration markers" in fail


def test_required_missing_names_ids():
    label, _, fail = classify([1], [1, 5, 6], [], GOOD)
    assert label == "fail" and "[5, 6]" in fail


@pytest.mark.parametrize("kw,passes", [
    ({"rms_px": 12.0}, True), ({"rms_px": 12.01}, False),
    ({"rms_cm": 1.0}, True), ({"rms_cm": 1.01}, False),
    ({"scale_ratio": 1.05}, True), ({"scale_ratio": 1.06}, False),
    ({"loo_worst": (4, 2.0)}, True), ({"loo_worst": (4, 2.01)}, False),
])
def test_hard_gate_edges(kw, passes):
    m = GateMetrics(**{**GOOD.__dict__, **kw})
    label, _, fail = classify([1, 2, 3], [1, 2, 3], [], m)
    assert (label == "full") if passes else (label == "fail" and fail)


def test_loo_failure_names_marker():
    m = GateMetrics(**{**GOOD.__dict__, "loo_worst": (9, 30.0)})
    _, _, fail = classify([1, 2, 3], [1, 2, 3], [], m)
    assert "marker 9" in fail


@pytest.mark.parametrize("kw,passes", [
    ({"angular_spread_deg": 10.0}, True), ({"angular_spread_deg": 9.9}, False),
    ({"ambiguity_ratio": 1.3, "angular_spread_deg": 12.0}, True),
    ({"ambiguity_ratio": 1.29, "angular_spread_deg": 12.0}, False),
    ({"layout_extent_ratio": 0.2}, True), ({"layout_extent_ratio": 0.19}, False),
    ({"rotation_ambiguous_ids": ()}, True), ({"rotation_ambiguous_ids": (4,)}, False),
])
def test_weak_geometry_caps_at_low(kw, passes):
    m = GateMetrics(**{**GOOD.__dict__, **kw})
    label, notes, fail = classify([1, 2, 3], [1, 2, 3], [], m)
    assert fail is None
    assert label == ("full" if passes else "low")
    assert bool(notes) != passes


def test_not_applicable_metrics_do_not_block():
    label, _, fail = classify([1, 2], [1, 2], [], GateMetrics())
    assert (label, fail) == ("full", None)


def test_gates_module_imports_no_cv2():
    import ast
    import pathlib
    tree = ast.parse(pathlib.Path(gates.__file__).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "cv2" not in imported
    assert imported <= {"__future__", "dataclasses", "typing", "numpy"}


def test_live_field_solve_with_survey_limited_pixel_rms_is_full():
    """2026-10-16 camera 2: 3.75 px / 0.50 cm was wrongly rejected by a 1.5 px cap."""
    m = GateMetrics(rms_px=3.75, rms_cm=0.50, scale_ratio=1.005, angular_spread_deg=70.0,
                    ambiguity_ratio=5.0, layout_extent_ratio=0.5, loo_worst=(7, 1.23))
    label, _, fail = classify([1, 2, 3], [1, 2, 3], [], m)
    assert label == "full", fail


def test_live_wide_layout_ambiguity_ratio_is_ignored_not_capped():
    """2026-10-16 camera 2: ratio 1.25 on a ten-marker, 70 deg layout must stay full."""
    m = GateMetrics(rms_px=3.83, rms_cm=0.51, scale_ratio=1.009, angular_spread_deg=69.9,
                    ambiguity_ratio=1.25, layout_extent_ratio=0.5, loo_worst=(7, 1.32))
    ids = list(range(1, 11))
    label, notes, fail = classify(ids, ids, [], m)
    assert (label, fail) == ("full", None)
    assert any("ignored" in n and "1.25" in n for n in notes)
    assert not any("capped" in n for n in notes)


def test_narrow_layout_ambiguity_still_caps_low():
    m = GateMetrics(**{**GOOD.__dict__, "angular_spread_deg": 12.0, "ambiguity_ratio": 1.1})
    label, notes, fail = classify([1, 2], [1, 2], [], m)
    assert (label, fail) == ("low", None)
    assert any("capped at low" in n and "ambiguity" in n for n in notes)


def test_ambiguity_spread_boundary_at_30_deg():
    base = {**GOOD.__dict__, "ambiguity_ratio": 1.1}
    ids = [1, 2, 3]
    at = classify(ids, ids, [], GateMetrics(**{**base, "angular_spread_deg": 30.0}))
    below = classify(ids, ids, [], GateMetrics(**{**base, "angular_spread_deg": 29.9}))
    assert at[0] == "full" and below[0] == "low"
    # wide spread but under 3 markers still caps
    two = classify([1, 2], [1, 2], [], GateMetrics(**{**base, "angular_spread_deg": 60.0}))
    assert two[0] == "low"
