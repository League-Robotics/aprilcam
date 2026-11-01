"""``aprilcam camera locate <camera> --reference ...`` (sprint 029 ticket
003, SUC-003, UC-29).

Solves a camera's ground nadir and optical height from 2+ elevated
AprilTag references at surveyed playfield positions, then -- quality
gated -- writes the result into the camera's ``camera_position`` config
field, on disk and live. Mirrors ``cli/calibrate.py``'s exact shape:
its own module, grouped under ``camera``, wired in by
``cli/camera.py``'s :func:`add_subparser` alongside
``calibrate_cli.add_subparser(sub)``.

**A pure RPC/orchestration shell, with one deliberate exception.** Every
other command in this package touches no vision and does no geometry of
its own -- it triggers an RPC and formats whatever comes back
(``adapters.md``'s "adapters hold no authoritative state, adapters do no
vision" rule). This module is almost that: detection -- the one
pixel-touching step -- happens entirely inside the daemon via ``GetTag``,
exactly like every other command here. The one exception, spelled out
explicitly rather than left implicit, is that the least-squares *solve*
itself (:func:`~aprilcam.calibration.geometry.solve_camera_geometry`,
ticket 002) runs **here, client-side** -- it is pure algebra over
already-vision-processed ``WorldPoint``\\ s returned by ``GetTag``, no
cv2, no pixel access, no camera. See sprint 029's sprint.md Design
Rationale ("client-side pure function, not a new ``LocateCamera`` RPC")
for the full reasoning behind keeping this off the daemon/proto surface
entirely. This is also why ``cli/`` gains its first-ever dependency on
``calibration/`` right here, imported directly
(``from aprilcam.calibration.geometry import ...``) and nothing from
``cv2`` -- this module does no pixel work in practice, matching every
other CLI module's existing discipline, even though ``cli/`` itself is
not in ``tests/test_no_cv2_in_client_modules.py``'s enforced
``_CV2_FREE_PACKAGES`` set.

**Quality-gated apply (sprint.md Design Rationale, "defaults to applying
... but refuses without ``--force`` when the fit is flagged as poor").**
A good solve is written automatically, matching ``calibrate``'s own
"store on success" precedent and the stakeholder's plain framing
("solves ... then writes the result"). But unlike ``Calibrate``'s crisp
all-or-nothing ``missing_ids`` gate, a poor solve here still *looks*
plausible -- exactly the failure sprint 029 hit once already (~20 cm off,
undetected by eye until compared against ground truth; see the module
constants below for the concrete numbers). ``--force`` overrides the
refusal; ``--dry-run`` reports without ever writing, regardless of fit
quality.
"""

from __future__ import annotations

import argparse
import dataclasses
import math
import json
import os
import sys
from pathlib import Path
from typing import Callable, List

from aprilcam.calibration.geometry import ReferenceObservation, solve_camera_geometry
from aprilcam.cli._common import (
    EXIT_ERROR,
    EXIT_OK,
    add_all_option,
    connect_for_camera,
    report_error,
)
from aprilcam.client.discovery import Discovery
from aprilcam.config.directories import Directories as SystemDirectories
from aprilcam.config.interfaces import CameraPosition, ConfigLoader, Directories
from aprilcam.config.loader import ConfigLoader as JsonConfigLoader
from aprilcam.errors import AprilCamError, NoPlayfieldLinked
from aprilcam.types import TagFamily, TagId, WorldPoint

__all__ = [
    "add_subparser",
    "cmd_locate",
    "RMS_RESIDUAL_THRESHOLD_CM",
    "CONDITION_NUMBER_THRESHOLD",
]

#: Provisional quality-gate threshold (ticket 029-003) -- tunable, same
#: "evidence-based, revisit if real data disagrees" posture this
#: codebase already applies to ``CONFIRMATION_STREAK``
#: (``daemon/detection.py``). Stated directly by sprint.md's Design
#: Rationale: "rms_residual_cm > 1.0 ... triggers the poor fit, refuse
#: without --force path". The real fixture's own regression rms is
#: ~0.16 cm -- more than 6x below this cutoff -- so a well-placed pair
#: of references clears it comfortably; ticket 004's real-hardware pass
#: is the first data point to sanity-check this default against.
RMS_RESIDUAL_THRESHOLD_CM = 1.0

