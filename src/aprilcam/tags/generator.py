"""Concrete :class:`TagGenerator` -- printable AprilTag/ArUco images (UC-21).

Implements ``tags.interfaces.TagGenerator``, the plain base class this
whole codebase uses for behavioral interfaces (see
``daemon.interfaces``'s module docstring for the rule). This class
derives from it exactly the way ``client.discovery.Discovery`` derives
from ``client.interfaces.Discovery``.

**Standalone by construction.** Beyond ``errors``, ``types``, and its
own ``interfaces``, this module imports nothing else from this tree --
no ``daemon``, ``client``, or ``vision`` anywhere in the call path,
matching design.md §4.10's "purely-offline utility" contract for
``tags/``.

**``cv2`` is imported lazily, at the point of use (sprint 036,
reversing sprint 028's "OpenCV is a permanent base dependency" call --
see this sprint's ``tags-DESIGN.diff.md`` Design Rationale for the
full reversal and why it was accepted).** ``opencv-contrib-python`` now
lives in the ``daemon`` extra, not ``[project.dependencies]``, so
``import cv2`` can genuinely fail here. ``_import_cv`` (below) is the
only place this module touches the ``cv2`` name: it is called first
thing inside :meth:`TagGenerator.valid_ids` and
:meth:`TagGenerator.generate`, and converts a missing/broken install
into a typed ``errors.MissingOptionalDependency`` naming
``pip install 'aprilcam[daemon]'`` -- never a raw
``ModuleNotFoundError`` traceback -- mirroring ``cli/mcp.py``'s
``_import_build_server`` for the ``mcp`` SDK. Simply
``import aprilcam.tags.generator``, or constructing a
``TagGenerator()``, touches no ``cv2`` symbol at all: ``aprilcam
taggen`` stays importable and discoverable (``--help``) in a base-only
install, and only fails -- clearly -- once actually invoked.

**Family -> dictionary mapping is deliberately duplicated, not
imported.** ``vision/detector.py`` already has an identical
``_DICTIONARY_BY_FAMILY``-shaped mapping (its own module, near the
top). This module does not import it: ``vision/`` is daemon-only
detection machinery, and creating an import edge from ``tags/`` to it
would couple two modules with no shared reason to change together
(sprint 028 sprint.md, Design Rationale Step 6). If one mapping
changes -- e.g. a new AprilTag family -- check the other. It is now
built by :func:`_dictionary_by_family`, inside the same lazy-import
boundary as ``cv2`` itself, rather than as a module-level dict.

**Whole-batch validation.** Every requested id is checked against
``valid_ids(family)`` before anything is rendered or written, and
output-directory writability is checked before anything is rendered.
Both checks happen up front so a rejected batch never partially lands
on disk (UC-21.S-3, SUC-002, SUC-003).
"""

from __future__ import annotations

import logging
import os
import warnings
from pathlib import Path
from typing import Any

from aprilcam.errors import InvalidTagId, MissingOptionalDependency, UnwritableOutputPath
from aprilcam.tags.interfaces import TagGenerator as _TagGeneratorBase
from aprilcam.types import TagFamily

__all__ = ["TagGenerator"]

logger = logging.getLogger(__name__)


def _is_missing_cv2(exc: ModuleNotFoundError) -> bool:
    """True if *exc* names ``cv2`` itself (or one of its submodules,
    e.g. ``cv2.aruco``) as the module that could not be found, as
    opposed to some unrelated import failure that happened to occur
    partway through importing it. Split out from :func:`_import_cv` so
    this classification is unit-testable against constructed
    exceptions, mirroring ``cli/mcp.py``'s ``_is_missing_mcp_sdk``.
    """
    name = exc.name or ""
    return name == "cv2" or name.startswith("cv2.")


def _import_cv() -> Any:
    """Import ``cv2`` lazily, at call time.

    Raises :class:`~aprilcam.errors.MissingOptionalDependency` -- not a
    raw ``ModuleNotFoundError`` -- if OpenCV is missing, which is
    expected in a base-only install: ``opencv-contrib-python`` lives in
    the ``daemon`` extra (sprint 036), not ``[project.dependencies]`.
    Any other ``ModuleNotFoundError`` -- one unrelated to ``cv2`` (see
    :func:`_is_missing_cv2`) -- is a real bug and is left to propagate
    rather than being misreported as a missing dependency.
    """
    try:
        import cv2 as cv
    except ModuleNotFoundError as exc:
        if not _is_missing_cv2(exc):
            raise
        raise MissingOptionalDependency(
            "aprilcam taggen",
            "opencv-contrib-python",
            install_hint=(
                "install the daemon extra with 'pip install "
                "\"aprilcam[daemon]\"' (or 'pipx install "
                "\"aprilcam[daemon]\"' for a pipx install)"
            ),
        ) from exc
    return cv


