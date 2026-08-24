"""L0 corner/calibration robustness assertions (sprint 035 ticket 006;
test plan §6 item 4).

For every confirmed fixture image, detected **ArUco** ids (AprilTag ids
are irrelevant here -- only ArUco markers are ever calibration
candidates, ``design.md`` Sec.6's "ArUco + AprilTags are calibration
ground truth" notwithstanding: a real ``Calibrator.calibrate`` solve
only ever matches against ``PlayfieldDefinition.aruco_tags``) via the
same ``vision.pipeline.run_pipeline`` ->
``aprilcam.daemon.detection._default_detector_factory(slug).detect``
call pair every other L0 module uses -- never a second detection code
path -- are compared against that camera's own **calibration marker
universe**: every ArUco id surveyed on the playfield that camera is
mounted over (10 for ``main-playfield`` -- 8 perimeter/corner + 2
internal -- 4 for ``secondary-playfield``, all perimeter, no internal
markers). A near-baseline config must recall the *required* subset of
that universe in full; a config already known, from real measurement,
to fall short carries an **exact**, pinned expected-recall constant
instead, so a silent regression *and* a silent improvement are both
visible -- this ticket's own acceptance criterion: "a config where
fewer than the required set is detected is an expected, named
exception, not a silent pass."

**Earned, not guessed, and independently cross-checked.** Every id in
:data:`_CALIBRATION_PROFILES` below was read off a real detection pass
(the same ``run_pipeline`` + ``_default_detector_factory`` call this
module's own :func:`test_calibration_robustness` makes) over every one
of ``fs-20260823``'s 55 committed JPEGs -- the same raw measurement
:mod:`test_recall`'s own ``_RECALL_PROFILES`` is earned from. The two
tables are not byte-identical, and deliberately so: this module
intersects each config's raw detections against its own ``universe``
(every surveyed marker, ArUco 1 included), while :mod:`test_recall`
intersects against its *required* set (ArUco 1 excluded for camera 5,
module docstring below) -- so wherever ArUco 1 was actually detected on
a degraded camera-5 config (``exp-005``, ``exp-015``, ``exp-150``,
``gain-01``, ``gain-08``), this module's degraded set includes it and
:mod:`test_recall`'s does not, both correctly, from the identical raw
measurement. The two modules declare their own separate tables rather
than one importing the other's: :mod:`test_recall` mixes ArUco *and*
AprilTag ids under a
recall-family framing (a phantom off-field AprilTag would matter
there), while this module is ArUco-only under a distinct
calibration-family framing (an AprilTag detection, phantom or real, is
never relevant to whether a homography solve would succeed) --
coupling the two via import would make a future, legitimately
recall-specific change to one silently ripple into the other's
unrelated calibration semantics.

**Phantom/off-field ArUco hits are never part of any universe or
required/degraded set** (mirroring :mod:`test_recall`'s identical
policy): a config's *actual* set below is always ``detected_aruco &
universe``, so a spurious id outside the surveyed marker set (observed
live: ArUco 37 on camera 3's darkest exposure and camera 2's
``exp-151``; ArUco 17 on several camera 5 mid-exposure frames) is
neither asserted for nor asserted against.

**Camera 5's ArUco 1 is excluded from `required`, included in
`universe`.** Sprint 035's own measured fact (7/12 live polls,
intermittent) means even this generation's own *baseline* image does
not detect it (verified directly: ``baseline``'s real detection is
``{2..10}``, ArUco 1 absent) -- holding it to a 100%-recall bar on
*any* config, including baseline, would be asserting something already
known false. It stays in ``universe`` (10, not 9) because it is still
a real, surveyed marker a calibration attempt *could* use if it
happens to be visible that burst -- ``universe`` documents "what
exists," ``required`` documents "what a near-baseline config is held
to."

**Scoped to `fs-20260823` only**, exactly like :mod:`test_recall`: a
fixture whose ``field_state_id`` has no entry in
:data:`_CALIBRATION_PROFILES` falls through to a fail-loud default
built from the conftest-provided, field-state-driven
:attr:`~tests.image_matrix.conftest.FixtureEntry.expected_ids` (ArUco
ids only) as both ``universe`` and ``required`` -- an unclassified
config is held to full recall of its own field-state-declared ArUco
set, honestly surfacing a gap for a human to fill in with a real
earned profile rather than silently under-testing a future fixture
generation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, Optional

import cv2 as cv

from aprilcam.daemon.detection import _default_detector_factory
from aprilcam.vision.pipeline import run_pipeline

from tests.image_matrix.conftest import FixtureEntry

__all__ = ["test_calibration_robustness"]


@dataclass(frozen=True, kw_only=True)
class CalibrationProfile:
    """One camera's calibration-robustness contract for one field-state
    generation.

    ``universe`` -- every ArUco id surveyed on this camera's own
    playfield (module docstring). ``required`` -- the subset of
    ``universe`` a *near-baseline* config (any config label not present
    in ``degraded``) must detect in full. ``degraded`` maps a config
    label to the *exact* subset of ``universe`` that config is known to
    detect.
    """

    universe: FrozenSet[int]
    required: FrozenSet[int]
    degraded: Dict[str, FrozenSet[int]]


#: Measured 2026-08-23 against every committed ``fs-20260823`` JPEG (module
#: docstring); cross-validated against :mod:`test_recall`'s independently
#: declared ArUco data.
_CALIBRATION_PROFILES: Dict[str, Dict[str, CalibrationProfile]] = {
    "fs-20260823": {
        "arducam-ov9782-usb-camera": CalibrationProfile(
            universe=frozenset(range(1, 11)),
            required=frozenset(range(1, 11)),
            degraded={
                "exp-03": frozenset({2, 3, 5, 6, 8, 9, 10}),
                "exp-24": frozenset({2, 3, 4, 5, 6, 7, 10}),
                "exp-40": frozenset({2, 3, 4, 5, 10}),
                "gain-01": frozenset({1, 2, 3, 5, 6, 7, 8, 9, 10}),
                "wb-6500": frozenset({1, 2, 3, 5, 6, 7, 8, 9, 10}),
                "stress-lowexp-higain": frozenset({1, 2, 3, 5, 6, 8, 9, 10}),
                "stress-highexp-logain": frozenset({3, 4, 5, 10}),
            },
        ),
        "arducam-ov9281-usb-camera": CalibrationProfile(
            universe=frozenset(range(1, 11)),
            # ArUco 1 excluded from `required` -- intermittent even at
            # baseline (module docstring); still a real marker in `universe`.
            required=frozenset(range(2, 11)),
            degraded={
                "exp-005": frozenset({1, 2, 3, 5, 6, 8, 9, 10}),
                "exp-015": frozenset({1, 2, 3, 4, 5, 6, 8, 9, 10}),
                "exp-075": frozenset({2, 3, 4, 5, 7, 8, 10}),
                "exp-100": frozenset({2, 3, 4, 5, 7, 10}),
                "exp-150": frozenset({1, 3, 4, 5, 7, 10}),
                "exp-200": frozenset({2, 3, 4, 5, 7, 10}),
                "exp-300": frozenset({2, 3, 4, 5, 7, 10}),
                "gain-01": frozenset({1, 2, 3, 4, 5, 6, 8, 9, 10}),
                "gain-08": frozenset({1, 2, 3, 4, 5, 6, 8, 9, 10}),
            },
        ),
        "hd-usb-camera": CalibrationProfile(
            # Secondary playfield: 4 perimeter markers, no internal ones.
            universe=frozenset({1, 2, 3, 4}),
            required=frozenset({1, 2, 3, 4}),
            degraded={
                "exp-401": frozenset({2}),
                "exp-1001": frozenset({2}),
                "exp-2500": frozenset(),
                "exp-5000": frozenset(),
                "gain-5": frozenset(),
                "gain-15": frozenset(),
                "gain-30": frozenset(),
                "gain-50": frozenset(),
                "gain-80": frozenset(),
                "gain-100": frozenset(),
                "auto-exposure-1": frozenset(),
                "wb-2800": frozenset(),
                "wb-4650": frozenset(),
                "wb-6500": frozenset(),
                "auto-wb-true": frozenset(),
                "stress-highexp-logain": frozenset(),
            },
        ),
    }
}


def _profile_for(entry: FixtureEntry) -> Optional[CalibrationProfile]:
    return _CALIBRATION_PROFILES.get(entry.field_state_id, {}).get(entry.camera_slug)


def test_calibration_robustness(fixture_entry: FixtureEntry) -> None:
    frame = cv.imread(str(fixture_entry.image_path))
    assert frame is not None, f"{fixture_entry.fixture_id}: failed to read {fixture_entry.image_path}"

    gray, rescale = run_pipeline(frame, None)
    detector = _default_detector_factory(fixture_entry.camera_slug)
    detections = detector.detect(gray, scale=rescale.scale)
    detected_aruco: FrozenSet[int] = frozenset(
        detection.number for detection in detections if detection.family.value == "aruco"
    )

    profile = _profile_for(fixture_entry)
    if profile is not None:
        universe, required, degraded = profile.universe, profile.required, profile.degraded
    else:
        # Fail-loud default (module docstring): held to full recall of
        # this camera's own field-state-declared ArUco set.
        aruco_ids = frozenset(number for family, number in fixture_entry.expected_ids if family == "aruco")
        universe = required = aruco_ids
        degraded = {}

    if fixture_entry.config_label in degraded:
        expected = degraded[fixture_entry.config_label]
        actual = detected_aruco & universe
        assert actual == expected, (
            f"{fixture_entry.fixture_id}: documented calibration-robustness boundary "
            f"changed -- this config was measured seeing exactly {sorted(expected)} of "
            f"the {sorted(universe)} calibration markers, now sees {sorted(actual)} (both "
            "a silent regression and a silent improvement are visible here -- update the "
            "pinned constant in test_calibration_robustness.py if this is a genuine, "
            "confirmed detector change, not a bug)"
        )
    else:
        missing = required - detected_aruco
        assert not missing, (
            f"{fixture_entry.fixture_id}: this near-baseline config would NOT survive a "
            f"real calibration attempt -- missing required marker(s) {sorted(missing)} "
            f"(required={sorted(required)}, detected={sorted(detected_aruco)}, full "
            f"calibration-marker universe for this camera={sorted(universe)})"
        )
