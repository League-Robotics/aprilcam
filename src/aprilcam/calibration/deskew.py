"""``Deskewer``: the fixed-bounding-box top-down playfield warp (ticket
022-005).

Implements ``calibration.interfaces.Deskewer.deskew`` -- constructed
from a camera's stored ``calibration.calibration.Calibration`` (ticket
003) plus its linked ``config.loader.PlayfieldDefinition``'s
``width_cm``/``height_cm``, this warps a raw camera frame into the
fixed bounding box (default 800x800, aspect-preserved --
``main-playfield``'s 134.3 x 89.3 cm yields 800 x 532) and reports the
derived ``px_per_cm``/``world_extent`` the warp was built from -- never
a chosen constant (specification.md Sec.4.1).

**Sizing and the anchor-invariant math are shared, not reimplemented
here.** ``calibration/geometry.py``'s :func:`~aprilcam.calibration.geometry.deskewed_output_size`
computes the aspect-preserved output dimensions;
:func:`~aprilcam.calibration.geometry.world_to_deskewed_pixel` computes
where any world point lands on the deskewed image. This module's only
own job is the mechanical one those two pure functions cannot do: read
four pixels' worth of *raw camera* content (via the injected
``Calibration.to_pixel``) and warp them into place with OpenCV. The
destination quad below is built by calling
``world_to_deskewed_pixel`` on the same four world corners
``geometry.py``'s docstring anchors on, so the warp this class produces
and the pixel positions ``client/renderer.py`` computes from the
*published metadata alone* are the same mapping by construction, not
independently re-derived and hoped to agree.

**Archive pointer** (mined for the warp-matrix construction approach,
not ported wholesale -- ``specification.md`` Sec.6 explicitly drops
v1's upper-left-origin corner table): ``archive/src/aprilcam/
calibration/geometry.py``'s ``metric_deskew_matrix`` builds a
``cv2.getPerspectiveTransform`` warp from a source polygon to a fixed
metric rectangle the same way this class does; unlike that function
(which took a caller-supplied ``px_per_cm`` and *chose* the output
size from it), this class's output size is fixed first (the bounding
box) and ``px_per_cm`` is *derived* from what actually came out --
the opposite direction, per this ticket's binding rule. v1's
``corner_pixels_from_homography`` needed a live ArUco corner detection
this class does not: the four world corners here come from the
playfield definition's own geometry (``+-width/2``, ``+-height/2``),
converted through the *already-solved* ``Calibration.to_pixel``, not
re-detected.
"""

from __future__ import annotations

from typing import Optional, Tuple

import cv2 as cv
import numpy as np

from aprilcam.calibration import geometry
from aprilcam.calibration.calibration import Calibration
from aprilcam.calibration.interfaces import Deskewer as _DeskewerBase
from aprilcam.types import WorldPoint

__all__ = ["Deskewer"]


def _world_corners(
    width_cm: float, height_cm: float
) -> Tuple[WorldPoint, WorldPoint, WorldPoint, WorldPoint]:
    """The field rectangle's four corners, A1-centred, in NW, NE, SE, SW
    order. A thin wrapper over ``calibration.geometry.world_corners``
    (sprint 037 ticket 002 promoted this function's body there, as a
    shared helper ``daemon/service.py``'s flat-field mask derivation also
    needs) -- kept here, under this module's own name, so every existing
    caller in this file is unchanged."""
    return geometry.world_corners(width_cm, height_cm)