def _dictionary_by_family(cv: Any) -> dict[TagFamily, int]:
    """Family -> OpenCV predefined-dictionary constant, built from an
    already-imported *cv* module (see :func:`_import_cv`). See the
    module docstring for why this mapping is duplicated from, not
    imported from, ``vision/detector.py``.
    """
    return {
        TagFamily.APRILTAG: cv.aruco.DICT_APRILTAG_36h11,
        TagFamily.ARUCO: cv.aruco.DICT_4X4_50,
    }


# Legibility threshold for the size/DPI warning (sprint architecture
# Open Question 4 -- left as an implementation judgment call, recorded
# here with its rationale).
#
# There is no single hard pixel count below which a printed tag stops
# detecting -- it depends on printer DPI, camera resolution, and viewing
# distance, none of which this function knows. Rather than pick a tight
# threshold tuned to a specific setup, this uses a conservative round
# number: 300px per edge is comfortably above what a consumer printer
# (300 DPI) renders for even a 2.5cm tag, and well above the ~100px
# floor where OpenCV's own ArUco/AprilTag detectors start missing
# corners in testing against real playfield captures (vision/detector.py's
# earned constants target images far larger than this). Below 300px the
# operator is warned -- not blocked -- because a small preview or a
# tag meant only for close-range detection may still be exactly what
# they want.
_MIN_LEGIBLE_PIXELS = 300


def _out_dir_writable(path: Path) -> bool:
    """True if *path* exists and is writable, or does not exist yet but
    its nearest existing ancestor is (so ``generate`` can create it on
    demand).

    Mirrors ``config/directories.py``'s ``_writable_or_creatable`` in
    spirit but is not imported from there: ``tags/`` has no dependency
    on ``config/`` (or anywhere else in this tree) beyond ``errors``/
    ``types``/its own ``interfaces``, and this check is small enough
    that duplicating it costs less than adding an import edge for it.
    """
    if path.exists():
        return path.is_dir() and os.access(path, os.W_OK | os.X_OK)
    for ancestor in path.parents:
        if ancestor.exists():
            return os.access(ancestor, os.W_OK | os.X_OK)
    return False


class TagGenerator(_TagGeneratorBase):
    """Concrete :class:`aprilcam.tags.interfaces.TagGenerator`.

    Derives from ``tags.interfaces.TagGenerator`` (a plain base class,
    not ``Protocol`` -- see ``daemon.interfaces``'s module docstring for
    the design rule this codebase follows everywhere).
    """

    def valid_ids(self, family: TagFamily) -> range:
        cv = _import_cv()
        dictionary = cv.aruco.getPredefinedDictionary(_dictionary_by_family(cv)[family])
        return range(0, dictionary.bytesList.shape[0])

    def generate(
        self,
        family: TagFamily,
        ids: tuple[int, ...],
        *,
        size_cm: float,
        out_dir: Path,
        dpi: int = 300,
    ) -> tuple[Path, ...]:
        # 0. Import cv2, or fail clearly (sprint 036) -- see the module
        # docstring and `_import_cv`. Done first, before any other work,
        # so a missing `daemon` extra is reported before anything else
        # runs (id validation below calls `self.valid_ids`, which also
        # imports cv2 -- cheap once cached in sys.modules, and this
        # earlier call is what makes the failure message appear first).
        cv = _import_cv()

        # 1. Validate every id up front -- a single bad id in the batch
        # must reject the whole batch before anything is rendered or
        # written (UC-21.S-3, SUC-002).
        valid = self.valid_ids(family)
        for tag_id in ids:
            if tag_id not in valid:
                raise InvalidTagId(family.value, tag_id, valid)

        # 2. Check output-directory writability before anything is
        # rendered (UC-21, SUC-003).
        out_dir = Path(out_dir)
        if not _out_dir_writable(out_dir):
            raise UnwritableOutputPath(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        # 3. Compute pixel size from the physical target; warn (do not
        # fail) if it falls below the legibility threshold.
        pixel_size = round(size_cm / 2.54 * dpi)
        if pixel_size < _MIN_LEGIBLE_PIXELS:
            warnings.warn(
                f"{family.value} tag at size_cm={size_cm}, dpi={dpi} renders "
                f"at {pixel_size}px, below the {_MIN_LEGIBLE_PIXELS}px "
                "legibility threshold; the image will still be generated "
                "but may not reliably detect once printed",
                stacklevel=2,
            )

        # 4. Render and write one PNG per id.
        dictionary = cv.aruco.getPredefinedDictionary(_dictionary_by_family(cv)[family])
        written: list[Path] = []
        for tag_id in ids:
            image = cv.aruco.generateImageMarker(dictionary, tag_id, pixel_size)
            path = out_dir / f"{family.value}-{tag_id}.png"
            cv.imwrite(str(path), image)
            written.append(path)
        return tuple(written)
