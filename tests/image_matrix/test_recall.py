"""L0 tag-recall assertions (sprint 035 ticket 005; test plan §6 item 1).

For every confirmed fixture image, detected tag ids (AprilTag + ArUco,
via the daemon's own ``vision.pipeline.run_pipeline`` ->
``aprilcam.daemon.detection._default_detector_factory(slug).detect``
call pair -- never a second detection code path) must be a **superset**
of that camera's required ids -- except for the configs already known,
from real measurement, to cross a washout/dark-floor cliff, which instead
carry an **exact** expected-degraded constant so a silent improvement is
just as visible as a silent regression (this ticket's own acceptance
criterion, echoing test-plan §6 item 1's "the test *documents* the
failure boundary").

**Required ids are per camera, never a shared global list** (this
sprint's own explicit warning, confirmed live 2026-08-23 -- see
``docs/knowledge/daemon-camera-grant-and-detection-priming.md``'s
"Visibility is per-camera, not global" section): camera 2
(``hd-usb-camera``) sits on the *secondary* playfield and never sees an
AprilTag at all; camera 5 (``arducam-ov9281-usb-camera``) measurably
misses ArUco 1 on this field's far corner about 42% of the time (12
consecutive live polls, sprint 035 ticket 005's own handoff: ArUco 1
7/12, every other ArUco 2-10 12/12, AprilTag 1 11/12) -- so ArUco 1 is
deliberately excluded from camera 5's *required* set, and AprilTag 1
(not perfect, but present in every one of this fixture generation's own
near-baseline images -- see :data:`_RECALL_PROFILES`) is required only
for camera 3, never demanded at 100% for camera 5. AprilTags 10, 11, 15,
100 are stakeholder-confirmed transients (2026-08-23) and never appear in
any required set here -- baked into ``field-state.json`` itself, not
special-cased in this module.

**Earned, not guessed.** Every id in :data:`_RECALL_PROFILES` below was
read off a real detection pass (``vision.pipeline.run_pipeline`` +
``aprilcam.daemon.detection._default_detector_factory``) over every one
of ``fs-20260823``'s 55 committed JPEGs, the same call pair this module's
own tests make -- not authored from the exposure-cliff *theory* in
``docs/test-plans/camera-image-matrix.md`` §2-3 alone. A config's
"expected-degraded" set is exactly (measured detections) intersected
with (this camera's required-id universe) -- phantom/off-field hits
(e.g. a spurious ArUco 37 the detector reported on camera 3's darkest
exposure) are never part of any required-id universe in the first
place, so they are neither asserted for nor asserted against; only
recall of ids that actually belong to the surveyed field is measured
here.

**Scoped to ``fs-20260823`` only.** :data:`_RECALL_PROFILES` is keyed by
``(field_state_id, camera_slug)`` -- a fixture whose ``field_state_id``
does not match falls through to the conftest-provided, field-state-driven
:attr:`~tests.image_matrix.conftest.FixtureEntry.expected_ids` as a
required (superset) set instead of reusing a stale, generation-specific
profile. This is a deliberately "fail loud" default: an unclassified
config is held to the same 100%-recall bar a near-baseline config would
be, which is honest (it surfaces a genuine gap needing a human to add a
real expected-degraded constant, rather than silently under-testing a
brand-new fixture generation).

Every one of ``fs-20260823``'s ``field-state.json`` entries currently has
``status: "unverified"`` -- deliberately (this ticket's own acceptance
criterion). That means these curated profiles do not run yet;
:mod:`tests.image_matrix.conftest`'s confirmation guard skips every
fixture image from this generation until a human flips that status. This
module's own mutation check (falsify one expectation, confirm a readable
failure, revert -- recorded in this ticket's commit) was run against a
local copy of ``field-state.json`` with ``status`` temporarily forced to
``"confirmed"``, exactly to prove this machinery actually fires once that
flip happens for real.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, Optional, Tuple

import cv2 as cv

from aprilcam.daemon.detection import _default_detector_factory
from aprilcam.vision.pipeline import run_pipeline

from tests.image_matrix.conftest import FixtureEntry

__all__ = ["test_recall"]

_TagKey = Tuple[str, int]


@dataclass(frozen=True, kw_only=True)
class CameraRecallProfile:
    """One camera's recall contract for one field-state generation.

    ``required`` is the id universe every *near-baseline* config (any
    config label not present in ``degraded``) must detect a superset of.
    ``degraded`` maps a config label to the *exact* subset of ``required``
    that config is known to detect -- config labels absent from both this
    dict's keys and treated as near-baseline default to the strict
    ``required``-superset check.
    """

    required: FrozenSet[_TagKey]
    degraded: Dict[str, FrozenSet[_TagKey]]


def _ids(*, april: Tuple[int, ...] = (), aruco: Tuple[int, ...] = ()) -> FrozenSet[_TagKey]:
    return frozenset({("apriltag", n) for n in april} | {("aruco", n) for n in aruco})


#: Measured 2026-08-23 against every committed ``fs-20260823`` JPEG (module
#: docstring). Camera 3's washout cliff lands at exp-24/exp-40 (doc's
#: theorized "12-18" was close but not exact -- exp-16 still recalls
#: fully); camera 5's lands in the exp-075..exp-300 band, where AprilTag 1
#: and several ArUco corners drop out together; camera 2's washout is
#: near-total from exp-401 upward (matches its ``washed`` manifest flag).
_RECALL_PROFILES: Dict[str, Dict[str, CameraRecallProfile]] = {
    "fs-20260823": {
        "arducam-ov9782-usb-camera": CameraRecallProfile(
            required=_ids(april=(1,), aruco=(1, 2, 3, 4, 5, 6, 7, 8, 9, 10)),
            degraded={
                "exp-03": _ids(april=(1,), aruco=(2, 3, 5, 6, 8, 9, 10)),
                "exp-24": _ids(aruco=(2, 3, 4, 5, 6, 7, 10)),
                "exp-40": _ids(aruco=(2, 3, 4, 5, 10)),
                "gain-01": _ids(april=(1,), aruco=(1, 2, 3, 5, 6, 7, 8, 9, 10)),
                "wb-6500": _ids(april=(1,), aruco=(1, 2, 3, 5, 6, 7, 8, 9, 10)),
                "stress-lowexp-higain": _ids(april=(1,), aruco=(1, 2, 3, 5, 6, 8, 9, 10)),
                "stress-highexp-logain": _ids(aruco=(3, 4, 5, 10)),
            },
        ),
        "arducam-ov9281-usb-camera": CameraRecallProfile(
            # ArUco 1 and AprilTag 1 are deliberately excluded from
            # camera 5's required set (module docstring) -- required
            # holds only the 9 ArUco ids measured at 12/12 live recall.
            required=_ids(aruco=(2, 3, 4, 5, 6, 7, 8, 9, 10)),
            degraded={
                "exp-005": _ids(aruco=(2, 3, 5, 6, 8, 9, 10)),
                "exp-015": _ids(aruco=(2, 3, 4, 5, 6, 8, 9, 10)),
                "exp-075": _ids(aruco=(2, 3, 4, 5, 7, 8, 10)),
                "exp-100": _ids(aruco=(2, 3, 4, 5, 7, 10)),
                "exp-150": _ids(aruco=(3, 4, 5, 7, 10)),
                "exp-200": _ids(aruco=(2, 3, 4, 5, 7, 10)),
                "exp-300": _ids(aruco=(2, 3, 4, 5, 7, 10)),
                "gain-01": _ids(aruco=(2, 3, 4, 5, 6, 8, 9, 10)),
                "gain-08": _ids(aruco=(2, 3, 4, 5, 6, 8, 9, 10)),
            },
        ),
        "hd-usb-camera": CameraRecallProfile(
            # Secondary playfield, no AprilTag in view (module docstring).
            required=_ids(aruco=(1, 2, 3, 4)),
            degraded={
                "exp-401": _ids(aruco=(2,)),
                "exp-1001": _ids(aruco=(2,)),
                "exp-2500": _ids(),
                "exp-5000": _ids(),
                "gain-5": _ids(),
                "gain-15": _ids(),
                "gain-30": _ids(),
                "gain-50": _ids(),
                "gain-80": _ids(),
                "gain-100": _ids(),
                "auto-exposure-1": _ids(),
                "wb-2800": _ids(),
                "wb-4650": _ids(),
                "wb-6500": _ids(),
                "auto-wb-true": _ids(),
                "stress-highexp-logain": _ids(),
            },
        ),
    }
}


def _profile_for(entry: FixtureEntry) -> Optional[CameraRecallProfile]:
    return _RECALL_PROFILES.get(entry.field_state_id, {}).get(entry.camera_slug)


def test_recall(fixture_entry: FixtureEntry) -> None:
    frame = cv.imread(str(fixture_entry.image_path))
    assert frame is not None, f"{fixture_entry.fixture_id}: failed to read {fixture_entry.image_path}"

    gray, rescale = run_pipeline(frame, None)
    detector = _default_detector_factory(fixture_entry.camera_slug)
    detections = detector.detect(gray, scale=rescale.scale)
    detected_ids: FrozenSet[_TagKey] = frozenset(
        (detection.family.value, detection.number) for detection in detections
    )

    profile = _profile_for(fixture_entry)
    required = profile.required if profile is not None else fixture_entry.expected_ids
    degraded = profile.degraded if profile is not None else {}

    if fixture_entry.config_label in degraded:
        expected = degraded[fixture_entry.config_label]
        actual = detected_ids & required
        assert actual == expected, (
            f"{fixture_entry.fixture_id}: degraded-config recall changed -- expected "
            f"exactly {sorted(expected)}, detected {sorted(actual)} (both a silent "
            "regression and a silent improvement are visible here -- update the "
            "pinned constant in test_recall.py if this is a genuine, confirmed "
            "detector change, not a bug)"
        )
    else:
        missing = required - detected_ids
        assert not missing, (
            f"{fixture_entry.fixture_id}: missing required detection(s) {sorted(missing)} "
            f"(required={sorted(required)}, detected={sorted(detected_ids)})"
        )
