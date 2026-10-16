"""Fixture discovery for the L0 image-matrix suite (sprint 035 ticket 005).

Walks ``tests/images/**`` for every ``manifest.json`` a capture session
(``testdev/capture_matrix.py``) has written, and turns each manifest's
per-image entries into one :class:`FixtureEntry` apiece -- the shared unit
``test_recall.py``/``test_world_accuracy.py`` (and later tickets 006-007)
parametrize their assertions over.

**Two skip-cleanly guards, never a failure.**

1. *Fixtures absent.* No ``tests/images/`` directory, or one with no
   ``manifest.json`` anywhere under it (a bare checkout that never ran the
   capture tool, or a renamed/removed directory): :func:`discover_fixture_entries`
   returns zero entries, and :func:`pytest_generate_tests` below turns that
   into exactly one cleanly-skipped test item per test function, naming why.
   v1's system tests died silently when their corpus rotted (project
   knowledge); this suite is built to degrade to "skipped, with a reason"
   instead, by construction, the same way every ``@pytest.mark.camera``
   hardware module in this project already does for missing hardware
   (``tests/aprilcam/hardware_camera.py``).
2. *Field state not yet confirmed.* A manifest's ``field_state_id`` names an
   entry in ``tests/images/field-state.json`` whose ``status`` is anything
   other than ``"confirmed"`` (every entry that exists today, including the
   July ``unverified-20260729`` stub and the freshly-captured
   ``fs-20260823`` -- see that file's own description for why it is
   deliberately not confirmed yet). Every image under that manifest is
   excluded from the returned entries, never silently treated as ground
   truth. This is what makes it safe for :mod:`test_recall`/
   :mod:`test_world_accuracy` to assert real numbers keyed to
   ``fs-20260823`` (see their own module docstrings) without those
   assertions running against a corpus a human has not yet actually
   reviewed against the physical field.

**Detection goes through the daemon's own entry points, nowhere else in
this package.** :mod:`test_recall`/:mod:`test_world_accuracy` call
``vision.pipeline.run_pipeline`` then ``aprilcam.daemon.detection.
_default_detector_factory(slug).detect`` directly -- the same two calls
``daemon.detection.DetectionEngine.run_burst`` makes, including that
factory's per-camera ``DetectorConfig`` override table
(``arducam-ov9281-usb-camera``'s loosened AprilTag-quad constants, sprint
035 ticket 002). This module itself never imports ``vision``/``daemon`` --
discovery only reads JSON and checks paths, keeping the "no second
detection code path" boundary entirely inside the two test modules that
actually detect anything.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

import pytest

__all__ = [
    "REPO_ROOT",
    "IMAGES_ROOT",
    "FIELD_STATE_PATH",
    "PLAYFIELDS_DIR",
    "playfield_definition_path",
    "FixtureEntry",
    "DiscoveryResult",
    "discover_fixture_entries",
]

#: This conftest lives at ``tests/image_matrix/conftest.py`` -- two
#: ``.parent`` hops reaches the repo root, one reaches ``tests/``. Computed
#: from ``__file__`` rather than ``Path.cwd()`` so discovery is correct
#: regardless of what directory ``pytest`` is invoked from.
_THIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = _THIS_DIR.parent.parent
IMAGES_ROOT = _THIS_DIR.parent / "images"
FIELD_STATE_PATH = IMAGES_ROOT / "field-state.json"
PLAYFIELDS_DIR = REPO_ROOT / "config" / "aprilcam" / "playfields"


def playfield_definition_path(field_state_id: str, playfield: str) -> Path:
    """The playfield definition a fixture's world-coordinate checks must be
    judged against: the field-state snapshot
    ``tests/images/<field_state_id>/<playfield>.json`` when one exists, else
    the live ``config/aprilcam/playfields/<playfield>.json``.

    A recorded field state is a physical layout at capture time, and the
    live definition is free to move on (2026-10-16: main-playfield's internal
    pair 9/10 were re-surveyed east/west after the ``fs-20260823`` frames
    were captured with them the other way round). Freezing a copy beside the
    frames keeps world-accuracy fixtures judging against the field they
    actually show, without pinning the live definition to history.
    """
    snapshot = IMAGES_ROOT / field_state_id / f"{playfield}.json"
    return snapshot if snapshot.is_file() else PLAYFIELDS_DIR / f"{playfield}.json"


@dataclass(frozen=True, kw_only=True)
class FixtureEntry:
    """One captured image plus everything a test needs to judge it --
    the unit :func:`pytest_generate_tests` parametrizes every L0 test
    over.

    ``expected_ids`` is **per camera**, never a global/shared list
    (sprint 035 ticket 005's own explicit warning: camera 2 sits on a
    different, secondary playfield and sees no AprilTag at all) -- it is
    read from ``field-state.json``'s ``per_camera`` section for
    :attr:`camera_slug` when present, falling back to that field state's
    flat ``visible_*`` union arrays only when no ``per_camera`` entry
    exists for this camera (never actually exercised by ``fs-20260823``,
    whose ``per_camera`` section covers every camera in this fixture set,
    but kept for a future generation captured before its ``per_camera``
    breakdown is filled in).

    ``calibration_path`` is ``None`` whenever this image's camera had no
    pinned calibration snapshot at capture time, *or* the snapshot on disk
    no longer hashes to the manifest's own ``calibration_snapshot_sha256``
    -- either way, "nothing trustworthy to compute world coordinates
    from," which is exactly what world-accuracy tests need to skip on
    (never fail on).
    """

    camera_slug: str
    config_label: str
    field_state_id: str
    playfield: str
    image_path: Path
    manifest_path: Path
    calibration_path: Optional[Path]
    calibration_snapshot_sha256: Optional[str]
    expected_ids: FrozenSet[Tuple[str, int]]
    controls: Dict[str, Any]
    dark: bool
    washed: bool
    mean_luminance: Optional[float]

    @property
    def fixture_id(self) -> str:
        """A human-readable, stable identifier -- used as this entry's
        pytest parametrize id and in every assertion message, so a
        failure names exactly which fixture (generation / camera /
        config) it came from (ticket 005's own acceptance criterion:
        "a readable failure naming the fixture and the mismatch")."""
        return f"{self.field_state_id}/{self.camera_slug}/{self.config_label}"


@dataclass(frozen=True, kw_only=True)
class DiscoveryResult:
    """What one :func:`discover_fixture_entries` call found.

    ``skip_reason`` is always populated (even when *entries* is
    non-empty) so callers never need to re-derive it, but it is only
    ever surfaced to a human when *entries* is empty -- see
    :func:`pytest_generate_tests`.
    """

    entries: Tuple[FixtureEntry, ...]
    skip_reason: str


def _expected_ids(state: Dict[str, Any], camera_slug: str) -> FrozenSet[Tuple[str, int]]:
    """The (family, number) pairs *camera_slug* is expected to see,
    per *state* (one ``field-state.json`` ``states[...]`` entry, already
    confirmed by the caller). Prefers ``state["per_camera"][camera_slug]``;
    falls back to *state*'s own flat ``visible_*`` arrays when this camera
    has no per-camera breakdown -- see :class:`FixtureEntry`'s docstring
    for why the fallback exists and when it does (and does not) fire.
    """
    per_camera = state.get("per_camera") or {}
    camera_state = per_camera.get(camera_slug)
    source = camera_state if camera_state is not None else state
    april = source.get("visible_april_tags") or []
    aruco = source.get("visible_aruco_tags") or []
    return frozenset(
        {("apriltag", int(n)) for n in april} | {("aruco", int(n)) for n in aruco}
    )


def _resolve_calibration_path(manifest_dir: Path, manifest: Dict[str, Any]) -> Optional[Path]:
    """*manifest_dir* / ``calibration.json`` if the manifest actually pins
    one (``calibration_snapshot_sha256`` set), the file exists, and it
    still hashes to that pinned value -- ``None`` otherwise (no
    calibration at capture time, per §6.2 of
    ``docs/test-plans/camera-image-matrix.md``, or the on-disk file no
    longer matches what was pinned). Never raises: an unreadable file
    degrades to "no calibration," matching this whole package's
    skip-cleanly stance.
    """
    expected_sha = manifest.get("calibration_snapshot_sha256")
    if not expected_sha:
        return None
    path = manifest_dir / "calibration.json"
    try:
        actual_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None
    if actual_sha != expected_sha:
        return None
    return path


def discover_fixture_entries() -> DiscoveryResult:
    """Walk :data:`IMAGES_ROOT` for every ``manifest.json`` (``rglob``,
    ticket 005's own discovery contract) and build one :class:`FixtureEntry`
    per image whose manifest's ``field_state_id`` resolves to a
    ``field-state.json`` entry with ``status == "confirmed"``.

    Returns an empty :attr:`DiscoveryResult.entries` -- never raises --
    for every reason this package treats as "nothing to test yet":
    :data:`IMAGES_ROOT` missing, no manifests under it, a manifest whose
    ``field_state_id`` is unknown or unconfirmed, or (the common case
    today) every discovered manifest falling into one of those buckets.
    :attr:`DiscoveryResult.skip_reason` explains which, for
    :func:`pytest_generate_tests` to surface.
    """
    if not IMAGES_ROOT.is_dir():
        return DiscoveryResult(
            entries=(),
            skip_reason=(
                f"fixture directory {IMAGES_ROOT} does not exist -- the L0 "
                "image-matrix suite needs tests/images/ (captured via "
                "'python testdev/capture_matrix.py <camera>'; see "
                "docs/test-plans/camera-image-matrix.md); skipping cleanly "
                "rather than failing"
            ),
        )

    manifest_paths = sorted(IMAGES_ROOT.rglob("manifest.json"))
    if not manifest_paths:
        return DiscoveryResult(
            entries=(),
            skip_reason=f"no manifest.json found under {IMAGES_ROOT} -- nothing to test",
        )

    field_state_doc: Optional[Dict[str, Any]] = None
    if FIELD_STATE_PATH.is_file():
        try:
            field_state_doc = json.loads(FIELD_STATE_PATH.read_text())
        except (OSError, json.JSONDecodeError):
            field_state_doc = None

    entries: List[FixtureEntry] = []
    notes: List[str] = []

    for manifest_path in manifest_paths:
        manifest_dir = manifest_path.parent
        camera_slug = manifest_dir.name
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            notes.append(f"{manifest_path}: unreadable/malformed manifest ({exc})")
            continue

        field_state_id = manifest.get("field_state_id")
        states = (field_state_doc or {}).get("states") or {}
        state = states.get(field_state_id) if field_state_id else None

        if state is None:
            notes.append(
                f"{manifest_path}: field_state_id {field_state_id!r} not found in "
                f"{FIELD_STATE_PATH}"
            )
            continue

        status = state.get("status")
        if status != "confirmed":
            notes.append(
                f"{manifest_path}: field state {field_state_id!r} has status "
                f"{status!r}, not 'confirmed' -- the stakeholder has not yet "
                "verified what was physically on the field for this capture "
                "session, so its images are excluded rather than treated as "
                "ground truth"
            )
            continue

        expected = _expected_ids(state, camera_slug)
        calibration_path = _resolve_calibration_path(manifest_dir, manifest)
        playfield = str(manifest.get("playfield", ""))

        for image in manifest.get("images", []):
            file_name = image.get("file")
            if not file_name:
                continue
            image_path = manifest_dir / file_name
            if not image_path.is_file():
                notes.append(f"{image_path}: referenced by manifest but missing on disk")
                continue
            entries.append(
                FixtureEntry(
                    camera_slug=camera_slug,
                    config_label=str(image.get("name", file_name)),
                    field_state_id=str(field_state_id),
                    playfield=playfield,
                    image_path=image_path,
                    manifest_path=manifest_path,
                    calibration_path=calibration_path,
                    calibration_snapshot_sha256=manifest.get("calibration_snapshot_sha256"),
                    expected_ids=expected,
                    controls=dict(image.get("controls") or {}),
                    dark=bool(image.get("dark", False)),
                    washed=bool(image.get("washed", False)),
                    mean_luminance=image.get("mean_luminance"),
                )
            )

    if not entries:
        reason = "; ".join(notes) if notes else "no confirmed fixture manifests found"
        return DiscoveryResult(
            entries=(),
            skip_reason=(
                f"no image-matrix fixtures are ready to test under {IMAGES_ROOT} -- {reason}"
            ),
        )

    return DiscoveryResult(entries=tuple(entries), skip_reason="")


def pytest_generate_tests(metafunc: "pytest.Metafunc") -> None:
    """Parametrize any test function in this package that takes a
    ``fixture_entry`` argument over :func:`discover_fixture_entries`'s
    result -- one test item per confirmed fixture image, or, when none
    exist, exactly one cleanly-skipped item naming why (never a failure
    or an error: ticket 005's "renaming tests/images/ skips every L0
    test in this module cleanly" acceptance criterion).

    Deliberately not a ``@pytest.fixture`` -- ``metafunc.parametrize``
    supplies ``fixture_entry`` directly, which is what lets the skip case
    attach ``pytest.mark.skip`` to the *parameter* rather than needing a
    fixture body that itself calls ``pytest.skip()`` (a fixture-level
    skip still reports as a skipped test, but this way the reason is
    visible directly on the single collected item's id/report, matching
    every other dynamically-parametrized suite in this project's own
    conventions).
    """
    if "fixture_entry" not in metafunc.fixturenames:
        return

    result = discover_fixture_entries()
    if not result.entries:
        metafunc.parametrize(
            "fixture_entry",
            [pytest.param(None, marks=pytest.mark.skip(reason=result.skip_reason), id="no-fixtures")],
        )
        return

    metafunc.parametrize(
        "fixture_entry",
        list(result.entries),
        ids=[entry.fixture_id for entry in result.entries],
    )
