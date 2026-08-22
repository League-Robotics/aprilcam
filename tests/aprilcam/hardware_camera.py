"""Shared real-camera discovery for ``@pytest.mark.camera`` hardware
suites (out-of-process fix, sprint 031 branch).

**The problem this replaces.** Eight hardware test modules each declared
their own module-level ``ARDUCAM_SLUG = "arducam-ov9782-usb-camera"``
constant and asserted against that one specific, hardcoded device name.
``tests/aprilcam/cli/test_cli_integration.py`` was fixed the same way
first (commit ``dfdac13c``): discover what is actually attached at run
time via :func:`aprilcam.camera.identity.resolve_all`, never hardcode a
device name, since camera hardware varies by machine and a specific
camera can also be genuinely busy in another concurrent process on the
*same* machine at any given moment (both true of this project's dev
environment: multiple cameras are attached, and the historical arducam
slug is not guaranteed free).

**Why "any attached camera" is not enough here, unlike the CLI test.**
The CLI round-trip test only needed "some camera is attached" -- it never
inspects tag ids, calibration results, or geometry. Every module this
helper serves does: they assert on marker counts, specific tag ids,
calibration success, and (``test_camera_geometry_hardware.py``) a
specific solved camera position, all of which only mean something for a
camera that is physically aimed at the surveyed playfield those
assertions describe. Picking an arbitrary attached camera (a laptop
webcam pointed at a wall, say) would make every one of those assertions
either fail or pass vacuously -- neither of which is the point of a
hardware test. So this module answers a narrower question than
``resolve_all()`` alone can: *which* attached camera is the one actually
mounted on the rig.

**How "the one actually mounted on the rig" is decided.** Mirrors
``cli/_common.py``'s :func:`resolve_playfield_camera` -- "read the link
from real, persisted state, never guess" -- generalized from "linked to
*a* playfield" (that function's job, inside one already-running daemon)
to "linked *and* calibrated against *this* specific playfield, on this
machine's own real system directories" (this module's job, with no
daemon running at all yet in most of these tests' own isolated fixtures).
Concretely: for every camera :func:`~aprilcam.camera.identity.resolve_all`
reports as currently attached, this checks the **real** system
``Directories()`` (never a test's own ``tmp_path`` override -- the
physical rig's config was set up by hand, once, outside any test) for

1. ``config_dir/cameras/<slug>/config.json`` whose ``"playfield"`` field
   equals the playfield being asked for, and
2. ``state_dir/cameras/<slug>/calibration.json`` existing at all.

Both together is the same "linked and it has actually been solved before"
signal ``camera list``'s own ``playfield=X ... calibrated`` row shows for
a real, working camera (``daemon/service.py``'s ``_playfield_link``/
``_calibration_flags`` -- calibration and the playfield link are tracked
independently there too, which is why both checks are needed: a stale,
never-relinked calibration and an un-calibrated fresh link are each
individually insufficient). Confirmed against this exact machine while
building this fix: ``arducam-ov9782-usb-camera`` carries
``playfield: main-playfield`` plus a real, camera-position-populated
``config.json`` and a real ``calibration.json``; a second, differently-
named arducam (``arducam-ov9281-usb-camera``) is attached and even
independently ``calibrated``, but carries no playfield link at all --
exactly the false-positive this two-part check exists to reject. (An
earlier, uncommitted stopgap in this working tree had simply swapped the
hardcoded slug from the first name to the second, which -- as this
confirms -- would have pointed every one of these eight modules' real
hardware assertions at the *wrong* camera.)

**If more than one attached camera qualifies: prefer the one that is
actually free right now, not just the first by sort order.** Two rigs on
the same dev machine can both legitimately end up configured and
calibrated against the same playfield (this project's own dev box has
exactly this: ``arducam-ov9782-usb-camera`` and, once linked the same
way, ``arducam-ov9281-usb-camera``) -- and one of them can be genuinely
busy in another concurrent process at any given moment (this project's
dev box again: a second checkout's own long-running ``aprilcam daemon``/
``aprilcam view`` session). Picking "sort-order-first" (or any other
fixed tiebreak that ignores availability) reintroduces exactly the
hardcoding problem this module exists to fix, just one level removed --
a differently-named contended camera could easily sort first by pure
accident of string ordering. :func:`_probe_available` -- a short-lived,
real open/read/close, mirroring ``camera.discovery.CameraDiscovery
.probe()``'s own "briefly open a Camera... to determine usable" method,
not a new invented technique -- tries every qualifying candidate in
order and returns the first one that is genuinely available; only if
every qualifying candidate fails that probe does this fall back to the
first one anyway (better to attempt the run against a possibly-busy
camera than to skip every hardware test outright), with a clear,
printed message either way so a run's own output always says which
physical camera it actually used and whether that choice was forced.

If none qualifies at all, every function below skips the calling test
cleanly (never fails, never silently substitutes an unrelated camera)
naming exactly what was checked and how to fix it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional

import pytest

from aprilcam.camera.backends.opencv_backend import OpenCVBackend
from aprilcam.camera.camera import Camera
from aprilcam.camera.identity import CameraIdentity, resolve_all
from aprilcam.config.directories import Directories

__all__ = [
    "resolve_hardware_camera_identity",
    "resolve_hardware_camera_slug",
]

#: The playfield every one of this project's current hardware test
#: modules is written against -- see each module's own docstring
#: ("aimed at the physical main-playfield"). A parameter, not baked in as
#: a bare default with no override point, so a future module aimed at a
#: different real playfield is not blocked from reusing this helper.
DEFAULT_HARDWARE_PLAYFIELD = "main-playfield"

#: The real system's own default :class:`Directories`, resolved **once,
#: at module import time** -- deliberately before any test's own fixture
#: (``real_directories``/``cli_env``/etc., several of which
#: ``monkeypatch.setenv`` ``APRILCAM_CONFIG_DIR``/``APRILCAM_STATE_DIR``
#: for their own isolated runs) has a chance to run. Every test module
#: that uses this helper imports it at collection time -- before pytest
#: executes any fixture or test body -- so this module-level constant
#: always captures the real, un-overridden environment. Constructing this
#: lazily inside a function instead (the first version of this module did
#: exactly that, and it was wrong: two real hardware tests skipped with a
#: false "no camera qualifies" the moment their own fixture had already
#: monkeypatched the environment before this helper's first call) would
#: resolve whatever *that test's own* isolated, ephemeral directories
#: happen to be at call time, not the real system ones, since
#: ``monkeypatch.setenv`` mutates ``os.environ`` process-wide for the life
#: of the test. Matches ``test_camera_geometry_hardware.py``'s own
#: ``_REAL_SYSTEM_DIRECTORIES`` precedent exactly, generalized into this
#: shared helper instead of re-declared per module.
_REAL_SYSTEM_DIRECTORIES = Directories()


def _real_system_directories() -> Directories:
    """The real system's own default :class:`Directories` -- **not**
    whatever a calling test's own fixture may have already
    ``monkeypatch.setenv``'d ``APRILCAM_CONFIG_DIR``/``APRILCAM_STATE_DIR``
    to for its own isolated run. See :data:`_REAL_SYSTEM_DIRECTORIES`'s
    own docstring for why this must be a module-import-time constant,
    not a fresh ``Directories()`` call here."""
    return _REAL_SYSTEM_DIRECTORIES


def _configured_playfield(config_path: Path) -> Optional[str]:
    """The ``"playfield"`` field of a camera ``config.json``, or ``None``
    on anything that stops this from being a clean answer (missing file,
    unreadable, malformed JSON, no such key) -- discovery degrades to
    "this candidate does not qualify" rather than raising, consistent
    with ``camera.identity``'s own "every source function degrades on
    failure" rule."""
    try:
        document = json.loads(config_path.read_text())
    except (OSError, ValueError):
        return None
    playfield = document.get("playfield")
    return playfield if isinstance(playfield, str) else None


