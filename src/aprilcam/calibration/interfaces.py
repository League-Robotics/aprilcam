"""Calibration: the pixel ↔ world mapping.

**Daemon-only** (spec §4.4). In v1 the CLI and MCP server ran this math
locally with their own OpenCV import, which broke the vision boundary; in
v2 they trigger it over RPC and report the result.

The invariants below are the ones ``docs/design/specification.md`` §7
calls sacrosanct. They cost enormous effort to get right, and every sign
and axis in a change here is guilty until proven innocent.
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

import numpy as np

from aprilcam.types import PixelPoint, WorldPoint


@runtime_checkable
class CalibrationResult(Protocol):
    """The outcome of a calibration attempt — explicitly good or bad.

    There is **no partial calibration**. Either every ArUco fiducial the
    playfield definition specifies was found and a homography was
    computed, or the attempt failed and nothing was stored (UC-04).
    """

    @property
    def succeeded(self) -> bool: ...

    @property
    def matched_ids(self) -> tuple[int, ...]:
        """ArUco ids found and matched to surveyed positions."""
        ...

    @property
    def missing_ids(self) -> tuple[int, ...]:
        """Specified ids that were **not** found.

        Non-empty means failure. Reported by id rather than as a count so
        the developer knows which marker to uncover (UC-04.S-2) — "9 of
        10 found" is not actionable; "ArUco 6 missing" is.
        """
        ...

    @property
    def reprojection_error_cm(self) -> Optional[float]:
        """How far surveyed markers land from where they should.

        Open (UC-04): completeness is necessary but may not be
        sufficient. A complete-but-sloppy solve is still a bad solve, so
        there is probably also a maximum acceptable error — that
        threshold is undecided.
        """
        ...

    @property
    def reason(self) -> Optional[str]:
        """Why a failure failed, when ids alone don't say.

        ``missing_ids`` covers the common case; this covers the rest —
        a complete-but-degenerate solve (collinear markers, singular
        homography) must name its problem, not report failure with
        every id present and leave the developer to guess.
        """
        ...


@runtime_checkable
class Calibration(Protocol):
    """A camera's established mapping between its pixels and the world.

    **File-backed**: written by the daemon to the state directory and
    reloaded at startup, so a calibrated camera stays calibrated across
    restarts (UC-01). This is why calibration survives while pushed
    config does not — the rule is file-backed vs in-memory, not
    config vs calibration.
    """

    def to_world(self, pixel: PixelPoint) -> WorldPoint:
        """Map a detection to the field, in cm.

        A1-centred, +x east, +y north. The origin shift is a **pure
        subtraction on both axes — never a y-flip**. Violating this
        silently mirrors the field north/south, which is the single
        easiest way to break everything downstream while producing
        plausible-looking numbers.
        """
        ...

    def to_pixel(self, world: WorldPoint) -> PixelPoint:
        """Inverse of :meth:`to_world`, for rendering."""
        ...

    @property
    def stale(self) -> bool:
        """Whether the camera or field has moved since calibrating.

        Advisory but important: stale means reported coordinates may be
        wrong while still looking entirely reasonable. Consumers must be
        told rather than silently served plausible garbage
        (UC-11.S-3) — the worst failure mode in the system.
        """
        ...

    @property
    def calibrated_at(self) -> float:
        """When this mapping was solved — provenance, not decoration.

        "Ready" without "as of when, against what" is how a January
        calibration silently serves a March field (UC-05.S-1).
        """
        ...

    @property
    def playfield(self) -> str:
        """The playfield definition this was solved against.

        The other half of provenance: replacing this definition is what
        marks the calibration stale (UC-02), so the calibration must
        name which definition it believes in.
        """
        ...


@runtime_checkable
class Calibrator(Protocol):
    """Runs calibration for a camera against its playfield."""

    def calibrate(self, camera_slug: str) -> CalibrationResult:
        """Detect fiducials, match to surveyed positions, solve.

        Requires the camera to be linked to a playfield: with no
        playfield there is no coordinate system to map to and **nothing
        to calibrate**, which is a precondition error rather than a
        calibration failure (UC-15.S-2).

        A failed attempt leaves any existing good calibration untouched.
        """
        ...


@runtime_checkable
class Deskewer(Protocol):
    """Produces the top-down playfield image (UC-06, UC-19).

    Sizing is a **fixed bounding box**: the field rectangle is scaled
    aspect-preserved to fit (default 800×800, per-playfield override), so
    the output is uniform-scale by construction. ``main-playfield`` at
    134.3 × 89.3 cm yields 800 × 532.

    The resulting px-per-cm is **derived and reported, never chosen** —
    clients convert world ↔ pixel using published metadata rather than a
    constant anyone had to agree on.

    On the deskewed image **north is up and east is right**. Image rows
    grow downward, so world → pixel flips y exactly **once**, inside this
    mapping, and never again in client code.
    """

    def deskew(self, frame: np.ndarray) -> np.ndarray: ...