#: Provisional quality-gate threshold (ticket 029-003) -- same tunable
#: posture as above. Picked by comparing ticket 002's own two
#: conditioning tests in ``tests/aprilcam/calibration/test_geometry.py``:
#: ``test_solve_camera_geometry_well_conditioned_three_reference_baseline``
#: asserts ``condition_number < 200.0`` (the real fixture itself scores
#: ~67.4); ``test_solve_camera_geometry_near_collinear_references_condition_number_blows_up``
#: asserts ``condition_number > 1000.0``. Condition numbers vary
#: multiplicatively, not additively, so this cutoff sits at their
#: *geometric* mean -- ``sqrt(200.0 * 1000.0) ~= 447.2`` -- meaningfully
#: between the two rather than an arbitrary round number.
CONDITION_NUMBER_THRESHOLD = 447.2


@dataclasses.dataclass(frozen=True)
class _ReferenceSpec:
    """One parsed ``--reference TAG:SLUG:HEIGHT_CM`` argument, before any
    RPC resolves *slug* to a surveyed position or *tag* to a detection."""

    tag: int
    slug: str
    height_cm: float


def _parse_reference(raw: str) -> _ReferenceSpec:
    """Parse one ``--reference`` value. Raises ``ValueError`` naming
    exactly what was wrong with *raw* -- caught by :func:`cmd_locate`
    and reported before any connection is attempted, matching
    UC-29.S-3's "CLI-level argument error" discipline and ``cmd_image``'s
    "error before capture" precedent."""
    parts = raw.split(":")
    if len(parts) != 3:
        raise ValueError(
            f"--reference {raw!r} must be TAG:SLUG:HEIGHT_CM (exactly two ':' "
            f"separators), got {len(parts) - 1}"
        )
    tag_text, slug, height_text = parts

    try:
        tag = int(tag_text)
    except ValueError as exc:
        raise ValueError(
            f"--reference {raw!r}: TAG must be an integer AprilTag id, got {tag_text!r}"
        ) from exc

    if not slug:
        raise ValueError(f"--reference {raw!r}: SLUG must not be empty")

    try:
        height_cm = float(height_text)
    except ValueError as exc:
        raise ValueError(
            f"--reference {raw!r}: HEIGHT_CM must be a number, got {height_text!r}"
        ) from exc
    if height_cm <= 0:
        raise ValueError(
            f"--reference {raw!r}: HEIGHT_CM must be a positive stand height, got {height_cm}"
        )

    return _ReferenceSpec(tag=tag, slug=slug, height_cm=height_cm)