def _probe_available(identity: CameraIdentity, index: int, *, attempts: int = 3) -> bool:
    """Best-effort, low-cost check that *identity* (enumerated at
    *index*) is actually free right now -- a short-lived, real open, up
    to *attempts* reads, and a close, mirroring
    ``camera.discovery.CameraDiscovery.probe()``'s own "briefly open a
    Camera... to determine usable" method (this module intentionally
    does not import/construct a full ``CameraDiscovery``/
    ``CameraRegistry`` pair just for this, since a bare ``Camera`` open
    is all this check needs). Never raises: a device already held
    exclusively by another process typically fails to open at all on
    this project's supported backends, but a slower or more permissive
    failure mode (opens, then never yields a frame) is covered too by
    the read attempts. Treated as unavailable either way -- this is a
    preference signal, not a hard requirement, so erring toward "treat
    as busy" only costs trying the next candidate, never a false
    positive that would hand back a camera mid-use elsewhere.
    """
    try:
        camera = Camera(identity.slug, OpenCVBackend(index))
        camera.open()
    except Exception:
        return False
    try:
        for _ in range(attempts):
            if camera.read() is not None:
                return True
        return False
    finally:
        camera.close()


def resolve_hardware_camera_identity(
    playfield: str = DEFAULT_HARDWARE_PLAYFIELD,
) -> CameraIdentity:
    """The :class:`CameraIdentity` of a currently-attached camera that is
    genuinely configured and calibrated against *playfield* on this
    machine's real, persisted system state -- see the module docstring
    for exactly what "genuinely" checks and why, and for how ties among
    several qualifying cameras are broken by real availability rather
    than sort order. Skips the calling test cleanly (``pytest.skip``) if
    none qualifies; never returns an unrelated camera and never fails the
    test outright, matching every ``@pytest.mark.camera`` module's own
    existing "skip cleanly when the hardware precondition is unmet"
    convention. Always prints which camera it picked and why, so a run's
    own output -- not an assumption -- says which physical camera it
    actually used.
    """
    identities = resolve_all()
    if not identities:
        pytest.skip("no camera attached to this machine")

    by_slug = {identity.slug: identity for identity in identities.values()}
    index_by_slug = {identity.slug: index for index, identity in identities.items()}
    directories = _real_system_directories()
    cameras_config_dir = directories.config_dir / "cameras"

    candidates = []
    if cameras_config_dir.is_dir():
        for camera_dir in sorted(cameras_config_dir.iterdir()):
            identity = by_slug.get(camera_dir.name)
            if identity is None:
                continue  # configured at some point, but not attached right now
            config_path = camera_dir / "config.json"
            if _configured_playfield(config_path) != playfield:
                continue
            calibration_path = directories.state_dir / "cameras" / camera_dir.name / "calibration.json"
            if not calibration_path.exists():
                continue
            candidates.append(identity)

    if not candidates:
        pytest.skip(
            f"no attached camera is both configured (config_dir/cameras/<slug>/"
            f"config.json's \"playfield\") and calibrated (state_dir/cameras/"
            f"<slug>/calibration.json present) against {playfield!r} on this "
            f"machine's real system state -- attached cameras right now: "
            f"{sorted(by_slug)}. Link and calibrate one with 'aprilcam camera "
            f"config <file>' and 'aprilcam camera calibrate <camera>' before "
            f"running these tests."
        )

    if len(candidates) == 1:
        chosen = candidates[0]
        print(f"[hardware_camera] resolved {chosen.slug!r} against {playfield!r} (only qualifying camera)")
        return chosen

    for identity in candidates:
        index = index_by_slug.get(identity.slug)
        if index is not None and _probe_available(identity, index):
            print(
                f"[hardware_camera] resolved {identity.slug!r} against {playfield!r} "
                f"(available, chosen over {sorted(c.slug for c in candidates if c is not identity)})"
            )
            return identity

    fallback = candidates[0]
    print(
        f"[hardware_camera] WARNING: every qualifying camera against {playfield!r} "
        f"failed the availability probe ({sorted(c.slug for c in candidates)}) -- "
        f"falling back to {fallback.slug!r} anyway; it may be busy in another process"
    )
    return fallback


def resolve_hardware_camera_slug(playfield: str = DEFAULT_HARDWARE_PLAYFIELD) -> str:
    """Convenience wrapper for callers that only need the slug (the vast
    majority of call sites -- most existing ``ARDUCAM_SLUG`` usages were
    a bare device-name string). See
    :func:`resolve_hardware_camera_identity` for the full contract."""
    return resolve_hardware_camera_identity(playfield).slug