class Deskewer(_DeskewerBase):
    """Concrete ``calibration.interfaces.Deskewer``.

    Parameters
    ----------
    calibration:
        The camera's already-solved pixel<->world mapping (ticket 003).
        Only ``to_pixel`` is used here -- the warp's source quad is the
        field's four world corners, mapped back to *this camera's* raw
        pixel space.
    width_cm, height_cm:
        The linked ``PlayfieldDefinition``'s field dimensions -- the
        world rectangle being deskewed. Not read from *calibration*
        itself: a ``Calibration`` records which playfield it was solved
        against by *name* (``Calibration.playfield``), not its
        dimensions, so the caller (``daemon/service.py``) resolves the
        definition and passes its geometry in directly.
    max_dimension_px:
        The fixed bounding box's side length (default 800, per
        ``geometry.DEFAULT_MAX_DIMENSION_PX`` --
        specification.md Sec.4.1's "default 800x800, overridable per
        playfield").

    ``output_size``/``px_per_cm``/``world_extent`` are computed once, at
    construction, from *width_cm*/*height_cm*/*max_dimension_px* alone --
    :meth:`deskew` never recomputes them, so every frame produced by one
    instance shares identical geometry (``Daemon.stream_images``'s "frame
    size is stable for the life of a stream" contract, extended here to
    one-shot ``get_frame`` calls too: two calls against the same
    calibration/playfield report the same dimensions).
    """

    def __init__(
        self,
        *,
        calibration: Calibration,
        width_cm: float,
        height_cm: float,
        max_dimension_px: int = geometry.DEFAULT_MAX_DIMENSION_PX,
    ) -> None:
        self._calibration = calibration
        self._width_cm = float(width_cm)
        self._height_cm = float(height_cm)
        self._output_width, self._output_height = geometry.deskewed_output_size(
            self._width_cm, self._height_cm, max_dimension_px
        )
        # Derived from the actual output width, never a chosen constant
        # (this ticket's binding rule) -- see geometry.deskewed_output_size's
        # docstring for why width, specifically, is the one dimension this
        # scalar is guaranteed to satisfy exactly.
        self._px_per_cm = self._output_width / self._width_cm
        #: Lazily-built ``cv2.remap`` lookup tables for a calibration
        #: that carries a lens model (see :meth:`_build_lens_maps`);
        #: stays ``None`` for a homography-only calibration, which keeps
        #: the original ``warpPerspective`` path untouched.
        self._lens_maps: Optional[Tuple[np.ndarray, np.ndarray]] = None
        self._lens_maps_built = False

    @property
    def output_size(self) -> Tuple[int, int]:
        """``(width_px, height_px)`` -- stable for this instance's life."""
        return (self._output_width, self._output_height)

    @property
    def px_per_cm(self) -> float:
        """Derived from :attr:`output_size`'s width, never chosen."""
        return self._px_per_cm

    @property
    def world_extent(self) -> Tuple[float, float]:
        """``(width_cm, height_cm)`` -- the field rectangle this warp
        spans exactly, per the anchor invariant."""
        return (self._width_cm, self._height_cm)

    def deskew(self, frame: np.ndarray) -> np.ndarray:
        """See ``calibration.interfaces.Deskewer.deskew``.

        Builds the warp from two independently-derived quads over the
        *same* four world corners: the source quad is those corners in
        this camera's raw pixel space (``Calibration.to_pixel``, the
        already-solved homography); the destination quad is the literal
        output-canvas corners ``(0, 0)``, ``(out_w, 0)``, ``(out_w,
        out_h)``, ``(0, out_h)`` -- not ``geometry.world_to_deskewed_pixel``
        applied to those same corners, deliberately: that function's
        result can fall a sub-pixel short of the canvas edge on a
        non-square field (``deskewed_output_size``'s own docstring), and
        using the literal canvas corners instead guarantees the *entire*
        published ``output_size`` is genuine warped field content, with
        no unfilled edge strip. Correspondence order is NW, NE, SE, SW
        throughout (:func:`_world_corners`), matching
        ``cv2.getPerspectiveTransform``'s requirement that both point
        lists describe the same four physical corners in the same order.

        **Lens model (2026-09-05 fix).** When the calibration carries a
        lens model (``camera_matrix``/``dist_coeffs``), a plain
        ``warpPerspective`` of the *distorted* raw frame is wrong
        everywhere but at the four corners: the frame keeps its barrel
        distortion while ``Calibration.to_world`` (and so every tag
        position drawn over this image) undistorts, and the two
        disagree radially -- measured at ~1 cm / 8 px on the ground tags
        near the corners of ``secondary-playfield``, zero at the centre.
        For that case the warp is a single ``cv2.remap`` through lookup
        tables that compose the inverse perspective (canvas -> undistorted
        raw pixel) with the forward lens model (undistorted -> distorted
        raw pixel), built once per instance (:meth:`_build_lens_maps`),
        so image and overlay agree by construction and the per-frame
        cost stays one interpolation pass. A calibration with no lens
        model takes exactly the original path below, unchanged.
        """
        if not self._lens_maps_built:
            self._lens_maps = self._build_lens_maps()
            self._lens_maps_built = True
        if self._lens_maps is not None:
            map_x, map_y = self._lens_maps
            return cv.remap(frame, map_x, map_y, cv.INTER_LINEAR)

        world_corners = _world_corners(self._width_cm, self._height_cm)
        src = np.array(
            [[p.x, p.y] for p in (self._calibration.to_pixel(w) for w in world_corners)],
            dtype=np.float32,
        )
        matrix = cv.getPerspectiveTransform(src, self._canvas_corners())
        return cv.warpPerspective(frame, matrix, (self._output_width, self._output_height))

    def _canvas_corners(self) -> np.ndarray:
        """The literal output-canvas corners, NW, NE, SE, SW -- the
        destination quad both warp paths share (see :meth:`deskew`'s
        docstring for why these, not ``world_to_deskewed_pixel``)."""
        return np.array(
            [
                [0.0, 0.0],
                [float(self._output_width), 0.0],
                [float(self._output_width), float(self._output_height)],
                [0.0, float(self._output_height)],
            ],
            dtype=np.float32,
        )

    def _build_lens_maps(self) -> Optional[Tuple[np.ndarray, np.ndarray]]:
        """``(map_x, map_y)`` for ``cv2.remap``, or ``None`` when the
        calibration has no lens model.

        For every output pixel: invert the perspective transform that
        maps the field's four corners in *undistorted* raw pixel space
        (``geometry.world_to_pixel`` -- the homography alone, before
        ``Calibration.to_pixel`` re-applies distortion) onto the canvas,
        giving the undistorted raw pixel this output pixel samples; then
        push that through the forward lens model -- normalise with the
        camera matrix, apply OpenCV's radial (k1..k3, rational k4..k6)
        and tangential (p1, p2) terms, project back -- giving the
        distorted raw pixel to actually read. This is the closed-form
        direction of the model (the same one ``distortion.distort_point``
        iterates ``cv2.undistortPoints`` to reach for a single point),
        evaluated for the whole canvas at once. At the four canvas
        corners the result equals ``Calibration.to_pixel`` of the world
        corners exactly, so the anchor invariant is preserved.
        """
        cam = self._calibration.camera_matrix
        dist = self._calibration.dist_coeffs
        if cam is None or dist is None:
            return None
        coeffs = np.zeros(8, dtype=np.float64)
        dist_arr = np.asarray(dist, dtype=np.float64).ravel()
        if dist_arr.size > 8:
            raise ValueError(
                f"Deskewer supports at most 8 distortion coefficients (k1 k2 p1 p2 k3 k4 k5 k6), "
                f"got {dist_arr.size}"
            )
        coeffs[: dist_arr.size] = dist_arr
        k1, k2, p1, p2, k3, k4, k5, k6 = coeffs
        camera_matrix = np.asarray(cam, dtype=np.float64)
        homography = np.asarray(self._calibration.homography, dtype=np.float64)

        world_corners = _world_corners(self._width_cm, self._height_cm)
        undistorted_src = np.array(
            [[p.x, p.y] for p in (geometry.world_to_pixel(homography, w) for w in world_corners)],
            dtype=np.float32,
        )
        perspective = cv.getPerspectiveTransform(undistorted_src, self._canvas_corners())
        inverse = np.linalg.inv(np.asarray(perspective, dtype=np.float64))

        us, vs = np.meshgrid(
            np.arange(self._output_width, dtype=np.float64),
            np.arange(self._output_height, dtype=np.float64),
        )
        ones = np.ones_like(us)
        canvas = np.stack([us, vs, ones])  # (3, H, W)
        raw = np.tensordot(inverse, canvas, axes=([1], [0]))  # undistorted raw, homogeneous
        raw_x = raw[0] / raw[2]
        raw_y = raw[1] / raw[2]

        k_inv = np.linalg.inv(camera_matrix)
        norm = np.tensordot(k_inv, np.stack([raw_x, raw_y, ones]), axes=([1], [0]))
        xn = norm[0] / norm[2]
        yn = norm[1] / norm[2]
        r2 = xn * xn + yn * yn
        radial = (1.0 + k1 * r2 + k2 * r2**2 + k3 * r2**3) / (1.0 + k4 * r2 + k5 * r2**2 + k6 * r2**3)
        xd = xn * radial + 2.0 * p1 * xn * yn + p2 * (r2 + 2.0 * xn * xn)
        yd = yn * radial + p1 * (r2 + 2.0 * yn * yn) + 2.0 * p2 * xn * yn
        distorted = np.tensordot(camera_matrix, np.stack([xd, yd, ones]), axes=([1], [0]))
        map_x = (distorted[0] / distorted[2]).astype(np.float32)
        map_y = (distorted[1] / distorted[2]).astype(np.float32)
        return map_x, map_y
