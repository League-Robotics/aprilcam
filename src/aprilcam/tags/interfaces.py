"""Fiducial generation: printable AprilTag and ArUco images (UC-21).

**Purely offline** — no daemon, no camera, no network. The one
capability in the system that works on a bare install, which is exactly
when it is needed: you generate tags *before* there is a field to look
at.

OpenCV is a base dependency so this works on any install (UC-21) — but
its being importable is not permission to do pixel work outside the
daemon. Generation writes images; it never reads them.

The dictionaries are fixed system-wide (spec §4.10): AprilTag **36h11**
and ArUco **4x4** — a family in ``TagId`` fully determines the
dictionary, so no dictionary parameter appears here.

See ``aprilcam.daemon.interfaces``'s module docstring for the interface
design rule applied below: ``TagGenerator`` is purely behavioral, so it
is a plain base class. No concrete implementation exists yet (out of
this pass's scope); converting now means the sprint that adds one
derives rather than duck-types.
"""

from __future__ import annotations

from pathlib import Path

from aprilcam.types import TagFamily


class TagGenerator:
    """Produces printable fiducial images at a known physical size.

    A plain base class, not ``Protocol`` — see ``daemon.interfaces``'s
    module docstring for the design rule.
    """

    def valid_ids(self, family: TagFamily) -> range:
        """The family's dictionary range.

        Published so the CLI can validate **before** generating and name
        the valid range in its error (UC-21.S-3) — an out-of-range id
        raises ``InvalidTagId`` (``aprilcam.errors``) and writes
        nothing.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement TagGenerator.valid_ids")

    def generate(
        self,
        family: TagFamily,
        ids: tuple[int, ...],
        *,
        size_cm: float,
        out_dir: Path,
        dpi: int = 300,
    ) -> tuple[Path, ...]:
        """Write one image per id, sized to print at ``size_cm``.

        ``size_cm`` is the physical edge length the printed tag must
        have — it ties generation to the survey (corner ArUcos match
        the playfield definition, UC-21.S-1) and to detection
        (a robot tag matches the ``size`` registered in UC-12,
        UC-21.S-2). A size unachievable at ``dpi`` **warns** rather
        than silently producing an unusable tag.

        An unwritable ``out_dir`` fails before generating; nothing is
        partially written.

        Open (UC-21): PDF output for multi-tag sheets, as v1 had.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement TagGenerator.generate")
