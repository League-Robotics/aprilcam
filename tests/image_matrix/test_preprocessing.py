"""L0 preprocessing-effectiveness assertions (sprint 035 ticket 006;
test plan §6 item 6). **Informational only -- never blocks the L0
gate on its own**, per this ticket's own acceptance criterion.

For every confirmed fixture image on the **exposure sweep** (test plan
§6.5's designated stress axis for this family -- see
:func:`_is_exposure_sweep_config`), detection is run twice through the
same ``vision.pipeline.run_pipeline`` ->
``aprilcam.daemon.detection._default_detector_factory(slug).detect``
call pair every other L0 module uses -- once with
``vision.pipeline.PipelineConfig.use_highpass=True`` (the production
default), once with it forced ``False`` -- and the ArUco/AprilTag
recall difference is printed, never asserted against. This is
deliberate: the whole point of this family is to let a future
preprocessing change (a different highpass kernel, a new stage, a
default flip) show its *real* effect on real fixtures, which a hard
pass/fail bound tied to "highpass must help" would instead silently
suppress the very signal this test exists to surface (a highpass win
on one exposure and a wash on another are both legitimate outcomes,
not failures).

**"Compared... and reported" (this ticket's own wording), concretely.**
Every parametrized case prints one line, in the same
``print(f"\\n[informational only -- ...")`` style already established
by this project's real-hardware informational assertions
(``tests/aprilcam/daemon/test_detection_hardware.py``,
``test_streaming_hardware.py``) -- visible with ``pytest -s`` or in a
failure's captured-output section, never gating the run on its own.
The one real assertion this module makes is a structural sanity check
(detected ids are plausible dictionary indices) that cannot fail from
any genuine highpass-on/off recall difference -- it exists only to
catch an actual pipeline crash or a family/id mixup, never to grade
preprocessing effectiveness.

**Detection goes through the daemon's own entry points, same as every
other L0 module.** The only deviation from :mod:`test_recall`'s call
shape is passing an explicit ``PipelineConfig(use_highpass=...)`` into
``run_pipeline`` instead of ``None`` (``run_pipeline``'s own default) --
the exact seam ``vision/DESIGN.md`` §5 documents
(``PipelineConfig.use_highpass``), never a parallel preprocessing
reimplementation.
"""

from __future__ import annotations

from pathlib import Path
from typing import FrozenSet, Tuple

import cv2 as cv
import pytest

from aprilcam.daemon.detection import _default_detector_factory
from aprilcam.vision.pipeline import PipelineConfig, run_pipeline

from tests.image_matrix.conftest import FixtureEntry

__all__ = ["test_preprocessing_highpass_effect"]


def _is_exposure_sweep_config(config_label: str) -> bool:
    """True for ``baseline`` (the sweep's own zero point) and every
    ``exp-*`` config label -- test plan §6.5's designated stress axis
    for this assertion family, deliberately narrower than "every
    fixture" (gain/white-balance/auto-mode/stress configs are each
    other families' own stress axes, per §6.5's table)."""
    return config_label == "baseline" or config_label.startswith("exp-")


def _detect_ids(
    image_path: Path, camera_slug: str, *, use_highpass: bool
) -> Tuple[FrozenSet[int], FrozenSet[int]]:
    """``(aruco_ids, april_ids)`` detected in *image_path* with the
    highpass illumination-flattening stage forced on or off -- the same
    ``run_pipeline`` -> ``_default_detector_factory(slug).detect`` call
    pair every other L0 module makes, differing only in the explicit
    ``PipelineConfig``."""
    frame = cv.imread(str(image_path))
    assert frame is not None, f"failed to read {image_path}"

    config = PipelineConfig(use_highpass=use_highpass)
    gray, rescale = run_pipeline(frame, config)
    detector = _default_detector_factory(camera_slug)
    detections = detector.detect(gray, scale=rescale.scale)

    aruco = frozenset(d.number for d in detections if d.family.value == "aruco")
    april = frozenset(d.number for d in detections if d.family.value == "apriltag")
    return aruco, april


def test_preprocessing_highpass_effect(fixture_entry: FixtureEntry) -> None:
    if not _is_exposure_sweep_config(fixture_entry.config_label):
        pytest.skip(
            f"{fixture_entry.fixture_id}: not on the exposure sweep -- preprocessing "
            "effectiveness is compared specifically across exposure (test plan §6.5's "
            "designated stress axis for this family), not every config axis"
        )

    aruco_on, april_on = _detect_ids(fixture_entry.image_path, fixture_entry.camera_slug, use_highpass=True)
    aruco_off, april_off = _detect_ids(fixture_entry.image_path, fixture_entry.camera_slug, use_highpass=False)

    total_on = len(aruco_on) + len(april_on)
    total_off = len(aruco_off) + len(april_off)
    print(
        f"\n[informational only -- preprocessing effectiveness, never gates the L0 suite] "
        f"{fixture_entry.fixture_id}: highpass ON -> aruco={sorted(aruco_on)} "
        f"april={sorted(april_on)} ({total_on} total); highpass OFF -> "
        f"aruco={sorted(aruco_off)} april={sorted(april_off)} ({total_off} total); "
        f"delta(on-off)={total_on - total_off:+d}"
    )

    # Structural sanity only -- every detected id is a plausible dictionary
    # index regardless of which pipeline variant produced it. This can
    # never fail from a genuine highpass-on/off recall difference; it
    # exists solely to catch an actual pipeline crash or a family/id mixup.
    all_ids = aruco_on | aruco_off | april_on | april_off
    assert all(0 <= number < 1000 for number in all_ids), (
        f"{fixture_entry.fixture_id}: implausible detected id(s) in {sorted(all_ids)} -- "
        "likely a family/id mixup, not a preprocessing effect"
    )
