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

import io
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


# Label-sheet geometry, taken from v1's sheet generator (evidence, not a
# port): the stakeholder prints on a 59 x 102 mm label printer at 100%.
_PAGE_W_MM = 59.0
_PAGE_H_MM = 102.0
_MARGIN_MM = 1.0
_CAPTION_H_MM = 6.0
_CAPTION_GAP_MM = 1.0
_CAPTION_FONT_PT = 7
# Quiet-zone border around the marker, as a fraction of its edge (v1's
# PDF value: just enough for detection while the tag fills the page).
_SHEET_QUIET_RATIO = 0.04
# Marker render edge in pixels. Divisible by both 6 (ArUco 4x4 plus
# border) and 8 (AprilTag 36h11 plus border) so every module is a whole
# number of pixels and edges stay crisp when scaled onto the page.
_SHEET_MARKER_PX = 960

_FAMILY_CAPTION = {
    TagFamily.ARUCO: "ArUco 4x4",
    TagFamily.APRILTAG: "AprilTag 36h11",
}


def _caption(family: TagFamily, tag_id: int) -> str:
    return f"{_FAMILY_CAPTION[family]} ID: {tag_id}"


def _file_writable(path: Path) -> bool:
    """True if a file could be created at *path*: its parent exists (or
    has an existing ancestor that does) and is writable, and *path* is
    not a directory or an unwritable existing file."""
    if path.is_dir():
        return False
    if path.exists():
        return os.access(path, os.W_OK)
    return _out_dir_writable(path.parent)


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

    def generate_sheet(
        self,
        family: TagFamily,
        ids: tuple[int, ...],
        *,
        out_path: Path,
    ) -> Path:
        cv = _import_cv()

        # Validate every id, then output writability, before rendering or
        # writing anything (UC-21.S-3).
        valid = self.valid_ids(family)
        for tag_id in ids:
            if tag_id not in valid:
                raise InvalidTagId(family.value, tag_id, valid)
        out_path = Path(out_path)
        if not _file_writable(out_path):
            raise UnwritableOutputPath(out_path)

        from fpdf import FPDF  # base dependency; imported where used

        dictionary = cv.aruco.getPredefinedDictionary(_dictionary_by_family(cv)[family])
        pdf = FPDF(unit="mm", format=(_PAGE_W_MM, _PAGE_H_MM))
        pdf.set_auto_page_break(auto=False)
        pdf.set_font("Helvetica", size=_CAPTION_FONT_PT)

        avail_w = _PAGE_W_MM - 2 * _MARGIN_MM
        avail_h = _PAGE_H_MM - 2 * _MARGIN_MM - _CAPTION_H_MM
        tag_dim = min(avail_w, avail_h)
        x = (_PAGE_W_MM - tag_dim) / 2
        y = _MARGIN_MM + (avail_h - tag_dim) / 2

        for tag_id in ids:
            marker = cv.aruco.generateImageMarker(dictionary, tag_id, _SHEET_MARKER_PX)
            pad = max(1, round(_SHEET_MARKER_PX * _SHEET_QUIET_RATIO))
            padded = cv.copyMakeBorder(
                marker, pad, pad, pad, pad, cv.BORDER_CONSTANT, value=255
            )
            ok, png = cv.imencode(".png", padded)
            if not ok:
                raise RuntimeError(f"could not encode {_caption(family, tag_id)} as PNG")

            pdf.add_page()
            pdf.image(io.BytesIO(png.tobytes()), x=x, y=y, w=tag_dim, h=tag_dim)
            pdf.set_xy(0, y + tag_dim + _CAPTION_GAP_MM)
            pdf.cell(_PAGE_W_MM, 4, _caption(family, tag_id), align="C")

        out_path.parent.mkdir(parents=True, exist_ok=True)
        pdf.output(str(out_path))
        return out_path