def _write_camera_config_atomic(path: Path, data: dict) -> None:
    """Write *data* (a ``CameraConfig.to_dict()`` payload) to *path*,
    atomically -- tmp + fsync + rename, ``docs/design/design.md`` §6's
    convention for every shared JSON file. Mirrors
    ``camera/registry.py``'s ``_save`` and ``daemon/service.py``'s
    calibration-result write exactly: a crash at any point before the
    ``os.replace`` call leaves the prior file untouched.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OSError(f"cannot create camera config directory {path.parent}: {exc}") from exc

    text = json.dumps(data, indent=2, sort_keys=True) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        raise OSError(f"cannot write camera config {path}: {exc}") from exc
    finally:
        if tmp.exists():
            tmp.unlink()


def cmd_locate(
    args: argparse.Namespace,
    *,
    discovery_factory: Callable[[], Discovery] = Discovery,
    loader_factory: Callable[[], ConfigLoader] = JsonConfigLoader,
    directories_factory: Callable[[], Directories] = SystemDirectories,
) -> int:
    """``aprilcam camera locate <camera> --reference TAG:SLUG:HEIGHT_CM
    [--reference ...] [--dry-run] [--force]``.

    See the module docstring for the overall design; see sprint 029's
    sprint.md Main Flow (SUC-003) for the numbered steps this function
    implements in order: single-target refusal, reference parsing,
    playfield-link precondition, exact-slug resolution, the
    registered-mount-z contamination guard, per-reference detection
    (fail fast), the solve, reporting, and the quality-gated apply.
    """
    if args.all:
        print(
            "error: 'camera locate' is single-target -- solving one camera's "
            "geometry from references observed by another daemon has no "
            "sensible meaning. Drop --all and name one camera.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    specs: List[_ReferenceSpec] = []
    for raw in args.reference or []:
        try:
            specs.append(_parse_reference(raw))
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_ERROR

    if len(specs) < 2:
        print(
            f"error: 'camera locate' requires at least 2 --reference args, got "
            f"{len(specs)} -- pass --reference TAG:SLUG:HEIGHT_CM at least twice",
            file=sys.stderr,
        )
        return EXIT_ERROR
    if len(specs) == 2:
        print(
            "advisory: only 2 references given -- 3+ is recommended for "
            "redundancy (not blocking; add more --reference args to improve "
            "the fit's conditioning)",
            file=sys.stderr,
        )

    try:
        daemon, args.camera = connect_for_camera(discovery_factory(), args.daemon, args.camera)
        config = daemon.get_camera_config(args.camera)
    except AprilCamError as exc:
        return report_error(exc)

    if config.playfield is None:
        return report_error(NoPlayfieldLinked(config.name))

    try:
        playfield = daemon.get_playfield(config.playfield)
    except AprilCamError as exc:
        return report_error(exc)

    # Exact-slug lookup into the surveyed definition -- deliberately not
    # the fuzzy FindFeature/FeatureQuery matcher (playfield/query.py): an
    # operator's exact-slug typo must be a hard, specific error, never a
    # near-miss fuzzy match (module docstring, sprint.md Main Flow #3).
    features_by_slug = {feature.slug: feature for feature in playfield.definition.features()}
    surveyed_by_slug: dict[str, WorldPoint] = {}
    for spec in specs:
        feature = features_by_slug.get(spec.slug)
        if feature is None:
            print(
                f"error: feature slug {spec.slug!r} was not found on playfield "
                f"{config.playfield!r} -- check for a typo (exact match required, "
                "no fuzzy lookup)",
                file=sys.stderr,
            )
            return EXIT_ERROR
        surveyed_by_slug[spec.slug] = feature.position

    # Contamination guard: a reference tag currently registered with a
    # *positive* mount_z would have its own (possibly stale) parallax
    # correction baked into GetTag's reported position, corrupting the
    # raw reading this solve needs. mount_z <= 0 is
    # project_tag_to_field_plane's own "nothing to correct" no-op
    # sentinel (calibration/geometry.py) -- identical to "unregistered"
    # for this purpose, so only mount_z > 0 is refused.
    try:
        # Values are MountRegistration (ticket 030-004, SUC-004) -- this
        # guard only needs .params.mount_z, never the paired
        # mount_z_applied flag.
        mount_registrations = daemon.list_tag_parameters()
    except AprilCamError as exc:
        return report_error(exc)

    for spec in specs:
        registration = mount_registrations.get(TagId(TagFamily.APRILTAG, spec.tag))
        params = registration.params if registration is not None else None
        if params is not None and params.mount_z > 0.0:
            print(
                f"error: reference tag {spec.tag} (AprilTag) is currently "
                f"registered with mount_z={params.mount_z} cm -- that "
                "registration's own (possibly stale) parallax correction "
                "would corrupt the raw reading this solve needs. Unregister "
                "it, or wait until it is re-registered with mount_z=0, "
                "before running 'camera locate'.",
                file=sys.stderr,
            )
            return EXIT_ERROR

    # Detection: the only pixel-touching step, and it happens entirely
    # inside the daemon (GetTag). Fail fast on the first reference not
    # seen -- never a partial solve over whatever was seen.
    observations = []
    for spec in specs:
        try:
            record = daemon.get_tag(args.camera, TagId(TagFamily.APRILTAG, spec.tag))
        except AprilCamError as exc:
            return report_error(exc)
        if record is None or record.world is None:
            print(
                f"error: reference tag {spec.tag} (AprilTag) was not detected "
                f"by {args.camera!r} -- check it is staged, in view, and the "
                "camera is calibrated, then retry",
                file=sys.stderr,
            )
            return EXIT_ERROR
        observations.append(
            ReferenceObservation(
                surveyed=surveyed_by_slug[spec.slug],
                observed=record.world,
                height_cm=spec.height_cm,
            )
        )

    solution = solve_camera_geometry(observations)

    print(
        f"solved: nadir=({solution.nadir.x:.3f}, {solution.nadir.y:.3f}) cm  "
        f"height={solution.height_cm:.3f} cm  "
        f"rms_residual_cm={solution.rms_residual_cm:.4f}  "
        f"condition_number={solution.condition_number:.2f}"
    )

    _print_pose_comparison(daemon, args.camera, solution)

    poor_fit = (
        solution.rms_residual_cm > RMS_RESIDUAL_THRESHOLD_CM
        or solution.condition_number > CONDITION_NUMBER_THRESHOLD
    )
    if poor_fit:
        print(
            f"warning: poor fit -- rms_residual_cm={solution.rms_residual_cm:.4f} "
            f"(threshold {RMS_RESIDUAL_THRESHOLD_CM}) or "
            f"condition_number={solution.condition_number:.2f} "
            f"(threshold {CONDITION_NUMBER_THRESHOLD}) exceeded. The supplied "
            "references may be too near-collinear with the estimated nadir to "
            "trust -- this is the exact failure that produced a ~20 cm error "
            "during sprint 029's own investigation: references placed "
            "diametrically opposite the camera are the trap. Spread references "
            "further apart in angle, or pass --force to write this solve anyway.",
            file=sys.stderr,
        )

    if args.dry_run:
        print("dry-run: reporting only -- nothing written")
        return EXIT_OK

    if poor_fit and not args.force:
        print(
            "error: refusing to write a poor-fit solve without --force -- "
            f"rms_residual_cm={solution.rms_residual_cm:.4f}, "
            f"condition_number={solution.condition_number:.2f}. Pass --force "
            "to write it anyway, or re-run with better-placed references; "
            "--dry-run reports without ever writing.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    directories = directories_factory()
    config_path = directories.config_dir / "cameras" / config.name / "config.json"
    loader = loader_factory()
    try:
        on_disk = loader.load_camera_config(config_path)
    except AprilCamError as exc:
        return report_error(exc)

    updated = dataclasses.replace(
        on_disk,
        camera_position=CameraPosition(nadir=solution.nadir, height_cm=solution.height_cm),
    )

    try:
        _write_camera_config_atomic(config_path, updated.to_dict())
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    try:
        daemon.set_camera_config(updated)
    except AprilCamError as exc:
        print(
            f"error: wrote {config_path} but failed to push it live: {exc} -- "
            f"retry with 'aprilcam camera config {config_path}' once the "
            "daemon is reachable, or restart it",
            file=sys.stderr,
        )
        return EXIT_ERROR

    print(
        f"wrote camera_position to {config_path} and pushed it live -- "
        "no daemon restart needed"
    )
    return EXIT_OK


#: Pose-vs-solved disagreement (cm) above which ``camera locate`` prints
#: an informational comparison (sprint 045 ticket 009, Open Question 4).
POSE_COMPARISON_THRESHOLD_CM = 5.0


def _print_pose_comparison(daemon, camera: str, solution) -> None:
    """Informational only: when the camera also holds a fresh pose
    calibration (``Calibration.camera_geometry()``), print how its
    camera position compares with this solve if they differ by more
    than :data:`POSE_COMPARISON_THRESHOLD_CM` in nadir or height. A
    fresh pose calibration takes precedence over the configured
    ``camera_position`` for parallax, so the operator should know when
    the two disagree. Never affects the solve, the write, or the exit
    code -- any failure fetching the calibration is silently skipped."""
    try:
        calibration = daemon.get_calibration(camera)
    except AprilCamError:
        return
    pose = calibration.camera_geometry() if calibration is not None else None
    if pose is None:
        return
    nadir, height_cm = pose
    nadir_delta = math.hypot(nadir.x - solution.nadir.x, nadir.y - solution.nadir.y)
    height_delta = abs(height_cm - solution.height_cm)
    if max(nadir_delta, height_delta) <= POSE_COMPARISON_THRESHOLD_CM:
        return
    print(
        f"note: pose calibration places the camera at nadir=({nadir.x:.3f}, "
        f"{nadir.y:.3f}) cm height={height_cm:.3f} cm; this solve differs by "
        f"{nadir_delta:.1f} cm (nadir) / {height_delta:.1f} cm (height). A "
        "fresh pose calibration takes precedence over the configured "
        "camera_position for parallax."
    )


def add_subparser(sub: "argparse._SubParsersAction") -> None:
    """Wire ``locate`` into *sub* -- ``cli/camera.py``'s own ``camera``
    subparsers action (**not** the top-level ``aprilcam`` one), exactly
    like ``cli/calibrate.py``'s :func:`~aprilcam.cli.calibrate.add_subparser`.
    """
    p_locate = sub.add_parser(
        "locate",
        help="solve a camera's ground nadir and optical height from surveyed "
        "elevated AprilTag references (sprint 029, UC-29)",
    )
    p_locate.add_argument("camera", help="camera name (slug or persistent number)")
    p_locate.add_argument(
        "--reference",
        action="append",
        metavar="TAG:SLUG:HEIGHT_CM",
        default=None,
        help="an AprilTag reference: numeric AprilTag id, playfield feature slug "
        "(exact match, e.g. dot-northeast-orange), and its physical stand height "
        "in cm, colon-separated (e.g. 15:dot-northeast-orange:17.2). Repeatable; "
        "at least 2 required, 3+ recommended for redundancy.",
    )
    add_all_option(p_locate, note=" (refused -- locate is single-target)")
    p_locate.add_argument(
        "--dry-run",
        action="store_true",
        help="report the solved nadir/height/RMS without writing it anywhere, "
        "regardless of fit quality",
    )
    p_locate.add_argument(
        "--force",
        action="store_true",
        help="write the solved camera_position even if the fit is poor "
        f"(rms_residual_cm > {RMS_RESIDUAL_THRESHOLD_CM} or condition_number > "
        f"{CONDITION_NUMBER_THRESHOLD} -- ticket 029-003's provisional, "
        "tunable thresholds)",
    )
    p_locate.set_defaults(func=cmd_locate)
